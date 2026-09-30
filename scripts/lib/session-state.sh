#!/usr/bin/env bash
# Scoped workflow state. Live writers carry their file; hooks only discover it.

[[ -n "${_OCTOPUS_SESSION_STATE_LOADED:-}" ]] && return 0
_OCTOPUS_SESSION_STATE_LOADED=true
_octo_session_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
source "${_octo_session_lib_dir}/session-id.sh" || return 1

octo_session_project_root() {
    local input="${1:-}" root
    root="${PROJECT_ROOT:-${OCTOPUS_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-$PWD}}}"
    if [[ -n "$input" ]] && command -v jq >/dev/null 2>&1; then
        local event_root
        event_root=$(jq -r '.cwd // empty' <<< "$input" 2>/dev/null) || return 1
        [[ -z "$event_root" ]] || root="$event_root"
    fi
    (cd "$root" 2>/dev/null && pwd -P)
}

octo_session_scope() {
    local input="${1:-}" root host project_key host_key workspace host_kind
    root=$(octo_session_project_root "$input") || return 1
    host=$(octo_resolve_session_id "standalone" "$input") || return 1
    project_key=$(printf '%s' "$root" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())') || return 1
    # Factory uses the Claude hook protocol and its session namespace.
    host_kind="${OCTOPUS_HOST:-claude}"
    [[ "$host_kind" != factory ]] || host_kind=claude
    host_key=$(printf '%s:%s' "$host_kind" "$host" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())') || return 1
    workspace="${WORKSPACE_DIR:-${CLAUDE_PLUGIN_DATA:-${CLAUDE_OCTOPUS_WORKSPACE:-${HOME}/.claude-octopus}}}"
    printf '%s/workflow-sessions/%s/%s\n' "${workspace%/}" "$project_key" "$host_key"
}

octo_session_host_file() {
    local scope
    scope=$(octo_session_scope "${1:-}") || return 1
    printf '%s/host.json\n' "$scope"
}

octo_session_owned() {
    local file="${1:-${OCTOPUS_SESSION_FILE:-}}" root host scope
    [[ -n "$file" && -n "${OCTOPUS_SESSION_RUN_ID:-}" && -f "$file" ]] || return 1
    case "$OCTOPUS_SESSION_RUN_ID" in *[!A-Za-z0-9._-]*) return 1 ;; esac
    scope=$(octo_session_scope) || return 1
    [[ "$file" == "$scope/runs/$OCTOPUS_SESSION_RUN_ID/session.json" ]] || return 1
    root=$(octo_session_project_root) || return 1
    host=$(octo_resolve_session_id "standalone") || return 1
    jq -e --arg run "$OCTOPUS_SESSION_RUN_ID" --arg root "$root" --arg host "$host" \
        'type == "object" and .run_id == $run and .project_root == $root and .host_session_id == $host' \
        "$file" >/dev/null 2>&1
}

octo_workflow_session_file() {
    local input="${1:-}" scope run file host root active_run=""
    if [[ -n "${OCTOPUS_SESSION_FILE:-}" ]] && octo_session_owned &&
       scope=$(octo_session_scope "$input") &&
       [[ "$OCTOPUS_SESSION_FILE" == "$scope/runs/$OCTOPUS_SESSION_RUN_ID/session.json" ]]; then
        printf '%s\n' "$OCTOPUS_SESSION_FILE"
        return 0
    fi
    # Unknown hook identities cannot select another session's workflow.
    host=$(octo_resolve_session_id "" "$input") || return 1
    scope=$(octo_session_scope "$input") || return 1
    [[ -r "$scope/latest" ]] || return 1
    IFS= read -r run < "$scope/latest" || [[ -n "${run:-}" ]] || return 1
    case "$run" in ''|*[!A-Za-z0-9._-]*) return 1 ;; esac
    file="$scope/runs/$run/session.json"
    root=$(octo_session_project_root "$input") || return 1
    jq -e --arg root "$root" --arg host "$host" \
        'type == "object" and .project_root == $root and .host_session_id == $host' \
        "$file" >/dev/null 2>&1 || return 2
    # Two live workflows in one host session are ambiguous to a hook without a
    # carried run ID. Skip coordination instead of changing the wrong run.
    active_run=$(python3 "${_octo_session_lib_dir}/../helpers/session-recovery.py" \
        active "$scope" "$root" "$host" "$$" 2>/dev/null) || return 2
    if [[ -n "$active_run" ]]; then
        file="$active_run"
    elif [[ "$(jq -r '.status // empty' "$file")" == in_progress ]]; then
        return 1
    fi
    printf '%s\n' "$file"
}

octo_session_new() {
    local scope run_dir
    scope=$(octo_session_scope) || return 1
    mkdir -p "$scope/runs" || return 1
    run_dir=$(mktemp -d "$scope/runs/run.XXXXXXXX") || return 1
    OCTOPUS_SESSION_RUN_ID="${run_dir##*/}"
    SESSION_FILE="$run_dir/session.json"
    OCTOPUS_SESSION_FILE="$SESSION_FILE"
    # The provider ledger also needs a unique invocation identity when none
    # was supplied by its caller. Keep explicit ledger IDs intact.
    OCTOPUS_RUN_ID="${OCTOPUS_RUN_ID:-$OCTOPUS_SESSION_RUN_ID}"
    export OCTOPUS_SESSION_RUN_ID OCTOPUS_SESSION_FILE OCTOPUS_RUN_ID
}

octo_session_bind_hook() {
    local input="${1:-}"
    PROJECT_ROOT=$(octo_session_project_root "$input") || return 1
    CLAUDE_CODE_SESSION=$(octo_resolve_session_id "" "$input") || return 1
    if [[ "${OCTOPUS_HOST:-claude}" == codex ]]; then
        CODEX_SESSION_ID="$CLAUDE_CODE_SESSION"
        export CODEX_SESSION_ID
    fi
    SESSION_FILE=$(octo_workflow_session_file "$input" 2>/dev/null || true)
    if [[ -n "$SESSION_FILE" ]]; then
        OCTOPUS_SESSION_FILE="$SESSION_FILE"
        OCTOPUS_SESSION_RUN_ID=$(jq -r '.run_id // empty' "$SESSION_FILE") || return 1
        export OCTOPUS_SESSION_FILE OCTOPUS_SESSION_RUN_ID PROJECT_ROOT CLAUDE_CODE_SESSION
    else
        unset OCTOPUS_SESSION_FILE OCTOPUS_SESSION_RUN_ID
        SESSION_FILE=$(octo_session_host_file "$input") || return 1
    fi
}

octo_session_publish_binding() {
    local scope pointer_tmp
    scope=$(octo_session_scope) || return 1
    pointer_tmp=$(mktemp "$scope/latest.tmp.XXXXXX") || return 1
    printf '%s\n' "$OCTOPUS_SESSION_RUN_ID" > "$pointer_tmp" && mv "$pointer_tmp" "$scope/latest"
}

octo_session_update() {
    local filter="$1" root host
    shift
    octo_session_owned "${SESSION_FILE:-}" || return 1
    root=$(octo_session_project_root) || return 1
    host=$(octo_resolve_session_id "standalone") || return 1
    python3 "${_octo_session_lib_dir}/../helpers/json-update.py" \
        "$SESSION_FILE" "$OCTOPUS_SESSION_RUN_ID" "$root" "$host" "$filter" "$@"
}

octo_session_resume_file() {
    local scope root host
    scope=$(octo_session_scope) || return 1
    root=$(octo_session_project_root) || return 1
    host=$(octo_resolve_session_id "standalone") || return 1
    python3 "${_octo_session_lib_dir}/../helpers/session-recovery.py" \
        select "$scope" "$root" "$host" "$$" "${OCTOPUS_SESSION_FILE:-}" "${1:-false}"
}

octo_session_recover() {
    local mode="$1" file="$2" scope root host recovered
    scope=$(octo_session_scope) || return 1
    root=$(octo_session_project_root) || return 1
    host=$(octo_resolve_session_id "standalone") || return 1
    mkdir -p "$scope/runs" || return 1
    recovered=$(python3 "${_octo_session_lib_dir}/../helpers/session-recovery.py" \
        "$mode" "$scope" "$root" "$host" "$$" "$file" "$(octo_session_process_identity)") || return 1
    [[ "$mode" == claim ]] || return 0
    SESSION_FILE="$recovered"
    OCTOPUS_SESSION_FILE="$recovered"
    OCTOPUS_SESSION_RUN_ID="${recovered%/session.json}"
    OCTOPUS_SESSION_RUN_ID="${OCTOPUS_SESSION_RUN_ID##*/}"
    OCTOPUS_RUN_ID="${OCTOPUS_RUN_ID:-$OCTOPUS_SESSION_RUN_ID}"
    export OCTOPUS_SESSION_FILE OCTOPUS_SESSION_RUN_ID OCTOPUS_RUN_ID
    _OCTOPUS_OWNS_SESSION=true
    octo_session_publish_binding
}

interrupt_session() {
    if [[ "${_OCTOPUS_OWNS_SESSION:-false}" == true ]] && octo_session_owned "${SESSION_FILE:-}"; then
        octo_session_update 'if .status == "in_progress" then .status = "interrupted" else . end'
    fi
}

# Host preferences and intent tracking exist before a workflow starts.
octo_host_session_update() {
    local filter="$1" root host file
    shift
    root=$(octo_session_project_root) || return 1
    host=$(octo_resolve_session_id "") || return 1
    file=$(octo_session_host_file) || return 1
    mkdir -p "${file%/*}" || return 1
    python3 "${_octo_session_lib_dir}/../helpers/json-update.py" \
        "$file" '@host' "$root" "$host" "$filter" "$@"
}

octo_session_process_identity() {
    python3 "${_octo_session_lib_dir}/../helpers/session-recovery.py" identity "$$"
}
