#!/bin/sh
set -eu
repository=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 "$repository/scripts/install.py" "$@"
