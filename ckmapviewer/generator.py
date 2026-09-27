"""Generate the FULL map (including never-explored areas) offline, from a disposable server copy.

  1. Mirror the installed Dedicated Server into a scratch folder (first run copies ~0.5 GB,
     later runs only changed files). The real install is only READ.
  2. Copy the world save (+ current server map) into a separate scratch data folder (-datapath).
     ServerConfig.json is NOT copied (it holds the real server's game ID / password).
  3. Put the FullMapGen mod into the COPY only (all other mods removed from the copy), plus
     <datapath>/mods/FullMapGen/ARMED which arms it. Without that marker, with the default data
     path, or as soon as any player connects, the mod stays inert / aborts.
  4. Run the disposable server (random port, random game ID and password, max 1 player); the mod
     generates terrain area by area, draws it into the server map and writes a DONE marker.
  5. Stop only the process we started, copy the map + POIs into the per-world app-data folder,
     where the viewer picks them up as the background layer.

The scratch folder is shared by every process of this app (viewer, second viewer, `generate`
command), so a run holds fullmap-work/.lock (see worklock.py) from before its first filesystem
change until it has cleaned up. While the disposable server runs, the job fails (instead of
waiting forever) if the mod's progress file stops changing or its marker folder disappears.
"""
import gzip
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import detect, paths, procs
from .worklock import LockBusy, WorkLock

MOD_DIR = Path("mods") / "FullMapGen"   # relative to the datapath (the mod's ConfigFilesystem)
ARM_TOKEN = "DISPOSABLE-COPY-OK"


class GenError(Exception):
    pass


class Cancelled(Exception):
    pass


class Busy(GenError):
    """Another process holds the scratch-folder lock."""
    def __init__(self, holder: dict):
        self.holder = holder or {}
        super().__init__(f"another generation is already running (pid {self.holder.get('pid', '?')})")


def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _check_gzip(path: Path):
    with gzip.open(path, "rb") as f:
        while f.read(1 << 20):
            pass


class Generator:
    """One generation run. Thread-safe state snapshot via .state(); cancel via .cancel()."""

    def __init__(self, install: Path, data_dir: Path, world: int, out_dir: Path, radius: int = 0,
                 timeout_min: int = 120, on_change=None, keep=False, stall_min: float = 10):
        self.install = Path(install) if install else None
        self.data_dir = Path(data_dir)
        self.world = int(world)
        self.out_dir = Path(out_dir)
        self.radius = int(radius or 0)
        self.timeout_min = timeout_min
        self.stall_min = stall_min
        self.keep = keep
        self.on_change = on_change or (lambda st: None)
        self.work = paths.work_dir()
        self.worklock = WorkLock(self.work)
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._proc = None
        self._st = {"state": "idle", "phase": None, "fraction": 0.0, "message": "", "log": [],
                    "startedAt": None, "finishedAt": None, "world": self.world, "error": None}

    # ---- state ------------------------------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            st = dict(self._st)
            st["log"] = list(self._st["log"])
            return st

    def _update(self, log=None, **kw):
        with self._lock:
            self._st.update(kw)
            if log:
                self._st["log"].append(time.strftime("[%H:%M:%S] ") + log)
                del self._st["log"][:-40]
        if log:
            print(time.strftime("[%H:%M:%S]"), "[fullmap]", log, flush=True)
        try:
            self.on_change(self.state())
        except Exception:
            pass

    @property
    def running(self):
        return self._st["state"] == "running"

    def cancel(self):
        self._cancel.set()
        p = self._proc
        if p is not None:
            threading.Thread(target=procs.stop_own_process, args=(p, 30), daemon=True).start()

    def _check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    # ---- safety -----------------------------------------------------------------------------
    def _preflight(self):
        if self.install is None or not (self.install / detect.SERVER_EXE).is_file():
            raise GenError("server_install_missing")
        if not (self.data_dir / "worlds" / f"{self.world}.world.gzip").is_file():
            raise GenError("world_save_missing")
        # the scratch folders must never be (inside) the real install or the real data folder
        for real in (self.install, self.data_dir):
            if paths.is_inside(self.work, real) or paths.is_inside(real, self.work):
                raise GenError("work_dir_overlaps_real_server")

    def acquire_lock(self):
        """Take the cross-process scratch-folder lock; raises Busy. Safe to call twice."""
        try:
            self.worklock.acquire()
        except LockBusy as e:
            raise Busy(e.holder) from None

    # ---- steps ------------------------------------------------------------------------------
    def _mirror_install(self, dst: Path):
        self._update(phase="copy", fraction=0.0, message="copy_server", log="copying the server install (first run ~0.5 GB, later only changes)")
        cmd = ["robocopy", str(self.install), str(dst), "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
               "/R:1", "/W:1", "/XF", "CoreKeeperServerLog.txt", "GameInfo.txt", "GameID.txt"]
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=procs.NO_WINDOW)
        self._proc = p
        procs.job().assign(p)
        while p.poll() is None:
            if self._cancel.is_set():
                procs.stop_own_process(p, 5)
                raise Cancelled()
            time.sleep(0.5)
        self._proc = None
        if p.returncode >= 8:   # robocopy: 0-7 = success variants
            raise GenError(f"copy_failed (robocopy rc={p.returncode})")
        mods = dst / "CoreKeeperServer_Data" / "StreamingAssets" / "Mods"
        shutil.rmtree(mods, ignore_errors=True)          # the COPY runs with our mod only
        shutil.copytree(paths.mod_source("FullMapGen"), mods / "FullMapGen")
        self._update(log="FullMapGen installed into the copy only")

    def _prepare_data(self, data: Path):
        self._update(phase="prepare", message="copy_world")
        if data.exists():
            shutil.rmtree(data)
        (data / "worlds").mkdir(parents=True)
        (data / "servermaps").mkdir()
        for extra in ("worldinfos", "worldgenparams"):
            if (self.data_dir / extra).is_dir():
                shutil.copytree(self.data_dir / extra, data / extra)
        # the live server may be writing the save right now: retry until the file is stable
        # during the whole copy (the save format is not plain gzip, so size+mtime is the check)
        src = self.data_dir / "worlds" / f"{self.world}.world.gzip"
        dst = data / "worlds" / src.name
        for attempt in range(15):
            before = src.stat()
            shutil.copy2(src, dst)
            after = src.stat()
            if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) \
                    and dst.stat().st_size == after.st_size and time.time() - after.st_mtime > 2:
                break
            self._update(log="world save is being written, retrying copy")
            time.sleep(2)
        else:
            raise GenError("world_save_unreadable")
        mp = self.data_dir / "servermaps" / f"{self.world}.mapparts.gzip"
        if mp.is_file():
            shutil.copy2(mp, data / "servermaps")
        (data / MOD_DIR).mkdir(parents=True)
        arm = ARM_TOKEN + (f"\nradius={self.radius}" if self.radius else "")
        (data / MOD_DIR / "ARMED").write_text(arm, encoding="utf-8")
        self._update(log=f"world {self.world} copied")

    def _run_server(self, install: Path, data: Path):
        exe = install / detect.SERVER_EXE
        logfile = data / "server.log"
        args = [str(exe), "-batchmode", "-logfile", str(logfile), "-datapath", str(data),
                "-world", str(self.world), "-port", str(_free_udp_port()), "-maxplayers", "1",
                "-gameid", uuid.uuid4().hex[:28], "-password", uuid.uuid4().hex]
        self._update(phase="start", fraction=0.0, message="start_server", log="starting disposable server")
        proc = subprocess.Popen(args, cwd=str(install), creationflags=procs.NO_WINDOW,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._proc = proc
        if not procs.job().assign(proc):
            self._update(log="note: could not attach the disposable server to the app's job object")
        t0, last = time.time(), None
        stamp, stamp_at = None, t0     # last seen PROGRESS.json (mtime, size) and when it changed
        try:
            while True:
                self._check_cancel()
                if proc.poll() is not None:
                    raise GenError(f"server_exited (rc={proc.returncode})")
                if not (data / MOD_DIR / "ARMED").is_file() and not (data / MOD_DIR / "DONE.json").exists():
                    raise GenError("work_dir_tampered (the mod's marker folder disappeared - another "
                                   "process changed the scratch folder)")
                done = data / MOD_DIR / "DONE.json"
                if done.exists():
                    self._update(fraction=1.0, log="done marker: " + done.read_text(encoding="utf-8").strip())
                    break
                aborted = data / MOD_DIR / "ABORTED.txt"
                if aborted.exists():
                    raise GenError("mod_aborted: " + aborted.read_text(encoding="utf-8").strip())
                prog = data / MOD_DIR / "PROGRESS.json"
                try:
                    st = prog.stat()
                    cur = (st.st_mtime_ns, st.st_size)
                except OSError:
                    cur = None
                if cur is not None and cur != stamp:
                    stamp, stamp_at = cur, time.time()
                elif time.time() - stamp_at > self.stall_min * 60:
                    raise GenError(f"stalled (no progress from the mod for {self.stall_min:g} min "
                                   "while the disposable server is still running)")
                if cur is not None:
                    try:
                        p = json.loads(prog.read_text(encoding="utf-8"))
                        key = (p.get("phase"), round(float(p.get("fraction", 0)), 3))
                        if key != last:
                            last = key
                            self._update(phase=str(p.get("phase")), fraction=float(p.get("fraction", 0)),
                                         message=str(p.get("msg", ""))[:300])
                    except (ValueError, OSError):
                        pass
                if time.time() - t0 > self.timeout_min * 60:
                    raise GenError(f"timeout ({self.timeout_min} min)")
                time.sleep(2)
        finally:
            self._update(phase="stop", message="stop_server")
            procs.stop_own_process(proc)
            self._proc = None
            self._update(log="disposable server stopped")

    def _publish(self, data: Path):
        out = data / "servermaps" / f"{self.world}.mapparts.gzip"
        if not out.exists():
            raise GenError("result_map_missing")
        _check_gzip(out)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        pairs = [(out, "fullmap.mapparts.gzip")]
        for src_name, dst_name in (("pois.json", "pois.json"), ("STATS.json", "stats.json"),
                                   ("DONE.json", "done.json")):
            src = data / MOD_DIR / src_name
            if src.exists():
                json.loads(src.read_text(encoding="utf-8"))   # refuse to publish a broken file
                pairs.append((src, dst_name))
            else:
                self._update(log=f"warning: {src_name} missing")
        for src, name in pairs:
            d = self.out_dir / name
            t = d.with_name(d.name + ".tmp")
            shutil.copy2(src, t)
            os.replace(t, d)   # atomic: the viewer never sees a half-written file
            self._update(log=f"saved {name} ({d.stat().st_size // 1024} KB)")

    # ---- main -------------------------------------------------------------------------------
    def run(self):
        self._update(state="running", startedAt=time.time(), finishedAt=None, error=None,
                     phase="check", fraction=0.0, message="", log="start")
        data = self.work / "data"
        try:
            self._preflight()
            self.acquire_lock()   # before ANY change in the shared scratch folder
            install = self.work / "server"
            self._mirror_install(install)
            self._check_cancel()
            self._prepare_data(data)
            self._check_cancel()
            self._run_server(install, data)
            self._publish(data)
            self._update(state="done", phase="done", fraction=1.0, message="done", finishedAt=time.time())
        except Cancelled:
            self._update(state="cancelled", message="cancelled", finishedAt=time.time(), log="cancelled")
        except GenError as e:
            self._update(state="failed", error=str(e), finishedAt=time.time(), log="failed: " + str(e))
        except Exception as e:   # unexpected: report, never crash the viewer
            self._update(state="failed", error=repr(e), finishedAt=time.time(), log="failed: " + repr(e))
        finally:
            if self.worklock.held:
                try:
                    self._cleanup(data)
                finally:
                    self.worklock.release()
        return self.state()

    def _cleanup(self, data: Path):
        """Only while holding the lock: the scratch folder may belong to another run otherwise."""
        try:
            log = data / "server.log"
            if self._st["state"] != "done" and log.is_file():
                try:   # keep the disposable server's log for troubleshooting
                    self.out_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(log, self.out_dir / "fullmap-last-server.log")
                    self._update(log="server log kept: fullmap-last-server.log (app data folder)")
                except OSError:
                    pass
        finally:
            if not self.keep:
                shutil.rmtree(data, ignore_errors=True)
