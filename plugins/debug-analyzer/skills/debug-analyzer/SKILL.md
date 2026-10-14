---
name: debug-analyzer
description: Flash firmware, then capture and decode Realtek Bluetooth SoC logs headlessly (mpcli + DebugAnalyzer.exe) through scripts/da.py. Use whenever the user wants to flash / 烧录 SDK or locally built images, grab / 抓 SoC log from a serial port, start or stop DebugAnalyzer, decode / 解析 a captured .bin, or read the latest device log for an RTL87x3G (RTL8773GTP, RTL8783GBF) or other supported IC. Also use inside a debug loop of "flash -> capture log -> reproduce -> stop -> analyze".
---

# DebugAnalyzer (headless)

`scripts/da.py` drives DebugAnalyzer.exe in command line mode. Windows only:
run it with Windows Python from PowerShell or Git Bash, never from WSL / a VM.

```
python "${CLAUDE_PLUGIN_ROOT}/skills/debug-analyzer/scripts/da.py" <command> ...
```

## Configuration

- `ics/<ic>.json` (shipped): where an IC's SDK keeps its trace files, as path
  templates using `{sdk}`, `{flash}`, `{chip}`, `{app}`.
- User config (local, not in the plugin):
  `%USERPROFILE%\.config\debug-analyzer\config.json`, or the path in `DA_CONFIG`.
  One entry per profile: `ic`, `exe` (the DebugAnalyzer build for that IC),
  `sdk` root, `vars` (`chip`, `flash`, `mic`), `port` (log), `flash_port`,
  `reset_port`, `out_dir`, optional extra `traces` / `extra_args`, optional
  `mpcli` path override. Start from `config.example.json`.

If the user config is missing, ask the user for the DebugAnalyzer path, SDK
path, chip, and the log / flash / reset COM ports, then write it.

## Commands

| Command | Purpose |
|---|---|
| `profiles` | Show profiles, exe check and every resolved trace (`ok` / `MISSING`) |
| `start [-p NAME] [--port COMx] [--tag ISSUE-123] [--trace aa=<app.trace>]` | Start background capture, returns immediately |
| `status` | Captures + latest .log/.bin/.cfa and sizes |
| `stop [-p NAME \| --port COMx \| --all]` | Graceful stop (CTRL_BREAK, kill after 20 s), prints output files |
| `decode [-p NAME] <file.bin>... [--out DIR] [--trace ...]` | Offline decode → `File_Decode_<name>.log/.cfa` |
| `flash [-p NAME] [--dry-run] [--var mic=2mic] [--app <app_MP.bin>] [--only app,...] [--image NAME=PATH] [--capture]` | Flash SDK images with mpcli |

## Flashing

`flash` resolves every image from the SDK prebuilt tree using the IC's
layout (`ics/<ic>.json` → `flash.layouts.<flash>`), generates the mpcli json,
and runs mpcli with RTS auto-reset (`reset_port`) like `west flash --auto`.
mpcli reboots the board afterwards (`-r`).

- **Always `--dry-run` first** and show the image list. Flashing overwrites the
  board's firmware, so confirm with the user unless they asked for it.
- Variants: `--var mic=2mic`, `--var chip=rtl8773gtp`. A local build:
  `--app <build>/.../app_MP-*.bin --only app`. The `app.trace` next to that
  bin is then used for the capture automatically.
- If a capture holds the flash or reset port, `flash` stops it and restarts it
  afterwards with the same traces, so the boot log of the new firmware is
  captured. `--capture` starts one even if nothing was running.
- `--no-auto`: the board has no RTS reset circuit, so the user must put it in
  download mode by hand (on RTL87x3G: P2_0 to GND, then reset).
- Typical time: full flash ~60 s, app only ~35 s.

## Workflow

1. `profiles`: every trace needed should be `ok`.
2. The app trace must match the firmware on the board. SDK prebuilt images →
   profile default. Locally built app → `--trace aa=<app>/bin/app.trace`.
   Ask if unsure which image is flashed; a wrong trace gives garbage strings.
3. `start`, then reproduce (the user, or you via other tools).
4. The `.log` is written in real time. Use `status` for the path and read only
   the tail with Read/Grep — logs grow to many MB.
5. `stop`, then analyze the `.log`. For BT issues mention the `.cfa`
   (btsnoop, opens in Ellisys / Frontline).

## Gotchas

- **A COM port has one owner.** The DebugAnalyzer GUI, a serial terminal, VM
  USB passthrough, or a flashing tool holding the port → `start` fails. When
  log and auto-reset share one port, `stop` before flashing and `start` again
  after. The board reboots after flashing, so start right away to catch the
  boot log.
- A capture that only produces a few-byte `.bin` and no `.log` means the SoC is
  not emitting logs (not running, stuck in download mode, wrong port/baud).
- Every run uses a generated ini (copy of the exe's `default.ini`, all trace
  keys replaced), so stale traces from the last GUI session are never used.
  `default.ini` itself is not modified.
- A trace whose file doesn't exist is skipped with a warning.
- `-bp` (offline decode) needs a directory; `decode` copies the bins into a temp dir.
- DebugAnalyzer's own diagnostic log: `DebugAnalyzer.log` next to the exe.
- Trace flag names: `CommandLineUserGuide.md` next to the exe.

## Adding an IC

Add `ics/<ic>.json` (e.g. `rtl87x3k.json`) with the IC's trace layout and a
`flash` section (mpcli `ic_type`, image globs and addresses taken from the
SDK's flash_map.ini), then a profile with `"ic": "<ic>"` pointing at the
DebugAnalyzer build shipped with that SDK. Newer builds (v4.2.x) add dual-core flags (`-cc`, `-aat`, `-ant`,
`-ra`, `-rn`, ...). Those have no ini key, so `da.py` passes them on the
command line. No script change needed.
