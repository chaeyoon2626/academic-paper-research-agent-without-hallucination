#!/usr/bin/env bash
# macOS: double-click to run.
#   First time only, run in Terminal:  chmod +x start.command
# Linux:  bash start.command

cd "$(dirname "$0")" || exit 1

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  echo
  echo "  Python not found."
  echo "  macOS:  brew install python   or  https://www.python.org/downloads/"
  echo "  Linux:  sudo apt install python3 python3-pip"
  echo
  read -r -p "  Press Enter to close..."
  exit 1
fi

"$PY" launcher.py
CODE=$?
if [ "$CODE" -ne 0 ] && [ "$CODE" -ne 130 ]; then
  read -r -p "  Press Enter to close..."
fi
