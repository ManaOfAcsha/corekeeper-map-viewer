"""Cross-process lock for full-map generation, the stall/tamper watchdog and CLI exit codes."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ckmapviewer import cli, config, detect, generator, procs, server, worklock

_real_sleep = time.sleep


def fake_install_and_data(root: Path):
    inst, data = root / "inst", root / "data"
    inst.mkdir()
    (inst / detect.SERVER_EXE).write_bytes(b"")
    (data / "worlds").mkdir(parents=True)
    (data / "worlds" / "0.world.gzip").write_bytes(b"x")
    return inst, data


def write_lock(path: Path, **info):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"pid": os.getpid(), "exe": "x", "started": time.time(), "token": "t0"}
    body.update(info)
    path.write_text(json.dumps(body), encoding="utf-8")


class WorkLockTests(unittest.TestCase):
    def test_acquire_contend_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = worklock.WorkLock(Path(tmp) / "w"), worklock.WorkLock(Path(tmp) / "w")
            a.acquire()
            self.assertTrue(a.held)
            info = json.loads(a.path.read_text(encoding="utf-8"))
            self.assertEqual(info["pid"], os.getpid())
            with self.assertRaises(worklock.LockBusy) as cm:   # our own process = a live holder
                b.acquire()
            self.assertIn(f"pid {os.getpid()}", str(cm.exception))
            self.assertFalse(b.held)
            b.release()                          # not held: must not remove a's lock
            self.assertTrue(a.path.exists())
            a.release()
            self.assertFalse(a.path.exists())
            b.acquire()
            self.assertTrue(b.held)
            b.release()

    def test_stale_lock_dead_pid_is_taken_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            lk = worklock.WorkLock(Path(tmp))
            write_lock(lk.path, pid=424242, token="old")
            with mock.patch.object(procs, "process_info", return_value=None):
                lk.acquire()
            self.assertTrue(lk.held)
            self.assertNotEqual(json.loads(lk.path.read_text(encoding="utf-8"))["token"], "old")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], [".lock"])   # no leftovers
            lk.release()

    def test_stale_lock_pid_reused_by_other_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            lk = worklock.WorkLock(Path(tmp))
            write_lock(lk.path, pid=4242)
            with mock.patch.object(procs, "process_info",
                                   return_value={"exe": r"C:\Windows\notepad.exe", "created": 1.0}):
                lk.acquire()
            self.assertTrue(lk.held)
            lk.release()
            # same program name but started after the lock was written = PID reused
            write_lock(lk.path, pid=4242, started=1000.0)
            with mock.patch.object(procs, "process_info",
                                   return_value={"exe": r"C:\x\CoreKeeperMapViewer.exe", "created": 5000.0}):
                lk.acquire()
            lk.release()

    def test_live_or_uninspectable_holder_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            lk = worklock.WorkLock(Path(tmp))
            for pinfo in ({"exe": r"C:\apps\CoreKeeperMapViewer.exe", "created": 1.0},
                          {"exe": r"C:\Python313\python.exe", "created": None},
                          {"exe": None, "created": None}):          # access denied
                write_lock(lk.path, pid=777, started=time.time())
                with mock.patch.object(procs, "process_info", return_value=pinfo):
                    with self.assertRaises(worklock.LockBusy) as cm:
                        lk.acquire()
                self.assertEqual(cm.exception.holder["pid"], 777)
                self.assertFalse(lk.held)

    def test_unreadable_lock_fresh_is_busy_old_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            lk = worklock.WorkLock(Path(tmp))
            lk.path.write_bytes(b"")
            with self.assertRaises(worklock.LockBusy):
                lk.acquire()
            old = time.time() - 3600
            os.utime(lk.path, (old, old))
            lk.acquire()
            self.assertTrue(lk.held)
            lk.release()

    def test_release_keeps_a_lock_that_is_no_longer_ours(self):
        with tempfile.TemporaryDirectory() as tmp:
            lk = worklock.WorkLock(Path(tmp))
            lk.acquire()
            write_lock(lk.path, token="someone-else")
            lk.release()
            self.assertTrue(lk.path.exists())


class GeneratorLockTests(unittest.TestCase):
    def test_busy_run_fails_fast_without_touching_work_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst, data = fake_install_and_data(root)
            work = root / "work"
            (work / "data").mkdir(parents=True)
            (work / "data" / "keep.txt").write_text("other run", encoding="utf-8")
            write_lock(work / ".lock", pid=9999, started=time.time())
            with mock.patch.dict(os.environ, {"CKMV_WORK": str(work)}), \
                    mock.patch.object(procs, "process_info", return_value={"exe": "python.exe", "created": 1.0}), \
                    mock.patch.object(generator.subprocess, "Popen") as popen:
                st = generator.Generator(inst, data, 0, root / "out").run()
            self.assertEqual(st["state"], "failed")
            self.assertIn("another generation is already running (pid 9999)", st["error"])
            popen.assert_not_called()                               # no robocopy, no server
            self.assertTrue((work / "data" / "keep.txt").is_file())  # other run's data untouched
            self.assertFalse((work / "server").exists())
            self.assertEqual(json.loads((work / ".lock").read_text(encoding="utf-8"))["pid"], 9999)
            self.assertFalse((root / "out").exists())

    def test_lock_released_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst, data = fake_install_and_data(root)
            work = root / "work"
            with mock.patch.dict(os.environ, {"CKMV_WORK": str(work)}):
                g = generator.Generator(inst, data, 0, root / "out")
                with mock.patch.object(g, "_mirror_install", side_effect=generator.GenError("copy_failed (robocopy rc=11)")):
                    st = g.run()
            self.assertEqual(st["state"], "failed")
            self.assertFalse((work / ".lock").exists())

    def test_viewer_returns_409_payload_when_busy_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst, data = fake_install_and_data(root)
            work = root / "work"
            write_lock(work / ".lock", pid=31337, started=time.time())
            env = {"CKMV_HOME": str(root / "home"), "CKMV_WORK": str(work)}
            with mock.patch.dict(os.environ, env), mock.patch.object(detect, "default_install", return_value=None), \
                    mock.patch.object(procs, "process_info", return_value={"exe": "CoreKeeperMapViewer.exe", "created": 1.0}):
                viewer = server.Viewer(dict(config.DEFAULTS, dataDir=str(data), serverInstall=str(inst)), {})
                r = viewer.start_generate(300)
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "already_running_elsewhere")
            self.assertEqual(r["pid"], 31337)
            self.assertIsNone(viewer.gen)


class FakeProc:
    pid = 1
    returncode = None

    def poll(self):
        return None


class WatchdogTests(unittest.TestCase):
    def _run_server(self, tmp, prepare, stall_min=10):
        root = Path(tmp)
        data = root / "data"
        (data / generator.MOD_DIR).mkdir(parents=True)
        (data / generator.MOD_DIR / "ARMED").write_text(generator.ARM_TOKEN, encoding="utf-8")
        g = generator.Generator(root / "inst", root / "src", 0, root / "out", timeout_min=5, stall_min=stall_min)
        prepare(data)
        stopped = []
        with mock.patch.object(generator.subprocess, "Popen", return_value=FakeProc()), \
                mock.patch.object(procs, "stop_own_process", side_effect=lambda p, *a: stopped.append(p)), \
                mock.patch.object(generator.time, "sleep", lambda s: _real_sleep(0.05)):
            with self.assertRaises(generator.GenError) as cm:
                g._run_server(root / "inst", data)
        self.assertEqual(len(stopped), 1)            # only the process it started
        self.assertIsInstance(stopped[0], FakeProc)
        return str(cm.exception)

    def test_marker_folder_vanished(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            err = self._run_server(tmp, lambda d: shutil.rmtree(d / "mods"))
            self.assertIn("work_dir_tampered", err)

    def test_no_progress_is_a_stall(self):
        with tempfile.TemporaryDirectory() as tmp:
            def prep(d):
                (d / generator.MOD_DIR / "PROGRESS.json").write_text('{"phase":"generate","fraction":0.4}', encoding="utf-8")
            err = self._run_server(tmp, prep, stall_min=0.005)   # 0.3 s
            self.assertIn("stalled", err)


class CliExitCodeTests(unittest.TestCase):
    def _generate(self, final_state, error=None):
        class FakeGen:
            def __init__(self, *a, **kw):
                self.on_change = kw.get("on_change")

            def run(self):
                return {"state": final_state, "error": error, "phase": "x", "fraction": 0}

            def cancel(self):
                pass
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"CKMV_HOME": tmp}), \
                mock.patch.object(detect, "default_install", return_value=None), \
                mock.patch.object(generator, "Generator", FakeGen), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return cli.main(["generate", "--radius", "300", "--data-dir", tmp])

    def test_generate_exit_codes(self):
        self.assertEqual(self._generate("done"), 0)
        self.assertEqual(self._generate("failed", "copy_failed (robocopy rc=11)"), 1)
        self.assertEqual(self._generate("failed", "another generation is already running (pid 5)"), 1)
        self.assertEqual(self._generate("cancelled"), 1)

    def test_generate_interrupted(self):
        class Boom:
            def __init__(self, *a, **kw):
                pass

            def run(self):
                raise KeyboardInterrupt

            def cancel(self):
                pass
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CKMV_HOME": tmp}), \
                mock.patch.object(detect, "default_install", return_value=None), \
                mock.patch.object(generator, "Generator", Boom), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(cli.main(["generate"]), 130)

    def test_mod_exit_codes(self):
        from ckmapviewer import modinstall
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CKMV_HOME": tmp}), \
                mock.patch.object(detect, "default_install", return_value=None), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            with mock.patch.object(modinstall, "install",
                                   side_effect=modinstall.ModError("server_running", "server is running")):
                self.assertEqual(cli.main(["mod", "install"]), 2)
            with mock.patch.object(modinstall, "uninstall", side_effect=PermissionError("denied")):
                self.assertEqual(cli.main(["mod", "uninstall"]), 1)
            with mock.patch.object(modinstall, "install", return_value={"action": "installed"}):
                self.assertEqual(cli.main(["mod", "install"]), 0)
            with mock.patch.object(modinstall, "status", return_value={"installed": False}):
                self.assertEqual(cli.main(["mod", "status"]), 0)
            self.assertEqual(cli.main(["detect", "--data-dir", tmp]), 0)   # informational


if __name__ == "__main__":
    unittest.main()
