_adk_coder() {
    local options="--help --version --workspace --continue --resume --print --json --yes --mode --install-completion /init /help /status /doctor /map /paste /settings /export /mode /plan /explore /session /context /pin /unpin /tdd /test-fix /commands /skills /skill /hooks /mcp /model /config /task /compact /reset /clear /review /analyze /analyse /feature /audit /scan /security /sbom /fix /create /scaffold /new /blueprint /workflow /diff /commit /undo-edit /branch /merge /stash /revert /undo /checkpoint /changelog /sync /exit"
    if [[ ${COMP_WORDS[COMP_CWORD-1]} == --workspace ]]; then
        compopt -o dirnames
        return
    fi
    COMPREPLY=( $(compgen -W "$options" -- "${COMP_WORDS[COMP_CWORD]}") )
}
complete -F _adk_coder adk-coder
