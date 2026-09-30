#!/usr/bin/env bash
# Tests for v9.6.0 workflow-aware context awareness hook
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

source "$SCRIPT_DIR/../helpers/test-framework.sh"
test_suite "v9.6.0 workflow-aware context awareness hook"

HOOK="$PROJECT_ROOT/hooks/context-awareness.sh"

pass() { test_case "$1"; test_pass; }
fail() { test_case "$1"; test_fail "${2:-$1}"; }

# ── Three severity levels ───────────────────────────────────────────

if grep -q 'AUTO_COMPACT' "$HOOK" 2>/dev/null; then
    pass "Has AUTO_COMPACT severity level"
else
    fail "Has AUTO_COMPACT severity level" "missing AUTO_COMPACT"
fi

if grep -q 'CRITICAL' "$HOOK" 2>/dev/null; then
    pass "Has CRITICAL severity level"
else
    fail "Has CRITICAL severity level" "missing CRITICAL"
fi

if grep -q 'WARNING' "$HOOK" 2>/dev/null; then
    pass "Has WARNING severity level"
else
    fail "Has WARNING severity level" "missing WARNING"
fi

# ── 80% threshold ───────────────────────────────────────────────────

if grep -qE '\-ge 80|>= 80' "$HOOK" 2>/dev/null; then
    pass "Has 80% AUTO_COMPACT threshold"
else
    fail "Has 80% AUTO_COMPACT threshold" "missing 80 check"
fi

# Use a real scoped run so this covers the reader rather than a filename.
source "$SCRIPT_DIR/../helpers/workflow-session-fixture.sh"
fixture="$TEST_TMP_DIR/context-workflow-$$"
sid="context-workflow-$$"
state_file=$(seed_workflow_session "$PROJECT_ROOT" "$fixture/project" \
    "$fixture/home" "$fixture/workspace" "$sid")
jq '.current_phase = "grasp"' "$state_file" > "$state_file.tmp"
mv "$state_file.tmp" "$state_file"
printf '{"used_pct":80}' > "/tmp/octopus-ctx-$sid.json"
output=$(env HOME="$fixture/home" WORKSPACE_DIR="$fixture/workspace" \
    PROJECT_ROOT="$fixture/project" CLAUDE_CODE_SESSION_ID="$sid" \
    OCTOPUS_HOST=claude OCTOPUS_CONTEXT_AWARENESS=on bash "$HOOK" <<< '{}')
rm -f "/tmp/octopus-ctx-$sid.json" "/tmp/octopus-ctx-debounce-$sid.count" "/tmp/octopus-ctx-severity-$sid.level"
if [[ "$output" == *"Research phase active"* ]]; then
    pass "Reads the owned workflow phase for context advice"
else
    fail "Reads the owned workflow phase for context advice" "research advice absent"
fi

if grep -q 'current_phase' "$HOOK" 2>/dev/null; then
    pass "Extracts current_phase from session"
else
    fail "Extracts current_phase from session" "missing current_phase"
fi

# ── Phase-specific advice ───────────────────────────────────────────

if grep -q 'probe\|grasp' "$HOOK" 2>/dev/null; then
    pass "Has advice for probe/grasp phases"
else
    fail "Has advice for probe/grasp phases" "missing research phase advice"
fi

if grep -q 'tangle' "$HOOK" 2>/dev/null; then
    pass "Has advice for tangle phase"
else
    fail "Has advice for tangle phase" "missing implementation phase advice"
fi

if grep -q 'ink' "$HOOK" 2>/dev/null; then
    pass "Has advice for ink phase"
else
    fail "Has advice for ink phase" "missing validation phase advice"
fi

# ── Workflow-specific command suggestions ────────────────────────────

if grep -q '/octo:quick\|/octo:develop' "$HOOK" 2>/dev/null; then
    pass "Suggests Octopus commands in advice"
else
    fail "Suggests Octopus commands in advice" "missing /octo: command suggestions"
fi

# ── Octopus branding ────────────────────────────────────────────────

if grep -q '🐙' "$HOOK" 2>/dev/null; then
    pass "Messages use 🐙 branding"
else
    fail "Messages use 🐙 branding" "missing octopus emoji"
fi

# ── Debounce mechanism preserved ────────────────────────────────────

if grep -q 'debounce' "$HOOK" 2>/dev/null; then
    pass "Debounce mechanism present"
else
    fail "Debounce mechanism present" "missing debounce"
fi

if grep -qE 'COUNT.*%.*5|5.*tool' "$HOOK" 2>/dev/null; then
    pass "Fires every 5 tool calls"
else
    fail "Fires every 5 tool calls" "missing modulo-5 pattern"
fi

# ── Severity escalation preserved ───────────────────────────────────

if grep -qi 'escalat' "$HOOK" 2>/dev/null; then
    pass "Severity escalation bypass present"
else
    fail "Severity escalation bypass present" "missing escalation"
fi
test_summary
