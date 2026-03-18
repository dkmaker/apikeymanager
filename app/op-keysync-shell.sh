#!/usr/bin/env bash
# op-keysync-shell.sh — Shell hook for op-keysync
#
# Source this from your .zshrc or .bashrc:
#   source /path/to/op-keysync-shell.sh
#
# How it works:
#   - Registers a precmd (zsh) or PROMPT_COMMAND (bash) hook
#   - Before every prompt, checks a version file in /run/user/$UID/op-keysync/
#   - If version unchanged → no-op (just one file read, ~1ms)
#   - If version changed   → check state, query socket, export/unset vars
#
# Security:
#   - SSH sessions are DENIED entirely ($SSH_CONNECTION check)
#   - Keys come from the machine you're sitting at, not from remote

_oks_dir="/run/user/$(id -u)/op-keysync"
_oks_vars=()   # tracks which env vars we've exported so we can unset them cleanly
_oks_ver=""    # last seen version string

_oks_check() {
    # ── Deny SSH sessions outright ────────────────────────────────────────────
    # Keys must come from the remote machine you're connecting from,
    # not from this machine's local secret store.
    [[ -n "${SSH_CONNECTION:-}" ]] && return

    # ── Bail if daemon not running ────────────────────────────────────────────
    [[ -f "$_oks_dir/version" ]] || return

    # ── Fast path: version unchanged — nothing to do ──────────────────────────
    local _ver
    _ver=$(<"$_oks_dir/version")
    [[ "$_ver" == "$_oks_ver" ]] && return
    _oks_ver="$_ver"

    # ── Version changed — check state ─────────────────────────────────────────
    local _state
    _state=$(<"$_oks_dir/state" 2>/dev/null)

    if [[ "$_state" == "locked" ]]; then
        # Screen locked — purge all tracked vars from this shell session
        local _v
        for _v in "${_oks_vars[@]}"; do
            unset "$_v"
        done
        _oks_vars=()
        return
    fi

    # ── Unlocked — query socket for fresh exports ─────────────────────────────
    local _reply
    _reply=$(printf 'GET\n' | socat -t2 - "UNIX-CONNECT:$_oks_dir/sock" 2>/dev/null)
    [[ -z "$_reply" ]] && return

    # Unset previously tracked vars before re-importing
    local _v
    for _v in "${_oks_vars[@]}"; do
        unset "$_v"
    done
    _oks_vars=()

    # Parse `export KEY=value` lines and inject into current shell
    local _name _value _line
    while IFS= read -r _line; do
        [[ -z "$_line" ]] && continue
        # Strip leading `export ` if present
        _line="${_line#export }"
        _name="${_line%%=*}"
        _value="${_line#*=}"
        [[ -z "$_name" ]] && continue
        export "${_name}=${_value}"
        _oks_vars+=("$_name")
    done <<< "$_reply"
}

# ── Register hook ─────────────────────────────────────────────────────────────
if [[ -n "${ZSH_VERSION:-}" ]]; then
    # zsh: add to precmd_functions array (won't duplicate if sourced twice)
    autoload -Uz add-zsh-hook 2>/dev/null
    if typeset -f add-zsh-hook &>/dev/null; then
        add-zsh-hook precmd _oks_check
    else
        precmd_functions+=(_oks_check)
    fi
else
    # bash: prepend to PROMPT_COMMAND (guard against double-source)
    if [[ "${PROMPT_COMMAND:-}" != *"_oks_check"* ]]; then
        if [[ -z "${PROMPT_COMMAND:-}" ]]; then
            PROMPT_COMMAND="_oks_check"
        else
            PROMPT_COMMAND="_oks_check;${PROMPT_COMMAND}"
        fi
    fi
fi
