"""Web backend: REST + SSE. No application-layer auth: the network layer (loopback + Tailscale ACLs)
is the trust boundary; this module enforces a bind-address policy, a Host allowlist and an Origin
check on writes. Duck-typed on the controller; never imports bot.py."""
import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import subprocess
import unicodedata
from urllib.parse import unquote, urlsplit

from aiohttp import web

from config_store import ConflictError

log = logging.getLogger("webui")

MAX_BODY = 4096
RETRY_INTERVAL = 15.0
SAFE_METHODS = frozenset({"GET", "HEAD"})
# Bind policy: loopback, Tailscale CGNAT, Tailscale ULA. Nothing else may ever be listened on.
BIND_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48"))
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
STATIC_FILES = (
    ("/", "index.html", "text/html"),
    ("/app.js", "app.js", "application/javascript"),
    ("/app.css", "app.css", "text/css"),
)
DEFAULT_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _normalize_ip(entry: str) -> str | None:
    """Canonical text of `entry` if it is an IP literal inside the bind policy, else None."""
    if "%" in entry:
        return None
    try:
        ip = ipaddress.ip_address(entry)
    except ValueError:
        return None
    if any(ip.version == n.version and ip in n for n in BIND_NETWORKS):
        return str(ip)
    return None


def validate_bind_hosts(hosts: list[str]) -> list[str]:
    """Fail-closed bind policy. Returns normalized, de-duplicated addresses or raises ValueError."""
    out: list[str] = []
    for raw in hosts:
        entry = str(raw).strip()
        addr = "127.0.0.1" if entry.lower() == "localhost" else _normalize_ip(entry)
        if addr is None:
            raise ValueError(f"bind address {entry!r} is not loopback or a Tailscale address")
        if addr not in out:
            out.append(addr)
    if not out:
        raise ValueError("no bind addresses given")
    return out


def parse_tailscale_ip(text: str) -> list[str]:
    """Parse `tailscale ip` output (one address per line); keeps only policy-compliant ones."""
    out = []
    for line in text.splitlines():
        addr = _normalize_ip(line.strip())
        if addr and addr not in out:
            out.append(addr)
    return out


def parse_ip_addr(text: str) -> list[str]:
    """Parse `ip -o addr show dev tailscale0` output (inet/inet6 fields); policy-compliant only."""
    out = []
    parts = text.split()
    for i, word in enumerate(parts[:-1]):
        if word in ("inet", "inet6"):
            addr = _normalize_ip(parts[i + 1].split("/")[0])
            if addr and addr not in out:
                out.append(addr)
    return out


def _run(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("%s failed: %s", cmd[0], e)
        return None
    return r.stdout


def detect_tailscale_addrs() -> list[str]:
    """Blocking. This host's Tailscale addresses, or [] (never raises)."""
    try:
        for cmd, parse in ((["tailscale", "ip"], parse_tailscale_ip),
                           (["ip", "-o", "addr", "show", "dev", "tailscale0"], parse_ip_addr)):
            out = _run(cmd)
            addrs = parse(out) if out else []
            if addrs:
                return addrs
    except Exception:
        log.debug("tailscale address detection failed", exc_info=True)
    log.info("no Tailscale address detected")
    return []


def host_of(value: str) -> str | None:
    """Lowercased hostname from a Host header value ('h', 'h:port', '[v6]:port'); IPs canonicalized."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if v.startswith("["):
        end = v.find("]")
        if end < 0:
            return None
        h = v[1:end]
    elif v.count(":") == 1:
        h = v.split(":")[0]
    else:
        h = v
    h = h.rstrip(".")
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        return h or None


def parse_allowed_hosts(raw: str | None) -> list[str]:
    """WEBUI_ALLOWED_HOSTS: comma-separated hostnames, no wildcards (such entries are dropped)."""
    out = []
    for item in (raw or "").split(","):
        if not item.strip():
            continue
        h = host_of(item)
        if h is None or any(c in h for c in "*/@ \t"):
            log.warning("ignoring invalid WEBUI_ALLOWED_HOSTS entry %r (hostnames only, no wildcards)", item.strip())
            continue
        out.append(h)
    return out


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


def make_app(config, controller, *, allowed_hosts, status_interval: float = 3.0,
             keepalive_interval: float = 15.0, static_dir: str | None = None) -> web.Application:
    allowed = {"localhost", "127.0.0.1", "::1"}
    allowed.update(h for h in (host_of(x) for x in allowed_hosts) if h)
    streams: set[asyncio.Task] = set()
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
    async def host_guard(request, handler):
        # DNS-rebinding defense: a page on evil.example resolving to our IP sends Host: evil.example.
        if host_of(request.headers.get("Host", "")) not in allowed:
            return _err("bad_host", 403)
        if request.method not in SAFE_METHODS:
            # Browsers always send Origin on cross-origin writes; curl/scripts send none, so absent is OK.
            origin = request.headers.get("Origin")
            if origin is not None:
                try:
                    oh = host_of(urlsplit(origin).netloc.rpartition("@")[2]) if origin != "null" else None
                except ValueError:
                    oh = None
                if oh not in allowed:
                    return _err("bad_origin", 403)
            if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                return _err("bad_origin", 403)
        return await handler(request)

    app = web.Application(middlewares=[security_headers, host_guard], client_max_size=MAX_BODY)

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
        except OSError:
            log.exception("config persist failed")
            return _err("persist_failed", 500)
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


class WebUIRunner(web.AppRunner):
    """AppRunner whose cleanup also stops the bind-retry task."""

    retry_task: asyncio.Task | None = None

    async def cleanup(self) -> None:
        t, self.retry_task = self.retry_task, None
        if t:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await super().cleanup()


async def _bind_site(runner: web.AppRunner, host: str, port: int) -> None:
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except BaseException:
        await site.stop()  # unregister the half-started site
        raise


async def _retry_binds(runner, pending: list[str], port: int, interval: float) -> None:
    while pending:
        await asyncio.sleep(interval)
        for host in list(pending):
            try:
                await _bind_site(runner, host, port)
            except OSError as e:
                log.debug("web UI still cannot bind %s:%s (%s)", host, port, e)
            else:
                pending.remove(host)
                log.info("web UI now listening on %s:%s", host, port)


async def start_webui(app: web.Application, hosts: list[str], port: int, *,
                      retry_interval: float = RETRY_INTERVAL) -> WebUIRunner:
    """Bind every policy-compliant address independently; retry failed ones in the background.
    Raises ValueError (policy) or OSError (nothing bound)."""
    hosts = validate_bind_hosts(hosts)
    runner = WebUIRunner(app, shutdown_timeout=5.0)
    await runner.setup()
    pending: list[str] = []
    last: OSError | None = None
    try:
        for host in hosts:
            try:
                await _bind_site(runner, host, port)
            except OSError as e:
                last = e
                pending.append(host)
                log.warning("web UI cannot bind %s:%s (%s); will retry", host, port, e)
            else:
                log.info("web UI listening on %s:%s", host, port)
        if len(pending) == len(hosts):
            raise OSError(f"web UI could not bind any of {hosts} on port {port}") from last
    except BaseException:
        await runner.cleanup()
        raise
    if pending:
        runner.retry_task = asyncio.create_task(_retry_binds(runner, pending, port, retry_interval),
                                                name="webui-bind-retry")
    return runner
