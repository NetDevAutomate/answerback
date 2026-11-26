#!/usr/bin/env zsh
# Block until any of the given bridge receipts leaves pending/submitted, or until a deadline.
# Used by the orchestrator as a background job: its exit is the wake-up. Prints one line per
# receipt that changed, then exits 0; exits 3 on deadline with nothing changed (a health check
# is due anyway). Read-only: only `answerback task <id>`.
#
#   tools/wake_on_receipt.sh [--max-min 45] <task-id> [<task-id> ...]
emulate -L zsh
set -u
zmodload zsh/datetime
max_min=45
if [[ ${1:-} == --max-min ]]; then max_min=$2; shift 2; fi
(( $# )) || { print -u2 "usage: wake_on_receipt.sh [--max-min N] <task-id>..."; exit 2 }
deadline=$(( EPOCHSECONDS + max_min * 60 ))
while (( EPOCHSECONDS < deadline )); do
  changed=()
  for id in "$@"; do
    st=$(answerback task "$id" 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("status",""))
except Exception: print("")')
    [[ -n $st && $st != (pending|submitted) ]] && changed+=("$id $st")
  done
  if (( ${#changed} )); then print -l -- $changed; exit 0; fi
  sleep 60
done
print "deadline: no receipt changed in ${max_min} min"
exit 3
