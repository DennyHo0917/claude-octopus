#!/usr/bin/env python3
"""Exercise public JSON writers across contention and process interruption."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(sys.argv.pop()).resolve()


class AtomicUpdates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="octopus-json-lock-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "state.json"
        self.file.write_text('{"n":0}')
        self.env = {"HOME": str(self.root), "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
                    "LC_ALL": "C", "STATE": str(self.file), "PLUGIN": str(REPO)}
        self.prefix = 'source "$PLUGIN/scripts/lib/validation.sh"; log() { :; }; '

    def shell(self, code, extra=None, timeout=15):
        return subprocess.run(["/bin/bash", "-c", self.prefix + code],
                              env={**self.env, **(extra or {})}, cwd=self.root,
                              text=True, capture_output=True, timeout=timeout)

    def test_empty_path_and_normal_update(self):
        result = self.shell('if atomic_json_update "" .; then exit 10; fi; atomic_json_update "$STATE" \'.n=$n\' --argjson n 4')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.file.read_text()), {"n": 4})
        self.assertTrue(Path(str(self.file) + ".update.lock").exists())

    def test_concurrent_updates_preserve_every_increment(self):
        code = self.prefix + 'atomic_json_update "$STATE" \'.n += 1\''
        workers = [subprocess.Popen(["/bin/bash", "-c", code], env=self.env,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(24)]
        results = [(worker.returncode, out, err) for worker in workers
                   for out, err in [worker.communicate(timeout=20)]]
        self.assertTrue(all(rc == 0 for rc, _, _ in results), results)
        self.assertEqual(json.loads(self.file.read_text())["n"], 24)

    def test_lock_wait_is_bounded_and_killed_holder_recovers(self):
        lockfile = str(self.file) + ".update.lock"
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1], 'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(60)", lockfile],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            inode = Path(lockfile).stat().st_ino
            start = time.monotonic()
            blocked = self.shell('atomic_json_update "$STATE" \'.n=3\'', {"OCTO_LOCK_WAIT_SECS": "1"})
            self.assertNotEqual(blocked.returncode, 0)
            self.assertLess(time.monotonic() - start, 4)
            holder.kill(); holder.wait(timeout=5)
            recovered = self.shell('atomic_json_update "$STATE" \'.n=7\'')
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(Path(lockfile).stat().st_ino, inode)
            self.assertEqual(json.loads(self.file.read_text())["n"], 7)
        finally:
            if holder.poll() is None:
                holder.kill(); holder.wait(timeout=5)
            holder.stdout.close()

    def test_jq_failure_and_multiple_results_never_publish(self):
        for expression in ('.n=9 | error("fixture failure")', '.n=9, .n=10'):
            result = self.shell('atomic_json_update "$STATE" "$EXPRESSION"', {"EXPRESSION": expression})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(self.file.read_text()), {"n": 0})
        self.assertEqual(list(self.root.glob("state.json.tmp.*")), [])

    def test_permissions_and_array_contract(self):
        self.file.chmod(0o640)
        self.file.write_text('[1]')
        result = self.shell('atomic_json_update "$STATE" \'. + [2]\'')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.file.read_text()), [1, 2])
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o640)

    def fake_jq(self, script):
        directory = self.root / "bin"
        directory.mkdir()
        binary = directory / "jq"
        binary.write_text("#!/bin/bash\n" + script)
        binary.chmod(0o755)
        return {"PATH": str(directory) + ":" + self.env["PATH"]}

    def test_term_preserves_caller_traps_and_releases_writer(self):
        extra = self.fake_jq('kill -TERM "$PPID"\nsleep .1\nprintf \'{"n":99}\\n\'\n')
        result = self.shell('trap \':\' TERM; before=$(trap -p TERM); rc=0; atomic_json_update "$STATE" . || rc=$?; [[ $rc -eq 143 && $(trap -p TERM) == "$before" ]]', extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.file.read_text()), {"n": 0})
        self.assertEqual(list(self.root.glob("state.json.tmp.*")), [])
        recovered = self.shell('atomic_json_update "$STATE" \'.n=8\'')
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

    def test_killed_writer_cannot_publish_after_successor(self):
        marker = self.root / "writer.pid"
        extra = self.fake_jq('printf \'%s %s\\n\' "$PPID" "$$" > "$MARKER"\nsleep 1\nprintf \'{"n":99}\\n\'\n')
        extra["MARKER"] = str(marker)
        worker = subprocess.Popen(["/bin/bash", "-c", self.prefix + 'atomic_json_update "$STATE" .'],
            env={**self.env, **extra}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        child_pid = None
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            writer_pid, child_pid = map(int, marker.read_text().split())
            os.kill(writer_pid, signal.SIGKILL)
            worker.communicate(timeout=5)
            result = self.shell('atomic_json_update "$STATE" \'.n=42\'')
            self.assertEqual(result.returncode, 0, result.stderr)
            time.sleep(1.1)
            self.assertEqual(json.loads(self.file.read_text()), {"n": 42})
        finally:
            if worker.poll() is None:
                worker.kill(); worker.communicate(timeout=5)
            if child_pid:
                try: os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError: pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
