"""Where things live.

Code, default settings and art ship in the repo. Your own data lives outside it, so `git pull` never touches it:

  ~/Library/Application Support/Nemotron Agent/
    settings.json   API key, voice, switches (written by the Settings window)
    memory/         what the agent learned on this Mac
    sessions/       one folder per task (text report; screenshots only if you turn them on)

Set NEMO_HOME to use another folder.
"""
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
DEFAULT_CONFIG = os.path.join(ROOT, "config.toml")

DATA = os.path.expanduser(os.environ.get("NEMO_HOME", "~/Library/Application Support/Nemotron Agent"))
SETTINGS = os.path.join(DATA, "settings.json")
MEMORY = os.path.join(DATA, "memory")
SESSIONS = os.path.join(DATA, "sessions")
LOG = os.path.expanduser("~/Library/Logs/NemotronAgent.log")

os.makedirs(MEMORY, exist_ok=True)
os.makedirs(SESSIONS, exist_ok=True)

# First run: start memory with the shipped rules (you can edit your copy by hand).
if not os.path.exists(os.path.join(MEMORY, "rules.md")):
    shutil.copy(os.path.join(ROOT, "memory", "rules.md"), os.path.join(MEMORY, "rules.md"))
