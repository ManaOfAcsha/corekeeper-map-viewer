"""Install / remove the LivePlayers mod in the REAL dedicated-server install.

Refuses while CoreKeeperServer.exe from that install folder is running (mods are compiled only
when the server starts, and files in use must not be swapped). The mod is server-only
(requiredOn = 0): players join with the unmodded game.
"""
import hashlib
import json
import shutil
from pathlib import Path

from . import detect, paths, procs

MOD_NAME = "LivePlayers"


class ModError(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def mods_dir(install: Path) -> Path:
    return Path(install) / "CoreKeeperServer_Data" / "StreamingAssets" / "Mods"


def _tree_hash(root: Path):
    if not root.is_dir():
        return None
    h = hashlib.sha1()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(f.relative_to(root).as_posix().encode())
        h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def _version(root: Path):
    try:
        src = (root / "Scripts" / "LivePlayersMod.cs").read_text(encoding="utf-8", errors="replace")
        for line in src.splitlines():
            if "VERSION =" in line:
                return line.split('"')[1]
    except (OSError, IndexError):
        pass
    return None


def status(install) -> dict:
    src = paths.mod_source(MOD_NAME)
    st = {"bundledVersion": _version(src), "installed": False, "upToDate": False, "sameFiles": False, "installedVersion": None,
          "installPath": None, "otherMods": [], "server": {"running": None, "pids": [], "unknown": []}}
    if not install or not (Path(install) / detect.SERVER_EXE).is_file():
        st["error"] = "no_install"
        return st
    dst = mods_dir(install) / MOD_NAME
    st["installPath"] = str(dst)
    if dst.is_dir():
        st["installed"] = True
        st["installedVersion"] = _version(dst)
        st["sameFiles"] = _tree_hash(dst) == _tree_hash(src)
        st["upToDate"] = st["installedVersion"] == st["bundledVersion"]
    try:
        st["otherMods"] = sorted(p.name for p in mods_dir(install).iterdir()
                                 if p.is_dir() and p.name != MOD_NAME)
    except OSError:
        pass
    st["server"] = procs.server_running_from(Path(install))
    return st


def _check_stopped(install: Path):
    run = procs.server_running_from(install)
    if run["running"] is None:
        raise ModError("check_failed", "Could not check whether the server is running.")
    if run["running"]:
        raise ModError("server_running", "The dedicated server from this folder is running. Stop it first.",
                       pids=run["pids"])
    if run["unknown"]:
        raise ModError("server_maybe_running",
                       "A CoreKeeperServer.exe is running whose location cannot be read "
                       "(different user or elevated). Stop it first.", pids=run["unknown"])


def install(install_dir) -> dict:
    install_dir = Path(install_dir or "")
    if not (install_dir / detect.SERVER_EXE).is_file():
        raise ModError("no_install", f"{detect.SERVER_EXE} not found in {install_dir}")
    src = paths.mod_source(MOD_NAME)
    if not (src / "ModManifest.json").is_file():
        raise ModError("no_source", f"bundled mod missing: {src}")
    json.loads((src / "ModManifest.json").read_text(encoding="utf-8"))   # never install a broken bundle
    _check_stopped(install_dir)
    mods = mods_dir(install_dir)
    dst = mods / MOD_NAME
    mods.mkdir(parents=True, exist_ok=True)
    action = "updated" if dst.exists() else "installed"
    tmp = mods / (MOD_NAME + ".installing")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp)
    if dst.exists():
        shutil.rmtree(dst)
    tmp.rename(dst)
    return {"ok": True, "action": action, "path": str(dst)}


def uninstall(install_dir) -> dict:
    install_dir = Path(install_dir or "")
    if not (install_dir / detect.SERVER_EXE).is_file():
        raise ModError("no_install", f"{detect.SERVER_EXE} not found in {install_dir}")
    dst = mods_dir(install_dir) / MOD_NAME
    if not dst.exists():
        return {"ok": True, "action": "not_installed", "path": str(dst)}
    _check_stopped(install_dir)
    shutil.rmtree(dst)
    return {"ok": True, "action": "removed", "path": str(dst)}
