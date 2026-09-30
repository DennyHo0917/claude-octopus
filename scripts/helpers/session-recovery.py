#!/usr/bin/env python3
"""Select active runs or explicitly recover interrupted project checkpoints."""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def process_identity(pid):
    if sys.platform.startswith("linux"):
        # Boot-relative ticks survive wall-clock steps and WSL clock resyncs.
        start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"linux:{boot}:{start}"
    result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                            env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"},
                            text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def alive(record):
    pid = record.get("owner_pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    started = record.get("owner_start")
    if not started:
        return True
    try:
        current = process_identity(pid)
    except (OSError, ValueError, IndexError):
        return True
    return not current or current == started


def read_record(file, root):
    record = json.loads(file.read_text())
    if not isinstance(record, dict) or record.get("project_root") != root or record.get("run_id") != file.parent.name:
        raise ValueError("checkpoint ownership mismatch")
    return record


def publish(file, record):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=file.parent, prefix="session.json.tmp.", delete=False) as output:
            temporary = output.name
            json.dump(record, output)
        os.replace(temporary, file)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if sys.argv[1] == "identity":
        try:
            print(process_identity(int(sys.argv[2])))
        except (OSError, ValueError, IndexError):
            print("")
        return
    mode, scope_text, root, host, pid_text, *arguments = sys.argv[1:]
    scope = Path(scope_text)
    pid = int(pid_text)
    if mode == "active":
        records = []
        for file in scope.glob("runs/*/session.json"):
            try:
                record = read_record(file, root)
            except (OSError, ValueError):
                continue
            if record.get("host_session_id") != host:
                continue
            if record.get("status") == "in_progress" and alive(record):
                records.append(file)
        if len(records) > 1:
            raise ValueError("ambiguous active workflow")
        if records:
            print(records[0])
        return
    if mode == "select":
        files = list(scope.parent.glob("*/runs/*/session.json"))
        if arguments and arguments[0]:
            file = Path(arguments[0])
            if file not in files:
                raise ValueError("explicit checkpoint is outside this project")
        cross_host = len(arguments) > 1 and arguments[1] == "true"
        candidates = []
        records = []
        for file in files:
            try:
                records.append((file, read_record(file, root)))
            except (OSError, ValueError):
                if arguments and arguments[0] == str(file):
                    raise
                continue
        predecessors = {record.get("resumed_from") for _, record in records}
        for file, record in records:
            if arguments and arguments[0] and arguments[0] != str(file):
                continue
            if str(file) in predecessors:
                continue
            if not cross_host and file.parent.parent.parent != scope:
                continue
            if record.get("status") in ("interrupted", "in_progress") and (
                (record.get("owner_pid") == pid and record.get("status") == "interrupted") or
                (record.get("owner_pid") != pid and not alive(record) and
                 ("owner_pid" in record or (arguments and arguments[0] == str(file))))):
                candidates.append(file)
        local = [file for file in candidates if file.parent.parent.parent == scope]
        if len(candidates) == 1 or len(local) == 1:
            print((local or candidates)[0])
        elif candidates:
            raise ValueError("multiple interrupted workflows; select OCTOPUS_SESSION_FILE explicitly")
        else:
            sys.exit(1)
        return
    file = Path(arguments[0])
    if file not in list(scope.parent.glob("*/runs/*/session.json")):
        raise ValueError("checkpoint is outside this project")
    # Use the same stable lock inode as checkpoint writers, with a bounded wait.
    with open(str(file) + ".update.lock", "a") as lock:
        import time
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("checkpoint recovery lock timed out")
                time.sleep(0.025)
        record = read_record(file, root)
        if record.get("status") not in ("interrupted", "in_progress"):
            raise ValueError("checkpoint is no longer resumable")
        if (record.get("owner_pid") == pid and record.get("status") != "interrupted") or (
            record.get("owner_pid") != pid and "owner_pid" in record and alive(record)):
            raise ValueError("workflow is still running")
        if mode == "abandon":
            record["status"] = "abandoned"
            publish(file, record)
            return
        if mode != "claim":
            raise ValueError("unknown recovery operation")
        (scope / "runs").mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="run.", dir=scope / "runs"))
        updated = {**record, "run_id": run_dir.name, "host_session_id": host,
                   "owner_pid": pid, "owner_start": arguments[1], "status": "in_progress",
                   "resumed_from": str(file)}
        publish(run_dir / "session.json", updated)
        record["status"] = "resumed"
        record["resumed_by"] = str(run_dir / "session.json")
        publish(file, record)
        print(run_dir / "session.json")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print("Session recovery failed: " + str(error), file=sys.stderr)
        sys.exit(2)
