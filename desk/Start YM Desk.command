#!/bin/sh
# Double-clickable launcher for macOS. Works on Linux too, from a terminal or
# from a file manager set to run executables.

cd "$(dirname "$0")" || exit 1

# Test that the interpreter actually runs. On macOS /usr/bin/python3 exists as
# a stub even when the developer tools are not installed, so merely finding the
# name on PATH is not enough.
PYTHON=""
for candidate in python3 python3.13 python3.12 python3.11 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" --version >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo
    echo "=================================================================="
    echo "  YM Desk needs Python, and could not find it on this computer."
    echo "=================================================================="
    echo
    echo "  macOS:  install it from https://www.python.org/downloads/"
    echo "          or run:  xcode-select --install"
    echo "  Linux:  sudo apt install python3 python3-venv"
    echo
    printf 'Press Return to close. '
    read -r _
    exit 1
fi

"$PYTHON" ./launch.py "$@"
RESULT=$?

if [ "$RESULT" -ne 0 ]; then
    echo
    echo "YM Desk stopped (code $RESULT). The reason should be above."
    echo
    printf 'Press Return to close. '
    read -r _
fi
exit "$RESULT"
