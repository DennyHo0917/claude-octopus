#!/usr/bin/env bash
# Seed a real scoped workflow without provider calls or user state.
seed_workflow_session() {
    local repo="$1" project="$2" home="$3" workspace="$4" host="$5" workflow="${6:-embrace}"
    mkdir -p "$project" "$home" "$workspace" || return 1
    env -u OCTOPUS_SESSION_FILE -u OCTOPUS_SESSION_RUN_ID -u OCTOPUS_RUN_ID \
        HOME="$home" WORKSPACE_DIR="$workspace" PROJECT_ROOT="$project" \
        OCTO_FIXTURE_OWNER_PID="$$" CLAUDE_CODE_SESSION_ID="$host" OCTOPUS_HOST=claude \
        bash -c '
            source "$1/scripts/lib/session.sh" || exit 1
            log() { :; }
            check_config_reload() { :; }
            init_state() { :; }
            set_current_workflow() { :; }
            init_session "$2" fixture || exit 1
            octo_session_update ".owner_pid = \$pid | del(.owner_start)" --argjson pid "$OCTO_FIXTURE_OWNER_PID" || exit 1
            printf "%s\n" "$SESSION_FILE"
        ' _ "$repo" "$workflow"
}
