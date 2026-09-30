#!/usr/bin/env python3
"""JSON and path checks used only by the careful and freeze hooks."""

import json
import os
from pathlib import Path
import re
import shlex
import sys


def decision(kind, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": kind,
        "permissionDecisionReason": reason}}, separators=(",", ":")))


def codex_host():
    if (os.environ.get("OCTOPUS_HOST") == "codex"
            or os.environ.get("CODEX_SANDBOX")
            or os.environ.get("CODEX_PLUGIN_ROOT")):
        return True
    # Both hosts set CLAUDE_PLUGIN_ROOT. CODEX_HOME alone can be inherited.
    roots = [str(Path(__file__).resolve())]
    roots += [os.environ.get(key, "") for key in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")]
    custom_home = os.environ.get("CODEX_HOME", "").rstrip("/")
    return any("/.codex/plugins/cache/" in root
               or (custom_home and root.startswith(custom_home + "/plugins/cache/"))
               for root in roots)


def string(value):
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("expected a string without NUL bytes")
    return value


def payload():
    data = json.load(sys.stdin)
    if not isinstance(data, dict):
        raise ValueError("expected a hook JSON object")
    return data


def tool_input(data):
    value = data.get("tool_input", {})
    if not isinstance(value, dict):
        raise ValueError("expected a tool_input object")
    return value


def careful_rm(command, cwd):
    """Only exempt literal, single-command cleanup with every target checked."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = None
    # Normalize literal shell quoting before recognizing the executable and
    # flags. GNU rm also accepts unambiguous long-option abbreviations.
    candidate_text = " ".join(tokens) if tokens is not None else command.replace('"', "").replace("'", "")
    candidates = re.findall(r"(?<![\w])rm\s+[^\n;&|]*", candidate_text)
    if not any(re.search(r"(?:^|\s)(?:-[a-zA-Z]*[rR][a-zA-Z]*|--r[a-z]*)(?=\s|$)", flags)
               and re.search(r"(?:^|\s)(?:-[a-zA-Z]*f[a-zA-Z]*|--f[a-z]*)(?=\s|$)", flags)
               for flags in candidates):
        return False
    if not tokens:
        return True
    # Quoted examples in known non-executing commands are ordinary data. Shell
    # operators or expansions prevent this exemption, even inside quotes.
    uncertain = any(char in command for char in "$`;|&<>\n")
    if not uncertain and (tokens[0] in ("echo", "printf", "rg", "grep")
                          or tokens[:2] == ["git", "commit"]):
        return False
    if uncertain or tokens[0] != "rm":
        return True
    if any(char in command for char in "*?[{}~"):
        return True
    targets = []
    recursive = force = False
    operands = False
    for token in tokens[1:]:
        if not operands and token == "--":
            operands = True
        elif not operands and token.startswith("-"):
            if token == "--recursive":
                recursive = True
            elif token == "--force":
                force = True
            elif token.startswith("--"):
                return True
            else:
                recursive |= "r" in token or "R" in token
                force |= "f" in token
        else:
            targets.append(token)
    if not recursive or not force:
        return False
    if not targets:
        return True
    safe_dirs = {"node_modules", "dist", ".next", "__pycache__", "build", "coverage", ".turbo"}
    for target in targets:
        path = Path(target)
        if ".." in path.parts:
            return True
        if not safe_dirs.intersection(path.parts):
            return True
        path = path if path.is_absolute() else Path(cwd) / path
        # A named cleanup directory may itself be a symlink into user data.
        within_cleanup = False
        for parent in reversed((path, *path.parents)):
            within_cleanup |= parent.name in safe_dirs
            if within_cleanup and parent.is_symlink():
                return True
        if not safe_dirs.intersection(path.resolve().parts):
            return True
    return False


def patch_paths(command):
    text = string(command).strip()
    # Accept a literal patch or a single quoted heredoc, never execute a wrapper.
    if not text.startswith("*** Begin Patch\n"):
        wrapper = re.fullmatch(
            r"apply_patch[ \t]+<<[ \t]*(['\"])([A-Za-z_][A-Za-z_0-9]*)\1[ \t]*\n"
            r"(.*)\n\2", text, re.DOTALL)
        if not wrapper:
            raise ValueError("unrecognized apply_patch command")
        text = wrapper.group(3)
    lines = text.split("\n")
    if lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("incomplete patch envelope")
    paths = []
    i = 1
    while i < len(lines) - 1:
        header = re.fullmatch(r"\*\*\* (Add File|Update File|Delete File): (.+)", lines[i])
        if not header:
            raise ValueError("unknown patch operation or missing filename")
        operation, filename = header.groups()
        paths.append(filename)
        i += 1
        if operation == "Update File" and lines[i].startswith("*** Move to: "):
            paths.append(lines[i][len("*** Move to: "):])
            i += 1
        if operation == "Delete File":
            continue
        body_count = 0
        hunk_count = 0
        while i < len(lines) - 1:
            line = lines[i]
            if line.startswith("*** ") and line != "*** End of File":
                break
            if operation == "Add File":
                if not line.startswith("+"):
                    raise ValueError("invalid Add File body")
            elif line == "@@" or line.startswith("@@ "):
                if body_count and not hunk_count:
                    raise ValueError("empty update hunk")
                hunk_count = 0
                i += 1
                # A hunk marker must be followed by patch content.
                if i >= len(lines) - 1 or not lines[i].startswith((" ", "+", "-")):
                    raise ValueError("empty update hunk")
                continue
            elif line == "*** End of File":
                if not hunk_count:
                    raise ValueError("misplaced end-of-file marker")
                i += 1
                break
            elif not line.startswith((" ", "+", "-")):
                raise ValueError("invalid Update File body")
            body_count += 1
            hunk_count += 1
            i += 1
        if operation == "Update File" and not body_count:
            raise ValueError("missing Update File body")
    if not paths:
        raise ValueError("patch contains no file operations")
    return paths


def canonical_path(value, cwd):
    value = string(value)
    if not value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError("empty path or control character in path")
    path = Path(value)
    path = path if path.is_absolute() else cwd / path
    # Newer Python versions suppress symlink-loop errors in non-strict resolve.
    # Permit missing files, but reject loops and inaccessible existing ancestors.
    for ancestor in (path, *path.parents):
        try:
            ancestor.stat()
        except FileNotFoundError:
            pass
    # resolve follows existing symlinks before processing '..', including when
    # the final file or some parent directories do not exist yet.
    return path.resolve()


def freeze(data, boundary):
    name = string(data.get("tool_name"))
    if name not in ("Edit", "Write", "apply_patch"):
        return
    cwd = Path(string(data.get("cwd", os.getcwd())))
    if not cwd.is_absolute():
        raise ValueError("hook cwd must be absolute")
    cwd = cwd.resolve()
    root = canonical_path(boundary, cwd)
    inputs = tool_input(data)
    paths = (patch_paths(inputs.get("command")) if name == "apply_patch"
             else [inputs.get("file_path")])
    for value in paths:
        path = canonical_path(value, cwd)
        if path != root and root not in path.parents:
            decision("deny", f"Edit blocked: {value} is outside freeze boundary ({root}). "
                     "Use /octo:unfreeze to remove the restriction.")
            return


def main():
    mode = ""
    try:
        mode = sys.argv[1]
        if mode == "careful-decision":
            reason = sys.argv[2]
            if codex_host():
                decision("deny", reason + " Codex cannot ask for approval from a hook. "
                         "Ask the user to review the command and authorize deactivating "
                         "/octo:careful for this session before retrying. "
                         "The hook environment also supports OCTO_CAREFUL_MODE=off.")
            else:
                decision("ask", reason + " Confirm you want to proceed.")
        elif mode == "field":
            data = payload()
            field = sys.argv[2]
            value = (data.get("tool_name") if field == "tool_name" else
                     tool_input(data).get("command", data.get("command", "")))
            print(string(value), end="")
        elif mode == "careful-rm":
            data = payload()
            command = string(tool_input(data).get("command", data.get("command", "")))
            cwd = string(data.get("cwd", os.getcwd()))
            print("ask" if careful_rm(command, cwd) else "allow")
        elif mode == "freeze":
            freeze(payload(), sys.argv[2])
        else:
            raise ValueError("unknown safety helper mode")
    except (ValueError, TypeError, OSError, RuntimeError, IndexError) as error:
        if mode == "field":
            return 1
        decision("deny", f"Safety check could not validate this operation: {error}. "
                 "Correct the hook input or patch before retrying.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
