#!/usr/bin/env python3
"""Headless wrapper around Realtek DebugAnalyzer.exe (Windows only).

Commands:
  profiles                    list configured profiles and resolved traces
  start  [-p NAME] [...]      start a background serial capture
  stop   [-p NAME|--port P]   stop a capture gracefully (CTRL_BREAK), kill on timeout
  status                      show captures and their latest log files
  decode [-p NAME] BIN...     offline-decode captured .bin files

Two layers of configuration:
  ics/<ic>.json         shipped with the plugin; describes where an IC's SDK
                        keeps its trace files (path templates).
  user config.json      local machine settings per profile: DebugAnalyzer exe,
                        SDK root, chip variant, serial port, output dir.
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
    traces = {}
    for flag, tmpl in {**ic.get("traces", {}), **prof.get("traces", {})}.items():
        if not tmpl:
            continue
        try:
            traces[flag.lstrip("-")] = str(Path(tmpl.format(**variables)))
        except KeyError as e:
            die(f"profile '{name}': trace -{flag} needs variable {e} (set it in 'vars')")
    prof["traces"] = traces
    prof.setdefault("baud", ic.get("baud", 2000000))
    prof["ic_description"] = ic.get("description", "")
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
    states = [(p, s) for p, s in states if s]
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
        print(f"{st['port']}: stopped ({st.get('stop_mode', 'unknown')}, rc={st.get('rc')})")
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

    s = sub.add_parser("_supervise")
    s.add_argument("state")

    a = ap.parse_args()
    {"profiles": cmd_profiles, "start": cmd_start, "stop": cmd_stop,
     "status": cmd_status, "decode": cmd_decode, "_supervise": cmd_supervise}[a.cmd](a)


if __name__ == "__main__":
    main()
