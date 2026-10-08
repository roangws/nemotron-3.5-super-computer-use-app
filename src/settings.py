"""User settings: GMI API key, voice and switches. Saved in settings.json (see paths.py).

  python src/settings.py                         show the settings (the key is masked)
  python src/settings.py --key YOUR_GMI_KEY      save the API key
  python src/settings.py --voice male            female (Ashley) or male (Dennis)
  python src/settings.py --screenshots on        keep screenshots in sessions/ (default off)

The GMI_API_KEY environment variable, or a .env file in the repo, wins over the saved key.
"""
import json
import os
import sys

import paths

KEY_URL = "https://console.gmicloud.ai/user-console/ie"
BASE_URL = "https://api.gmi-serving.com/v1"

# The two voices. Both are Inworld TTS voices served by GMI Cloud.
VOICES = {
    "female": {"label": "Female (Ashley)", "voice": "Ashley"},
    "male": {"label": "Male (Dennis)", "voice": "Dennis"},
}

DEFAULTS = {
    "gmi_api_key": "",
    "voice": "female",        # female | male
    "speak": True,            # say each step out loud
    "save_screenshots": False,  # keep step screenshots in sessions/ (off: only the text report is kept)
}


def _load_dotenv():
    path = os.path.join(paths.ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load():
    s = dict(DEFAULTS)
    if os.path.exists(paths.SETTINGS):
        try:
            s.update(json.load(open(paths.SETTINGS)))
        except (OSError, json.JSONDecodeError):
            pass
    if s["voice"] not in VOICES:
        s["voice"] = "female"
    return s


def save(s):
    os.makedirs(os.path.dirname(paths.SETTINGS), exist_ok=True)
    tmp = paths.SETTINGS + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:  # only you can read the key
        json.dump({k: s.get(k, v) for k, v in DEFAULTS.items()}, f, indent=2)
    os.replace(tmp, paths.SETTINGS)


def api_key():
    return os.environ.get("GMI_API_KEY") or load()["gmi_api_key"]


def base_url():
    return os.environ.get("GMI_BASE_URL") or BASE_URL


def voice_id(s=None):
    return VOICES[(s or load())["voice"]]["voice"]


def mask(key):
    return (key[:4] + "…" + key[-4:]) if len(key) > 12 else ("set" if key else "not set")


_load_dotenv()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Nemotron Agent settings")
    ap.add_argument("--key", help="your GMI Cloud API key (get one at %s)" % KEY_URL)
    ap.add_argument("--voice", choices=list(VOICES))
    ap.add_argument("--speak", choices=["on", "off"])
    ap.add_argument("--screenshots", choices=["on", "off"])
    a = ap.parse_args()
    s = load()
    if a.key is not None:
        s["gmi_api_key"] = a.key.strip()
    if a.voice:
        s["voice"] = a.voice
    if a.speak:
        s["speak"] = a.speak == "on"
    if a.screenshots:
        s["save_screenshots"] = a.screenshots == "on"
    if len(sys.argv) > 1:
        save(s)
    print(f"settings file: {paths.SETTINGS}")
    print(f"GMI API key:   {mask(api_key())}" + ("  (from environment)" if os.environ.get("GMI_API_KEY") else ""))
    print(f"voice:         {VOICES[s['voice']]['label']}")
    print(f"speak steps:   {'on' if s['speak'] else 'off'}")
    print(f"screenshots:   {'kept' if s['save_screenshots'] else 'not kept'}")
