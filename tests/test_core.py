import gzip
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from ckmapviewer import config, detect, modinstall, paths, server

LIBRARY_VDF = r'''
"libraryfolders"
{
	"0"
	{
		"path"		"C:\\Steam"
		"apps"		{ "1963720"		"484635061" }
	}
	"1"
	{
		"path"		"D:\\Games\\SteamLibrary"
	}
}
'''

OLD_LIBRARY_VDF = r'''
"LibraryFolders"
{
	"TimeNextStatsReport"		"123"
	"1"		"E:\\SteamLibrary"
}
'''

MANIFEST = '''
"AppState"
{
	"appid"		"1963720"
	"name"		"Core Keeper Dedicated Server"
	"installdir"		"Core Keeper Dedicated Server"
}
'''


def write_map(path: Path, parts):
    """parts: {(x, y): png bytes}"""
    data = {"mapParts": {"keys": [{"x": x, "y": y} for (x, y) in parts],
                         "values": [{"png": list(v)} for v in parts.values()]}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as f:
        f.write(json.dumps(data).encode())


class VdfTests(unittest.TestCase):
    def test_new_format(self):
        d = detect.parse_vdf(LIBRARY_VDF)
        self.assertEqual(d["libraryfolders"]["0"]["path"], "C:\\Steam")
        self.assertEqual(d["libraryfolders"]["1"]["path"], "D:\\Games\\SteamLibrary")
        self.assertEqual(d["libraryfolders"]["0"]["apps"]["1963720"], "484635061")

    def test_old_format(self):
        d = detect.parse_vdf(OLD_LIBRARY_VDF)
        self.assertEqual(d["LibraryFolders"]["1"], "E:\\SteamLibrary")

    def test_manifest(self):
        st = detect.parse_vdf(MANIFEST)["AppState"]
        self.assertEqual(st["appid"], detect.DEDICATED_SERVER_APPID)


class DetectTests(unittest.TestCase):
    def test_finds_install_in_second_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, lib = Path(tmp) / "Steam", Path(tmp) / "Lib2"
            (root / "steamapps").mkdir(parents=True)
            (root / "steamapps" / "libraryfolders.vdf").write_text(
                '"libraryfolders" { "0" { "path" "%s" } "1" { "path" "%s" } }'
                % (str(root).replace("\\", "\\\\"), str(lib).replace("\\", "\\\\")), encoding="utf-8")
            inst = lib / "steamapps" / "common" / "Core Keeper Dedicated Server"
            inst.mkdir(parents=True)
            (inst / detect.SERVER_EXE).write_bytes(b"")
            (lib / "steamapps" / "appmanifest_1963720.acf").write_text(MANIFEST, encoding="utf-8")
            with mock.patch.object(detect, "steam_roots", return_value=[root]):
                found = detect.find_server_installs()
            self.assertEqual(len(found), 1)
            self.assertEqual(Path(found[0]["path"]), inst)
            self.assertEqual(found[0]["source"], "steam")

    def test_data_status_and_worlds(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertFalse(detect.data_status(d / "nope", 0)["ok"])
            write_map(d / "servermaps" / "2.mapparts.gzip", {(0, 0): b"x"})
            (d / "worlds").mkdir()
            (d / "worlds" / "2.world.gzip").write_bytes(b"x")
            st = detect.data_status(d, 2)
            self.assertTrue(st["ok"])
            self.assertEqual(st["worlds"], [2])
            self.assertFalse(detect.data_status(d, 0)["ok"])


class ConfigTests(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(config.validate_patch({"world": "", "dataDir": ""}), {"world": None, "dataDir": None})
        self.assertEqual(config.validate_patch({"port": "8770", "lan": 1})["port"], 8770)
        for bad in ({"port": 80}, {"world": -1}, {"dataDir": 5}):
            with self.assertRaises(ValueError):
                config.validate_patch(bad)

    def test_effective_world_from_serverconfig(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "ServerConfig.json").write_text('{"world": 3, "worldName": "Test"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"CKMV_HOME": str(d / "home")}), \
                    mock.patch.object(detect, "default_install", return_value=None):
                eff = config.Effective(dict(config.DEFAULTS, dataDir=str(d)))
                self.assertEqual(eff.world, 3)
                self.assertEqual(eff.world_name, "Test")
                self.assertTrue(paths.is_inside(eff.full_map, d / "home"))
                self.assertFalse(paths.is_inside(eff.full_map, d / "servermaps"))

    def test_world_dir_differs_per_data_dir(self):
        with mock.patch.dict(os.environ, {"CKMV_HOME": "X:/home"}):
            self.assertNotEqual(paths.world_dir(Path("A:/one"), 0), paths.world_dir(Path("A:/two"), 0))
            self.assertEqual(paths.world_dir(Path("A:/one"), 0), paths.world_dir(Path("a:/ONE/"), 0))


class ModTests(unittest.TestCase):
    def test_bundled_mods_present_and_valid(self):
        for name in ("LivePlayers", "FullMapGen"):
            man = json.loads((paths.mod_source(name) / "ModManifest.json").read_text(encoding="utf-8"))
            self.assertEqual(man["name"], name)
            self.assertEqual(man["requiredOn"], 0)          # vanilla clients can join
            for f in man["files"]:
                self.assertTrue((paths.mod_source(name) / f["path"]).is_file(), f)
        self.assertTrue(json.loads((paths.mod_source("LivePlayers") / "ModManifest.json")
                                   .read_text(encoding="utf-8"))["disableHarmonyPatching"])

    def test_install_refuses_while_server_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            (inst / detect.SERVER_EXE).write_bytes(b"")
            with mock.patch("ckmapviewer.procs.server_running_from",
                            return_value={"running": True, "pids": [123], "unknown": []}):
                with self.assertRaises(modinstall.ModError) as cm:
                    modinstall.install(inst)
                self.assertEqual(cm.exception.code, "server_running")
            self.assertFalse((modinstall.mods_dir(inst) / "LivePlayers").exists())
            with mock.patch("ckmapviewer.procs.server_running_from",
                            return_value={"running": None, "pids": [], "unknown": []}):
                with self.assertRaises(modinstall.ModError):
                    modinstall.install(inst)
            with mock.patch("ckmapviewer.procs.server_running_from",
                            return_value={"running": False, "pids": [], "unknown": []}):
                self.assertEqual(modinstall.install(inst)["action"], "installed")
                self.assertTrue((modinstall.mods_dir(inst) / "LivePlayers" / "ModManifest.json").is_file())
                self.assertEqual(modinstall.uninstall(inst)["action"], "removed")


class ServerTests(unittest.TestCase):
    def test_http_api_and_access_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            write_map(d / "servermaps" / "0.mapparts.gzip", {(0, 0): b"\x89PNGfake", (-1, 2): b"\x89PNGtwo"})
            env = {"CKMV_HOME": str(Path(tmp) / "home")}
            with mock.patch.dict(os.environ, env), mock.patch.object(detect, "default_install", return_value=None):
                viewer = server.Viewer(dict(config.DEFAULTS, dataDir=str(d)), {})
                httpd = server.Server(("127.0.0.1", 0), server.make_handler(viewer))
                viewer.httpd = httpd
                port = httpd.server_address[1]
                th = threading.Thread(target=httpd.serve_forever, daemon=True)
                th.start()
                try:
                    base = f"http://127.0.0.1:{port}"
                    m = json.load(urllib.request.urlopen(base + "/api/map"))
                    self.assertEqual(len(m["layers"]["live"]["tiles"]), 2)
                    self.assertNotIn("seed", json.dumps(m))
                    v = m["layers"]["live"]["tiles"][0]
                    png = urllib.request.urlopen(f"{base}/api/tile/live/{v['x']}/{v['y']}.png").read()
                    self.assertTrue(png.startswith(b"\x89PNG"))
                    st = json.load(urllib.request.urlopen(base + "/api/status"))
                    self.assertTrue(st["admin"])
                    self.assertFalse(st["needsSetup"])
                    # mutation without the custom header is refused
                    req = urllib.request.Request(base + "/api/detect", data=b"{}", method="POST")
                    with self.assertRaises(urllib.error.HTTPError) as cm:
                        urllib.request.urlopen(req)
                    self.assertEqual(cm.exception.code, 403)
                    # a foreign Host header (DNS rebinding) is not admin
                    req = urllib.request.Request(base + "/api/status", headers={"Host": "evil.example"})
                    self.assertFalse(json.load(urllib.request.urlopen(req))["admin"])
                    # shared pins work with the header, stored under app data (not the server folder)
                    req = urllib.request.Request(base + "/api/markers", method="PUT",
                                                 data=b'[{"x":1,"y":2,"name":"a"}]', headers={"X-CKMV": "1"})
                    self.assertTrue(json.load(urllib.request.urlopen(req))["ok"])
                    self.assertTrue(viewer.bundle.eff.markers.is_file())
                    self.assertTrue(paths.is_inside(viewer.bundle.eff.markers, Path(tmp) / "home"))
                finally:
                    httpd.shutdown()
                    httpd.server_close()


if __name__ == "__main__":
    unittest.main()
