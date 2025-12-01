#!/bin/bash

VENV_PYTHON="./.venv/bin/python"
MAIN_SCRIPT="main.py"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "Error: Virtual environment not found at $VENV_PYTHON"
    echo "Did you create it? (python3 -m venv .venv)"
    exit 1
fi

sudo SSH_AUTH_SOCK="$SSH_AUTH_SOCK" "$VENV_PYTHON" "$MAIN_SCRIPT" "$@"

