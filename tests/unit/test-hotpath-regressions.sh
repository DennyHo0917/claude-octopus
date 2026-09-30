#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
source "$SCRIPT_DIR/../helpers/test-framework.sh"
test_suite "plugin hotpath regressions"
test_case "real hooks and libraries preserve their behavioral contracts"
if python3 "$SCRIPT_DIR/hotpath_regressions_test.py" "$PROJECT_ROOT"; then
    test_pass
else
    test_fail "hotpath behavior regressions failed"
fi
test_summary
