"""Auto-detection of the Core Keeper Dedicated Server install and its data folder.

Install folder: Steam (registry -> libraryfolders.vdf -> every library -> appmanifest_1963720.acf).
Data folder:    %USERPROFILE%/AppData/LocalLow/Pugstorm/Core Keeper/DedicatedServer (Windows default).
World index:    "world" in <data>/ServerConfig.json.
"""
import json
import os
import re
import sys
from pathlib import Path

DEDICATED_SERVER_APPID = "1963720"      # "Core Keeper Dedicated Server" on Steam
DEFAULT_INSTALL_DIRNAME = "Core Keeper Dedicated Server"
SERVER_EXE = "CoreKeeperServer.exe"


# ---------------------------------------------------------------------------- VDF (Valve KeyValues)
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*')


def parse_vdf(text: str) -> dict:
    """Minimal KeyValues parser: quoted keys/values and nested { } blocks (enough for Steam's files)."""
    root, stack, key = {}, [], None
    cur = root
    for m in _TOKEN.finditer(text):
        s, brace = m.group(1), m.group(2)
        if s is None and brace is None:
            continue   # comment
        if brace == "{":
            new = {}
            if key is not None:
                cur[key] = new
            stack.append(cur)
            cur, key = new, None
        elif brace == "}":
            cur = stack.pop() if stack else root
            key = None
        else:
            s = s.replace('\\\\', '\\').replace('\\"', '"')
            if key is None:
                key = s
            else:
                cur[key] = s
                key = None
    return root


# ---------------------------------------------------------------------------- Steam
def _reg_value(root_name: str, subkey: str, value: str):
    if sys.platform != "win32":
        return None
    try:
        import winreg
        root = getattr(winreg, root_name)
        for flag in (0, getattr(winreg, "KEY_WOW64_32KEY", 0), getattr(winreg, "KEY_WOW64_64KEY", 0)):
            try:
                with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | flag) as k:
                    v, _ = winreg.QueryValueEx(k, value)
                    if v:
                        return str(v)
            except OSError:
                continue
    except ImportError:
        pass
    return None


def steam_roots() -> list:
    cands = [
        _reg_value("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
        _reg_value("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        _reg_value("HKEY_LOCAL_MACHINE", r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]
    pf86 = os.environ.get("ProgramFiles(x86)")
    if pf86:
        cands.append(str(Path(pf86) / "Steam"))
    out, seen = [], set()
    for c in cands:
        if not c:
            continue
        p = Path(c)
        k = os.path.normcase(str(p.resolve() if p.exists() else p))
        if k not in seen and (p / "steamapps").is_dir():
            seen.add(k)
            out.append(p.resolve() if p.exists() else p)
    return out


def library_folders(steam_root: Path) -> list:
    """All Steam library folders listed in <root>/steamapps/libraryfolders.vdf (+ the root itself)."""
    libs = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        data = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return libs
    block = data.get("libraryfolders") or data.get("LibraryFolders") or {}
    for k, v in block.items():
        if not k.isdigit():
            continue
        path = v.get("path") if isinstance(v, dict) else v     # new format: dict, old format: plain path
        if path:
            libs.append(Path(path))
    return libs


def find_server_installs() -> list:
    """Every install of the dedicated server found through Steam. Each: {path, source, appid}."""
    found, seen = [], set()
    for root in steam_roots():
        for lib in library_folders(root):
            apps = lib / "steamapps"
            manifest = apps / f"appmanifest_{DEDICATED_SERVER_APPID}.acf"
            installdir = None
            if manifest.is_file():
                try:
                    st = parse_vdf(manifest.read_text(encoding="utf-8", errors="replace")).get("AppState", {})
                    if str(st.get("appid")) == DEDICATED_SERVER_APPID:
                        installdir = st.get("installdir")
                except OSError:
                    pass
            for d in filter(None, (installdir, DEFAULT_INSTALL_DIRNAME)):
                p = apps / "common" / d
                key = os.path.normcase(str(p))
                if key in seen:
                    continue
                if (p / SERVER_EXE).is_file():
                    seen.add(key)
                    found.append({"path": str(p), "source": "steam" if installdir else "steam-default"})
                    break
    return found


def default_install():
    env = os.environ.get("CK_SERVER_INSTALL")
    if env and (Path(env) / SERVER_EXE).is_file():
        return Path(env)
    found = find_server_installs()
    return Path(found[0]["path"]) if found else None


# ---------------------------------------------------------------------------- data folder
def default_data_dir() -> Path:
    env = os.environ.get("CK_SERVER_DATA")
    if env:
        return Path(env)
    base = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(base) / "AppData" / "LocalLow" / "Pugstorm" / "Core Keeper" / "DedicatedServer"


def read_server_config(data_dir: Path) -> dict:
    try:
        cfg = json.loads((Path(data_dir) / "ServerConfig.json").read_text(encoding="utf-8-sig"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def list_worlds(data_dir: Path) -> list:
    """World slots that have a save (<data>/worlds/<n>.world.gzip) or a server map."""
    ids = set()
    for sub, pat in (("worlds", r"^(\d+)\.world\.gzip$"), ("servermaps", r"^(\d+)\.mapparts\.gzip$")):
        try:
            for f in (Path(data_dir) / sub).iterdir():
                m = re.match(pat, f.name)
                if m:
                    ids.add(int(m.group(1)))
        except OSError:
            pass
    return sorted(ids)


def install_status(path) -> dict:
    if not path:
        return {"path": None, "ok": False, "reason": "not_found"}
    p = Path(path)
    ok = (p / SERVER_EXE).is_file()
    return {"path": str(p), "ok": ok, "reason": None if ok else "no_exe"}


def data_status(path, world) -> dict:
    p = Path(path) if path else None
    if p is None or not p.is_dir():
        return {"path": str(p) if p else None, "ok": False, "reason": "not_found", "worlds": [],
                "mapFile": False, "saveFile": False}
    worlds = list_worlds(p)
    mapf = (p / "servermaps" / f"{world}.mapparts.gzip").is_file()
    savef = (p / "worlds" / f"{world}.world.gzip").is_file()
    return {"path": str(p), "ok": mapf, "reason": None if mapf else "no_map", "worlds": worlds,
            "mapFile": mapf, "saveFile": savef, "hasServerConfig": (p / "ServerConfig.json").is_file()}
