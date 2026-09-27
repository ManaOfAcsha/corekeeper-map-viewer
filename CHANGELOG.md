# Changelog

## 1.0.1 - 2026-09-27

### Fixed
- Full-map generation is now exclusive across processes. A second viewer (another port) or the
  `generate` command started while a generation was running used to re-mirror the shared scratch
  folder, removing the running job's mod and marker so that job hung forever. A lock file
  (`fullmap-work/.lock`, holder PID recorded, stale locks taken over) is now taken before any
  file is touched; the second contender stops immediately with "another generation is already
  running (pid N)" (HTTP 409 in the viewer, exit code 1 on the command line).
- A running generation now fails with a clear error instead of waiting until the overall timeout
  when the mod's progress file stops changing for 10 minutes or its marker folder disappears;
  only its own disposable server is stopped.
- Command line: documented exit codes (0 ok, 1 failed/cancelled/busy, 2 mod change refused,
  130 interrupted); mod install/uninstall file errors return 1 instead of a traceback; options
  such as `--data-dir` are also accepted after the sub-command (`generate --data-dir X`).

## 1.0.0 - 2026-09-26

- First public release.
