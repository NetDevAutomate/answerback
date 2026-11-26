#!/usr/bin/env zsh
# Launch a code harness (claude | codex | grok) as a bridge worker under core (hc run).
#
# Why this exists: core (hc) does not forward the launching shell's environment, and the
# bridge identifies a worker by its foreground argv, so a wrapper that runs the binary as a
# child (zsh -> claude) is rejected as "_NotAgent". This script resolves the Bedrock token
# inside the new terminal, exports it, and exec()s the native binary so the foreground
# process IS the harness. The token never appears in argv, a file, or a log.
#
#   hc run --bg --no-parent --name <class>-<tool>-<yymmdd>-<n> --cwd <root> \
#       /abs/path/tools/harness_launch.sh <tool> [harness args...]
#
# Token source order: CODE_HARNESS_BEDROCK_TOKEN already in env; aictl credentials
# bedrock-token; 1Password `op read` with the service-account token from ~/.config/op/sa-token.
emulate -L zsh
set -u

tool=${1:-}
shift || true
[[ $tool == (claude|codex|grok) ]] || { print -u2 "usage: harness_launch.sh claude|codex|grok [args]"; exit 2 }

path=($HOME/.local/bin /opt/homebrew/bin /usr/local/bin $path)
typeset -U path
bin=${commands[$tool]:-}
[[ -n $bin && -x $bin ]] || { print -u2 "harness_launch: $tool binary not found on PATH"; exit 127 }

token=${CODE_HARNESS_BEDROCK_TOKEN:-}
if [[ -z $token && -x ${commands[aictl]:-} ]]; then
  token=$(aictl credentials bedrock-token --tool "$tool" --api-key CODE_HARNESS_BEDROCK_TOKEN 2>/dev/null) || token=""
fi
if [[ -z $token && -x ${commands[op]:-} ]]; then
  sa=$HOME/.config/op/sa-token
  if [[ -r $sa && -z ${OP_SERVICE_ACCOUNT_TOKEN:-} ]]; then
    export OP_SERVICE_ACCOUNT_TOKEN="$(<$sa)"
  fi
  token=$(op read "op://API_KEYS/CODE_HARNESS_BEDROCK_TOKEN/credential" 2>/dev/null) || token=""
  unset OP_SERVICE_ACCOUNT_TOKEN
fi
if [[ -z $token || $token == *[[:space:]]* ]]; then
  print -u2 "harness_launch: could not resolve CODE_HARNESS_BEDROCK_TOKEN for $tool (aictl, then 1Password sa-token)."
  exit 1
fi

# Each harness reads the bearer under its own name: Claude Code and Codex's amazon-bedrock-runtime
# provider read AWS_BEARER_TOKEN_BEDROCK; Grok reads CODE_HARNESS_BEDROCK_TOKEN (its config env_key) and
# must not inherit AWS_BEARER_TOKEN_BEDROCK, which overrides SigV4 for AWS clients its tools start.
# Exporting only CODE_HARNESS_BEDROCK_TOKEN left claude/codex without a token, so codex fell back to
# ChatGPT login (2026-11-18, other-host/example-host workers).
if [[ $tool == grok ]]; then
  export CODE_HARNESS_BEDROCK_TOKEN=$token
  unset AWS_BEARER_TOKEN_BEDROCK
else
  export AWS_BEARER_TOKEN_BEDROCK=$token
  unset CODE_HARNESS_BEDROCK_TOKEN
fi
# Claude workers on the [1m] model hit "Server error mid-response" above ~200k context (2026-11-14 triage;
# a T-16 worker died at 378k on 2026-11-17 despite modelSettings). Compact well below that tier.
if [[ $tool == claude ]]; then export CLAUDE_CODE_AUTO_COMPACT_WINDOW=${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-180000}; fi
unset token
exec "$bin" "$@"
