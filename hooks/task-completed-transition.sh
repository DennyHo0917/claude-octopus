#!/usr/bin/env bash
# TaskCompleted Hook Handler - Claude Code v2.1.33+
# Manages phase transitions when workflow tasks complete
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail
# EXIT trap — emits diagnostic stderr ONLY when the hook exits non-zero, so
# the Claude Code harness error "No stderr output" can never recur. EXIT (not
# ERR) avoids over-firing on intermediate `grep -o`/`cmd | ...` inside $() that
# the hook's logic already handles. See issue #313.
_octo_hook_exit() { local c=$?; if [[ $c -ne 0 ]]; then echo "[hook:$(basename "$0")] exit $c" >&2 2>/dev/null || true; fi; return 0; }
trap _octo_hook_exit EXIT


source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts/lib" && pwd -P)/session-state.sh" || exit 0
_SESSION_INPUT=$(cat 2>/dev/null || true)
octo_session_bind_hook "$_SESSION_INPUT" || exit 0

# Only act if an active workflow session exists
if [[ ! -f "$SESSION_FILE" ]]; then
    exit 0
fi

# Check if jq is available
if ! command -v jq &>/dev/null; then
    exit 0
fi

if ! jq -e 'type == "object"' "$SESSION_FILE" >/dev/null 2>&1; then
    exit 0
fi

_update_session_state() {
    octo_session_update "$@"
}

CURRENT_PHASE=$(jq -r '.phase // empty' "$SESSION_FILE" 2>/dev/null)
if [[ -z "$CURRENT_PHASE" ]]; then
    exit 0
fi

# Bind this completion to the observed phase. Increment and transition in one
# locked update so a phase change during this callback cannot change another ledger.
# Events arriving after a phase change need a phase ID from the host to distinguish them.
CLAIM=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
_update_session_state '
    if .phase == $phase and .status == "in_progress" and
       (.phase_tasks.total | type) == "number" and .phase_tasks.total > 0 and
       .phase_tasks.total == (.phase_tasks.total | floor) and
       (.phase_tasks.completed | type) == "number" and .phase_tasks.completed >= 0 and
       .phase_tasks.completed == (.phase_tasks.completed | floor) and
       .phase_tasks.completed < .phase_tasks.total then
        .phase_tasks.completed += 1 |
        .completion_claims[$claim] = {completed: .phase_tasks.completed, total: .phase_tasks.total,
            autonomy: (.autonomy // .autonomy_mode // "supervised"),
            next: ({probe: "grasp", grasp: "tangle", tangle: "ink", ink: "complete"}[$phase] // "unknown")} |
        if .phase_tasks.completed == .phase_tasks.total then
            if .completion_claims[$claim].next == "complete" then
                .phase = "complete" | .workflow_status = "finished"
            elif (.completion_claims[$claim].autonomy == "autonomous" or
                  .completion_claims[$claim].autonomy == "semi-autonomous") and
                  .completion_claims[$claim].next != "unknown" then
                .phase = .completion_claims[$claim].next |
                .phase_tasks = {total: 0, completed: 0} | .agent_queue = []
            else . end
        else . end
    else . end' --arg phase "$CURRENT_PHASE" --arg claim "$CLAIM" || exit 0
CLAIM_DATA=$(jq -c --arg claim "$CLAIM" '.completion_claims[$claim] // empty' "$SESSION_FILE")
[[ -n "$CLAIM_DATA" ]] || exit 0
COMPLETED=$(jq -r '.completed' <<< "$CLAIM_DATA")
TOTAL=$(jq -r '.total' <<< "$CLAIM_DATA")
NEXT_PHASE=$(jq -r '.next' <<< "$CLAIM_DATA")
AUTONOMY=$(jq -r '.autonomy' <<< "$CLAIM_DATA")
METRICS_DIR="${HOME}/.claude-octopus/metrics"
{ mkdir -p "$METRICS_DIR" && jq -nc --arg phase "$CURRENT_PHASE" --argjson completed "$COMPLETED" --argjson total "$TOTAL" \
    '{event: "task_completed", phase: $phase, completed: $completed, total: $total,
      timestamp: (now | todate)}' >> "${METRICS_DIR}/completion-events.jsonl"; } 2>/dev/null || true
if [[ "$COMPLETED" -eq "$TOTAL" ]]; then
    if [[ "$NEXT_PHASE" == complete ]]; then
        echo "TaskCompleted: All workflow phases complete."
    elif [[ "$AUTONOMY" == autonomous || "$AUTONOMY" == semi-autonomous ]]; then
        echo "TaskCompleted: Phase '$CURRENT_PHASE' complete. Next phase: '$NEXT_PHASE'."
    else
        echo "TaskCompleted: Phase '$CURRENT_PHASE' complete ($COMPLETED/$TOTAL tasks)."
        echo "Next phase: '$NEXT_PHASE', awaiting user approval."
    fi
else
    echo "TaskCompleted: Phase '$CURRENT_PHASE' progress: $COMPLETED/$TOTAL ($((COMPLETED * 100 / TOTAL))%)"
fi
_update_session_state 'del(.completion_claims[$claim])' --arg claim "$CLAIM" >/dev/null 2>&1 || true
