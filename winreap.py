"""winreap - reap orphaned helper processes left behind by AI coding agents on Windows.

Windows has no SIGHUP and no re-parenting to PID 1: when Claude Code, Codex, an MCP host or an
IDE dies (or simply exits without a Job Object), the background helpers it spawned (tail -F,
grep --line-buffered, shell wrappers, MCP servers, ...) keep running forever.  winreap finds and
stops them, conservatively.

A process is a candidate only when ALL of these hold:
  1. its parent is dead: the parent PID no longer exists, or the process now holding that PID was
     created after the child (PID reuse).  An unreadable parent creation time counts as alive.
  2. it is at least --min-age old (default 2 h; --kill refuses less than 2 h).
  3. it does not match any PROTECT pattern, and EITHER
     a. its command line matches an ALLOW pattern, OR
     b. "never-ran": every thread is Suspended, it used exactly 0 CPU time, and it lives in the same
        session as winreap.  These were created suspended and orphaned before anyone resumed them;
        they never executed an instruction, so stopping them loses nothing.
Default is a dry run.  With --kill, right before TerminateProcess the PID's creation time is
re-checked (and, for never-ran, CPU time is re-checked to still be 0) on the same handle.

Customize with --config <json> (see README) or by importing this module and calling main() with
your own allow/protect lists.  Stdlib only; one Win32_Process query per run.
"""
import argparse
import ctypes
import datetime as dt
import json
import os
import re
import subprocess
import sys

__version__ = "1.0.0"

MIN_AGE = 2 * 3600
DEFAULT_LOG = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "winreap", "winreap.log")
NEVER_RAN = "never-ran"

# Helpers that AI coding agents commonly leave behind.  Matched with re.search on the full
# command line, case-insensitive.  EXE() anchors the program at the start of the command line, so
# the same words inside another program's arguments do not match.
def EXE(name):
    """Regex prefix: the command line starts with <name> or <name>.exe, optionally quoted/with a path."""
    # A quoted program path may contain spaces; an unquoted one ends at the first space.
    return (r'^\s*(?:"(?:[^"]*[\\/])?' + name + r'(?:\.exe)?"|(?:[^\s"]*[\\/])?' + name
            + r'(?:\.exe)?)\s+')


DEFAULT_ALLOW = [
    ("tail-follow", EXE("tail") + r"(?:-n\s+\d+\s+)?-F\s"),
    ("grep-linebuf", EXE("grep") + r"-E\s+--line-buffered\s"),
    ("claude-shell-snapshot", EXE("bash") + r"-c\s+\"?source\s+\S*/\.claude/shell-snapshots/snapshot-bash-"),
    ("codex-oneshot", EXE("codex") + r"(?:--version|debug\s+models)\s*$"),
    ("cmd-taskkill", EXE("cmd") + r"/c\s+taskkill\s"),
    ("playwright-mcp", EXE("node") + r".*@playwright[\\/]mcp"),
]
# Long-lived processes that must never be stopped even if they match ALLOW or look never-ran.
DEFAULT_PROTECT = [
    r"\bapp-server\b",
    r"\bdaemon\s+run\b",
]

PS = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | ForEach-Object { "
    "$c = if ($_.CommandLine) { $_.CommandLine -replace '[\\t\\r\\n]', ' ' } else { '' }; "
    "$t = if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().ToString('o') } else { '' }; "
    "\"{0}`t{1}`t{2}`t{3}\" -f $_.ProcessId, $_.ParentProcessId, $t, $c }"
)
# CPU 0 alone is tick-charged (a process that ran briefly can read 0), so never-ran also needs
# every thread Suspended.  Same session only: never-ran processes have an empty command line,
# so PROTECT cannot recognise them; the session limit keeps services and other users out.
PS_FROZEN = (
    "$me = (Get-Process -Id $PID).SessionId; "
    "Get-Process | ForEach-Object { "
    "try { $t = $_.Threads } catch { return }; "
    "if ($_.SessionId -ne $me -or $t.Count -eq 0) { return }; "
    "foreach ($x in $t) { if ($x.ThreadState -ne 'Wait' -or $x.WaitReason -ne 'Suspended') { return } }; "
    "$_.Id }"
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def compile_rules(allow, protect):
    return [(n, re.compile(p, re.I)) for n, p in allow], [re.compile(p, re.I) for p in protect]


_RULES = compile_rules(DEFAULT_ALLOW, DEFAULT_PROTECT)


def _parse_time(s):
    if not s:
        return None
    s = s.rstrip("Z")[:26]  # .NET prints 7 fractional digits; Python takes 6
    try:
        return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def snapshot():
    """{pid: (ppid, created_epoch_or_None, cmdline)} from a single Win32_Process query."""
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
                       capture_output=True, timeout=120, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise OSError("Win32_Process query failed (rc=%d)" % r.returncode)
    procs = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        f = line.split("\t", 3)
        if len(f) != 4 or not f[0].isdigit() or not f[1].isdigit():
            continue
        procs[int(f[0])] = (int(f[1]), _parse_time(f[2]), f[3])
    return procs


def frozen_pids():
    """PIDs in our session whose threads are all Suspended.  Empty set if unreadable."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_FROZEN],
                           capture_output=True, timeout=120, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(x) for x in r.stdout.decode("ascii", "replace").split() if x.isdigit()}


def match(cmd, rules=None):
    """Name of the matching ALLOW rule; None when PROTECT matches or nothing does."""
    allow, protect = rules or _RULES
    if not cmd or any(p.search(cmd) for p in protect):
        return None
    for name, rx in allow:
        if rx.search(cmd):
            return name
    return None


def candidates(procs, now, min_age=MIN_AGE, self_pid=None, never_ran=None, rules=None):
    """[(pid, created, rule_name, cmdline)] oldest first.  never_ran(pid) -> bool enables rule 3b."""
    allow, protect = rules or _RULES
    out = []
    for pid, (ppid, created, cmd) in procs.items():
        if pid == self_pid or created is None or now - created < min_age:
            continue
        parent = procs.get(ppid)
        if not (parent is None or (parent[1] is not None and parent[1] > created)):
            continue
        name = match(cmd, (allow, protect))
        if not name and never_ran is not None and not any(p.search(cmd or "") for p in protect) \
                and never_ran(pid):
            name = NEVER_RAN
        if name:
            out.append((pid, created, name, cmd))
    return sorted(out, key=lambda x: x[1])


# --- Win32 ---
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _times(handle):
    ft = [ctypes.c_ulonglong() for _ in range(4)]
    if not ctypes.windll.kernel32.GetProcessTimes(handle, *[ctypes.byref(x) for x in ft]):
        return None
    return [x.value for x in ft]  # creation, exit, kernel, user (100 ns units)


def _cpu_zero_handle(handle):
    t = _times(handle)
    return t is not None and t[2] == 0 and t[3] == 0


def _open(pid, access):
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    return k.OpenProcess(access, False, pid)


def cpu_zero(pid):
    """True when the process used exactly 0 CPU time.  False if it cannot be opened or read."""
    h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return False
    try:
        return _cpu_zero_handle(ctypes.c_void_p(h))
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))


def terminate(pid, created, need_cpu_zero=False):
    """Stop pid only if its creation time still matches.  Returns a short result string."""
    k = ctypes.windll.kernel32
    h = _open(pid, PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return "gone-or-denied"
    try:
        t = _times(ctypes.c_void_p(h))
        if t is None or abs((t[0] / 1e7 - 11644473600) - created) > 2:
            return "pid-reused"
        if need_cpu_zero and not (t[2] == 0 and t[3] == 0):
            return "ran-since"
        return "killed" if k.TerminateProcess(ctypes.c_void_p(h), 1) else "terminate-failed"
    finally:
        k.CloseHandle(ctypes.c_void_p(h))


def load_config(path):
    """Read a JSON config: {"allow": [[name, regex], ...], "protect": [regex, ...],
    "replace_builtin": false, "never_ran": true, "log": "path"}."""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("config must be a JSON object")
    allow, protect = cfg.get("allow", []), cfg.get("protect", [])
    if not isinstance(allow, list) or not all(
            isinstance(x, list) and len(x) == 2 and all(isinstance(v, str) for v in x) for x in allow):
        raise ValueError("allow must be a list of [name, regex] pairs")
    if not isinstance(protect, list) or not all(isinstance(x, str) for x in protect):
        raise ValueError("protect must be a list of regex strings")
    for key in ("replace_builtin", "never_ran"):
        if key in cfg and not isinstance(cfg[key], bool):
            raise ValueError("%s must be true or false" % key)
    if "log" in cfg and not (isinstance(cfg["log"], str) and cfg["log"]):
        raise ValueError("log must be a non-empty string")
    return cfg, [tuple(x) for x in allow], list(protect)


def is_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _fmt(epoch):
    return dt.datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")


def main(argv=None, allow=None, protect=None, log=None, prog=None):
    """CLI.  Wrappers may pass their own allow/protect lists (they replace the defaults) and log path."""
    ap = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    ap.add_argument("--kill", action="store_true", help="actually stop candidates (default: dry run)")
    ap.add_argument("--min-age", type=float, default=MIN_AGE / 3600, help="hours (default 2)")
    ap.add_argument("--quiet", action="store_true", help="print nothing when there are no candidates")
    ap.add_argument("--config", help="JSON file with extra allow/protect patterns")
    ap.add_argument("--no-never-ran", action="store_true", help="disable the never-ran rule")
    ap.add_argument("--log", help="log file for --kill (default %s)" % (log or DEFAULT_LOG))
    ap.add_argument("--allow-elevated", action="store_true",
                    help="allow --kill from an elevated (administrator) process; it can then reach other users")
    ap.add_argument("--version", action="version", version=__version__)
    if sys.stdout is None:  # pythonw under Task Scheduler has no stdout
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    for stream in (sys.stdout, sys.stderr):  # odd characters in a command line must not abort a --kill run
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    a = ap.parse_args(argv)
    # nan compares false and would slip past the age check; --kill never goes below 2 h
    if not (a.min_age >= 0) or (a.kill and a.min_age * 3600 < MIN_AGE):
        ap.error("--min-age must be >= 0, and >= %g with --kill" % (MIN_AGE / 3600))
    if a.kill and not a.allow_elevated and is_elevated():
        ap.error("refusing --kill as administrator (it could stop other users' processes); "
                 "run as a normal user or add --allow-elevated")
    allow = list(DEFAULT_ALLOW if allow is None else allow)
    protect = list(DEFAULT_PROTECT if protect is None else protect)
    use_never_ran = not a.no_never_ran
    log_path = a.log or log or DEFAULT_LOG
    if a.config:
        try:
            cfg, c_allow, c_protect = load_config(a.config)
        except (OSError, ValueError) as e:
            ap.error("--config: %s" % e)
        if cfg.get("replace_builtin"):
            allow, protect = [], []
        allow += c_allow
        protect += c_protect
        if cfg.get("never_ran") is False:
            use_never_ran = False
        log_path = a.log or cfg.get("log") or log_path
    try:
        rules = compile_rules(allow, protect)
    except re.error as e:
        ap.error("bad pattern: %s" % e)
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    never_ran = None
    if use_never_ran:
        frozen = frozen_pids()
        never_ran = lambda pid: pid in frozen and cpu_zero(pid)  # noqa: E731
    try:
        procs = snapshot()
    except (OSError, subprocess.SubprocessError) as e:
        print("winreap: cannot list processes: %s" % e, file=sys.stderr)
        return 1
    cands = candidates(procs, now, a.min_age * 3600, os.getpid(), never_ran, rules)
    if not cands and a.quiet:
        return 0
    logf = None
    if a.kill and cands:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
            logf = open(log_path, "a", encoding="utf-8")
        except OSError as e:  # refuse to stop anything we could not record
            print("winreap: cannot open log %s: %s" % (log_path, e), file=sys.stderr)
            return 1
    for pid, created, name, cmd in cands:
        res = terminate(pid, created, name == NEVER_RAN) if a.kill else "dry-run"
        line = "%s\tpid=%d\tstarted=%s\t%s\t%s\t%s" % (
            dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), pid, _fmt(created), name, res, cmd[:160])
        print(line)
        if logf:
            try:
                logf.write(line + "\n")
                logf.flush()
            except OSError as e:
                # Stop here: anything further would be stopped without a record.
                print("winreap: cannot write log %s: %s; not stopping the rest" % (log_path, e), file=sys.stderr)
                try:
                    logf.close()
                except OSError:
                    pass
                return 1
    if logf:
        try:
            logf.close()
        except OSError:
            pass
    if not a.quiet:
        print("candidates: %d%s" % (len(cands), "" if a.kill else " (dry run; add --kill to stop them)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
