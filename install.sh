#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v uv >/dev/null 2>&1; then
    echo "Install uv first, then run this installer again: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi
exec uv run --project "$SCRIPT_DIR" --frozen --no-dev answerback install --source "$SCRIPT_DIR" "$@"
