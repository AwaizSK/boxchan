"""Web backend: REST + SSE. No application-layer auth: the network layer (loopback + Tailscale ACLs)
is the trust boundary; this module enforces a bind-address policy, a Host allowlist and an Origin
check on writes. Duck-typed on the controller; never imports bot.py."""
import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import subprocess
import time
import unicodedata
from urllib.parse import unquote, urlsplit

from aiohttp import web

from config_store import ConflictError
from messages import MAX_MESSAGE_CHARS, MessageError
from voices import VOICE_NAME_RE, BadVoiceError, SayError, VoiceLoadError

log = logging.getLogger("webui")

MAX_BODY = 16384  # 2000 non-ASCII chars JSON-escaped (\uXXXX) are ~12 KB
MAX_SAY_CHARS = 200
RETRY_INTERVAL = 15.0
ALLOWED_HOSTS_KEY = web.AppKey("allowed_hosts", set)
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


def _in_policy(ip) -> bool:
    return any(ip.version == n.version and ip in n for n in BIND_NETWORKS)


def _normalize_ip(entry: str) -> str | None:
    """Canonical text of `entry` if it is an IP literal inside the address policy, else None."""
    if "%" in entry:
        return None
    try:
        ip = ipaddress.ip_address(entry)
    except ValueError:
        return None
    return str(ip) if _in_policy(ip) else None


def peer_allowed(peername) -> bool:
    """Is this TCP peer (asyncio `peername`) loopback or Tailscale? Same ranges as the bind policy.
    IPv4-mapped IPv6 peers are unwrapped first; a missing/odd peername is refused."""
    try:
        host = str(peername[0]).split("%")[0]
        ip = ipaddress.ip_address(host)
    except (TypeError, IndexError, ValueError):
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return _in_policy(ip)


def _peer_of(request):
    t = request.transport
    return t.get_extra_info("peername") if t is not None else None


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


_last_run_error = ""
_detect_found: bool | None = None  # last detection outcome, so INFO logs fire only on change


def _run(cmd: list[str]) -> str | None:
    global _last_run_error
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        _last_run_error = f"{cmd[0]}: {type(e).__name__}"
        log.debug("%s failed: %s", cmd[0], e)
        return None
    return r.stdout


def detect_tailscale_addrs() -> list[str]:
    """Blocking. This host's Tailscale addresses, or [] (never raises). Logs at INFO only when the
    outcome changes (first call included), so periodic re-detection does not spam."""
    global _detect_found, _last_run_error
    addrs: list[str] = []
    _last_run_error = ""
    try:
        for cmd, parse in ((["tailscale", "ip"], parse_tailscale_ip),
                           (["ip", "-o", "addr", "show", "dev", "tailscale0"], parse_ip_addr)):
            out = _run(cmd)
            addrs = parse(out) if out else []
            if addrs:
                break
    except Exception as e:
        _last_run_error = f"{type(e).__name__}"
        log.debug("tailscale address detection failed", exc_info=True)
        addrs = []
    if bool(addrs) != _detect_found:
        _detect_found = bool(addrs)
        if addrs:
            log.info("Tailscale address(es) detected: %s", ", ".join(addrs))
        else:
            log.info("no Tailscale address detected%s", f" ({_last_run_error})" if _last_run_error else "")
    return addrs


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


_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")
_BRACKET_RE = re.compile(r"^\[([0-9a-fA-F:.]+)\](:\d{1,5})?$")


def _allowed_entry(item: str) -> str | None:
    """Canonical host for one WEBUI_ALLOWED_HOSTS entry, or None if it is not a plain host[:port]."""
    item = item.strip()
    if not item or any(c.isspace() for c in item) or "://" in item or any(c in item for c in "*/\\@?#"):
        return None
    m = _BRACKET_RE.match(item)
    if m:
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            return None
        return str(ip) if ip.version == 6 else None
    if "[" in item or "]" in item or item.count(":") > 1:
        return None
    host, _, port = item.partition(":")
    if _ and not (port.isdigit() and len(port) <= 5):
        return None
    host = host.lower().rstrip(".")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    return host if _HOSTNAME_RE.match(host) else None


def parse_allowed_hosts(raw: str | None) -> list[str]:
    """WEBUI_ALLOWED_HOSTS: comma-separated hostnames (optional :port, ignored). URLs, paths,
    wildcards and malformed entries are dropped with a warning."""
    out = []
    for item in (raw or "").split(","):
        if not item.strip():
            continue
        h = _allowed_entry(item)
        if h is None:
            log.warning("ignoring invalid WEBUI_ALLOWED_HOSTS entry %r (plain hostnames only: no URL, path or wildcard)",
                        item.strip())
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
        ch["guild_id"] = _s(ch["guild_id"])  # None (global change, e.g. voice) stays JSON null
        if ch.get("field") == "channel_id":
            ch["old"], ch["new"] = _s(ch["old"]), _s(ch["new"])
        return {**ev, "snapshot": wire_snapshot(ev["snapshot"]), "change": ch}
    return ev


def _json(data, status=200):
    return web.json_response(data, status=status)


def _make_shielded_callback(task_name: str, expected_exceptions: tuple):
    """Returns a done-callback for shielded tasks that logs unexpected exceptions.

    Use when a shielded task (cancelled mid-write) needs exception tracking that doesn't emit
    "Task exception was never retrieved" warnings for expected failures.
    Example:
        task = asyncio.create_task(coro)
        task.add_done_callback(_make_shielded_callback("my_task", (OSError, ConflictError)))
    """
    def callback(fut):
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc and not isinstance(exc, expected_exceptions):
            log.error("%s failed after client disconnect", task_name, exc_info=exc)
    return callback


def _err(code, status):
    return _json({"error": code}, status)


def make_app(config, controller, *, allowed_hosts, status_interval: float = 3.0,
             keepalive_interval: float = 15.0, static_dir: str | None = None) -> web.Application:
    allowed = {"localhost", "127.0.0.1", "::1"}
    allowed.update(h for h in (host_of(x) for x in allowed_hosts) if h)
    streams: set[asyncio.Task] = set()
    peer_logged: dict[str, float] = {}
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
    async def peer_guard(request, handler):
        # Runs first, on every route. A legitimate tailnet connection always has a tailnet source
        # address; a LAN host abusing the weak-host model arrives with its LAN source address.
        peer = _peer_of(request)
        if not peer_allowed(peer):
            ip = str(peer[0]) if peer else "?"
            now = time.monotonic()
            if now - peer_logged.get(ip, -1e9) > 60:
                if len(peer_logged) > 256:
                    peer_logged.clear()
                peer_logged[ip] = now
                log.warning("refused connection from non-loopback, non-Tailscale peer %s", ip)
            return _err("bad_peer", 403)
        return await handler(request)

    @web.middleware
    async def host_guard(request, handler):
        # DNS-rebinding defense: a page on evil.example resolving to our IP sends Host: evil.example.
        if host_of(request.headers.get("Host", "")) not in allowed:
            return _err("bad_host", 403)
        cross = request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site"
        if cross and request.path.startswith("/api/"):
            return _err("bad_origin", 403)  # also GET: a no-cors cross-site fetch must not open SSE
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
            if cross:
                return _err("bad_origin", 403)
        return await handler(request)

    app = web.Application(middlewares=[security_headers, peer_guard, host_guard], client_max_size=MAX_BODY)

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

    app[ALLOWED_HOSTS_KEY] = allowed  # mutable: start_webui adds late-detected Tailscale addresses

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
        task = asyncio.create_task(coro)
        task.add_done_callback(_make_shielded_callback("config_write", (ConflictError, OSError)))
        try:
            snap = await asyncio.shield(task)
        except ConflictError as e:
            return _json({"error": "conflict", "snapshot": wire_snapshot(e.snapshot)}, 409)
        except OSError:
            log.exception("config persist failed")
            return _err("persist_failed", 500)
        return _json({"snapshot": wire_snapshot(snap)})

    def voice_summary():
        fn = getattr(controller, "voice_summary", None)
        return fn() if fn else None

    async def apply_voice(request):
        editor = editor_of(request)
        body = await body_of(request)
        if "name" not in body:
            return _err("bad_request", 400)
        name = body["name"]
        if name is not None:
            if not isinstance(name, str):
                return _err("bad_request", 400)
            if VOICE_NAME_RE.fullmatch(name) is None:
                return _err("bad_voice", 400)
        task = asyncio.create_task(controller.apply_voice(name, body.get("rev"), editor))
        task.add_done_callback(_make_shielded_callback("apply_voice",
                                                       (ConflictError, BadVoiceError, VoiceLoadError, OSError)))
        try:
            # shield: a client disconnect must not cancel a half-finished engine swap
            snap = await asyncio.shield(task)
        except ConflictError as e:
            return _json({"error": "conflict", "snapshot": wire_snapshot(e.snapshot)}, 409)
        except BadVoiceError:
            return _err("bad_voice", 400)
        except VoiceLoadError:
            return _err("voice_load_failed", 422)
        except OSError:
            log.exception("config persist failed (voice)")
            return _err("persist_failed", 500)
        except Exception:
            log.exception("voice change failed")
            return _err("internal", 500)
        return _json({"snapshot": wire_snapshot(snap)})

    async def list_voices(request):
        return _json(await controller.list_voices())

    async def say(request):
        gid = known_guild(request)
        editor = editor_of(request)
        body = await body_of(request)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return _err("bad_request", 400)
        if len(text) > MAX_SAY_CHARS:
            return _err("too_long", 400)
        try:
            try:
                return _json(controller.say(gid, text, editor=editor))
            except TypeError:
                return _json(controller.say(gid, text))
        except SayError as e:
            if e.code == "bad_text":
                return _err("bad_request", 400)
            return _err(e.code, 409)

    MESSAGE_STATUS = {"bad_request": 400, "bad_mention": 400, "missing_permission": 403, "no_channel": 409,
                      "rate_limited": 429, "discord_error": 502}

    def message_error(e):
        status = MESSAGE_STATUS.get(e.code)
        if status is None:
            return _err("internal", 500)
        if e.code == "rate_limited":
            return _json({"error": "rate_limited", "retry_after": int(e.retry_after or 1)}, 429)
        return _err(e.code, status)

    async def members(request):
        gid = known_guild(request)
        q = request.query.get("q", "")[:100]
        try:
            return _json(await controller.members_for(gid, q))
        except MessageError as e:
            return message_error(e)
        except Exception:
            log.exception("member lookup failed")
            return _err("internal", 500)

    async def send_message(request):
        gid = known_guild(request)
        editor = editor_of(request)
        body = await body_of(request)
        content = body.get("content")
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= MAX_MESSAGE_CHARS:
            return _err("bad_request", 400)
        task = asyncio.create_task(controller.send_message(gid, content, editor))
        task.add_done_callback(_make_shielded_callback("send_message", (MessageError,)))
        try:
            # shield: a client disconnect must not cancel the send (nor invite a retry-duplicate)
            return _json(await asyncio.shield(task))
        except MessageError as e:
            return message_error(e)
        except Exception:
            log.exception("message send failed")
            return _err("internal", 500)

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
                                 "guilds": controller.guilds_view(),
                                 "voices": voice_summary()})
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
    app.router.add_get("/api/voices", list_voices)
    app.router.add_put("/api/voice", apply_voice)
    app.router.add_post("/api/guilds/{gid}/say", say)
    app.router.add_get("/api/guilds/{gid}/members", members)
    app.router.add_post("/api/guilds/{gid}/send", send_message)
    app.router.add_get("/api/events", events)
    return app


class WebUIRunner(web.AppRunner):
    """AppRunner whose cleanup also stops the bind-retry task."""

    retry_task: asyncio.Task | None = None

    async def cleanup(self) -> None:
        t, self.retry_task = self.retry_task, None
        try:
            if t:
                t.cancel()
                await asyncio.wait({t})
                if not t.cancelled():
                    t.exception()  # mark retrieved; the loop already logged it
        finally:
            await super().cleanup()


async def _bind_site(runner: web.AppRunner, host: str, port: int) -> None:
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except BaseException:
        await site.stop()  # unregister the half-started site
        raise


async def _retry_binds(runner, pending: list[str], port: int, interval: float,
                       detect, known: set[str], tailnet_found: bool) -> None:
    """Retry failed binds; in auto mode (`detect` given) also re-detect Tailscale addresses until
    one is found. Only cancellation ends the loop early; other errors are logged and retried."""
    while pending or (detect is not None and not tailnet_found):
        await asyncio.sleep(interval)
        try:
            if detect is not None and not tailnet_found:
                for addr in await asyncio.to_thread(detect):
                    addr = _normalize_ip(addr)
                    if addr and addr not in known:
                        known.add(addr)
                        pending.append(addr)
                        tailnet_found = True
                        runner.app[ALLOWED_HOSTS_KEY].add(host_of(addr))
            for host in list(pending):
                try:
                    await _bind_site(runner, host, port)
                except OSError as e:
                    log.debug("web UI still cannot bind %s:%s (%s)", host, port, e)
                else:
                    pending.remove(host)
                    log.info("web UI now listening on %s:%s", host, port)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("web UI bind retry failed; will try again")


async def start_webui(app: web.Application, hosts: list[str], port: int, *,
                      retry_interval: float = RETRY_INTERVAL, detect=None) -> WebUIRunner:
    """Bind every policy-compliant address independently; retry failed ones in the background.
    `detect` (auto mode only): blocking callable returning Tailscale addresses, re-run every
    `retry_interval` until one is found. Raises ValueError (policy) or OSError (nothing bound)."""
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
    tailnet_found = any(not ipaddress.ip_address(h).is_loopback for h in hosts)
    if pending or (detect is not None and not tailnet_found):
        runner.retry_task = asyncio.create_task(
            _retry_binds(runner, pending, port, retry_interval, detect, set(hosts), tailnet_found),
            name="webui-bind-retry")
    return runner
