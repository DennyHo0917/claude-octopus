#!/usr/bin/env bash
# Source-safe activation helpers for hooks that must stay dormant by default.

[[ -n "${_OCTOPUS_HOOK_ACTIVATION_LOADED:-}" ]] && return 0
_OCTOPUS_HOOK_ACTIVATION_LOADED=true

_octopus_hook_activation_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
source "${_octopus_hook_activation_dir}/session-state.sh" || return 1

octo_normalize_context_profile() {
    case "${1:-}" in
        core|minimal) printf 'core\n' ;;
        orchestration|workflow) printf 'orchestration\n' ;;
        full|all) printf 'full\n' ;;
        *) printf 'core\n' ;;
    esac
}

octo_context_profile() {
    local profile="${OCTOPUS_CONTEXT_PROFILE:-}"
    if [[ -z "$profile" && -r "${HOME}/.claude-octopus/user-config.json" ]] && command -v jq >/dev/null 2>&1; then
        profile="$(jq -r '.context_profile // empty' "${HOME}/.claude-octopus/user-config.json" 2>/dev/null || true)"
    fi
    octo_normalize_context_profile "$profile"
}

octo_hook_profile() {
    octo_normalize_context_profile "${OCTOPUS_HOOK_PROFILE:-$(octo_context_profile)}"
}

# Profiles control optional context work only. Safety and lifecycle hooks never
# call this function, so a profile cannot disable their checks.
octo_hook_profile_allows() {
    local hook_id="${1:-}" profile config
    [[ -n "$hook_id" ]] || return 1
    profile="${2:-$(octo_hook_profile)}"
    config="${OCTOPUS_HOOK_PROFILE_CONFIG:-${_octopus_hook_activation_dir}/../../config/hook-profiles.json}"
    [[ -r "$config" ]] || return 1
    command -v jq >/dev/null 2>&1 || return 1
    jq -e --arg profile "$profile" --arg hook "$hook_id" \
        '.profiles[$profile] // [] | any(. == "*" or . == $hook)' "$config" >/dev/null 2>&1
}

octo_hook_session_id() {
    octo_resolve_session_id "" "${1:-}"
}

octo_hook_workflow_active() {
    local input="${1:-}" state_file="${2:-}" hook_session=""
    case "${OCTOPUS_ACTIVE_WORKFLOW:-}" in
        1|true|on|yes) return 0 ;;
    esac
    if [[ -z "$state_file" ]]; then
        state_file=$(octo_workflow_session_file "$input" 2>/dev/null) || return 1
    fi
    [[ -r "$state_file" ]] || return 1
    command -v jq >/dev/null 2>&1 || return 1
    hook_session="$(octo_hook_session_id "$input" 2>/dev/null || true)"
    [[ -n "$hook_session" ]] || return 1
    # Validate and retrieve the relevant fields in one structured read.
    jq -e --arg session "$hook_session" '
        type == "object" and .host_session_id == $session and
        ((.status // .workflow_status // .phase_status // "") as $status |
         ["in_progress", "active", "running", "started"] | index($status) != null) and
        ((.current_phase // .phase // "") as $phase |
         ["complete", "completed", "finished", "done"] | index($phase) == null)
    ' "$state_file" >/dev/null 2>&1
}
