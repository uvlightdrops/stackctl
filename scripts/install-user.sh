#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV_DIR="${STACKCTL_VENV_DIR:-$HOME/.local/share/stackctl/venv}"
BIN_DIR="${STACKCTL_BIN_DIR:-$HOME/.local/bin}"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -e "$ROOT_DIR"
mkdir -p "$BIN_DIR"
ln -sfn "$VENV_DIR/bin/stackctl" "$BIN_DIR/stackctl"

cat <<EOF
stackctl installiert:
  venv: $VENV_DIR
  bin:  $BIN_DIR/stackctl
EOF
