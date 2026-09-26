"""Persistent settings (%APPDATA%/CoreKeeperMapViewer/config.json) + resolution of effective paths.

A value of null means "auto-detect". Command-line options override the file for one run only.
"""
import json
import os
import threading
from pathlib import Path

from . import detect, paths

DEFAULTS = {
    "serverInstall": None,   # folder containing CoreKeeperServer.exe
    "dataDir": None,         # DedicatedServer data folder (ServerConfig.json, servermaps/, worlds/)
    "world": None,           # world slot; null = ServerConfig.json "world"
    "port": 8765,
    "lan": False,            # True = listen on 0.0.0.0 (other devices on the network can view)
    "openBrowser": True,
}

_lock = threading.Lock()


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        data = json.loads(paths.config_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict):
    with _lock:
        p = paths.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}, indent=2,
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)


def validate_patch(patch: dict) -> dict:
    """Checks a settings update from the UI; returns the cleaned subset. Raises ValueError."""
    out = {}
    for k in ("serverInstall", "dataDir"):
        if k in patch:
            v = patch[k]
            if v in (None, ""):
                out[k] = None
            elif isinstance(v, str) and len(v) < 1024:
                out[k] = str(Path(v.strip().strip('"')))
            else:
                raise ValueError(f"bad {k}")
    if "world" in patch:
        v = patch["world"]
        if v in (None, ""):
            out["world"] = None
        else:
            v = int(v)
            if not 0 <= v <= 999:
                raise ValueError("bad world")
            out["world"] = v
    if "port" in patch:
        v = int(patch["port"])
        if not 1024 <= v <= 65535:
            raise ValueError("port must be 1024-65535")
        out["port"] = v
    for k in ("lan", "openBrowser"):
        if k in patch:
            out[k] = bool(patch[k])
    return out


class Effective:
    """Resolved paths for one configuration (auto-detected where the config says null)."""

    def __init__(self, cfg: dict, overrides: dict = None):
        o = overrides or {}
        self.install_source = "config" if cfg.get("serverInstall") else "auto"
        if o.get("serverInstall"):
            self.install_source = "cli"
        inst = o.get("serverInstall") or cfg.get("serverInstall")
        self.install = Path(inst) if inst else detect.default_install()

        self.data_source = "cli" if o.get("dataDir") else ("config" if cfg.get("dataDir") else "auto")
        d = o.get("dataDir") or cfg.get("dataDir")
        self.data_dir = Path(d) if d else detect.default_data_dir()

        self.server_cfg = detect.read_server_config(self.data_dir)
        w = o.get("world") if o.get("world") is not None else cfg.get("world")
        self.world_source = "config" if w is not None else "serverconfig"
        if w is None:
            try:
                w = int(self.server_cfg.get("world", 0))
            except (TypeError, ValueError):
                w = 0
        self.world = int(w)
        self.world_name = str(self.server_cfg.get("worldName") or "Core Keeper")
        self.players_file = Path(o["playersFile"]) if o.get("playersFile") else \
            self.data_dir / "mods" / "LivePlayers" / "players.json"
        self.out_dir = paths.world_dir(self.data_dir, self.world)

    # outputs of the full-map generator + markers, per world, under %APPDATA%
    @property
    def full_map(self) -> Path:
        return self.out_dir / "fullmap.mapparts.gzip"

    @property
    def pois(self) -> Path:
        return self.out_dir / "pois.json"

    @property
    def markers(self) -> Path:
        return self.out_dir / "markers.json"

    @property
    def live_map(self) -> Path:
        return self.data_dir / "servermaps" / f"{self.world}.mapparts.gzip"
