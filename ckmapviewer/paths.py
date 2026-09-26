"""Where things live: bundled resources (web page, mods) and per-user app data."""
import hashlib
import os
import sys
from pathlib import Path

from . import APP_NAME


def resource_dir() -> Path:
    """Folder that contains web/ and mods/ (the package folder, or PyInstaller's bundle dir)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "ckmapviewer"
    return Path(__file__).resolve().parent


def web_dir() -> Path:
    return resource_dir() / "web"


def mod_source(name: str) -> Path:
    return resource_dir() / "mods" / name


def app_data_dir() -> Path:
    """%APPDATA%/CoreKeeperMapViewer (config, markers, generated full maps). Override: CKMV_HOME."""
    override = os.environ.get("CKMV_HOME")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".config" / APP_NAME   # non-Windows (running from source for development)


def work_dir() -> Path:
    """Scratch space for the disposable server copy used by full-map generation (~0.5 GB)."""
    override = os.environ.get("CKMV_WORK")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
    if base:
        return Path(base) / APP_NAME / "fullmap-work"
    return app_data_dir() / "fullmap-work"


def config_path() -> Path:
    return app_data_dir() / "config.json"


def norm_key(p) -> str:
    return os.path.normcase(os.path.abspath(str(p))).replace("\\", "/").rstrip("/")


def world_dir(data_dir: Path, world: int) -> Path:
    """Per-world output folder. The data folder is part of the key so two servers never mix."""
    h = hashlib.sha1(norm_key(data_dir).encode("utf-8")).hexdigest()[:8]
    return app_data_dir() / "worlds" / f"{int(world)}-{h}"


def is_inside(child: Path, parent: Path) -> bool:
    try:
        c, p = norm_key(child), norm_key(parent)
    except (OSError, ValueError):
        return False
    return c == p or c.startswith(p + "/")
