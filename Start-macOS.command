#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$root/.venv/bin/python" ]; then
    printf '%s\n' 'Create this repository .venv with macOS Python before starting.' >&2
    exit 1
fi
exec "$root/.venv/bin/python" -I "$root/run.py" "$@"
