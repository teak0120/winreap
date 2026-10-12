import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import winreap as W  # noqa: E402

NOW = 1_800_000_000.0
OLD = NOW - 3 * 3600
NEW = NOW - 600
TAIL = r'"C:\Program Files\Git\usr\bin\tail.exe" -n 0 -F /e/x.log'
GREP = r'"C:\Program Files\Git\usr\bin\grep.exe" -E --line-buffered "rc=|Error"'
SNAP = r'"C:\Program Files\Git\bin\..\usr\bin\bash.exe" -c "source /c/Users/u/.claude/shell-snapshots/snapshot-bash-1.sh && x"'


def pids(procs, **kw):
    return [c[0] for c in W.candidates(procs, NOW, **kw)]


def nr(procs, zero, rules=None):
    return [c[0] for c in W.candidates(procs, NOW, never_ran=lambda p: p in zero, rules=rules)]


class Candidates(unittest.TestCase):
    def test_dead_parent_old_allowed_is_candidate(self):
        procs = {10: (999, OLD, TAIL), 11: (998, OLD, GREP), 12: (997, OLD, SNAP)}
        self.assertEqual(sorted(pids(procs)), [10, 11, 12])

    def test_pid_reused_parent_counts_as_dead(self):
        self.assertEqual(pids({1: (0, NOW - 60, "explorer.exe"), 10: (1, OLD, TAIL)}), [10])

    def test_live_parent_is_kept(self):
        self.assertEqual(pids({1: (0, OLD - 100, "claude.exe"), 10: (1, OLD, TAIL)}), [])

    def test_unreadable_parent_time_counts_as_alive(self):
        self.assertEqual(pids({1: (0, None, "x.exe"), 10: (1, OLD, TAIL)}), [])

    def test_young_is_kept(self):
        self.assertEqual(pids({10: (999, NEW, TAIL)}), [])

    def test_unlisted_orphan_is_kept(self):
        procs = {10: (999, OLD, r"python.exe C:\x\server.py"), 11: (999, OLD, "notepad.exe"), 12: (999, OLD, "")}
        self.assertEqual(pids(procs), [])

    def test_protected_is_kept(self):
        procs = {10: (999, OLD, r"codex.exe app-server"), 11: (999, OLD, r"claude.exe daemon run")}
        self.assertEqual(pids(procs), [])

    def test_codex_oneshot_only_exact(self):
        procs = {10: (999, OLD, r"C:\x\codex.exe --version"), 11: (999, OLD, r"C:\x\codex.exe exec --version x")}
        self.assertEqual(pids(procs), [10])

    def test_unknown_creation_is_kept(self):
        self.assertEqual(pids({10: (999, None, TAIL)}), [])

    def test_self_is_kept(self):
        self.assertEqual(pids({10: (999, OLD, TAIL)}, self_pid=10), [])

    def test_allow_matches_the_program_not_its_arguments(self):
        hit = {
            TAIL: "tail-follow", "tail -F x.log": "tail-follow", GREP: "grep-linebuf", SNAP: "claude-shell-snapshot",
            "cmd /c taskkill /F /PID 1": "cmd-taskkill", r"C:\x\codex.exe debug models": "codex-oneshot",
            r'"C:\n\node.exe" C:\x\@playwright\mcp\cli.js': "playwright-mcp",
            # node with @playwright/mcp anywhere in its arguments is the intended match
            r"C:\x\node.exe server.js --note @playwright\mcp": "playwright-mcp",
        }
        for cmd, name in hit.items():
            self.assertEqual(W.match(cmd), name, cmd)
        for cmd in (r'python.exe w.py --example "tail -F x.log"', r"C:\x\mytail.exe -F x",
                    r"python.exe C:\tools\tail.exe -F x.log", r'python.exe "C:\a b\tail.exe" -F x',
                    r"python.exe C:\t\w.py --exec /usr/bin/tail -F x",
                    r'pwsh -c "grep -E --line-buffered x"'):
            self.assertIsNone(W.match(cmd), cmd)

    def test_custom_rules_replace_defaults(self):
        rules = W.compile_rules([("mine", r"myhelper\.exe")], [])
        procs = {10: (999, OLD, "myhelper.exe"), 11: (999, OLD, TAIL)}
        self.assertEqual(pids(procs, rules=rules), [10])


class NeverRan(unittest.TestCase):
    def test_outside_allowlist_is_candidate(self):
        c = W.candidates({10: (999, OLD, "git.exe ls-remote x")}, NOW, never_ran=lambda p: True)
        self.assertEqual([(x[0], x[2]) for x in c], [(10, W.NEVER_RAN)])

    def test_needs_cpu_zero_dead_parent_and_age(self):
        procs = {1: (0, OLD - 100, "claude.exe"), 10: (999, OLD, "git.exe a"),
                 11: (1, OLD, "git.exe b"), 12: (999, NEW, "git.exe c")}
        self.assertEqual(nr(procs, {11, 12}), [])
        self.assertEqual(nr(procs, set()), [])
        self.assertEqual(nr(procs, {10, 11, 12}), [10])

    def test_respects_protect(self):
        self.assertEqual(nr({10: (999, OLD, "codex.exe app-server")}, {10}), [])

    def test_not_probed_without_callback(self):
        self.assertEqual(pids({10: (999, OLD, "git.exe ls-remote x")}), [])

    def test_allow_name_wins_over_never_ran(self):
        c = W.candidates({10: (999, OLD, TAIL)}, NOW, never_ran=lambda p: True)
        self.assertEqual(c[0][2], "tail-follow")


class Config(unittest.TestCase):
    def _write(self, obj):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        self.addCleanup(os.remove, path)
        return path

    def test_load_valid(self):
        cfg, allow, protect = W.load_config(self._write({"allow": [["a", "x"]], "protect": ["y"]}))
        self.assertEqual(allow, [("a", "x")])
        self.assertEqual(protect, ["y"])

    def test_load_rejects_bad_shapes(self):
        for bad in ([], {"allow": [["only-name"]]}, {"allow": [[1, "x"]]}, {"protect": [3]},
                    {"allow": ["ab"]}, {"allow": None}, {"allow": [1]}, {"protect": None}, {"protect": "abc"},
                    {"log": 123}, {"log": ""}, {"never_ran": "no"}, {"replace_builtin": 1}):
            with self.assertRaises(ValueError):
                W.load_config(self._write(bad))

    def test_cli_rejects_bad_regex(self):
        path = self._write({"allow": [["a", "("]]})
        with self.assertRaises(SystemExit):
            W.main(["--config", path])


class Cli(unittest.TestCase):
    def test_kill_refuses_short_min_age_and_nan(self):
        for v in ("1", "nan", "-1"):
            with self.assertRaises(SystemExit):
                W.main(["--kill", "--min-age", v])

    def test_kill_refused_when_elevated(self):
        orig = W.is_elevated
        W.is_elevated = lambda: True
        try:
            with self.assertRaises(SystemExit):
                W.main(["--kill"])
        finally:
            W.is_elevated = orig

    def test_snapshot_failure_returns_1(self):
        orig = W.snapshot

        def boom():
            raise OSError("no powershell")
        W.snapshot = boom
        try:
            self.assertEqual(W.main(["--no-never-ran", "--quiet"]), 1)
        finally:
            W.snapshot = orig

    def test_log_write_failure_stops_the_rest(self):
        class BadLog:
            def write(self, s):
                raise OSError("disk full")

            def flush(self):
                pass

            def close(self):
                pass
        stopped = []
        saved = (W.snapshot, W.terminate, W.is_elevated, W.open if hasattr(W, "open") else None)
        old = time.time() - 3 * 3600
        W.snapshot = lambda: {10: (999, old, TAIL), 11: (998, old, TAIL)}
        W.terminate = lambda pid, created, nr=False: stopped.append(pid) or "stopped"
        W.is_elevated = lambda: False
        W.open = lambda *a, **k: BadLog()
        try:
            self.assertEqual(W.main(["--kill", "--no-never-ran", "--quiet", "--log", os.devnull]), 1)
        finally:
            W.snapshot, W.terminate, W.is_elevated = saved[:3]
            del W.open
        self.assertEqual(len(stopped), 1)

    def test_time_parse(self):
        self.assertAlmostEqual(W._parse_time("2026-10-13T00:00:00.1234567Z"),
                               W._parse_time("2026-10-13T00:00:00.123456Z"))
        self.assertIsNone(W._parse_time(""))
        self.assertIsNone(W._parse_time("garbage"))


@unittest.skipUnless(os.name == "nt", "windows")
class Live(unittest.TestCase):
    def test_snapshot_lists_self(self):
        p = W.snapshot()
        self.assertIn(os.getpid(), p)
        self.assertIsNotNone(p[os.getpid()][1])

    def test_frozen_excludes_running_self(self):
        f = W.frozen_pids()
        self.assertIsInstance(f, set)
        self.assertNotIn(os.getpid(), f)

    def test_self_is_not_cpu_zero(self):
        self.assertFalse(W.cpu_zero(os.getpid()))

    def test_terminate_refuses_reused_pid(self):
        # A wrong creation time for our own PID must not stop us (if it did, this test would die).
        self.assertEqual(W.terminate(os.getpid(), 12345.0), "pid-reused")


if __name__ == "__main__":
    unittest.main()
