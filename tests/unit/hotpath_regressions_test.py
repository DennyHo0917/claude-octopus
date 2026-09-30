#!/usr/bin/env python3
"""Run hook and library regressions with isolated state and inert commands."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

REPO = Path(sys.argv.pop()).resolve()


class Hotpaths(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="octo-hotpaths-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / ".claude-octopus").mkdir()
        self.sid = "hotpaths-" + uuid.uuid4().hex
        self.env = {"PATH": "/opt/homebrew/bin:/usr/bin:/bin", "LC_ALL": "C",
                    "HOME": str(self.home), "CLAUDE_PLUGIN_ROOT": str(REPO)}

    def run_shell(self, script, extra=None):
        result = subprocess.run(["/bin/bash", "-c", script], env={**self.env, **(extra or {})},
                                cwd=self.home, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def hook(self, name, payload, extra=None):
        result = subprocess.run(["/bin/bash", str(REPO / "hooks" / name)],
                                input=payload, env={**self.env, **(extra or {})},
                                cwd=self.home, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def state_file(self, name, extension, content):
        file = Path(f"/tmp/octopus-{name}-{self.sid}.{extension}")
        file.write_text(content)
        self.addCleanup(file.unlink, missing_ok=True)
        return file

    def test_cooldown_transitions_and_invalid_state(self):
        self.run_shell('''
set -euo pipefail
source "$CLAUDE_PLUGIN_ROOT/scripts/provider-router.sh"
log() { :; }
mkdir -p "$_PROVIDER_STATE_DIR"
file="$_PROVIDER_STATE_DIR/codex.cooldown"
date +%s > "$file"
if is_provider_available codex; then exit 11; fi
[[ -f "$file" ]]
[[ "$(get_circuit_breaker_status)" == *"codex: OPEN"* ]]
printf '%s\n' "$(( $(date +%s) - 301 ))" > "$file"
is_provider_available codex
[[ ! -f "$file" ]]
for invalid in '' garbage '1+2' 9999999999999999999999999; do
    printf '%s\n' "$invalid" > "$file"
    [[ "$(get_circuit_breaker_status)" == *"invalid cooldown timestamp"* ]]
    if is_provider_available codex; then exit 12; fi
    [[ "$(cat "$file")" =~ ^[0-9]+$ ]]
    [[ "$(get_circuit_breaker_status)" != *"invalid cooldown timestamp"* ]]
done
printf '9999999999\n' > "$file"
if is_provider_available codex; then exit 13; fi
[[ "$(cat "$file")" -le "$(date +%s)" ]]
''')

    def test_explicit_prompt_formats_and_message_field(self):
        for prompt_key in ("prompt", "message"):
            for separators, indent in (((",", ":"), None), (None, None), (None, 2)):
                with self.subTest(key=prompt_key, indent=indent, separators=separators):
                    event = {"session_id": uuid.uuid4().hex,
                             prompt_key: "/octo:discover authentication"}
                    output = self.hook("user-prompt-submit.sh", json.dumps(
                        event, separators=separators, indent=indent))
                    self.assertEqual(output["hookSpecificOutput"]["sessionTitle"],
                                     "Octopus: /octo:discover")
        output = self.hook("user-prompt-submit.sh", json.dumps(
            {"session_id": self.sid, "prompt": "/OCTO:CONFIG providers"}))
        self.assertIn("/octo:setup", output["hookSpecificOutput"]["additionalContext"])
        output = self.hook("user-prompt-submit.sh", json.dumps(
            {"session_id": uuid.uuid4().hex, "prompt": "/octo:discover encoded"}).replace("octo:", "octo\\u003a"))
        self.assertEqual(output["hookSpecificOutput"]["sessionTitle"], "Octopus: /octo:discover")
        self.assertIsNone(self.hook("user-prompt-submit.sh", json.dumps(
            {"session_id": self.sid, "prompt": "explain this function"})))
        self.assertIsNone(self.hook("user-prompt-submit.sh", json.dumps(
            {"session_id": "build", "prompt": None}), {"OCTOPUS_AUTO_ROUTER_MODE": "invoke"}))

    def test_context_cadence_and_escalation(self):
        bridge = self.state_file("ctx", "json", '{"used_pct":70}')
        counter = self.state_file("ctx-debounce", "count", "garbage")
        self.state_file("ctx-severity", "level", "garbage")
        extra = {"CLAUDE_SESSION_ID": self.sid, "OCTOPUS_CONTEXT_AWARENESS": "on"}
        calls = [n for n in range(1, 7) if self.hook(
            "post-tool-dispatch.sh", json.dumps({"session_id": self.sid}), extra)]
        self.assertEqual(calls, [5])
        self.assertEqual(counter.read_text(), "6")
        bridge.write_text('{"used_pct":80}')
        calls = [n for n in range(1, 7) if self.hook(
            "post-tool-dispatch.sh", json.dumps({"session_id": self.sid}), extra)]
        self.assertEqual(calls, [1, 4])

    def test_opus_resolver_agrees_with_command(self):
        output = self.run_shell('''
set -eo pipefail
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/model-resolver.sh"
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/dispatch.sh"
log() { :; }
PLUGIN_DIR="$CLAUDE_PLUGIN_ROOT"
OCTOPUS_CLAUDE_MODEL=claude-sonnet-4-6
for seat in claude-opus claude-opus-fast claude-opus:claude-sonnet-4-6; do
    model="$(get_agent_model "$seat" ink synthesizer)"
    command="$(get_agent_command "$seat" ink synthesizer 100)"
    [[ "$command" == *"--model $model "* ]] || exit 15
done
unset OCTOPUS_CLAUDE_MODEL
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/fable5.sh"
OCTOPUS_FABLE5_ROUTING=escalate
SUPPORTS_OPUS_5=true
OCTOPUS_PROVIDERS_CONFIG="$HOME/providers.json"
for config in \
    '{"routing":{"roles":{"synthesizer":{"provider":"claude","model":"claude-opus-5"}}}}' \
    '{"routing":{"phases":{"ink":{"provider":"claude","model":"claude-opus-5"}}}}' \
    '{"routing":{"roles":{"synthesizer":"claude:claude-opus-5"}}}' \
    '{"overrides":{"claude":"claude-opus-5"}}'; do
    printf '%s' "$config" > "$OCTOPUS_PROVIDERS_CONFIG"
    model="$(get_agent_model claude-opus ink synthesizer)"
    command="$(get_agent_command claude-opus ink synthesizer 100)"
    [[ "$model" == claude-opus-5 && "$command" == *"--model $model "* ]] || exit 16
done
rm "$OCTOPUS_PROVIDERS_CONFIG"
OCTOPUS_FABLE5_ROUTING=off
model="$(get_agent_model claude-opus ink synthesizer)"
command="$(get_agent_command claude-opus ink synthesizer 100)"
[[ "$command" == *"--model ${model//./-} "* ]]
''', {"OCTOPUS_DISPATCH_PREVIEW": "true"})
        self.assertEqual(output, "")

    def test_shared_dispatch_validator_corpus(self):
        # Both public callers must reject shell syntax and preserve literal URLs.
        self.run_shell('''
set -euo pipefail
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/utils.sh"
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/dispatch.sh"
for value in 'https://api.example.test/v1' 'model/name-v2:2026' ordinary_id localhost:8080; do
    _octopus_is_safe_openai_compatible_value "$value"
    _octopus_is_safe_openai_compatible_dispatch_value "$value"
done
for value in '' 'two words' $'line\\nnext' $'line\\rnext' 'a\\b' "a'b" 'a;b' \
    '$(id)' '`id`' 'a|b' 'a&b' '(a)' '<a>' 'a!' 'a?' '[a]' '{a}'; do
    if _octopus_is_safe_openai_compatible_value "$value"; then exit 17; fi
    if _octopus_is_safe_openai_compatible_dispatch_value "$value"; then exit 18; fi
done
''')

    def test_compressor_envelopes_and_large_finite_input(self):
        body = "\n".join(f"line {n}: fixture-{n} " + "x" * 60 for n in range(80))
        counter = self.state_file("compress-debounce", "count", "2")
        for response in ({"stdout": body, "stderr": "final stderr marker"},
                         {"file": {"content": body}}, body * 80):
            with self.subTest(response_type=type(response).__name__):
                counter.write_text("2")
                payload = (json.dumps({"tool_name": "Read", "tool_response": response})
                           if isinstance(response, dict) else response)
                output = self.hook("output-compressor.sh", payload,
                                   {"CLAUDE_SESSION_ID": self.sid})
                context = output["hookSpecificOutput"]["additionalContext"]
                self.assertIn("fixture-0", context)
                self.assertIn("fixture-79", context)
                self.assertLess(len(context), 4000)
                self.assertNotIn("saved", context)
        analytics = self.home / ".claude-octopus/analytics/compression.jsonl"
        records = [json.loads(line) for line in analytics.read_text().splitlines()]
        self.assertEqual(len(records), 3)
        self.assertTrue(all(row["additive"] for row in records))
        self.assertTrue(all("saved" not in row for row in records))

    def test_failure_retrospective_only_on_failed_gate(self):
        script = '''
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/workflows.sh"
RESULTS_DIR="$HOME/ink-results"
mkdir -p "$RESULTS_DIR"
DRY_RUN=false
OCTOPUS_REVIEW_4X10=true
log() { :; }
octopus_phase_banner() { :; }
display_workflow_cost_estimate() { return 0; }
begin_progress_phase() { :; }
retrospective_ceremony() { echo invoked >> "$HOME/retrospective"; }
build_ink_delivery_context() { echo '## Source: tangle.md'; }
run_agent_sync() { echo 'Security: 9/10'; }
score_cross_model_review() { echo '9:9:9:9'; }
format_review_scorecard() { :; }
write_structured_decision() { :; }
for status in PASSED FAILED; do
    printf 'Quality Gate: %s\n' "$status" > "$RESULTS_DIR/tangle.md"
    ink_deliver fixture "$RESULTS_DIR/tangle.md" >/dev/null || true
    if [[ "$status" == PASSED ]]; then [[ ! -e "$HOME/retrospective" ]] || exit 16; fi
done
[[ "$(cat "$HOME/retrospective")" == invoked ]]
'''
        self.run_shell(script)

    def test_run_ownership_concurrent_slots_and_resume(self):
        self.run_shell('''
set -euo pipefail
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/session.sh"
log() { :; }
check_config_reload() { :; }
init_state() { :; }
set_current_workflow() { :; }
update_metrics() { :; }
write_state_md() { :; }
memory_available() { echo false; }
shasum() { return 127; }
CLAUDE_CODE_SESSION=same-host
PROJECT_ROOT="$HOME/project-A"
mkdir -p "$PROJECT_ROOT" "$HOME/project-B"
init_session embrace prompt-A
file_a="$SESSION_FILE"; run_a="$OCTOPUS_SESSION_RUN_ID"
init_session embrace prompt-B
file_b="$SESSION_FILE"; run_b="$OCTOPUS_SESSION_RUN_ID"
[[ "$file_a" != "$file_b" ]]
if (unset OCTOPUS_SESSION_FILE OCTOPUS_SESSION_RUN_ID; octo_workflow_session_file); then exit 20; fi
SESSION_FILE="$file_a"; OCTOPUS_SESSION_FILE="$file_a"; OCTOPUS_SESSION_RUN_ID="$run_a"
[[ "$(octo_workflow_session_file)" == "$file_a" ]]
pids=()
for n in $(seq 1 20); do save_phase_slot probe "slot-$n" "value-$n" & pids+=("$!"); done
failed=false
for pid in "${pids[@]}"; do wait "$pid" || failed=true; done
[[ "$failed" == false ]]
save_session_checkpoint probe completed output-A.md
[[ "$(jq '.phases.probe.slots | length' "$file_a")" == 20 ]]
if octo_session_update '.run_id="changed"' 2>/dev/null; then exit 24; fi
[[ "$(jq -r .run_id "$file_a")" == "$run_a" ]]
[[ "$(jq -r .prompt "$file_a")" == prompt-A ]]
[[ "$(jq -r .prompt "$file_b")" == prompt-B ]]
[[ "$(jq '.phases | length' "$file_b")" == 0 ]]
OCTOPUS_SESSION_RUN_ID="$run_b"
if save_phase_slot probe foreign wrong; then exit 21; fi
[[ "$(jq '.phases.probe.slots | length' "$file_a")" == 20 ]]
SESSION_FILE="$file_b"; OCTOPUS_SESSION_FILE="$file_b"
complete_session
unset OCTOPUS_SESSION_FILE OCTOPUS_SESSION_RUN_ID
[[ "$(octo_workflow_session_file)" == "$file_a" ]]
SESSION_FILE="$file_a"; OCTOPUS_SESSION_FILE="$file_a"; OCTOPUS_SESSION_RUN_ID="$run_a"
interrupt_session
unset OCTOPUS_SESSION_FILE OCTOPUS_SESSION_RUN_ID
[[ "$(octo_session_resume_file)" == "$file_a" ]]
PROJECT_ROOT="$HOME/project-B"
if octo_session_resume_file; then exit 22; fi
PROJECT_ROOT="$HOME/project-A"
CLAUDE_CODE_SESSION=another-host
if octo_session_resume_file; then exit 23; fi
init_session embrace prompt-other-host
[[ "$(jq -r .host_session_id "$SESSION_FILE")" == another-host ]]
[[ "$(jq -r .prompt "$file_a")" == prompt-A ]]
unset OCTOPUS_SESSION_FILE OCTOPUS_SESSION_RUN_ID
OCTOPUS_HOST=codex
CODEX_SESSION_ID=codex-host
init_session embrace prompt-codex
[[ "$(jq -r .host_session_id "$SESSION_FILE")" == codex-host ]]
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/hook-activation.sh"
octo_hook_workflow_active '{}'
export PROJECT_ROOT CODEX_SESSION_ID OCTOPUS_HOST
node --input-type=module -e '
import assert from "node:assert/strict";
const {readSession} = await import(process.env.CLAUDE_PLUGIN_ROOT + "/hooks/octopus-hud.mjs");
assert.equal(readSession({cwd:process.env.PROJECT_ROOT}).prompt, "prompt-codex");
assert.equal(readSession({cwd:process.env.HOME + "/project-B"}), null);
'
''')

    def test_session_recovery_and_factory_scope(self):
        self.run_shell('''
set -euo pipefail
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/session.sh"
log() { :; }; check_config_reload() { :; }; init_state() { :; }; set_current_workflow() { :; }
PROJECT_ROOT="$HOME"
CLAUDE_CODE_SESSION=old-host
init_session embrace original-prompt
old="$SESSION_FILE"
save_phase_slot probe evidence original-evidence
interrupt_session
[[ "$(jq -r .status "$old")" == interrupted ]]
CLAUDE_CODE_SESSION=new-host
unset OCTOPUS_SESSION_FILE OCTOPUS_SESSION_RUN_ID
CI_MODE=false
YELLOW= CYAN= NC= _BOX_TOP= _BOX_BOT=
check_resume_session <<< y
[[ "$SESSION_FILE" != "$old" ]]
[[ "$(jq -r .status "$old")" == resumed ]]
if python3 "$CLAUDE_PLUGIN_ROOT/scripts/helpers/json-update.py" "$old" "$(jq -r .run_id "$old")" "$HOME" old-host '.late="wrong"' 2>/dev/null; then exit 32; fi
[[ "$(jq -r '.late // empty' "$old")" == '' ]]
[[ "$(get_phase_slot probe evidence)" == original-evidence ]]
[[ "$(jq -r .host_session_id "$SESSION_FILE")" == new-host ]]
OCTOPUS_HOST=factory
[[ "$(octo_workflow_session_file)" == "$SESSION_FILE" ]]
complete_session
init_session embrace declined
interrupt_session
file="$SESSION_FILE"
CI_MODE=true
if check_resume_session; then exit 30; fi
[[ "$(jq -r .status "$file")" == abandoned ]]
''')

    def test_live_foreign_owner_survives_timezone_and_ps_failure(self):
        start = subprocess.check_output([sys.executable, str(REPO / "scripts/helpers/session-recovery.py"), "identity", str(os.getpid())],
            env={**self.env, "TZ": "UTC0", "LC_ALL": "C"}, text=True).strip()
        file = Path(self.run_shell('''
set -euo pipefail
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/session.sh"
log() { :; }; check_config_reload() { :; }; init_state() { :; }; set_current_workflow() { :; }
PROJECT_ROOT="$HOME"; CLAUDE_CODE_SESSION=live-host
init_session embrace live
octo_session_update '.owner_pid=$pid | .owner_start=$start | .phase="probe" |
    .agent_queue=[{task:"must-not-claim"}]' --argjson pid "$FIXTURE_PID" --arg start "$FIXTURE_START"
printf '%s' "$SESSION_FILE"
''', {"FIXTURE_PID": str(os.getpid()), "FIXTURE_START": start}))
        self.run_shell('''
set -euo pipefail
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/session-state.sh"
PROJECT_ROOT="$HOME"; CLAUDE_CODE_SESSION=other-host
if octo_session_resume_file true; then exit 31; fi
''', {"TZ": "Asia/Tokyo"})
        binaries = self.home / "fake-ps"
        binaries.mkdir()
        (binaries / "ps").write_text("#!/bin/bash\nexit 1\n")
        (binaries / "ps").chmod(0o755)
        self.run_shell('''
set -euo pipefail
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/session-state.sh"
PROJECT_ROOT="$HOME"; CLAUDE_CODE_SESSION=other-host
if octo_session_resume_file true; then exit 33; fi
''', {"PATH": str(binaries) + ":" + self.env["PATH"]})
        self.assertIsNone(self.hook("teammate-idle-dispatch.sh", json.dumps({"cwd": str(self.home)})))
        self.assertEqual(json.loads(file.read_text())["agent_queue"], [{"task": "must-not-claim"}])

    def test_auto_diamond_route_owns_checkpoint(self):
        self.run_shell('''
set -euo pipefail
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/session.sh"
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/auto-route.sh"
log() { :; }; check_config_reload() { :; }; init_state() { :; }; set_current_workflow() { :; }
classify_task() { echo diamond-discover; }; estimate_complexity() { echo 2; }
get_tier_name() { echo standard; }; evaluate_branch_condition() { echo direct; }
get_branch_display() { echo direct; }; detect_context() { echo code:test; }
get_context_display() { echo code; }; classify_cynefin() { echo complicated; }
detect_response_mode() { echo full; }
probe_discover() { octo_session_owned; save_session_checkpoint probe completed owned-auto.md; }
PROJECT_ROOT="$HOME"; CLAUDE_CODE_SESSION=auto-host
MAGENTA= NC= BLUE= GREEN= YELLOW= CYAN= _BOX_TOP= _BOX_BOT=
auto_route 'research a substantial architecture decision'
[[ "$(get_phase_output probe)" == owned-auto.md ]]
[[ "$_octo_standalone_session" == true ]]
''')

    def test_intent_tracking_before_workflow(self):
        extra = {"OCTOPUS_AUTO_ROUTER_MODE": "invoke", "CLAUDE_SESSION_ID": self.sid}
        event = json.dumps({"session_id": self.sid, "cwd": str(self.home),
                            "prompt": "How do cache eviction strategies affect memory usage?"})
        start = subprocess.run(["/bin/bash", str(REPO / "hooks/session-start-memory.sh")],
            input=event, env={**self.env, **extra, "OCTOPUS_REMOTE_SESSION": "true"},
            cwd=self.home, text=True, capture_output=True, timeout=15)
        self.assertEqual(start.returncode, 0, start.stderr)
        first = self.hook("user-prompt-submit.sh", event, extra)
        second = self.hook("user-prompt-submit.sh", event, extra)
        self.assertNotEqual(first, second)
        preferences = list((self.home / ".claude-octopus/workflow-sessions").glob("*/*/host.json"))
        self.assertEqual(len(preferences), 1)
        self.assertEqual(json.loads(preferences[0].read_text())["detected_intent"], "discover")

    def test_concurrent_task_hooks_claim_unique_tasks(self):
        file = Path(self.run_shell('''
set -euo pipefail
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/session.sh"
log() { :; }; check_config_reload() { :; }; init_state() { :; }; set_current_workflow() { :; }
PROJECT_ROOT="$HOME"; CLAUDE_CODE_SESSION="$FIXTURE_SID"
init_session embrace tasks
octo_session_update '.owner_pid=$pid | del(.owner_start) | .phase="probe" |
    .autonomy="autonomous" | .phase_tasks={total:2,completed:0} |
    .agent_queue=[range(0;10) | {task:("unique-task-" + tostring),role:"researcher"}]' --argjson pid "$FIXTURE_PID"
printf '%s' "$SESSION_FILE"
''', {"FIXTURE_SID": self.sid, "FIXTURE_PID": str(os.getpid())}))
        event = json.dumps({"session_id": self.sid, "cwd": str(self.home)})
        def run_batch(hook, count):
            processes = [subprocess.Popen(["/bin/bash", str(REPO / "hooks" / hook)],
                env=self.env, cwd=self.home, text=True, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(count)]
            for process in processes:
                process.stdin.write(event)
                process.stdin.close()
                process.stdin = None
            return [(process.returncode, out, err) for process in processes
                    for out, err in [process.communicate(timeout=15)]]
        # Fail only the cleanup write after the task or completion was claimed.
        binaries = self.home / "cleanup-failure"
        binaries.mkdir()
        python = binaries / "python3"
        python.write_text('#!/bin/bash\nif [[ ${1##*/} == json-update.py && ${6:-} == del* ]]; then exit 1; fi\nexec "' + sys.executable + '" "$@"\n')
        python.chmod(0o755)
        self.env["PATH"] = str(binaries) + ":" + self.env["PATH"]
        results = run_batch("teammate-idle-dispatch.sh", 20)
        assignments = [err.split("Your next task: ")[1].splitlines()[0]
                       for code, out, err in results if code == 2]
        self.assertEqual(len(assignments), 10, results)
        self.assertEqual(len(set(assignments)), 10, assignments)
        self.assertEqual(json.loads(file.read_text())["agent_queue"], [])
        completions = run_batch("task-completed-transition.sh", 10)
        self.assertTrue(all(code == 0 for code, _, _ in completions), completions)
        record = json.loads(file.read_text())
        self.assertEqual(record["phase"], "grasp")
        self.assertEqual(record["phase_tasks"], {"total": 0, "completed": 0})
        metrics = self.home / ".claude-octopus/metrics/completion-events.jsonl"
        self.assertEqual(len(metrics.read_text().splitlines()), 2)
        # Finishing agent tasks does not finish the owning orchestrator's gates.
        record["phase"] = "ink"
        record["phase_tasks"] = {"total": 2, "completed": 0}
        file.write_text(json.dumps(record))
        final = run_batch("task-completed-transition.sh", 10)
        self.assertTrue(all(code == 0 for code, _, _ in final), final)
        record = json.loads(file.read_text())
        self.assertEqual(record["phase"], "complete")
        self.assertEqual(record["status"], "in_progress")
        record["phase"] = "probe"
        record["phase_tasks"] = {"total": 1, "completed": 0}
        record["agent_queue"] = [{"task": "metrics-failure-task"}]
        file.write_text(json.dumps(record))
        for child in metrics.parent.iterdir():
            child.unlink()
        metrics.parent.rmdir()
        metrics.parent.write_text("not a directory")
        idle = run_batch("teammate-idle-dispatch.sh", 1)
        self.assertEqual(idle[0][0], 2, idle)
        self.assertIn("Your next task: metrics-failure-task", idle[0][2])
        completed = run_batch("task-completed-transition.sh", 1)
        self.assertEqual(completed[0][0], 0, completed)
        self.assertIn("complete", completed[0][1])



    def test_session_lock_recovers_after_interruption(self):
        file = self.home / "session.json"
        record = {"run_id": "fixture", "project_root": str(self.home),
                  "host_session_id": "fixture-host"}
        file.write_text(json.dumps(record))
        holder = subprocess.Popen([sys.executable, "-c", """
import fcntl,sys,time
with open(sys.argv[1], 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    print('locked', flush=True)
    time.sleep(600)
""", str(file) + ".update.lock"], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            holder.kill()
            holder.wait(timeout=5)
            update = subprocess.run([sys.executable,
                str(REPO / "scripts/helpers/json-update.py"), str(file),
                "fixture", str(self.home), "fixture-host", '.checkpoint="recovered"'],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(update.returncode, 0, update.stderr)
            self.assertEqual(json.loads(file.read_text())["checkpoint"], "recovered")
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)
            holder.stdout.close()

    def test_probe_cache_project_source_and_configuration_identity(self):
        self.run_shell('''
set -euo pipefail
source "$CLAUDE_PLUGIN_ROOT/scripts/lib/semantic-cache.sh"
log() { :; }
for project in A B; do
    mkdir -p "$HOME/project-$project"
    git -C "$HOME/project-$project" init -q
    git -C "$HOME/project-$project" -c user.name=fixture -c user.email=fixture@example.invalid commit --allow-empty -qm baseline
done
PROJECT_ROOT="$HOME/project-A"
cd "$PROJECT_ROOT"
WORKSPACE_DIR="$HOME/.claude-octopus"
key_a=$(get_cache_key identical-prompt)
printf 'private A analysis\n' > "$HOME/analysis.md"
save_to_cache "$key_a" "$HOME/analysis.md"
check_cache "$key_a"
[[ "$(get_cached_result "$key_a")" == 'private A analysis' ]]
PROJECT_ROOT="$HOME/project-B"
cd "$PROJECT_ROOT"
key_b=$(get_cache_key identical-prompt)
[[ "$key_a" != "$key_b" ]]
if check_cache "$key_b"; then exit 31; fi
PROJECT_ROOT="$HOME/project-A"
cd "$PROJECT_ROOT"
OCTOPUS_RESEARCH_INTENSITY=deep
[[ "$(get_cache_key identical-prompt)" != "$key_a" ]]
unset OCTOPUS_RESEARCH_INTENSITY
OCTOPUS_CLAUDE_MODEL=claude-sonnet-4-6
[[ "$(get_cache_key identical-prompt)" != "$key_a" ]]
unset OCTOPUS_CLAUDE_MODEL
OCTOPUS_TASK_CLASS=mechanical
[[ "$(get_cache_key identical-prompt)" != "$key_a" ]]
unset OCTOPUS_TASK_CLASS
printf 'resource_tier: premium\n' > "$WORKSPACE_DIR/.user-config"
[[ "$(get_cache_key identical-prompt)" != "$key_a" ]]
rm "$WORKSPACE_DIR/.user-config"
mkdir -p "$HOME/.claude-octopus/config"
printf '{"routing":{"phases":{"probe":"claude:claude-opus-5"}}}' > "$HOME/.claude-octopus/config/providers.json"
[[ "$(get_cache_key identical-prompt)" != "$key_a" ]]
rm "$HOME/.claude-octopus/config/providers.json"
OCTOPUS_PROVIDERS_CONFIG="$HOME/custom-providers.json"
printf '{}' > "$OCTOPUS_PROVIDERS_CONFIG"
custom_key=$(get_cache_key identical-prompt)
printf '{"overrides":{"claude":"claude-sonnet-4-6"}}' > "$OCTOPUS_PROVIDERS_CONFIG"
[[ "$(get_cache_key identical-prompt)" != "$custom_key" ]]
unset OCTOPUS_PROVIDERS_CONFIG
OCTOPUS_FABLE5_ROUTING=escalate
[[ "$(get_cache_key identical-prompt)" != "$key_a" ]]
unset OCTOPUS_FABLE5_ROUTING
git -C "$PROJECT_ROOT" -c user.name=fixture -c user.email=fixture@example.invalid commit --allow-empty -qm changed-source
[[ "$(get_cache_key identical-prompt)" != "$key_a" ]]
git -C "$PROJECT_ROOT" config status.showUntrackedFiles no
printf changed > "$PROJECT_ROOT/untracked"
dirty_key=$(get_cache_key identical-prompt)
if check_cache "$key_a"; then exit 32; fi
if save_to_cache "$key_a" "$HOME/analysis.md"; then exit 33; fi
rm "$PROJECT_ROOT/untracked"
[[ "$(get_cache_key identical-prompt)" != "$dirty_key" ]]
clean_key=$(get_cache_key identical-prompt)
save_to_cache "$clean_key" "$HOME/analysis.md"
GIT_DIR="$HOME/project-B/.git" GIT_WORK_TREE="$HOME/project-B" check_cache "$clean_key"
printf changed > "$PROJECT_ROOT/untracked"
if GIT_DIR="$HOME/project-B/.git" GIT_WORK_TREE="$HOME/project-B" check_cache "$clean_key"; then exit 34; fi
rm "$PROJECT_ROOT/untracked"
OCTOPUS_SEMANTIC_CACHE=true
bigram_similarity() { printf '1\n'; }
old_key=$(get_cache_key similar-prompt)
OCTOPUS_OTHER_MODEL=changed-unexported-model
new_key=$(get_cache_key similar-prompt)
[[ "$old_key" != "$new_key" ]]
save_to_cache_semantic "$old_key" "$HOME/analysis.md" similar-prompt
if check_cache_semantic similar-prompt; then exit 35; fi
for invalid in '' '..' '../escape' garbage; do
    if save_to_cache "$invalid" "$HOME/analysis.md"; then exit 36; fi
done
unset WORKSPACE_DIR
CLAUDE_PLUGIN_DATA="$HOME/other-data"
first=$(get_cache_key config-prompt)
printf 'changed user settings' > "$HOME/.claude-octopus/.user-config"
[[ "$(get_cache_key config-prompt)" != "$first" ]]
rm "$HOME/.claude-octopus/.user-config"
cd "$HOME"
if save_to_cache "$new_key" "$HOME/analysis.md"; then exit 37; fi
''')


if __name__ == "__main__":
    unittest.main()
