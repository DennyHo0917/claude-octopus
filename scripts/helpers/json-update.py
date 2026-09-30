#!/usr/bin/env python3
"""Serialize JSON updates and optional session ownership with a POSIX lock."""

import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

child = None


def interrupt(signum, _frame):
    if child is not None and child.poll() is None:
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS may retain an exited group whose leader still needs reaping.
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass
    raise SystemExit(128 + signum)


def setting(name, default, maximum):
    value = os.environ.get(name, "")
    return int(value) if len(value) <= 4 and value.isascii() and value.isdecimal() and 0 < int(value) <= maximum else default


def owned(record, run, root, host):
    return run == "@json" or (isinstance(record, dict) and
        (record.get("run_id") if run != "@host" else "@host",
         record.get("project_root"), record.get("host_session_id")) == (run, root, host))


def update():
    global child
    file, run, root, host, expression, *arguments = sys.argv[1:]
    destination = Path(file)
    # The inode remains stable across releases, successors and interrupted writers.
    with open(str(destination) + ".update.lock", "a") as lock:
        deadline = time.monotonic() + setting("OCTO_LOCK_WAIT_SECS", 5, 300)
        interval = setting("OCTO_LOCK_RETRY_MILLIS", 25, 1000) / 1000
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("JSON update lock timed out")
                time.sleep(interval)
        if run == "@host":
            try:
                record = json.loads(destination.read_text())
            except (FileNotFoundError, ValueError):
                record = {}
            if not owned(record, run, root, host):
                record = {"project_root": root, "host_session_id": host}
        else:
            record = json.loads(destination.read_text())
        if not owned(record, run, root, host):
            raise ValueError("session ownership mismatch")
        if run not in ("@host", "@json") and record.get("status") in ("resumed", "abandoned"):
            raise ValueError("session is no longer writable")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent,
                    prefix=destination.name + ".tmp.", delete=False) as output:
                temporary = output.name
                child = subprocess.Popen(["jq", expression, *arguments], stdin=subprocess.PIPE,
                                         stdout=output, start_new_session=True)
                try:
                    child.communicate(json.dumps(record).encode())
                    if child.returncode:
                        raise subprocess.CalledProcessError(child.returncode, "jq")
                finally:
                    if child.poll() is None:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        except PermissionError:
                            try:
                                child.kill()
                            except ProcessLookupError:
                                pass
                        child.wait()
                    child = None
            updated = json.loads(Path(temporary).read_text())
            if not owned(updated, run, root, host):
                raise ValueError("session update changed ownership")
            if destination.exists():
                os.chmod(temporary, stat.S_IMODE(destination.stat().st_mode))
            os.replace(temporary, destination)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        update()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("JSON update failed: " + str(error), file=sys.stderr)
        sys.exit(1)
