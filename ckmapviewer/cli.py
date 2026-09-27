"""Command line.

  CoreKeeperMapViewer.exe                     start the viewer (opens the browser)
  CoreKeeperMapViewer.exe --lan --port 8766   one-off overrides (not saved)
  CoreKeeperMapViewer.exe detect              print what was detected
  CoreKeeperMapViewer.exe generate [--radius N]
  CoreKeeperMapViewer.exe mod install|uninstall|status

From source: `py app.py ...` or `py -m ckmapviewer ...`.

Exit codes:
  0  success (`detect` and `mod status` are informational: 0 even when something is missing -
     read the printed JSON)
  1  failed: generation failed / was cancelled / another generation is already running,
     unexpected error
  2  refused: mod install/uninstall refused (e.g. the real server is running)
  130 interrupted (Ctrl+C)
"""
import argparse
import json
import sys
import time

from . import APP_NAME, __version__, config, detect, modinstall, paths


def _utf8_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _common(ap, default=None):
    # sub-commands accept them too (`generate --data-dir X`); SUPPRESS keeps a value given before
    # the sub-command from being reset by the sub-parser's default
    ap.add_argument("--data-dir", default=default, help="DedicatedServer data folder (default: auto / saved setting)")
    ap.add_argument("--server-install", default=default, help="folder with CoreKeeperServer.exe (default: auto / saved setting)")
    ap.add_argument("--world", type=int, default=default, help="world slot (default: ServerConfig.json)")


def _overrides(args) -> dict:
    return {"dataDir": getattr(args, "data_dir", None), "serverInstall": getattr(args, "server_install", None),
            "world": getattr(args, "world", None), "port": getattr(args, "port", None),
            "host": getattr(args, "host", None) or ("0.0.0.0" if getattr(args, "lan", False) else None),
            "playersFile": getattr(args, "players_file", None)}


def main(argv=None):
    _utf8_console()
    ap = argparse.ArgumentParser(prog=APP_NAME, description="Core Keeper dedicated server live map viewer")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    _common(ap)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default=None, help="listen address (0.0.0.0 = LAN)")
    ap.add_argument("--lan", action="store_true", help="share on the local network (same as --host 0.0.0.0)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--players-file", default=None, help=argparse.SUPPRESS)   # testing
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="start the viewer (default)")
    _common(sub.add_parser("detect", help="print detected paths"), argparse.SUPPRESS)
    g = sub.add_parser("generate", help="generate the full map (disposable server copy)")
    _common(g, argparse.SUPPRESS)
    g.add_argument("--radius", type=int, default=0, help="tiles from the core (0 = automatic world size)")
    g.add_argument("--timeout", type=int, default=120, help="minutes")
    g.add_argument("--keep", action="store_true", help="keep the disposable data folder")
    m = sub.add_parser("mod", help="LivePlayers mod on the real server")
    _common(m, argparse.SUPPRESS)
    m.add_argument("action", choices=["install", "uninstall", "status"])
    args = ap.parse_args(argv)

    cfg = config.load()
    ov = _overrides(args)
    cmd = args.cmd or "serve"

    if cmd == "serve":
        from .server import serve
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleTitleW(f"{APP_NAME} {__version__} (close this window to stop)")
            except (AttributeError, OSError):
                pass
        return serve(cfg, ov, open_browser=cfg.get("openBrowser", True) and not args.no_browser)

    eff = config.Effective(cfg, ov)
    if cmd == "detect":
        out = {"version": __version__, "configFile": str(paths.config_path()),
               "install": detect.install_status(eff.install), "installSource": eff.install_source,
               "steamInstalls": detect.find_server_installs(),
               "data": detect.data_status(eff.data_dir, eff.world), "dataSource": eff.data_source,
               "world": eff.world, "worldSource": eff.world_source, "outputDir": str(eff.out_dir)}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if cmd == "generate":
        from .generator import Generator
        last = [None]

        def show(st):
            line = f"{st['state']} {st.get('phase')} {st.get('fraction', 0) * 100:.0f}% {st.get('message', '')}"
            if line != last[0]:
                last[0] = line
                print(time.strftime("[%H:%M:%S]"), line, flush=True)
        g = Generator(eff.install, eff.data_dir, eff.world, eff.out_dir, radius=args.radius,
                      timeout_min=args.timeout, keep=args.keep, on_change=show)
        try:
            st = g.run()
        except KeyboardInterrupt:
            g.cancel()
            print("result: interrupted", file=sys.stderr)
            return 130
        print(f"result: {st['state']} {st.get('error') or ''}  output: {eff.out_dir}")
        if st["state"] != "done":
            print(f"generation {st['state']}: {st.get('error') or st['state']}", file=sys.stderr)
            return 1
        return 0

    if cmd == "mod":
        if args.action == "status":
            print(json.dumps(modinstall.status(eff.install), indent=2, ensure_ascii=False))
            return 0
        try:
            fn = modinstall.install if args.action == "install" else modinstall.uninstall
            print(json.dumps(fn(eff.install), indent=2, ensure_ascii=False))
            if args.action == "install":
                print("Restart the dedicated server. Its log should show '[LivePlayers] v... loaded'.")
            return 0
        except modinstall.ModError as e:
            print(f"refused ({e.code}): {e}", file=sys.stderr)
            return 2
        except OSError as e:
            print(f"failed: {e}", file=sys.stderr)
            return 1
    return 1   # unknown command (argparse normally rejects it first)
