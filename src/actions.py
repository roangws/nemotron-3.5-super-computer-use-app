"""Action schema, validation, safety checks and the pyautogui executor."""
import difflib
import glob
import os
import re
import subprocess
import time

import pyautogui

pyautogui.FAILSAFE = True   # slam the mouse into a screen corner to stop
pyautogui.PAUSE = 0.05

CLICKS = ("click", "double_click", "right_click")
ACTIONS = CLICKS + ("type", "key", "scroll", "open_app", "wait", "done", "fail", "ask_human")

KEY_ALIASES = {
    "cmd": "command", "⌘": "command", "meta": "command", "super": "command", "win": "command",
    "opt": "option", "alt": "option", "⌥": "option",
    "control": "ctrl", "⌃": "ctrl",
    "return": "enter", "escape": "esc", "del": "delete", "bksp": "backspace",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "spacebar": "space", "pgup": "pageup", "pgdn": "pagedown",
}


def normalize(a):
    """Coerce common model slips into the schema. Returns a new dict."""
    a = dict(a)
    a["action"] = str(a.get("action", "")).strip().lower().replace("-", "_").replace(" ", "_")
    if a["action"] in ("doubleclick",):
        a["action"] = "double_click"
    if a["action"] in ("rightclick",):
        a["action"] = "right_click"
    if a["action"] in ("hotkey", "press", "keypress"):
        a["action"] = "key"
    for k in ("x", "y"):
        if k in a:
            try:
                a[k] = float(a[k])
            except (TypeError, ValueError):
                pass
    if a["action"] == "scroll":
        try:
            a["dy"] = int(float(a.get("dy", -5)))
        except (TypeError, ValueError):
            a["dy"] = -5
    a["risky"] = bool(a.get("risky", False))
    return a


def validate(a):
    """Returns an error string, or None when the action is valid."""
    t = a.get("action")
    if t not in ACTIONS:
        return f"unknown action {t!r}"
    if t in CLICKS:
        x, y = a.get("x"), a.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return "click needs numeric x and y"
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            return "x and y must be on the 0-1000 grid"
    if t == "type" and not isinstance(a.get("text"), str):
        return "type needs text"
    if t == "key" and not a.get("keys"):
        return "key needs keys"
    if t == "open_app":
        app = a.get("app")
        if not isinstance(app, str) or not re.fullmatch(r"[\w .&'+-]{1,60}", app):
            return "open_app needs a plain app name"
    return None


def risky_reason(a, words):
    """Why this action needs a human yes, or None."""
    if a.get("action") in ("done", "fail", "ask_human", "wait", "scroll"):
        return None
    if a.get("risky"):
        return "the model marked it risky"
    # Only what will be clicked or typed. The thought often mentions later steps ("...then delete it"),
    # which made harmless keys like Esc ask for confirmation.
    hay = " ".join(str(a.get(k, "")) for k in ("target", "text")).lower()
    for w in words:
        if re.search(r"\b" + re.escape(w) + r"\b", hay):
            return f'it mentions "{w}"'
    return None


def is_password_typing(a):
    return a.get("action") == "type" and "password" in (str(a.get("target", "")) + str(a.get("thought", ""))).lower()


def signature(a):
    """Identity of an action for repeat-blocking: same kind and same target or same spot."""
    t = a.get("action")
    if t in CLICKS:
        return (t, str(a.get("target", "")).strip().lower(), int(a["x"] // 40), int(a["y"] // 40))
    return (t, str(a.get("text") or a.get("keys") or a.get("app") or a.get("dy") or ""))


def same(sig, blocked):
    for b in blocked:
        if sig[0] != b[0]:
            continue
        if sig[0] in CLICKS and (sig[1] and sig[1] == b[1] or sig[2:] == b[2:]):
            return True
        if sig == b:
            return True
    return False


# Names people (and the model) use for apps whose real name is different.
APP_ALIASES = {
    "chrome": "Google Chrome", "google": "Google Chrome", "browser": "Safari",
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code", "code": "Visual Studio Code",
    "settings": "System Settings", "system preferences": "System Settings", "preferences": "System Settings",
    "word": "Microsoft Word", "excel": "Microsoft Excel", "powerpoint": "Microsoft PowerPoint", "outlook": "Microsoft Outlook",
    "teams": "Microsoft Teams", "photoshop": "Adobe Photoshop", "finder": "Finder", "mail": "Mail",
}
APP_DIRS = ["/Applications", "/Applications/Utilities", "/System/Applications", "/System/Applications/Utilities",
            os.path.expanduser("~/Applications"), "/System/Library/CoreServices"]

# Keys that work no matter which app is in front, so they are allowed even when a protected app is in front.
GLOBAL_KEYS = {"command+space", "command+tab", "command+shift+3", "command+shift+4", "command+shift+5", "ctrl+up", "ctrl+down"}


def installed_apps():
    names = set()
    for d in APP_DIRS:
        for p in glob.glob(os.path.join(d, "*.app")):
            names.add(os.path.basename(p)[:-4])
    return names


def resolve_app(name, apps=None):
    """Real app name for what the model asked, or (None, close matches). 'Chrome' -> 'Google Chrome'."""
    apps = installed_apps() if apps is None else set(apps)
    want = str(name).strip()
    low = {a.lower(): a for a in apps}
    if want.lower() in low:
        return low[want.lower()], []
    alias = APP_ALIASES.get(want.lower())
    if alias and alias.lower() in low:
        return low[alias.lower()], []
    contains = sorted(a for a in apps if want.lower() in a.lower())
    if len(contains) == 1:
        return contains[0], []
    close = contains or difflib.get_close_matches(want, sorted(apps), n=3, cutoff=0.6)
    return None, close[:3]


def key_combo(a):
    return "+".join(KEY_ALIASES.get(k.strip().lower(), k.strip().lower()) for k in re.split(r"\+", str(a.get("keys", ""))) if k.strip())


# App bundle name -> process name, where they differ (System Events reports the process name).
PROCESS_NAMES = {"visual studio code": "code", "system settings": "system settings", "iterm": "iterm2"}


def same_app(a, b):
    """True when two app names mean the same app ('Chrome' ~ 'Google Chrome', 'Visual Studio Code' ~ 'Code')."""
    if not a or not b:
        return False
    def norm(n):
        n = str(n).strip().lower()
        n = APP_ALIASES.get(n, n).lower()
        return PROCESS_NAMES.get(n, n)
    x, y = norm(a), norm(b)
    return x == y or x in y or y in x


def wrong_app(a, front_app):
    """Reason to block typing/keys when the model says they are meant for another app than the one in front."""
    want = a.get("in_app")
    if a.get("action") not in ("type", "key") or not want or not front_app:
        return None
    if a["action"] == "key" and key_combo(a) in GLOBAL_KEYS:
        return None
    if same_app(want, front_app):
        return None
    return f"these keys are meant for {want}, but {front_app} is in front"


def protected_front(a, front_app, task, protected_apps):
    """Reason to block typing/keys because they would land in a protected app (the user's terminal), or None."""
    if a.get("action") not in ("type", "key") or not front_app or front_app not in protected_apps:
        return None
    if front_app.lower() in str(task).lower():
        return None  # the task is about this app
    if a["action"] == "key" and key_combo(a) in GLOBAL_KEYS:
        return None
    return f"{front_app} is in front, so this would type into {front_app}"


def to_points(a, screen_pts):
    return round(a["x"] / 1000 * screen_pts[0]), round(a["y"] / 1000 * screen_pts[1])


def execute(a, screen_pts, dry_run=False, stop=None):
    """Perform one action. Returns the mapped (x, y) point for clicks, else None."""
    t = a["action"]
    pt = to_points(a, screen_pts) if t in CLICKS else None
    if dry_run or t in ("done", "fail", "ask_human"):
        return pt
    if t in CLICKS:
        pyautogui.moveTo(pt[0], pt[1], duration=0.35, tween=pyautogui.easeOutQuad)
        if t == "click":
            pyautogui.click()
        elif t == "double_click":
            pyautogui.doubleClick(interval=0.08)
        else:
            pyautogui.rightClick()
    elif t == "type":
        type_text(a["text"], stop)
    elif t == "key":
        keys = [KEY_ALIASES.get(k.strip().lower(), k.strip().lower()) for k in re.split(r"\+", str(a["keys"])) if k.strip()]
        pyautogui.hotkey(*keys, interval=0.06)
    elif t == "scroll":
        pyautogui.scroll(int(a.get("dy", -5)) * 3)
    elif t == "open_app":
        subprocess.run(["open", "-a", a["app"]], check=False, timeout=15)
        time.sleep(1.0)
    elif t == "wait":
        time.sleep(1.5)
    return pt


def type_text(text, stop=None):
    """Short plain ASCII is typed key by key (you can watch it); anything else is pasted. Stops mid-word on Esc."""
    if len(text) <= 200 and all(32 <= ord(c) < 127 or c == "\n" for c in text):
        for c in text:
            if stop is not None and stop.is_set():
                return
            pyautogui.write(c, interval=0)
            time.sleep(0.012)
    else:
        old = subprocess.run(["pbpaste"], capture_output=True).stdout
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        pyautogui.hotkey("command", "v", interval=0.05)
        time.sleep(0.3)
        subprocess.run(["pbcopy"], input=old, check=False)
