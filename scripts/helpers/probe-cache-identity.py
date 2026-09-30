#!/usr/bin/env python3
"""Partition legacy probe entries by project, source and routing inputs."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def git(root, *args):
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], text=True,
                            capture_output=True, timeout=5,
                            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    return result.stdout.strip() if result.returncode == 0 else None


def clean(root):
    top = git(root, "rev-parse", "--show-toplevel")
    return bool(top and Path(top).resolve() == root and
                git(root, "status", "--porcelain", "--untracked-files=normal",
                    "--ignore-submodules=none") == "")


def main():
    mode, root_value, plugin_value = sys.argv[1:]
    root = Path(root_value).resolve()
    revision = git(root, "rev-parse", "HEAD")
    if mode == "eligible":
        return 0 if revision and clean(root) and (Path.cwd().resolve() == root or root in Path.cwd().resolve().parents) else 1
    plugin = Path(plugin_value)
    config_files = [plugin / ".claude-plugin/plugin.json",
                    plugin / "config/models.json",
                    Path(os.environ.get("OCTOPUS_PROVIDERS_CONFIG",
                         str(Path.home() / ".claude-octopus/config/providers.json"))),
                    Path(os.environ.get("USER_CONFIG_FILE", str(Path.home() / ".claude-octopus/.user-config")))]
    files = {}
    for file in config_files:
        try:
            files[str(file)] = hashlib.sha256(file.read_bytes()).hexdigest()
        except OSError:
            files[str(file)] = "missing"
    settings = {name: value for name, value in os.environ.items()
                if name.startswith("OCTOPUS_") and name.endswith(
                    ("_MODEL", "_MODE", "_TIER", "_INTENSITY", "_BREADTH"))}
    settings["CLAUDE_MODEL"] = os.environ.get("CLAUDE_MODEL", "")
    settings["FORCE_TIER"] = os.environ.get("FORCE_TIER", "")
    for name in ("OCTOPUS_FABLE5_ROUTING", "OCTOPUS_ROUTING_POLICY", "OCTOPUS_TASK_CLASS"):
        settings[name] = os.environ.get(name, "")
    settings.update({name: value for name, value in os.environ.items()
                     if name.startswith("SUPPORTS_")})
    context = {"schema": 3, "project": str(root), "revision": revision, "clean": clean(root),
               "config": files, "settings": settings}
    print(hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        sys.exit(1)
