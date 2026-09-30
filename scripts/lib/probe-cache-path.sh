#!/usr/bin/env bash
# Source-safe path and admission rules shared by standalone cache consumers.
_octo_probe_cache_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

octo_probe_cache_identity() {
    local mode="${1:-namespace}" root setting
    local -a cache_env
    cache_env=("OCTOPUS_RESEARCH_INTENSITY=${OCTOPUS_RESEARCH_INTENSITY:-standard}"
        "USER_CONFIG_FILE=${USER_CONFIG_FILE:-${WORKSPACE_DIR:-${HOME}/.claude-octopus}/.user-config}")
    root="${PROJECT_ROOT:-${OCTOPUS_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-$PWD}}}"
    while IFS= read -r setting; do
        case "$setting" in
            OCTOPUS_*_MODEL|OCTOPUS_*_MODE|OCTOPUS_*_TIER|OCTOPUS_*_INTENSITY|OCTOPUS_*_BREADTH|SUPPORTS_*|OCTOPUS_FABLE5_ROUTING|OCTOPUS_ROUTING_POLICY|OCTOPUS_PROVIDERS_CONFIG|OCTOPUS_TASK_CLASS|CLAUDE_MODEL|FORCE_TIER)
                cache_env+=("$setting=${!setting}") ;;
        esac
    done < <(compgen -v)
    env "${cache_env[@]}" python3 "${_octo_probe_cache_lib_dir}/../helpers/probe-cache-identity.py" \
        "$mode" "$root" "${_octo_probe_cache_lib_dir}/../.."
}

octo_probe_cache_dir() {
    local workspace="${WORKSPACE_DIR:-}" identity
    if [[ -z "$workspace" ]] && type resolve_octopus_workspace >/dev/null 2>&1; then
        workspace="$(resolve_octopus_workspace 2>/dev/null || true)"
    fi
    [[ -n "$workspace" ]] || workspace="${CLAUDE_PLUGIN_DATA:-${HOME:-${TMPDIR:-/tmp}}/.claude-octopus}"
    identity=$(octo_probe_cache_identity) || return 1
    printf '%s/.cache/probe-results/%s\n' "${workspace%/}" "$identity"
}
