#!/bin/sh
set -eu

pkg_dir=$(CDPATH= cd -P "$(dirname "$0")/.." && pwd)
parent_dir=$(CDPATH= cd -P "$pkg_dir/.." && pwd)

if [ -f "$parent_dir/.env" ]; then
    . "$parent_dir/.env"
elif [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/adk-coder/.env" ]; then
    . "${XDG_CONFIG_HOME:-$HOME/.config}/adk-coder/.env"
fi

if [ -x "$pkg_dir/.venv/bin/adk-coder-app" ]; then
    exec "$pkg_dir/.venv/bin/adk-coder-app" "$@"
fi
if [ -x "$pkg_dir/.venv/bin/python" ]; then
    exec "$pkg_dir/.venv/bin/python" -m adk_coder.desktop_app "$@"
fi
exec python3 -m adk_coder.desktop_app "$@"
