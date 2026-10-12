# winreap

[日本語](README.ja.md)

Reap orphaned helper processes that AI coding agents (Claude Code, Codex, MCP hosts, IDEs) leave
behind on **Windows**.

## Why

On Windows a child process does not die with its parent: there is no SIGHUP and no re-parenting
to PID 1. Unless the parent puts its children in a Job Object with `KILL_ON_JOB_CLOSE`, helpers
such as background `bash`, `tail -F`, `grep --line-buffered` or MCP servers keep running after the
agent exits. Many reports describe this for Claude Code and Codex on Windows.

Existing reapers (cc-reaper, devreap, ...) target macOS/Linux and look for `PPID == 1`, which never
happens on Windows. winreap is built for Windows instead.

## What it stops

A process is a candidate only when **all** of these hold:

1. **Parent is dead** — the parent PID no longer exists, or the process now holding that PID was
   created *after* the child (PID reuse is detected by comparing creation times). If the parent's
   creation time cannot be read, the parent is treated as alive.
2. **Old enough** — at least `--min-age` hours (default 2; `--kill` refuses anything below 2).
3. **Not protected**, and either
   - its command line matches an **allow** pattern (built-in list below, plus your config), or
   - it **never ran**: every thread is Suspended, it used exactly 0 CPU time, and it is in the same
     session as winreap. Such processes were created suspended and orphaned before anyone resumed
     them; they never executed a single instruction.

Safety:

- **Dry run by default.** Nothing is stopped without `--kill`.
- Right before `TerminateProcess`, the creation time is re-checked on the same handle, and for
  never-ran processes the CPU time is re-checked to still be 0.
- No `taskkill`, no process-tree kill: one process at a time.
- Allow patterns match the program at the start of the command line, not words inside its arguments.
- `--kill` refuses to run as administrator (it could then reach other users' processes) unless you
  add `--allow-elevated`. `install-task.ps1` registers the task as a normal, non-elevated user.

Built-in allow patterns: `tail -F`, `grep -E --line-buffered`, Claude Code shell-snapshot `bash -c`,
one-shot `codex --version` / `codex debug models`, `cmd /c taskkill`, `@playwright/mcp`.
Built-in protect patterns: `app-server`, `daemon run`.

## Usage

Requires Python 3.8+ on Windows. Standard library only.

```powershell
python winreap.py                 # dry run: list candidates
python winreap.py --kill          # stop them (logged to %LOCALAPPDATA%\winreap\winreap.log)
python winreap.py --config my.json --kill
python winreap.py --no-never-ran  # allow-list rule only
```

Run it every hour with Task Scheduler:

```powershell
.\install-task.ps1                       # uses python on PATH (pythonw.exe if present)
.\install-task.ps1 -Config .\my.json
.\uninstall-task.ps1
```

## Configuration

See [`winreap.example.json`](winreap.example.json):

| key | meaning |
|---|---|
| `allow` | extra `[name, regex]` pairs, matched case-insensitively against the full command line |
| `protect` | extra regexes; a match is never stopped (checked first) |
| `replace_builtin` | `true` drops the built-in allow/protect lists |
| `never_ran` | `false` disables the never-ran rule |
| `log` | log file for `--kill` |

You can also import it and pass your own lists:

```python
import winreap
winreap.main(allow=winreap.DEFAULT_ALLOW + [("mine", r"myhelper\.exe")],
             protect=winreap.DEFAULT_PROTECT + [r"keep-me"])
```

## Tests

```powershell
python -m unittest test_winreap
```

## License

MIT
