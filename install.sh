#!/bin/sh
# One-step setup: Python env, packages, speech model, and "Nemotron Agent.app" in /Applications.
#   ./install.sh                   -> /Applications/Nemotron Agent.app
#   ./install.sh ~/Applications    -> another folder (no admin rights needed)
set -e
cd "$(dirname "$0")"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "Nemotron Agent needs a Mac with Apple Silicon (M1 or newer)."; exit 1
fi

PY=""
for p in python3.13 python3.12 python3.11 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer is needed. Install it with:  brew install python@3.12"; exit 1
fi

echo "==> Python: $($PY --version)"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
echo "==> Installing packages (a few minutes the first time)"
.venv/bin/python -m pip install -q -r requirements.txt

echo "==> Downloading the speech-to-text model (whisper-large-v3-turbo, ~1.6 GB, runs on your Mac)"
.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/whisper-large-v3-turbo')"

echo "==> Building the app"
./make_app.sh "${1:-/Applications}"

cat <<MSG

Done. Next:
  1. Open "Nemotron Agent" from Applications (or Spotlight). The orb shows up in the menu bar.
  2. The Settings window opens. Paste your GMI Cloud API key.
     Get one at https://console.gmicloud.ai/user-console/ie
  3. Allow the permissions macOS asks for (see README: Permissions).
  4. Hold the \` key, say a task, let go.
MSG
