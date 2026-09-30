#!/usr/bin/env bash
# Check public JSON writes under contention and interruption.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/../helpers/test-framework.sh"
test_suite "Process-released JSON locks"
test_case "Concurrent updates, failures and signals preserve JSON state"
if python3 "$SCRIPT_DIR/atomic_json_update_test.py" "$PROJECT_ROOT"; then
    test_pass
else
    test_fail "JSON writer regression failed"
fi
test_summary
