# Core Keeper Map Viewer

A local web map for **Core Keeper dedicated servers** — see the explored map update live in your
browser, optionally with the **whole world pre-generated** (bosses, chests, merchants, ores …) and
**live player positions**.

[한국어 README](README.ko.md)

![Map viewer](docs/screenshot-map.png)

- **Live explored map** — reads the map file the dedicated server saves (`servermaps/<world>.mapparts.gzip`)
  and pushes changed parts to the browser.
- **Full map (optional)** — generates the *entire* world offline from a disposable copy of your server,
  shown under the explored map (dimmed), with an overlay of bosses, chests, statues, merchants,
  portals, waypoints, dungeons, altars, ore boulders and ore veins. Filter by explored / unexplored.
- **Live players (optional)** — a tiny read-only server mod writes player positions once per second;
  the viewer shows names, health bars, and can follow a player.
- Shared pins (double-click), PNG export, links to a position (`?at=x,y,zoom`), Korean / English UI.
- Everything runs on your PC. No account, no upload, no internet needed.

> Windows app. The dedicated server can run anywhere — see [Linux / Docker servers](#linux--docker-servers).

## Install

1. Download `CoreKeeperMapViewer-vX.Y.Z-windows.zip` from the [Releases](../../releases) page.
2. Unzip it anywhere (e.g. `Documents\CoreKeeperMapViewer`).
3. Run `CoreKeeperMapViewer.exe`. A console window (the log) opens and your browser opens
   `http://127.0.0.1:8765`. Close the console window to stop the viewer.

On first start the viewer looks for your server automatically. If something is missing, the
**⚙ Settings** panel opens and shows what was found:

| Setting | Auto-detection |
|---|---|
| Dedicated server install (`CoreKeeperServer.exe`) | Steam registry (`HKCU\Software\Valve\Steam` → `SteamPath`, `HKLM\SOFTWARE\WOW6432Node\Valve\Steam` → `InstallPath`) → every library in `steamapps\libraryfolders.vdf` → `appmanifest_1963720.acf` (*Core Keeper Dedicated Server*) |
| Server data folder | `%USERPROFILE%\AppData\LocalLow\Pugstorm\Core Keeper\DedicatedServer` |
| World | `"world"` in `ServerConfig.json` of that data folder |

You can override each value in Settings. Settings are stored in
`%APPDATA%\CoreKeeperMapViewer\config.json`; pins and generated maps in
`%APPDATA%\CoreKeeperMapViewer\worlds\<world>-<id>\`. The viewer never writes into your server's
data folder or install folder, except when you click *Install / Remove* for the player mod.

Windows SmartScreen may warn about an unsigned app — choose *More info → Run anyway*, or
[run from source](#run-from-source).

## Full map (unexplored areas)

**⚙ Settings → Generate full map.** Progress is shown live. It takes roughly 10–60 minutes
(radius-limited test runs take a few minutes).

How it works — and why it is safe:

1. The installed dedicated server is **copied** to a scratch folder
   (`%LOCALAPPDATA%\CoreKeeperMapViewer\fullmap-work`, ~0.5 GB, later runs copy only changes).
2. Your world save (and current server map) is **copied** into a separate scratch data folder.
   `ServerConfig.json` is *not* copied (it contains your server's game ID / password).
3. The bundled **FullMapGen** mod is put into the **copy only** (other mods are removed from the copy),
   together with an `ARMED` marker file. A disposable server starts on the copy with a random port,
   random game ID and password, max 1 player.
4. The mod generates the world area by area through the same mechanism the game uses when players explore, draws
   it into the server map with the game's own map colors, and exports points of interest.
5. The viewer stops **only the process it started** (by PID; it is also attached to a Windows job
   object, so it can never outlive the viewer) and copies the result into its app-data folder.

Safety interlocks in the mod — **all** must hold or it stays completely inert:
`-datapath` was given explicitly, that path is *not* the default (real) server data path, and the
`ARMED` marker with the right token exists. It also aborts immediately if any player or network
connection appears. Your real server may keep running meanwhile; its files are only read.

Notes: the pre-generated terrain uses your world's seed and the game's own generator, but the exact
placement of some structures may differ slightly from what your real server will generate later.
Generate again after big game updates.

## Live player positions (LivePlayers mod)

**⚙ Settings → Player position mod → Install / update**, then **restart the dedicated server**.

- The button refuses while `CoreKeeperServer.exe` from that install folder is running
  (mods are compiled when the server starts). Stop the server, install, start it again.
  The server log (`CoreKeeperServerLog.txt`) then shows `[LivePlayers] v1.0.0 loaded`.
- **Server-only**: `requiredOn: 0` in its manifest — players join with the normal, unmodded game.
- **Read-only**: it only reads player entities (name, position, health, dead/alive). No entity writes,
  no Harmony patches (`disableHarmonyPatching: true`), no network messages. The file write runs on a
  background thread and errors are caught and logged once.
- Output: `<data folder>\mods\LivePlayers\players.json`, about once per second
  (every 2 s while the server is idle / paused with nobody online).
- Remove it the same way (*Remove*, then restart the server).

## Share on your network (LAN)

Settings → *Let other devices on my network view the map* makes the viewer listen on `0.0.0.0`
(all network interfaces) instead of `127.0.0.1`. The Settings panel then lists the addresses to open
on a phone/tablet/other PC.

- **There is no login.** Anyone who can reach the port can see the map, including player names and
  positions, and can add/remove shared pins.
- Settings, map generation and mod install/remove are accepted **only from the PC running the viewer**
  (loopback address + Host check + a custom header), never from other devices.
- Restrict access with Windows Firewall: when Windows asks, allow *Private networks* only; or create
  an inbound rule for the viewer's TCP port limited to your local subnet / specific devices.
  Do not forward the port on your router.

## Linux / Docker servers

The viewer app is Windows-only, but it only needs **read access** to two things from the server's
data folder:

- `servermaps/<world>.mapparts.gzip` (the explored map), and
- `mods/LivePlayers/players.json` (only if you install the LivePlayers mod on that server).

Mount or sync the server's data folder to the Windows PC (SMB share / network drive, a synced
folder, or a periodic copy) and set it as *Server data folder* in Settings. To use LivePlayers on a
Linux server, copy `LivePlayers` from this repository's `ckmapviewer/mods/` into the server's
`CoreKeeperServer_Data/StreamingAssets/Mods/` yourself (server stopped). Full-map generation needs a
Windows install of the free *Core Keeper Dedicated Server* (Steam) plus a copy of the world save in
the data folder you selected.

## Limitations

- "Live" map: the game server writes its map file only every few minutes, so explored areas appear
  with that delay. Player positions (with the mod) update every second.
- The full map is a snapshot generated from a copy; it may differ slightly from what the real server
  generates later, and it does not change with player edits (digging, building). The explored layer
  on top always shows the real, current map.
- Windows only for the app. Tested with the Steam dedicated server; game updates can break the mods
  (they then log an error and stay inactive — the viewer itself keeps working).

## Troubleshooting

| Symptom | What to do |
|---|---|
| "Server map file not found" | Check *Server data folder* and *World* in Settings. The file appears after the server has run and saved once. |
| Install folder "missing" | Enter the folder that contains `CoreKeeperServer.exe`. |
| "Real server is running" when installing the mod | Stop the dedicated server first, then install, then start it again. |
| Player positions: "mod not installed" | Install the mod and restart the server; check the server log for `[LivePlayers] ... loaded`. |
| Player positions: "stopped" | The server is off, or the mod stopped writing (see the server log). |
| Full map generation failed | The log in Settings shows the step. The disposable server's log is kept as `fullmap-last-server.log` in the world's app-data folder (*Open*). |
| Port already in use | Another program uses 8765 — change *Port* in Settings, or start with `--port 8766`. |
| Browser didn't open | Open `http://127.0.0.1:8765` manually. Starting the exe again just opens the browser. |

Command line (also works from source): `CoreKeeperMapViewer.exe [--port N] [--lan] [--data-dir PATH]
[--server-install PATH] [--world N] [--no-browser]`, plus `detect`, `generate [--radius N]`,
`mod install|uninstall|status`.

## Run from source

Requires Python 3.9+ (standard library only):

```
py app.py                 # or: py -m ckmapviewer, or start-from-source.bat
py app.py detect          # print what was detected
py -m unittest discover -s tests
```

Build the Windows app: `build.bat` (creates `.venv`, installs PyInstaller, runs the tests, builds
`dist\CoreKeeperMapViewer\` and a zip). Tagging `v*` builds and publishes a release via GitHub Actions.

Layout: `ckmapviewer/` (Python package: `server.py` HTTP + watchers, `detect.py`, `config.py`,
`generator.py`, `modinstall.py`, `procs.py`), `ckmapviewer/web/index.html` (the viewer),
`ckmapviewer/mods/` (C# source of the two server mods, compiled by the game's mod loader).

## Credits

- Map file format knowledge from [Ceddini/CoreKeeperMapTool](https://github.com/Ceddini/CoreKeeperMapTool)
  and [SomniferousWallaby/CoreKeeperMapServer](https://github.com/SomniferousWallaby/CoreKeeperMapServer).
- The mods use the game's official modding API (PugMod) and public game types; this repository
  contains no game code or assets.

Core Keeper is developed by Pugstorm and published by Fireshine Games. This project is a fan-made
tool and is **not affiliated with or endorsed by Pugstorm or Fireshine Games**.

License: [MIT](LICENSE).
