"""Cross-process lock for the shared full-map scratch folder (fullmap-work).

Only one full-map generation may touch fullmap-work at a time, across ALL processes (the viewer,
a second viewer on another port, the `generate` command). The lock is a file created atomically
(O_CREAT | O_EXCL) that records who holds it:

    fullmap-work/.lock   {"pid": 1234, "exe": "...", "started": 1790000000.0, "token": "..."}

A lock is stale (and taken over) when its process is gone, when the PID now belongs to an
unrelated program (not this app / not Python), or when that process started after the lock was
written (PID reused). A process that exists but cannot be inspected counts as a live holder.
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

from . import procs

LOCK_NAME = ".lock"
UNREADABLE_GRACE_SEC = 30   # a lock file that is still being written by its creator


class LockBusy(Exception):
    def __init__(self, holder: dict):
        self.holder = holder or {}
        super().__init__(f"another generation is already running (pid {self.holder.get('pid', '?')})")


def _is_our_kind(exe) -> bool:
    name = os.path.basename(str(exe)).lower()
    own = os.path.basename(sys.executable or "").lower()
    return name == own or name == "corekeepermapviewer.exe" or name.startswith("python") or name == "py.exe"


def holder_alive(info: dict) -> bool:
    """Is the process recorded in a lock file still the one that holds it?"""
    try:
        pid = int(info.get("pid"))
    except (TypeError, ValueError):
        return False
    p = procs.process_info(pid)
    if p is None:
        return False
    if p.get("exe") and not _is_our_kind(p["exe"]):
        return False          # PID reused by an unrelated program
    started, created = info.get("started"), p.get("created")
    if isinstance(started, (int, float)) and created and created > started + 2:
        return False          # PID reused: this process started after the lock was written
    return True


class WorkLock:
    def __init__(self, work_dir: Path):
        self.path = Path(work_dir) / LOCK_NAME
        self.token = None

    @property
    def held(self) -> bool:
        return self.token is not None

    def read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {}

    def acquire(self):
        """Take the lock or raise LockBusy. Never touches anything else in the work folder."""
        if self.held:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        body = json.dumps({"pid": os.getpid(), "exe": sys.executable, "started": time.time(),
                           "token": token}).encode("utf-8")
        for _ in range(5):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
            except FileExistsError:
                self._take_over_if_stale()
                continue
            try:
                os.write(fd, body)
            finally:
                os.close(fd)
            self.token = token
            return
        raise LockBusy(self.read() or {})

    def _take_over_if_stale(self):
        info = self.read()
        if info is None:
            return            # vanished meanwhile: just retry the atomic create
        if not info:          # empty / unreadable: may be mid-write by its creator
            try:
                age = time.time() - self.path.stat().st_mtime
            except FileNotFoundError:
                return
            if age < UNREADABLE_GRACE_SEC:
                raise LockBusy({})
        elif holder_alive(info):
            raise LockBusy(info)
        # stale: move it aside atomically (only one contender wins the rename), then retry create
        aside = self.path.with_name(f"{LOCK_NAME}.stale-{uuid.uuid4().hex[:8]}")
        try:
            os.rename(self.path, aside)
        except FileNotFoundError:
            return
        except OSError:
            raise LockBusy(info)
        try:
            moved = json.loads(aside.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            moved = {}
        if info and moved.get("token") != info.get("token"):
            # we moved a FRESH lock someone created in between: give it back and report busy
            try:
                os.rename(aside, self.path)
            except OSError:
                pass
            raise LockBusy(moved)
        try:
            aside.unlink()
        except OSError:
            pass

    def release(self):
        """Remove the lock file, but only if it is still ours."""
        if not self.held:
            return
        info = self.read()
        if info and info.get("token") == self.token:
            try:
                self.path.unlink()
            except OSError:
                pass
        self.token = None
