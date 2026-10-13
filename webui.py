"""Web backend: REST + SSE + bearer auth. Duck-typed on the controller; never imports bot.py."""
import asyncio
import hmac
import json
import logging
import os
import time
import unicodedata
from urllib.parse import unquote

from aiohttp import web

from config_store import ConflictError

log = logging.getLogger("webui")

MIN_TOKEN_LEN = 24
MAX_BODY = 4096
THROTTLE_FAILS = 5
THROTTLE_WINDOW = 60.0
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
STATIC_FILES = (
    ("/", "index.html", "text/html"),
    ("/app.js", "app.js", "application/javascript"),
    ("/app.css", "app.css", "text/css"),
)
DEFAULT_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def load_gui_token() -> str | None:
    """First source found wins: $CREDENTIALS_DIRECTORY/gui_token, token file, $WEBUI_TOKEN."""
    raw = None
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    paths = []
    if cred:
        paths.append(os.path.join(cred, "gui_token"))
    paths.append(os.environ.get("WEBUI_TOKEN_FILE") or os.path.expanduser("~/.config/tts-bot/gui_token"))
    for p in paths:
        try:
            with open(p) as f:
                raw = f.read().strip()
        except OSError:
            continue
        if raw:
            break
        raw = None
    if raw is None:
        raw = (os.environ.get("WEBUI_TOKEN") or "").strip() or None
    if raw is None:
        return None
    if len(raw) < MIN_TOKEN_LEN:
        log.warning("GUI token is too short (%d chars, need >= %d); web UI disabled", len(raw), MIN_TOKEN_LEN)
        return None
    return raw


def sanitize_editor(value) -> str | None:
    if not isinstance(value, str):
        return None
    s = " ".join(value.split())
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    s = " ".join(s.split())[:40].strip()
    return s or None


def _s(v):
    return None if v is None else str(v)


def wire_snapshot(snap: dict) -> dict:
    """Snapshot with channel ids as strings (JS-safe)."""
    return {**snap, "guilds": {g: {**e, "channel_id": _s(e.get("channel_id"))}
                               for g, e in snap["guilds"].items()}}


def wire_event(ev: dict) -> dict:
    if ev.get("type") == "config":
        ch = dict(ev["change"])
        ch["guild_id"] = str(ch["guild_id"])
        if ch.get("field") == "channel_id":
            ch["old"], ch["new"] = _s(ch["old"]), _s(ch["new"])
        return {**ev, "snapshot": wire_snapshot(ev["snapshot"]), "change": ch}
    return ev


def _json(data, status=200):
    return web.json_response(data, status=status)


def _err(code, status):
    return _json({"error": code}, status)


def make_app(config, controller, token: str, *, status_interval: float = 3.0,
             keepalive_interval: float = 15.0, static_dir: str | None = None,
             clock=time.monotonic) -> web.Application:
    if not token or len(token) < MIN_TOKEN_LEN:
        raise ValueError(f"GUI token must be at least {MIN_TOKEN_LEN} characters")
    token_b = token.encode()
    streams: set[asyncio.Task] = set()
    fails: dict[str, list[float]] = {}
    blocked: dict[str, float] = {}
    static_dir = static_dir or DEFAULT_STATIC_DIR

    @web.middleware
    async def security_headers(request, handler):
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            _apply_headers(request, e)
            raise
        _apply_headers(request, resp)
        return resp

    def _apply_headers(request, resp):
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"

    @web.middleware
    async def auth(request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        ip = request.remote or "?"
        now = clock()
        if blocked.get(ip, 0) > now:
            return _err("too_many_attempts", 429)
        header = request.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        ok = (scheme.lower() == "bearer"
              and hmac.compare_digest(presented.strip().encode(), token_b))
        if not ok:
            recent = [t for t in fails.get(ip, []) if now - t < THROTTLE_WINDOW] + [now]
            fails[ip] = recent
            if len(recent) >= THROTTLE_FAILS:
                blocked[ip] = now + THROTTLE_WINDOW
                fails.pop(ip, None)
            return _err("unauthorized", 401)
        fails.pop(ip, None)
        return await handler(request)

    app = web.Application(middlewares=[security_headers, auth], client_max_size=MAX_BODY)

    # --- static ---
    def static_handler(name, ctype):
        try:
            with open(os.path.join(static_dir, name), "rb") as f:
                body = f.read()
        except OSError:
            log.error("static file %s missing", name)
            body = None

        async def h(request):
            if body is None:
                return web.Response(status=500, text="static file missing")
            return web.Response(body=body, content_type=ctype, charset="utf-8")
        return h

    for path, name, ctype in STATIC_FILES:
        app.router.add_get(path, static_handler(name, ctype))

    # --- helpers ---
    def known_guild(request):
        gid = request.match_info["gid"]
        if not (gid.isascii() and gid.isdigit()):
            raise web.HTTPNotFound()
        if gid not in {g["id"] for g in controller.guilds_view()}:
            raise web.HTTPNotFound()
        return int(gid)

    def editor_of(request):
        ed = sanitize_editor(unquote(request.headers.get("X-Editor") or ""))
        if ed is None:
            raise web.HTTPBadRequest(text=json.dumps({"error": "editor_required"}),
                                     content_type="application/json")
        return ed

    async def body_of(request) -> dict:
        try:
            body = json.loads(await request.read())
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text=json.dumps({"error": "bad_request"}),
                                     content_type="application/json")
        rev = body.get("rev")
        if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int)):
            raise web.HTTPBadRequest(text=json.dumps({"error": "bad_request"}),
                                     content_type="application/json")
        return body

    async def write(coro):
        # shield: a client disconnect must not cancel a half-finished persist
        try:
            snap = await asyncio.shield(coro)
        except ConflictError as e:
            return _json({"error": "conflict", "snapshot": wire_snapshot(e.snapshot)}, 409)
        return _json({"snapshot": wire_snapshot(snap)})

    # --- API ---
    async def state(request):
        return _json({"config": wire_snapshot(config.snapshot()), "guilds": controller.guilds_view()})

    async def put_channel(request):
        gid = known_guild(request)
        editor = editor_of(request)
        body = await body_of(request)
        ch = body.get("channel_id")
        if ch is None:
            cid = None
        elif isinstance(ch, str) and ch.isascii() and ch.isdigit():
            cid = int(ch)
            if not controller.is_text_channel(gid, cid):
                return _err("bad_channel", 400)
        else:
            return _err("bad_channel", 400)
        return await write(config.set_channel(gid, cid, expected_rev=body.get("rev"), editor=editor))

    async def put_enabled(request):
        gid = known_guild(request)
        editor = editor_of(request)
        body = await body_of(request)
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return _err("bad_request", 400)
        return await write(config.set_enabled(gid, enabled, expected_rev=body.get("rev"), editor=editor))

    async def skip(request):
        gid = known_guild(request)
        editor_of(request)
        return _json({"skipped": bool(controller.skip(gid))})

    async def clear(request):
        gid = known_guild(request)
        editor_of(request)
        return _json({"cleared": int(controller.clear_queue(gid))})

    async def events(request):
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "X-Accel-Buffering": "no",
        })
        _apply_headers(request, resp)  # must precede prepare(); middleware re-sets harmlessly
        await resp.prepare(request)
        q = config.hub.subscribe()
        streams.add(asyncio.current_task())

        async def send(event, data):
            await resp.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())

        try:
            await send("hello", {"snapshot": wire_snapshot(config.snapshot()),
                                 "guilds": controller.guilds_view()})
            loop = asyncio.get_running_loop()
            next_status = loop.time() + status_interval
            next_keep = loop.time() + keepalive_interval
            last_status = None
            while True:
                now = loop.time()
                timeout = max(0.0, min(next_status, next_keep) - now)
                try:
                    ev = await asyncio.wait_for(q.get(), timeout)
                except asyncio.TimeoutError:
                    ev = None
                if ev is not None:
                    ev = wire_event(ev)
                    await send(ev.get("type", "config"), ev)
                now = loop.time()
                if now >= next_status:
                    next_status = now + status_interval
                    st = controller.status_view()
                    if st != last_status:
                        last_status = st
                        await send("status", st)
                if now >= next_keep:
                    next_keep = now + keepalive_interval
                    await resp.write(b": keepalive\n\n")
        except (ConnectionResetError, ConnectionError):
            pass
        except Exception:
            log.exception("SSE stream failed")
        finally:
            config.hub.unsubscribe(q)
            streams.discard(asyncio.current_task())
        return resp

    async def end_streams(app):
        for t in list(streams):
            t.cancel()

    app.on_shutdown.append(end_streams)
    app.router.add_get("/api/state", state)
    app.router.add_put("/api/guilds/{gid}/channel", put_channel)
    app.router.add_put("/api/guilds/{gid}/enabled", put_enabled)
    app.router.add_post("/api/guilds/{gid}/skip", skip)
    app.router.add_post("/api/guilds/{gid}/clear", clear)
    app.router.add_get("/api/events", events)
    return app


async def start_webui(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, shutdown_timeout=5.0)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    log.info("web UI listening on %s:%s", host, port)
    return runner
