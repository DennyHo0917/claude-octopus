#!/usr/bin/env bash
# Regression coverage for #894: persistent Octopus state is optional cache,
# not a reason for lifecycle hooks to fail or for readers to see partial JSON.

set -uo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -P "$SCRIPT_DIR/../.." && pwd)"

source "$SCRIPT_DIR/../helpers/test-framework.sh"
test_suite "session state resilience (#894)"

sandbox="$(mktemp -d)"
trap 'rm -rf "$sandbox"' EXIT
mkdir -p "$sandbox/.claude-octopus/.octo" "$sandbox/plugin-data"
source "$SCRIPT_DIR/../helpers/workflow-session-fixture.sh"
export CLAUDE_CODE_SESSION_ID=resilience-test OCTOPUS_HOST=claude
state_file=$(seed_workflow_session "$PROJECT_ROOT" "$PROJECT_ROOT" "$sandbox" "$sandbox/plugin-data" resilience-test)
export OCTOPUS_SESSION_FILE="$state_file" OCTOPUS_SESSION_RUN_ID="$(jq -r .run_id "$state_file")"
printf '{"workflow":"embrace"}\n}\n' > "$state_file"

state_reading_hooks=(
    session-end
    workflow-verification
    instructions-loaded
    teammate-idle-dispatch
    task-completed-transition
    pre-compact
    user-prompt-submit
)
for hook in "${state_reading_hooks[@]}"; do
    test_case "${hook}.sh ignores malformed persistent session state"
    stdout_file="$sandbox/${hook}.stdout"
    stderr_file="$sandbox/${hook}.stderr"
    hook_rc=0
    env HOME="$sandbox" \
        CLAUDE_PLUGIN_ROOT="$PROJECT_ROOT" \
        CLAUDE_PLUGIN_DATA="$sandbox/plugin-data" \
        bash "$PROJECT_ROOT/hooks/${hook}.sh" </dev/null \
        >"$stdout_file" 2>"$stderr_file" || hook_rc=$?

    if [[ "$hook_rc" -eq 0 && ! -s "$stderr_file" ]]; then
        test_pass
    else
        test_fail "exit=${hook_rc}; stderr=$(<"$stderr_file")"
    fi
done

test_case "workflow-verification.sh ignores a malformed compaction snapshot"
state_file=$(seed_workflow_session "$PROJECT_ROOT" "$PROJECT_ROOT" "$sandbox" "$sandbox/plugin-data" resilience-test)
export OCTOPUS_SESSION_FILE="$state_file" OCTOPUS_SESSION_RUN_ID="$(jq -r .run_id "$state_file")"
mkdir -p "$(dirname "$state_file")/.octo"
printf '{"workflow":"embrace"}\n}\n' > "$(dirname "$state_file")/.octo/pre-compact-snapshot.json"
snapshot_rc=0
env HOME="$sandbox" \
    CLAUDE_PLUGIN_ROOT="$PROJECT_ROOT" \
    CLAUDE_PLUGIN_DATA="$sandbox/plugin-data" \
    bash "$PROJECT_ROOT/hooks/workflow-verification.sh" </dev/null \
    >"$sandbox/snapshot.stdout" 2>"$sandbox/snapshot.stderr" || snapshot_rc=$?
if [[ "$snapshot_rc" -eq 0 && ! -s "$sandbox/snapshot.stderr" ]]; then
    test_pass
else
    test_fail "exit=${snapshot_rc}; stderr=$(<"$sandbox/snapshot.stderr")"
fi

test_case "valid workflow state still triggers missing-dispatch verification"
state_file=$(seed_workflow_session "$PROJECT_ROOT" "$PROJECT_ROOT" "$sandbox" "$sandbox/plugin-data" resilience-test)
export OCTOPUS_SESSION_FILE="$state_file" OCTOPUS_SESSION_RUN_ID="$(jq -r .run_id "$state_file")"
rm -f "$(dirname "$state_file")/.octo/pre-compact-snapshot.json"
mkdir -p "$sandbox/.claude-octopus/results"
valid_rc=0
valid_output="$(env HOME="$sandbox" \
    CLAUDE_PLUGIN_ROOT="$PROJECT_ROOT" \
    CLAUDE_PLUGIN_DATA="$sandbox/plugin-data" \
    bash "$PROJECT_ROOT/hooks/workflow-verification.sh" </dev/null 2>&1)" || valid_rc=$?
if [[ "$valid_rc" -eq 0 && "$valid_output" == *"WORKFLOW VERIFICATION"* ]]; then
    test_pass
else
    test_fail "valid state no longer verifies dispatch (exit=${valid_rc}; output=${valid_output})"
fi

test_case "TaskCompleted ignores a phase whose task ledger is not initialized"
jq '.phase = "probe" | .phase_tasks = {total: 0, completed: 0}' "$state_file" > "$sandbox/state-update.json"
mv "$sandbox/state-update.json" "$state_file"
zero_total_rc=0
env HOME="$sandbox" \
    CLAUDE_PLUGIN_ROOT="$PROJECT_ROOT" \
    CLAUDE_PLUGIN_DATA="$sandbox/plugin-data" \
    bash "$PROJECT_ROOT/hooks/task-completed-transition.sh" </dev/null \
    >"$sandbox/zero-total.stdout" 2>"$sandbox/zero-total.stderr" || zero_total_rc=$?
zero_total_completed="$(jq -r '.phase_tasks.completed' "$state_file")"
if [[ "$zero_total_rc" -eq 0 && ! -s "$sandbox/zero-total.stderr" &&
      "$zero_total_completed" == "0" ]]; then
    test_pass
else
    test_fail "exit=${zero_total_rc}; completed=${zero_total_completed}; stderr=$(<"$sandbox/zero-total.stderr")"
fi

test_case "init_session publishes state through an atomic rename"
init_session_source="$(awk '
    /^init_session\(\)/ { capture=1 }
    /^# Save checkpoint after phase completion/ { capture=0 }
    capture
' "$PROJECT_ROOT/scripts/lib/session.sh")"
if [[ "$init_session_source" == *'mktemp "${SESSION_FILE}.tmp.'* &&
      "$init_session_source" == *'mv "$session_tmp" "$SESSION_FILE"'* &&
      "$init_session_source" != *'cat > "$SESSION_FILE"'* ]]; then
    test_pass
else
    test_fail "init_session must render to a unique temporary file and rename it"
fi

test_case "Embrace state publishes through an atomic rename"
embrace_writer_source="$(awk '
    /^    _write_embrace_session_state\(\)/ { capture=1 }
    /^    _latest_embrace_output\(\)/ { capture=0 }
    capture
' "$PROJECT_ROOT/scripts/lib/workflows.sh")"
if [[ "$embrace_writer_source" == *'octo_session_update'* &&
      "$embrace_writer_source" != *'> "$session_dir/session.json"'* ]]; then
    test_pass
else
    test_fail "Embrace state must use the owned atomic session updater"
fi

test_case "shared session writers never reuse a fixed temporary path"
fixed_tmp_writers="$(grep -R -nE \
    '\$\{SESSION_FILE\}\.tmp"|\$SESSION_FILE\.tmp"|session\.json\.tmp"' \
    "$PROJECT_ROOT/hooks/session-start-memory.sh" \
    "$PROJECT_ROOT/hooks/teammate-idle-dispatch.sh" \
    "$PROJECT_ROOT/hooks/task-completed-transition.sh" \
    "$PROJECT_ROOT/hooks/user-prompt-submit.sh" \
    "$PROJECT_ROOT/scripts/lib/session.sh" \
    "$PROJECT_ROOT/scripts/lib/yaml-workflow.sh" 2>/dev/null || true)"
if [[ -z "$fixed_tmp_writers" ]]; then
    test_pass
else
    test_fail "fixed temporary paths let concurrent writers corrupt session.json:\n${fixed_tmp_writers}"
fi

test_summary
