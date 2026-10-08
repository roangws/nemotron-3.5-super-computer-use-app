#!/bin/sh
# Double-click to start Nemotron Agent without building the app. Close this window to quit it.
cd "$(dirname "$0")"
pkill -f "Python -u src/app.py" 2>/dev/null
export HF_HUB_OFFLINE=1
exec .venv/bin/python -u src/app.py
