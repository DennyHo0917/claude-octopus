#!/usr/bin/env bash
# TeammateIdle Hook Handler - Claude Code v2.1.33+
# Dispatches queued work to idle agents during multi-agent workflows
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

# Claim the task under the writer lock. Each event reads only its own claim.
CLAIM=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
_update_session_state '
    if .phase == $phase and .status == "in_progress" and ((.agent_queue // []) | length) > 0 then
        .idle_claims[$claim] = {task: .agent_queue[0], remaining: ((.agent_queue | length) - 1)} |
        .agent_queue = .agent_queue[1:]
    else . end' --arg phase "$CURRENT_PHASE" --arg claim "$CLAIM" || exit 0
CLAIM_DATA=$(jq -c --arg claim "$CLAIM" '.idle_claims[$claim] // empty' "$SESSION_FILE")
[[ -n "$CLAIM_DATA" ]] || exit 0
NEXT_TASK=$(jq -r '.task.task // "No task description"' <<< "$CLAIM_DATA")
NEXT_ROLE=$(jq -r '.task.role // "general"' <<< "$CLAIM_DATA")
QUEUE_REMAINING=$(jq -r '.remaining' <<< "$CLAIM_DATA")
METRICS_DIR="${HOME}/.claude-octopus/metrics"
{ mkdir -p "$METRICS_DIR" && jq -nc --arg phase "$CURRENT_PHASE" --arg task "$NEXT_TASK" --arg role "$NEXT_ROLE" \
    '{event: "teammate_idle", phase: $phase, dispatched_task: $task, dispatched_role: $role,
      timestamp: (now | todate)}' >> "${METRICS_DIR}/idle-events.jsonl"; } 2>/dev/null || true
echo "TeammateIdle: Dispatching queued task to idle agent" >&2
echo "Phase: $CURRENT_PHASE | Role: $NEXT_ROLE | Queue remaining: $QUEUE_REMAINING" >&2
echo "Your next task: $NEXT_TASK" >&2
_update_session_state 'del(.idle_claims[$claim])' --arg claim "$CLAIM" >/dev/null 2>&1 || true
exit 2
