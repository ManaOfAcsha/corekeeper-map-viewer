"""HTTP server: map tiles, POIs, players, markers, settings, full-map generation, mod install.

Map file format (<data>/servermaps/<world>.mapparts.gzip): gzip -> JSON
{"mapParts": {"keys": [{"x","y"}...], "values": [{"png": [bytes...], ...}]}}.
Each part is a 256x256 PNG; part (x, y) covers world tiles x*256..x*256+255 / y*256..y*256+255,
+y is north (image rows are flipped).

Layers: "live" = the server's explored map, "full" = generated full map (optional background).
Players come from <data>/mods/LivePlayers/players.json (LivePlayers server mod).

Access model: everyone who can reach the port may VIEW (and edit shared pins). Settings, full-map
generation and mod install are only accepted from this PC (loopback + Host check + custom header).
"""
import gzip
import hashlib
import ipaddress
import json
import os
import socket
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_NAME, __version__, config, detect, modinstall, paths, procs
from .generator import Busy, Generator

POLL_SEC = 2.0
PLAYER_POLL_SEC = 0.5


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ============================================================================ change hub
class Hub:
    """Change counters for SSE clients. 'map' = default event, others are named events."""

    def __init__(self):
        self.cond = threading.Condition()
        self.revision = 0
        self.named = {}   # event name -> (rev, body bytes)

    def bump(self):
        with self.cond:
            self.revision += 1
            self.cond.notify_all()

    def push(self, name: str, body: bytes):
        with self.cond:
            rev = self.named.get(name, (0, b""))[0] + 1
            self.named[name] = (rev, body)
            self.cond.notify_all()


# ============================================================================ watched files
class MapStore:
    """Watches one mapparts file and keeps the decoded tiles in memory."""

    def __init__(self, path: Path, hub: Hub, optional=False):
        self.path = path
        self.hub = hub
        self.optional = optional
        self.lock = threading.Lock()
        self.tiles = {}
        self.versions = {}
        self.loaded_mtime = None
        self.updated_at = None
        self.error = None

    @staticmethod
    def _read(path: Path):
        with gzip.open(path, "rb") as f:
            data = json.loads(f.read())
        parts = data["mapParts"]
        tiles = {}
        for key, val in zip(parts["keys"], parts["values"]):
            if val and val.get("png"):
                tiles[(key["x"], key["y"])] = bytes(val["png"])
        return tiles

    def poll(self):
        try:
            mtime = self.path.stat().st_mtime
        except (FileNotFoundError, NotADirectoryError, OSError):
            if self.optional:
                if self.tiles or self.error:
                    with self.lock:
                        self.tiles, self.versions, self.loaded_mtime = {}, {}, None
                        self.updated_at, self.error = None, None
                    self.hub.bump()
                return
            if self.tiles:
                with self.lock:
                    self.tiles, self.versions, self.loaded_mtime, self.updated_at = {}, {}, None, None
            self._set_error("missing")
            return
        if mtime == self.loaded_mtime:
            return
        try:
            tiles = self._read(self.path)
        except Exception as e:  # file may be mid-write; retry next poll
            self._set_error("read_failed", repr(e))
            return
        versions = {k: hashlib.md5(v).hexdigest()[:10] for k, v in tiles.items()}
        with self.lock:
            changed = [k for k in versions if self.versions.get(k) != versions[k]]
            self.tiles, self.versions = tiles, versions
            self.loaded_mtime = self.updated_at = mtime
            self.error = None
        self.hub.bump()
        log(f"map loaded: {self.path.name} parts={len(tiles)} changed={len(changed)}")

    def _set_error(self, code, detail=None):
        with self.lock:
            if self.error == code:
                return
            self.error = code
        self.hub.bump()
        log("WARN map", code, self.path.name, detail or "")

    def snapshot(self):
        with self.lock:
            return {"updatedAt": self.updated_at, "error": self.error,
                    "tiles": [{"x": x, "y": y, "v": v} for (x, y), v in self.versions.items()]}


class PoiStore:
    """Watches the generated pois.json; serves the raw bytes, bumps the hub on change."""

    EMPTY = b'{"objects":[],"ores":[],"missing":true}'

    def __init__(self, path: Path, hub: Hub):
        self.path = path
        self.hub = hub
        self.lock = threading.Lock()
        self.body = self.EMPTY
        self.loaded_mtime = None
        self.version = None
        self.counts = None
        self.error = None

    def poll(self):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self.version is not None or self.error:
                with self.lock:
                    self.body, self.loaded_mtime, self.version = self.EMPTY, None, None
                    self.counts, self.error = None, None
                self.hub.bump()
            return
        if mtime == self.loaded_mtime:
            return
        try:
            data = json.loads(self.path.read_bytes().decode("utf-8-sig"))
            counts = {"objects": len(data.get("objects") or []), "ores": len(data.get("ores") or [])}
        except Exception as e:
            if self.error != "read_failed":
                self.error = "read_failed"
                log("WARN pois.json", repr(e))
            return
        with self.lock:
            self.body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.version = hashlib.md5(self.body).hexdigest()[:10]
            self.loaded_mtime, self.counts, self.error = mtime, counts, None
        self.hub.bump()
        log(f"pois loaded: objects={counts['objects']} ores={counts['ores']}")

    def snapshot(self):
        with self.lock:
            return {"v": self.version, "updatedAt": self.loaded_mtime, "counts": self.counts, "error": self.error}


class PlayerStore:
    """Watches players.json written ~1/s by the LivePlayers server mod.

    status: missing | live | paused (server up, nobody online) | stopped (exit marker or stale) | error
    """

    STALE_SEC = 5.0

    def __init__(self, path: Path, hub: Hub):
        self.path = path
        self.hub = hub
        self.lock = threading.Lock()
        self.loaded_mtime = None
        self.data = None
        self.error = None
        self.body = b'{"status":"missing","players":[]}'
        self.key = None

    def _status(self, mtime, now):
        if mtime is None:
            return "missing"
        if self.data is None:
            return "error" if now - mtime > self.STALE_SEC else "missing"
        if self.data.get("stopped") or now - mtime > self.STALE_SEC:
            return "stopped"
        return "paused" if self.data.get("paused") else "live"

    def poll(self):
        now = time.time()
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
            self.data, self.loaded_mtime, self.error = None, None, None
        if mtime is not None and mtime != self.loaded_mtime:
            try:
                data = json.loads(self.path.read_bytes().decode("utf-8-sig"))
                if not isinstance(data.get("players"), list):
                    raise ValueError("players is not a list")
                self.data, self.loaded_mtime, self.error = data, mtime, None
            except (OSError, ValueError, AttributeError):
                self.error = "read_failed"
        status = self._status(mtime, now)
        players = []
        if self.data is not None and status in ("live", "paused"):
            for p in self.data.get("players") or []:
                try:
                    players.append({
                        "id": str(p.get("id") or p.get("name") or ""),
                        "name": str(p.get("name") or "?")[:40],
                        "x": float(p["x"]), "y": float(p["y"]),
                        "hp": int(p["hp"]) if p.get("hp") is not None else None,
                        "maxHp": int(p["maxHp"]) if p.get("maxHp") is not None else None,
                        "dead": bool(p["dead"]) if p.get("dead") is not None else None,
                    })
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
        key = json.dumps([status, players], sort_keys=True)
        snap = {"status": status, "players": players, "utc": (self.data or {}).get("utc"),
                "modVersion": (self.data or {}).get("version"),
                "age": round(now - mtime, 1) if mtime else None, "error": self.error}
        body = json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self.lock:
            self.body = body
            changed = key != self.key
            old = json.loads(self.key)[0] if (changed and self.key) else None
            if changed:
                self.key = key
        if changed:
            self.hub.push("players", body)
            if old != status:
                log(f"players: {old} -> {status} ({len(players)})")

    def snapshot_body(self):
        with self.lock:
            return self.body


class Markers:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def save(self, items):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


class Bundle:
    """Everything that depends on the current settings. Replaced as a whole on reconfigure."""

    def __init__(self, eff: config.Effective, hub: Hub):
        self.eff = eff
        self.stores = {"live": MapStore(eff.live_map, hub), "full": MapStore(eff.full_map, hub, optional=True)}
        self.pois = PoiStore(eff.pois, hub)
        self.players = PlayerStore(eff.players_file, hub)
        self.markers = Markers(eff.markers)

    def poll_maps(self):
        for st in self.stores.values():
            st.poll()
        self.pois.poll()


# ============================================================================ app state
class Viewer:
    def __init__(self, cfg: dict, overrides: dict):
        self.cfg = cfg
        self.overrides = overrides
        self.hub = Hub()
        self.lock = threading.RLock()
        self.bundle = None
        self.gen = None            # current / last Generator
        self.httpd = None
        self.bound = None          # (host, port) actually listening
        self.pending_restart = False
        self.bind_error = None
        self.reconfigure()

    # ---- settings ---------------------------------------------------------------------------
    def reconfigure(self):
        eff = config.Effective(self.cfg, self.overrides)
        b = Bundle(eff, self.hub)
        b.poll_maps()
        b.players.poll()
        with self.lock:
            self.bundle = b
        self.hub.bump()
        self.push_status()
        log(f"world {eff.world} | data: {eff.data_dir} | install: {eff.install or '(not found)'}")
        log(f"app data: {eff.out_dir}")

    def listen_addr(self):
        host = self.overrides.get("host") or ("0.0.0.0" if self.cfg.get("lan") else "127.0.0.1")
        port = int(self.overrides.get("port") or self.cfg.get("port") or 8765)
        return host, port

    def watch_forever(self):
        next_maps = 0.0
        while True:
            try:
                b = self.bundle
                b.players.poll()
                if time.time() >= next_maps:
                    next_maps = time.time() + POLL_SEC
                    b.poll_maps()
            except Exception:
                traceback.print_exc()
            time.sleep(PLAYER_POLL_SEC)

    # ---- status -----------------------------------------------------------------------------
    def status(self, admin: bool) -> dict:
        b = self.bundle
        eff = b.eff
        base = {"app": APP_NAME, "version": __version__, "admin": admin, "worldName": eff.world_name,
                "world": eff.world}
        if not admin:
            return base
        dstat = detect.data_status(eff.data_dir, eff.world)
        istat = detect.install_status(eff.install)
        host, port = self.bound or self.listen_addr()
        full = b.stores["full"]
        base.update({
            "config": {k: self.cfg.get(k) for k in config.DEFAULTS},
            "overrides": sorted(k for k, v in self.overrides.items() if v not in (None, "")),
            "install": dict(istat, source=eff.install_source,
                            candidates=detect.find_server_installs()),
            "data": dict(dstat, source=eff.data_source, default=str(detect.default_data_dir())),
            "worldSource": eff.world_source,
            "outDir": str(eff.out_dir),
            "appData": str(paths.app_data_dir()),
            "fullMap": {"exists": eff.full_map.is_file(), "parts": len(full.versions),
                        "updatedAt": full.updated_at},
            "players": json.loads(b.players.snapshot_body()),
            "mod": modinstall.status(eff.install),
            "job": self.gen.state() if self.gen else None,
            "listen": {"host": host, "port": port, "lan": host not in ("127.0.0.1", "localhost", "::1"),
                       "lanUrls": lan_urls(port) if host not in ("127.0.0.1", "localhost", "::1") else [],
                       "pendingRestart": self.pending_restart, "bindError": self.bind_error},
            "needsSetup": not dstat["ok"],
        })
        return base

    def push_status(self):
        self.hub.push("status", b"{}")   # clients re-fetch /api/status (admin-only details)

    # ---- actions ----------------------------------------------------------------------------
    def update_config(self, patch: dict) -> dict:
        clean = config.validate_patch(patch)
        with self.lock:
            old_addr = self.listen_addr()
            self.cfg.update(clean)
            config.save(self.cfg)
            for k in ("serverInstall", "dataDir", "world"):
                if k in clean:
                    self.overrides.pop(k, None)
            if "port" in clean:
                self.overrides.pop("port", None)
            if "lan" in clean:
                self.overrides.pop("host", None)
        if any(k in clean for k in ("serverInstall", "dataDir", "world")):
            self.reconfigure()
        restart = self.listen_addr() != old_addr
        if restart:
            self.pending_restart = True
            threading.Thread(target=self._restart_listener, daemon=True).start()
        self.push_status()
        host, port = self.listen_addr()
        return {"ok": True, "restart": restart, "port": port, "host": host}

    def _restart_listener(self):
        time.sleep(0.5)   # let the HTTP response go out first
        if self.httpd:
            self.httpd.shutdown()

    def start_generate(self, radius=0) -> dict:
        with self.lock:
            if self.gen and self.gen.running:
                return {"ok": False, "error": "already_running"}
            eff = self.bundle.eff
            g = Generator(eff.install, eff.data_dir, eff.world, eff.out_dir, radius=radius,
                          on_change=lambda st: self.hub.push("job", json.dumps(st).encode("utf-8")))
            try:
                g._preflight()
                g.acquire_lock()   # cross-process: another viewer / the `generate` command
            except Busy as e:
                return {"ok": False, "error": "already_running_elsewhere", "message": str(e),
                        "pid": e.holder.get("pid")}
            except Exception as e:
                return {"ok": False, "error": str(e)}
            self.gen = g
            try:
                threading.Thread(target=g.run, daemon=True, name="fullmap").start()
            except Exception:
                g.worklock.release()
                raise
        return {"ok": True}

    def cancel_generate(self) -> dict:
        g = self.gen
        if not g or not g.running:
            return {"ok": False, "error": "not_running"}
        g.cancel()
        return {"ok": True}

    def mod_action(self, action: str) -> dict:
        eff = self.bundle.eff
        try:
            if action == "install":
                res = modinstall.install(eff.install)
            elif action == "uninstall":
                res = modinstall.uninstall(eff.install)
            else:
                return {"ok": False, "error": "bad_action"}
        except modinstall.ModError as e:
            return {"ok": False, "error": e.code, "message": str(e), **e.extra}
        except OSError as e:
            return {"ok": False, "error": "os_error", "message": str(e)}
        log(f"LivePlayers mod: {res['action']}")
        self.push_status()
        return res


def lan_urls(port):
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:   # the address used for the default route (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    out = []
    for ip in sorted(ips):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not a.is_loopback and not a.is_link_local:
            out.append(f"http://{ip}:{port}")
    return out


# ============================================================================ HTTP
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]"}


def make_handler(viewer: Viewer):
    hub = viewer.hub
    web = paths.web_dir()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = APP_NAME

        def setup(self):
            super().setup()
            # headers and body go out as separate writes; without this, Nagle + delayed ACK stall
            # every keep-alive response (~0.2 s per map part)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, fmt, *args):
            pass

        # ---- helpers ----
        def _is_local(self):
            try:
                if not ipaddress.ip_address(self.client_address[0].split("%")[0]).is_loopback:
                    return False
            except ValueError:
                return False
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            return host in LOCAL_HOSTNAMES   # blocks DNS-rebinding style requests

        def _mutation_ok(self):
            # custom header => a cross-site form/fetch cannot send it without a CORS preflight,
            # which this server never approves
            return self.headers.get("X-CKMV") == "1"

        def _send(self, code, body: bytes, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", {"Cache-Control": "no-store"})

        def _body(self):
            n = int(self.headers.get("Content-Length", "0"))
            if n > 2_000_000:
                raise ValueError("too large")
            return json.loads(self.rfile.read(n).decode("utf-8") or "null")

        # ---- GET ----
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            b = viewer.bundle
            try:
                if path in ("/", "/index.html", "/setup"):
                    body = (web / "index.html").read_bytes()
                    self._send(200, body, "text/html; charset=utf-8",
                               {"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
                elif path == "/api/ping":
                    self._json({"app": APP_NAME, "version": __version__})
                elif path == "/api/map":
                    self._json({"revision": hub.revision,
                                "server": {"worldName": b.eff.world_name, "world": b.eff.world},
                                "layers": {k: st.snapshot() for k, st in b.stores.items()},
                                "pois": b.pois.snapshot()})
                elif path == "/api/status":
                    self._json(viewer.status(self._is_local()))
                elif path == "/api/pois":
                    with b.pois.lock:
                        body = b.pois.body
                    self._send(200, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
                elif path.startswith("/api/tile/"):
                    layer, x, y = path[len("/api/tile/"):].removesuffix(".png").split("/")
                    st = b.stores.get(layer)
                    if st is None:
                        raise ValueError(layer)
                    with st.lock:
                        png = st.tiles.get((int(x), int(y)))
                    if png is None:
                        self._send(404, b"no tile", "text/plain")
                    else:
                        self._send(200, png, "image/png", {"Cache-Control": "public, max-age=31536000, immutable"})
                elif path == "/api/markers":
                    self._json(b.markers.load())
                elif path == "/api/players":
                    self._send(200, b.players.snapshot_body(), "application/json; charset=utf-8",
                               {"Cache-Control": "no-store"})
                elif path == "/api/events":
                    self._events()
                else:
                    self._send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True
            except ValueError:
                self._send(400, b"bad request", "text/plain")

        # ---- PUT (shared pins: any viewer) ----
        def do_PUT(self):
            if self.path.split("?", 1)[0] != "/api/markers":
                return self._send(404, b"not found", "text/plain")
            if not self._mutation_ok():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            try:
                items = self._body()
                if not isinstance(items, list):
                    raise ValueError("markers must be a list")
                clean = [{"x": int(m["x"]), "y": int(m["y"]), "name": str(m.get("name", ""))[:60],
                          "color": str(m.get("color", "#ffd54a"))[:16]} for m in items][:2000]
                viewer.bundle.markers.save(clean)
                self._json({"ok": True, "count": len(clean)})
            except (ValueError, KeyError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)

        # ---- POST (admin: this PC only) ----
        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not (self._is_local() and self._mutation_ok()):
                return self._json({"ok": False, "error": "local_only"}, 403)
            try:
                body = self._body() or {}
                if path == "/api/config":
                    self._json(viewer.update_config(body))
                elif path == "/api/detect":
                    viewer.reconfigure()
                    self._json({"ok": True})
                elif path == "/api/generate":
                    r = viewer.start_generate(int(body.get("radius") or 0))
                    self._json(r, 200 if r["ok"] else 409)
                elif path == "/api/generate/cancel":
                    self._json(viewer.cancel_generate())
                elif path == "/api/mod":
                    r = viewer.mod_action(str(body.get("action")))
                    self._json(r, 200 if r.get("ok") else 409)
                elif path == "/api/open-folder":
                    p = paths.app_data_dir() if body.get("which") != "world" else viewer.bundle.eff.out_dir
                    p.mkdir(parents=True, exist_ok=True)
                    procs.open_folder(p)
                    self._json({"ok": True})
                else:
                    self._send(404, b"not found", "text/plain")
            except (ValueError, TypeError) as e:
                self._json({"ok": False, "error": "bad_request", "message": str(e)}, 400)

        def _events(self):
            """SSE: map revision as the default event; named events: players, job (admin), status (admin)."""
            admin = self._is_local()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            names = ("players", "job", "status") if admin else ("players",)
            last = -1
            seen = {n: -1 if n == "players" else hub.named.get(n, (0, b""))[0] for n in names}
            while True:
                with hub.cond:
                    def fresh():
                        return hub.revision != last or any(hub.named.get(n, (0, b""))[0] != seen[n] for n in names)
                    if not fresh():
                        hub.cond.wait(timeout=15)
                    rev = hub.revision
                    named = {n: hub.named.get(n, (0, b"")) for n in names}
                out = b""
                for n, (r, body) in named.items():
                    if r != seen[n]:
                        if r:
                            out += b"event: " + n.encode() + b"\ndata: " + body + b"\n\n"
                        seen[n] = r
                if rev != last:
                    out += f"data: {rev}\n\n".encode()
                    last = rev
                self.wfile.write(out or b": ping\n\n")
                self.wfile.flush()

    return Handler


# ============================================================================ run
class Server(ThreadingHTTPServer):
    """No SO_REUSEADDR: on Windows it would let a second instance bind the same port silently.
    SO_EXCLUSIVEADDRUSE additionally stops other programs from hijacking the port."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if excl is not None:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, excl, 1)
            except OSError:
                pass
        super().server_bind()


def _already_running(port) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return json.loads(r.read()).get("app") == APP_NAME
    except (OSError, ValueError):
        return False


def serve(cfg: dict, overrides: dict, open_browser=True):
    viewer = Viewer(cfg, overrides)
    threading.Thread(target=viewer.watch_forever, daemon=True, name="watch").start()
    first = True
    prev_ok = None
    while True:
        host, port = viewer.listen_addr()
        httpd = None
        try:
            httpd = Server((host, port), make_handler(viewer))
            viewer.bind_error = None
        except OSError as e:
            if first and _already_running(port):
                log(f"already running on port {port}" + (" - opening the browser" if open_browser else ""))
                if open_browser:
                    webbrowser.open(f"http://127.0.0.1:{port}/")
                return 0
            viewer.bind_error = f"{host}:{port}: {e.strerror or e}"
            log(f"cannot listen on {host}:{port} ({e})")
            if prev_ok and prev_ok != (host, port):
                host, port = prev_ok        # settings change failed: keep the previous address
            else:
                for p in range(port + 1, port + 11):
                    try:
                        httpd = Server((host, p), make_handler(viewer))
                        port = p
                        break
                    except OSError:
                        continue
            if httpd is None:
                try:
                    httpd = Server((host, port), make_handler(viewer))
                except OSError:
                    log("no free port found; use --port")
                    return 1
        httpd.daemon_threads = True
        viewer.httpd = httpd
        viewer.bound = (host, port)
        viewer.pending_restart = False
        viewer.push_status()
        prev_ok = (host, port)
        url = f"http://127.0.0.1:{port}/"
        log(f"{APP_NAME} {__version__} listening on {host}:{port}  ->  open {url}")
        if host not in ("127.0.0.1", "localhost", "::1"):
            log("LAN sharing is ON (no login!). Other devices: " + ", ".join(lan_urls(port) or ["-"]))
        if first and open_browser:
            threading.Timer(0.6, webbrowser.open, args=(url,)).start()
        first = False
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log("stopped")
            break
        finally:
            httpd.server_close()
        if not viewer.pending_restart:
            break
        log("listener restarting with new settings")
    g = viewer.gen
    if g and g.running:
        g.cancel()
    return 0
