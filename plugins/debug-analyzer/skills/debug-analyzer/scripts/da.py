#!/usr/bin/env python3
"""Headless wrapper around Realtek DebugAnalyzer.exe (Windows only).

Commands:
  profiles                    list configured profiles and resolved traces
  start  [-p NAME] [...]      start a background serial capture
  stop   [-p NAME|--port P]   stop a capture gracefully (CTRL_BREAK), kill on timeout
  status                      show captures and their latest log files
  decode [-p NAME] BIN...     offline-decode captured .bin files
  flash  [-p NAME] [...]      flash SDK images with mpcli (optionally re-capture)

Two layers of configuration:
  ics/<ic>.json         shipped with the plugin; describes where an IC's SDK
                        keeps its trace files and flash images (path templates,
                        flash addresses).
  user config.json      local machine settings per profile: DebugAnalyzer exe,
                        SDK root, chip variant, serial ports, output dir.
                        Default: %USERPROFILE%\\.config\\debug-analyzer\\config.json
                        (override with DA_CONFIG). See config.example.json.

DebugAnalyzer falls back to its own default.ini for anything not given on the
command line, and that file usually carries stale trace paths from the last GUI
session. To keep decoding deterministic, every run gets a generated ini (a copy
of default.ini with all trace entries replaced) passed via -config.

Why a supervisor process: DebugAnalyzer only stops cleanly on CTRL_BREAK_EVENT,
which can only be delivered to a process sharing the sender's console. So
`start` spawns a hidden-console supervisor that owns DebugAnalyzer and waits
for a stop request file; `stop` just drops that file and waits.
"""

import argparse
import ctypes
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
IC_DIR = SKILL_DIR / "ics"
CONFIG_PATH = Path(os.environ.get(
    "DA_CONFIG", Path.home() / ".config" / "debug-analyzer" / "config.json"))
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "debug-analyzer"

STOP_TIMEOUT_S = 20
DECODE_TIMEOUT_S = 600
FLASH_TIMEOUT_S = 900

# Trace flag -> key in DebugAnalyzer's ini. Every key listed here is cleared in
# the generated ini, so traces not set by the profile are really unset.
TRACE_INI_KEYS = {
    "mc": "ModuleTraceFile", "ar": "ROMTraceFile", "ri": "ROMIndexFile",
    "aa": "AppTraceFile", "ap": "PatchTraceFile", "pi": "PatchIndexFile",
    "spi": "StackPatchIndexFile", "ae": "PatchExtensionTraceFile",
    "apf": "PlatformTraceFile", "aus": "UpperstackTraceFile",
    "da": "DAppTraceFile", "du": "DAudioTraceFile", "dv": "DVoiceTraceFile",
    "dp": "DPatchTraceFile", "ds2": "DSP2SysTraceFile", "adsp": "AdspSysTraceFile",
}


# ---------------------------------------------------------------- helpers

def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def pid_alive(pid):
    if not pid:
        return False
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
    k32.CloseHandle(handle)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def load_config():
    if not CONFIG_PATH.is_file():
        die(f"user config not found: {CONFIG_PATH}\n"
            f"copy {SKILL_DIR / 'config.example.json'} there and edit it")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_profile(name):
    cfg = load_config()
    profiles = cfg.get("profiles", {})
    name = name or cfg.get("default")
    if not name:
        if len(profiles) != 1:
            die("several profiles configured; pass -p NAME or set 'default' in config")
        name = next(iter(profiles))
    if name not in profiles:
        die(f"profile '{name}' not in {CONFIG_PATH} (have: {', '.join(profiles) or 'none'})")
    prof = dict(profiles[name])
    prof["name"] = name

    ic_path = IC_DIR / f"{prof.get('ic', name)}.json"
    if not ic_path.is_file():
        avail = ", ".join(p.stem for p in sorted(IC_DIR.glob("*.json")))
        die(f"profile '{name}': unknown ic '{prof.get('ic')}' (supported: {avail})")
    ic = json.loads(ic_path.read_text(encoding="utf-8"))

    variables = dict(ic.get("vars", {}))
    variables.update(prof.get("vars", {}))
    variables["sdk"] = prof.get("sdk", "")

    def expand(tmpl, what):
        try:
            return tmpl.format(**variables)
        except KeyError as e:
            die(f"profile '{name}': {what} needs variable {e} (set it in 'vars')")

    traces = {}
    for flag, tmpl in {**ic.get("traces", {}), **prof.get("traces", {})}.items():
        if tmpl:
            traces[flag.lstrip("-")] = str(Path(expand(tmpl, f"trace -{flag}")))
    prof["traces"] = traces
    prof.setdefault("baud", ic.get("baud", 2000000))
    prof["ic_description"] = ic.get("description", "")
    prof["vars"] = variables
    prof["ic_flash"] = ic.get("flash")
    prof["expand"] = expand
    return prof


def resolve_traces(prof, overrides):
    """Profile traces + CLI overrides ('aa=path'); drops missing files."""
    traces = dict(prof["traces"])
    for item in overrides or []:
        if "=" not in item:
            die(f"--trace expects FLAG=PATH, got '{item}'")
        flag, path = item.split("=", 1)
        traces[flag.strip().lstrip("-")] = path.strip()
    present = {}
    for flag, path in traces.items():
        if Path(path).is_file():
            present[flag] = path
        else:
            print(f"warning: skipping -{flag}, file not found: {path}", file=sys.stderr)
    return present


def check_exe(prof):
    exe = Path(prof.get("exe", ""))
    if not exe.is_file():
        die(f"profile '{prof['name']}': DebugAnalyzer exe not found: {exe}")
    return exe


def write_session_ini(exe, traces, path):
    """Copy default.ini with every trace key replaced by the given traces."""
    src = exe.parent / "default.ini"
    # default.ini is in the ANSI code page and may hold non-ASCII paths; latin-1
    # round-trips the untouched bytes, new values are encoded the same way.
    text = src.read_bytes().decode("latin-1") if src.is_file() else "[SettingsParams]\r\n"
    wanted = {TRACE_INI_KEYS[f]: v for f, v in traces.items() if f in TRACE_INI_KEYS}
    for key in TRACE_INI_KEYS.values():
        value = wanted.get(key, "").encode("mbcs").decode("latin-1")
        pattern = re.compile(rf"^{key}=[^\r\n]*", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(lambda _m, v=value: f"{key}={v}", text)
        elif value:
            text = text.replace("[SettingsParams]", f"[SettingsParams]\r\n{key}={value}", 1)
    path.write_bytes(text.encode("latin-1"))


def da_trace_args(traces):
    """Flags DebugAnalyzer has no ini key for (newer dual-core builds) go on the CLI."""
    args = []
    for flag, path in traces.items():
        if flag not in TRACE_INI_KEYS:
            args += [f"-{flag}", path]
    return args


def state_file(port):
    return STATE_DIR / f"{port.upper()}.json"


def read_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_state(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def latest_files(out_dir, port, since=None):
    """Newest .log/.bin/.cfa for this port in out_dir (created after `since`)."""
    result = {}
    out = Path(out_dir)
    if not out.is_dir():
        return result
    for ext in (".log", ".bin", ".cfa"):
        cands = [p for p in out.glob(f"{port}*{ext}")
                 if since is None or p.stat().st_mtime >= since - 2]
        if cands:
            result[ext] = str(max(cands, key=lambda p: p.stat().st_mtime))
    return result


# ---------------------------------------------------------------- commands

def cmd_profiles(_):
    cfg = load_config()
    print(f"config: {CONFIG_PATH}")
    for name in cfg.get("profiles", {}):
        prof = load_profile(name)
        exe_ok = "ok" if Path(prof.get("exe", "")).is_file() else "MISSING"
        mark = " (default)" if cfg.get("default") == name else ""
        print(f"\n{name}{mark}: {prof['ic_description']}")
        print(f"  exe   [{exe_ok}] {prof.get('exe')}")
        print(f"  port  {prof.get('port')}  baud {prof['baud']}")
        if prof.get("ic_flash"):
            print(f"  flash port {prof.get('flash_port')}  reset port {prof.get('reset_port')}  "
                  f"vars {prof['vars'].get('chip')}/{prof['vars'].get('flash')}/{prof['vars'].get('mic')}")
        print(f"  out   {prof.get('out_dir')}")
        for flag, path in prof["traces"].items():
            ok = "ok" if Path(path).is_file() else "MISSING"
            print(f"  -{flag:4s} [{ok}] {path}")


def cmd_start(a):
    prof = load_profile(a.profile)
    exe = check_exe(prof)
    port = (a.port or prof.get("port") or "").upper()
    if not port:
        die("no serial port: pass --port or set 'port' in the profile")
    sf = state_file(port)
    st = read_state(sf)
    if st and pid_alive(st.get("sup_pid")):
        die(f"{port} already captured by profile '{st['profile']}' (da pid {st.get('da_pid')}); stop it first")

    out_dir = Path(a.out or prof.get("out_dir") or Path.cwd())
    if a.tag:
        out_dir = out_dir / a.tag
    out_dir.mkdir(parents=True, exist_ok=True)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    traces = resolve_traces(prof, a.trace)
    ini = sf.with_suffix(".ini")
    write_session_ini(exe, traces, ini)
    da_args = ["-j0", "-fs", "-sp", port,
               "-br", str(a.baud or prof["baud"]),
               "-reopen", str(prof.get("reopen", 3)),
               "-config", str(ini)]
    da_args += da_trace_args(traces)
    da_args += list(prof.get("extra_args", []))
    da_args += ["-fp", str(out_dir)]

    sf.with_suffix(".stop").unlink(missing_ok=True)
    write_state(sf, {
        "profile": prof["name"], "port": port, "exe": str(exe),
        "args": da_args, "traces": traces, "out_dir": str(out_dir),
        "status": "starting", "started": time.time(),
    })

    # Hidden new console: DebugAnalyzer inherits it, so CTRL_BREAK can reach it.
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    sup = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_supervise", str(sf)],
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + 10
    while time.time() < deadline:
        st = read_state(sf) or {}
        if st.get("status") in ("running", "stopped"):
            break
        time.sleep(0.3)
    st = read_state(sf) or {}
    if st.get("status") != "running":
        die(f"DebugAnalyzer did not stay up (status={st.get('status')}, rc={st.get('rc')}); "
            f"is {port} in use? see {exe.parent / 'DebugAnalyzer.log'}")
    print(f"capturing {port} with profile '{prof['name']}' (supervisor {sup.pid}, da {st['da_pid']})")
    print(f"output dir: {out_dir}")
    for flag, path in traces.items():
        print(f"  -{flag:4s} {path}")


def cmd_supervise(a):
    """Internal: owns DebugAnalyzer inside a hidden console."""
    sf = Path(a.state)
    st = read_state(sf)
    stop_flag = sf.with_suffix(".stop")
    exe = st["exe"]
    proc = subprocess.Popen([exe] + st["args"], cwd=str(Path(exe).parent),
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    st.update(sup_pid=os.getpid(), da_pid=proc.pid, status="running")
    time.sleep(1.5)
    if proc.poll() is not None:
        st.update(status="stopped", stop_mode="exited", rc=proc.returncode, stopped=time.time())
        write_state(sf, st)
        return
    write_state(sf, st)

    while proc.poll() is None and not stop_flag.exists():
        time.sleep(0.5)

    if proc.poll() is None:
        mode = "graceful"
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            proc.wait(STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            mode = "killed"
            proc.kill()
            proc.wait()
    else:
        mode = "exited"  # died on its own (port gone, crash, ...)
    stop_flag.unlink(missing_ok=True)
    st.update(status="stopped", stop_mode=mode, rc=proc.returncode, stopped=time.time())
    write_state(sf, st)


def find_states(a):
    if not STATE_DIR.is_dir():
        return []
    states = [(p, read_state(p)) for p in sorted(STATE_DIR.glob("*.json"))]
    states = [(p, s) for p, s in states if s and "port" in s]
    if getattr(a, "port", None):
        states = [(p, s) for p, s in states if s["port"] == a.port.upper()]
    if getattr(a, "profile", None):
        states = [(p, s) for p, s in states if s["profile"] == a.profile]
    return states


def cmd_stop(a):
    running = [(p, s) for p, s in find_states(a) if pid_alive(s.get("sup_pid"))]
    if not running:
        print("no running capture matched")
        return
    if len(running) > 1 and not a.all:
        die("several captures running; pass --port/--profile or --all: "
            + ", ".join(s["port"] for _, s in running))
    for sf, _ in running:
        sf.with_suffix(".stop").touch()
    for sf, st in running:
        deadline = time.time() + STOP_TIMEOUT_S + 10
        while time.time() < deadline and pid_alive(st.get("sup_pid")):
            time.sleep(0.3)
        st = read_state(sf) or st
        mode, rc = st.get("stop_mode", "unknown"), st.get("rc")
        # 0xC000013A (STATUS_CONTROL_C_EXIT) is the normal exit code after CTRL_BREAK.
        rc_note = "" if mode == "graceful" or rc is None else f", rc={rc & 0xFFFFFFFF:#x}"
        print(f"{st['port']}: stopped ({mode}{rc_note})")
        for ext, path in latest_files(st["out_dir"], st["port"], st.get("started")).items():
            print(f"  {ext[1:]:4s} {path}")
        if st.get("stop_mode") == "killed":
            print("  warning: CTRL_BREAK was ignored and the process was killed; "
                  "the tail of the log may be missing")


def cmd_status(a):
    states = find_states(a)
    if not states:
        print("no captures recorded")
        return
    for sf, st in states:
        alive = pid_alive(st.get("sup_pid")) and pid_alive(st.get("da_pid"))
        started = dt.datetime.fromtimestamp(st["started"]).strftime("%Y-%m-%d %H:%M:%S")
        state = "RUNNING" if alive else f"{st.get('status')} ({st.get('stop_mode', '-')})"
        print(f"{st['port']}  {state}  profile={st['profile']}  started={started}")
        for ext, path in latest_files(st["out_dir"], st["port"], st.get("started")).items():
            print(f"  {ext[1:]:4s} {path}  ({Path(path).stat().st_size} bytes)")
        if not alive and st.get("status") == "running":
            # Supervisor vanished without updating state; record that.
            st["status"] = "lost"
            write_state(sf, st)


def cmd_decode(a):
    prof = load_profile(a.profile)
    exe = check_exe(prof)
    bins = [Path(b).resolve() for b in a.bins]
    for b in bins:
        if not b.is_file():
            die(f"not a file: {b}")
    out_dir = Path(a.out) if a.out else bins[0].parent
    out_dir.mkdir(parents=True, exist_ok=True)
    traces = resolve_traces(prof, a.trace)

    # -bp must be a directory: DebugAnalyzer enumerates every file inside it
    # (a file path crashes it with an unhandled IOException).
    with tempfile.TemporaryDirectory(prefix="da_bp_") as bp:
        for b in bins:
            shutil.copy2(b, bp)
        ini = Path(bp).parent / f"{Path(bp).name}.ini"
        write_session_ini(exe, traces, ini)
        da_args = ["-j0", "-ff", "-bp", bp, "-config", str(ini)] + da_trace_args(traces)
        da_args += list(prof.get("extra_args", [])) + ["-fp", str(out_dir)]
        started = time.time()
        try:
            rc = subprocess.run([str(exe)] + da_args, cwd=str(exe.parent),
                                timeout=DECODE_TIMEOUT_S).returncode
        except subprocess.TimeoutExpired:
            die(f"decode did not finish within {DECODE_TIMEOUT_S}s")
        finally:
            ini.unlink(missing_ok=True)
    if rc != 0:
        die(f"DebugAnalyzer exited with rc={rc:#x}; see {exe.parent / 'DebugAnalyzer.log'}")
    for b in bins:
        for ext in (".log", ".cfa"):
            out = out_dir / f"File_Decode_{b.stem}{ext}"
            if out.is_file() and out.stat().st_mtime >= started - 2:
                print(f"{ext[1:]:4s} {out}")


def parse_kv(items, what):
    out = {}
    for item in items or []:
        if "=" not in item:
            die(f"{what} expects KEY=VALUE, got '{item}'")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def resolve_images(prof, layout, overrides, only):
    """Return [(name, address, path)] for the flash layout, after overrides."""
    expand = prof["expand"]
    base = Path(expand(layout["base"], "flash base"))
    names = {img["name"] for img in layout["images"]}
    for key in list(overrides) + list(only or []):
        if key not in names:
            die(f"unknown image '{key}' (known: {', '.join(sorted(names))})")
    images, problems = [], []
    for img in layout["images"]:
        name = img["name"]
        if only and name not in only:
            continue
        if name in overrides:
            path = Path(overrides[name])
            if not path.is_file():
                problems.append(f"{name}: not a file: {path}")
                continue
        else:
            pattern = expand(img["glob"], f"image {name}")
            matches = sorted(base.glob(pattern))
            if not matches:
                if not img.get("optional"):
                    problems.append(f"{name}: no match for {base / pattern}")
                continue
            if len(matches) > 1:
                problems.append(f"{name}: {len(matches)} matches for {base / pattern}; "
                                f"pass --image {name}=<path>")
                continue
            path = matches[0]
        images.append((name, img["address"], path.resolve()))
    if problems:
        die("cannot resolve flash images:\n  " + "\n  ".join(problems))
    return images


def stop_captures_on(ports):
    """Stop running captures holding any of `ports`; return the profiles stopped."""
    stopped = []
    for sf, st in find_states(argparse.Namespace(port=None, profile=None)):
        if st["port"] in ports and pid_alive(st.get("sup_pid")):
            print(f"{st['port']} is being captured; stopping it before flashing")
            cmd_stop(argparse.Namespace(port=st["port"], profile=None, all=False))
            stopped.append(st)
    return stopped


def cmd_flash(a):
    prof = load_profile(a.profile)
    prof["vars"].update(parse_kv(a.var, "--var"))
    flash = prof.get("ic_flash")
    if not flash:
        die(f"ic of profile '{prof['name']}' has no 'flash' section")
    layout_name = prof["vars"].get("flash", "nor")
    layout = flash.get("layouts", {}).get(layout_name)
    if not layout:
        die(f"no flash layout '{layout_name}' (have: {', '.join(flash.get('layouts', {}))})")

    overrides = parse_kv(a.image, "--image")
    if a.app:
        overrides["app"] = a.app
    only = [s.strip() for s in a.only.split(",")] if a.only else None
    images = resolve_images(prof, layout, overrides, only)

    port = (a.port or prof.get("flash_port") or "").upper()
    if not port:
        die("no flash port: pass --port or set 'flash_port' in the profile")
    reset_port = None if a.no_auto else (a.reset_port or prof.get("reset_port") or "").upper() or None
    mpcli = Path(prof.get("mpcli") or prof["expand"](flash["mpcli"], "mpcli"))
    autoreset = Path(prof.get("autoreset") or prof["expand"](flash["autoreset"], "autoreset"))
    if not mpcli.is_file():
        die(f"mpcli not found: {mpcli}")
    if reset_port and not autoreset.is_file():
        die(f"auto-reset helper not found: {autoreset}")

    print(f"flash {prof['vars'].get('chip')} ({layout_name}) via {port}"
          + (f", auto-reset on {reset_port}" if reset_port else ", manual reset"))
    for name, addr, path in images:
        print(f"  {addr}  {name:14s} {path}")
    if a.dry_run:
        return

    held = stop_captures_on({port, reset_port} - {None})

    flash_dir = STATE_DIR / "flash"  # kept apart from the capture state files
    flash_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = flash_dir / f"{port}.json"
    cfg_path.write_text(json.dumps({"mptoolconfig": {
        "port": port, "baud": "1000000",
        "images": {"file": [{"address": addr, "path": str(path)} for _, addr, path in images]},
    }}, indent=2), encoding="utf-8")

    cmd = [str(mpcli), "-c", port, "-M", "5", "-T", flash["ic_type"],
           "-f", str(cfg_path), "-a", "-r"]
    # Like the SDK's west runner: the RTS helper runs concurrently and resets the
    # board into the loader while mpcli is handshaking. mpcli must run from its
    # own folder to find fw/ and config/.
    pulser = (subprocess.Popen([sys.executable, str(autoreset), "--port", reset_port])
              if reset_port else None)
    started = time.time()
    output = []
    try:
        proc = subprocess.Popen(cmd, cwd=str(mpcli.parent), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace")
        for line in proc.stdout:
            output.append(line)
            if a.verbose or re.search(r"download .* ok!|Success|fail|error|exit", line, re.I):
                print(line.rstrip())
        rc = proc.wait(FLASH_TIMEOUT_S)
    finally:
        if pulser and pulser.poll() is None:
            pulser.terminate()
    ok = rc == 0 and any("MPCLI exit: success" in l for l in output)
    print(f"flash {'OK' if ok else 'FAILED'} in {time.time() - started:.1f}s (rc={rc})")
    if not ok:
        tail = "".join(output[-15:])
        die(f"mpcli failed; last output:\n{tail}")

    if a.capture or held:
        # Resume an interrupted capture with its traces, or start a new one.
        prev = held[0] if held else {}
        capture_traces = [f"{k}={v}" for k, v in prev.get("traces", {}).items()]
        app_path = dict((n, p) for n, _, p in images).get("app")
        if "app" in overrides and app_path and (app_path.parent / "app.trace").is_file():
            capture_traces.append(f"aa={app_path.parent / 'app.trace'}")
        capture_traces += list(a.trace or [])
        cmd_start(argparse.Namespace(
            profile=prev.get("profile", prof["name"]), port=prev.get("port"), baud=None,
            out=prev.get("out_dir") if not a.tag else None, tag=a.tag, trace=capture_traces))


# ---------------------------------------------------------------- main

def main():
    if os.name != "nt":
        die("DebugAnalyzer is a Windows tool; run this script with Windows Python")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("profiles", help="list profiles and resolved traces")

    s = sub.add_parser("start", help="start background serial capture")
    s.add_argument("-p", "--profile")
    s.add_argument("--port", help="override serial port, e.g. COM33")
    s.add_argument("--baud", type=int)
    s.add_argument("--out", help="override output directory")
    s.add_argument("--tag", help="sub-folder under the output dir, e.g. an issue key")
    s.add_argument("--trace", action="append", metavar="FLAG=PATH",
                   help="override/add a trace, e.g. aa=D:\\build\\app.trace (repeatable)")

    s = sub.add_parser("stop", help="stop capture")
    s.add_argument("-p", "--profile")
    s.add_argument("--port")
    s.add_argument("--all", action="store_true")

    s = sub.add_parser("status", help="show captures")
    s.add_argument("-p", "--profile")
    s.add_argument("--port")

    s = sub.add_parser("decode", help="offline decode .bin files")
    s.add_argument("-p", "--profile")
    s.add_argument("bins", nargs="+")
    s.add_argument("--out", help="output directory (default: next to the first bin)")
    s.add_argument("--trace", action="append", metavar="FLAG=PATH")

    s = sub.add_parser("flash", help="flash SDK images with mpcli")
    s.add_argument("-p", "--profile")
    s.add_argument("--port", help="flash port (default: profile flash_port)")
    s.add_argument("--reset-port", help="RTS auto-reset port (default: profile reset_port)")
    s.add_argument("--no-auto", action="store_true",
                   help="no RTS auto-reset; put the board in download mode by hand")
    s.add_argument("--var", action="append", metavar="KEY=VALUE",
                   help="override a layout variable, e.g. mic=2mic or chip=rtl8773gtp")
    s.add_argument("--app", help="flash this app image instead of the SDK one (e.g. a local app_MP-*.bin)")
    s.add_argument("--image", action="append", metavar="NAME=PATH",
                   help="replace any image of the layout (repeatable)")
    s.add_argument("--only", help="comma-separated image names to flash, e.g. app")
    s.add_argument("--capture", action="store_true", help="start a capture right after flashing")
    s.add_argument("--tag", help="capture sub-folder (with --capture)")
    s.add_argument("--trace", action="append", metavar="FLAG=PATH", help="capture trace override")
    s.add_argument("--dry-run", action="store_true", help="only print the resolved images")
    s.add_argument("-v", "--verbose", action="store_true", help="print full mpcli output")

    s = sub.add_parser("_supervise")
    s.add_argument("state")

    a = ap.parse_args()
    {"profiles": cmd_profiles, "start": cmd_start, "stop": cmd_stop,
     "status": cmd_status, "decode": cmd_decode, "flash": cmd_flash,
     "_supervise": cmd_supervise}[a.cmd](a)


if __name__ == "__main__":
    main()
