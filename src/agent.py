"""Nemotron Mac agent: screenshot -> one action -> act -> repeat.

  python agent.py "open Calculator and compute 17*23"
  python agent.py --dry-run "open Safari"
  python agent.py --eval [--only 1 2]      # run evals/tasks.yaml

The menu bar app (app.py) uses the same Agent class.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import tomllib

import pyautogui

import actions
import memory
import screen
import paths
import settings
from logbook import Session
from model import chat, parse_json, parse_point

CONFIG = tomllib.load(open(paths.DEFAULT_CONFIG, "rb"))
SETTINGS = settings.load()   # the app calls apply_settings() after the Settings window is saved

SYSTEM = """You control a Mac by looking at a screenshot and choosing ONE action at a time.
Coordinates: your normalized 0-1000 grid of the whole screenshot; (0,0) is top-left, (1000,1000) is bottom-right.
The red ring in the screenshot shows the mouse position.

Reply with ONE JSON object:
{"thought": "<at most 12 words: what you see and why this action>",
 "action": "click" | "double_click" | "right_click" | "type" | "key" | "scroll" | "open_app" | "wait" | "done" | "fail" | "ask_human",
 "x": <0-1000>, "y": <0-1000>,      (click actions only: the center of the element)
 "target": "<name of the UI element>", (click actions and type only)
 "text": "<text to type>",           (type only; add \\n to press Return after it)
 "keys": "cmd+space",                (key only; combos joined with +, e.g. "cmd+l", "enter", "esc")
 "in_app": "Google Chrome",          (type and key only: the app that must receive these keys)
 "dy": -5,                           (scroll only; positive scrolls up, negative scrolls down)
 "app": "Safari",                    (open_app only: the exact app name)
 "risky": true | false,              (true when the action sends, deletes, buys, submits, or posts)
 "say": "<3 to 7 words spoken to the user about this step, e.g. 'Opening Safari' or 'Typing the address'>"}

Rules:
- To open or switch to an app, use open_app with its name. Do not click Dock icons, except to pick a Chrome profile (right-click the Google Chrome Dock icon, then click the profile name).
- Prefer keyboard shortcuts over clicking when they do the same thing (cmd+l for the address bar, cmd+t for a new tab, cmd+n for a new window or document).
- To enter text or numbers, type them with the keyboard instead of clicking on-screen keys or buttons (for a calculator, type the whole expression, e.g. "12*3=").
- Keys and typing go to the FRONT app named in the message, even when another app's window is visible. If the front app is not the one you need, use open_app first.
- Before typing, make sure the right field is focused (click it first if needed).
- If a click gave a different result than you expected, switch to the keyboard or another approach instead of clicking again.
- If the last action did not change the screen, try a different approach, not the same click.
- When the task is complete and the screen shows the result, return "done".
- If the task is impossible, return "fail" and say why in "thought".
- If you need information only the user has, return "ask_human" and put the question in "thought".
- Never type passwords. Use "ask_human" instead."""


def wait_front(app, secs=3.0):
    """Poll until `app` is the front app (open -a can take a moment). Returns the front app at the end."""
    end = time.time() + secs
    front = screen.frontmost_app()
    while not actions.same_app(front, app) and time.time() < end:
        time.sleep(0.3)
        front = screen.frontmost_app()
    return front


class Stopped(Exception):
    """Raised inside the loop the moment the user presses Esc."""


class Hooks:
    """Callbacks the agent uses to talk to its host (terminal or menu bar app)."""

    def status(self, state, text):  # state: working | confirm | done | fail | stopped
        print(text, flush=True)

    def confirm(self, question):
        return input(f"\n{question} [y/N] ").strip().lower() in ("y", "yes")

    def ask(self, question):
        ans = input(f"\nThe agent asks: {question}\nYour answer (empty = stop): ").strip()
        return ans or None


_speaker = None


def speaker():
    """Shared TTS speaker (Inworld via GMI). The app reads its `level` to animate the orb."""
    global _speaker
    if _speaker is None:
        from tts import Speaker
        _speaker = Speaker(CONFIG.get("tts_engine", "inworld-tts-2-flash"), settings.voice_id(SETTINGS))
    return _speaker


def apply_settings(new=None):
    """Use new settings at once (talk key, voice, speak, screenshots). The API key is read on every call."""
    SETTINGS.update(new or settings.load())
    if _speaker is not None:
        _speaker.voice = settings.voice_id(SETTINGS)
        _speaker.cache.clear()  # cached phrases were spoken in the old voice


def say(text, interrupt=False):
    if SETTINGS.get("speak") and text:
        speaker().say(text, interrupt=interrupt)


class Agent:
    def __init__(self, hooks=None, dry_run=None, max_steps=None, use_memory=None):
        self.hooks = hooks or Hooks()
        self.use_memory = CONFIG.get("memory", True) if use_memory is None else use_memory
        self.dry_run = CONFIG["dry_run"] if dry_run is None else dry_run
        self.max_steps = max_steps or CONFIG["max_steps"]
        self.stop_event = threading.Event()

    def stop(self):
        """Esc: stop at once. In-flight model calls are abandoned, typing and speech stop mid-word."""
        self.stop_event.set()
        if _speaker is not None:
            _speaker.stop_now()

    def call(self, fn, *args, **kwargs):
        """Run a model call in a worker thread so Esc does not have to wait for the reply."""
        box = {}

        def work():
            try:
                box["r"] = fn(*args, **kwargs)
            except Exception as e:
                box["e"] = e
        th = threading.Thread(target=work, daemon=True)
        th.start()
        while th.is_alive():
            if self.stop_event.wait(0.03):
                raise Stopped()
        if "e" in box:
            raise box["e"]
        return box["r"]

    def run(self, task, source="typed", audio_path=None):
        self.stop_event.clear()
        s = Session(task, source)
        if audio_path and os.path.exists(audio_path):
            os.replace(audio_path, s.path("audio.wav"))
        self.session = s
        rows = []                        # every logged step, for memory.learn at the end

        def log(row, record=None):
            rows.append(row)
            s.log_step(row, record)

        protected = CONFIG.get("protected_apps", [])
        mem_text, mem_ids = memory.recall(task, screen.frontmost_app()) if self.use_memory else ("", [])
        system = SYSTEM + ("\n\n" + mem_text if mem_text else "")
        with open(s.path("memory.txt"), "w") as f:
            f.write(mem_text or "(no memory recalled)")
        screen_pts = screen.points_size()
        history, blocked, fails = [], [], {}
        done_sigs = []                   # signatures of executed actions, in order
        notes = []                       # human answers to ask_human
        prev_img, prev_sig = None, None
        next_img = None                  # settled screenshot taken right after the last action
        no_change_run, bad_json_run, blocked_run = 0, 0, 0
        result, reason = "fail", "max steps reached"
        say("Got it.")
        self.hooks.status("working", f"Task: {task}" + ("  (dry run)" if self.dry_run else ""))
        try:
            for step in range(1, self.max_steps + 1):
                if self.stop_event.is_set():
                    result, reason = "stopped", "stopped by user"
                    break
                raw_path = s.path(f"step-{step:02d}.png")
                if next_img is not None:
                    img = next_img
                    img.save(raw_path)
                else:
                    img = screen.capture(raw_path)
                next_img = None
                changed = screen.changed(prev_img, img) if prev_img is not None else None
                if prev_sig is not None:
                    history[-1]["changed"] = changed
                    if changed is False and not self.dry_run:
                        no_change_run += 1
                        fails[prev_sig] = fails.get(prev_sig, 0) + 1
                        if fails[prev_sig] >= 2 and prev_sig not in blocked:
                            blocked.append(prev_sig)
                    else:
                        no_change_run = 0
                prev_img = img

                mouse = pyautogui.position()
                model_img = screen.for_model(img, CONFIG.get("model_image_width", 1512), (mouse.x, mouse.y), screen_pts)
                thinking = (no_change_run >= CONFIG["think_after_no_change"]
                            or bad_json_run >= CONFIG["think_after_bad_json"]
                            or blocked_run >= 2)   # stuck asking for a blocked action: think it over
                front = screen.frontmost_app()
                prompt = self.user_prompt(task, history, blocked, notes, changed, mouse, screen_pts, front)
                self.hooks.status("working", "Thinking it over…" if thinking else "Looking at the screen…")
                try:
                    r = self.call(chat, [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                                  image=model_img, thinking=thinking, response_format={"type": "json_object"},
                                  image_format=CONFIG.get("model_image_format", "JPEG"))
                except RuntimeError as e:
                    log({"step": step, "error": str(e), "label": "model error"})
                    result, reason = "fail", f"model error: {e}"
                    break
                a = parse_json(r["content"])
                record = {"prompt": prompt, "reply": r["content"], "reasoning": r["reasoning"],
                          "finish_reason": r["finish_reason"], "usage": r["usage"], "thinking": thinking}
                base = {"step": step, "model_secs": r["secs"], "thinking": thinking,
                        "prompt_tokens": r["usage"].get("prompt_tokens"),
                        "completion_tokens": r["usage"].get("completion_tokens"),
                        "finish_reason": r["finish_reason"], "screen_changed_before": changed, "front_app": front}
                if a is None:
                    bad_json_run += 1
                    log({**base, "error": f"invalid JSON: {r['content'][:200]!r}", "label": "invalid reply"}, record)
                    prev_sig = None
                    continue
                a = actions.normalize(a)
                err = actions.validate(a)
                if err:
                    bad_json_run += 1
                    history.append({"line": f"invalid action ({err})", "changed": None})
                    log({**base, "action": a, "error": err, "label": "invalid action"}, record)
                    prev_sig = None
                    continue
                bad_json_run = 0
                if a["action"] in actions.CLICKS and CONFIG.get("refine_clicks", True):
                    record["refine"] = self.refine(img, a)
                label = screen.describe(a)
                img_name = f"step-{step:02d}.jpg"
                screen.annotate(img, a, s.path(img_name))
                row = {**base, "action": a, "label": label, "image": img_name}

                t = a["action"]
                if t == "done":
                    log(row, record)
                    result, reason = "done", a.get("thought", "")
                    break
                if t == "fail":
                    log(row, record)
                    result, reason = "fail", a.get("thought", "")
                    break
                if t == "ask_human" or actions.is_password_typing(a):
                    q = a.get("thought") or "I need your help."
                    say("I have a question for you.", interrupt=True)
                    self.hooks.status("confirm", f"Question: {q}")
                    ans = self.hooks.ask(q)
                    row["human_answer"] = ans
                    log(row, record)
                    if not ans:
                        result, reason = "stopped", "user gave no answer"
                        break
                    notes.append(f"Q: {q} A: {ans}")
                    history.append({"line": f"asked the user: {q}", "changed": None})
                    prev_sig = None
                    continue

                sig = actions.signature(a)
                third_in_a_row = (len(done_sigs) >= 2 and actions.same(sig, done_sigs[-2:-1])
                                  and actions.same(sig, done_sigs[-1:]))   # it already took effect: move on
                if third_in_a_row and not actions.same(sig, blocked):
                    blocked_run += 1
                    row["error"] = "blocked: repeated three times in a row"
                    history.append({"line": f"{label} -> BLOCKED: you already did this twice and it took effect. Do the NEXT step of the task now.", "changed": None})
                    log(row, record)
                    prev_sig = None
                    continue
                if actions.same(sig, blocked):
                    blocked_run += 1
                    row["error"] = "blocked: already tried this without success"
                    history.append({"line": f"{label} -> BLOCKED (already tried; it did not change the screen)", "changed": None})
                    log(row, record)
                    prev_sig = None
                    continue
                blocked_run = 0

                # Guard: keys and typing go to the front app. Never let them land in the user's terminal.
                guard = actions.protected_front(a, front, task, protected)
                kind = "protected_front_app"
                if not guard:
                    guard, kind = actions.wrong_app(a, front), "wrong_front_app"
                if guard:
                    row.update(error=f"blocked: {guard}", guard=kind)
                    history.append({"line": f"{label} -> BLOCKED: {guard}. Use open_app to bring the right app to the front first.", "changed": None})
                    log(row, record)
                    prev_sig = None
                    continue

                # open_app: use the real installed name ("Chrome" -> "Google Chrome"); report a missing app instead of failing silently.
                if t == "open_app":
                    real, close = actions.resolve_app(a["app"])
                    if real is None:
                        msg = f"no app named {a['app']!r} is installed" + (f"; close names: {', '.join(close)}" if close else "")
                        row.update(error=f"open failed: {msg}", open_error=msg, app_found=False)
                        history.append({"line": f"{label} -> FAILED: {msg}", "changed": None})
                        log(row, record)
                        prev_sig = None
                        continue
                    if real != a["app"]:
                        row["app_resolved"] = real
                        a["app"] = real
                        label = screen.describe(a)
                        row["label"] = label

                why = actions.risky_reason(a, CONFIG["risky_words"])
                if why and not self.dry_run:
                    say("I need your OK for this one.", interrupt=True)
                    self.hooks.status("confirm", f"Confirm: {label}?")
                    ok = self.hooks.confirm(f"The agent wants to: {label}\n\n{a.get('thought', '')}\n\nAsked because {why}. Allow it?")
                    row["confirmed"] = ok
                    if not ok:
                        log(row, record)
                        result, reason = "stopped", f"user declined: {label}"
                        break
                if self.stop_event.is_set():
                    log(row, record)
                    result, reason = "stopped", "stopped by user"
                    break

                self.hooks.status("working", a.get("say") or label)
                say(a.get("say"))
                print(f"[{step:02d}] {r['secs']:.1f}s{' T' if thinking else ''}  {label}  | {a.get('thought', '')}", flush=True)
                row["point"] = actions.execute(a, screen_pts, self.dry_run, self.stop_event)
                done_sigs.append(sig)
                log(row, record)
                line = label
                if self.stop_event.is_set():
                    raise Stopped()
                if not self.dry_run:  # move on as soon as the screen stops changing
                    next_img = screen.settle(s.path("settle.png"), self.stop_event,
                                             max_wait=4.0 if t == "open_app" else CONFIG.get("settle_max", 1.5))
                    if next_img is None:
                        raise Stopped()
                if t == "open_app" and not self.dry_run:
                    now_front = wait_front(a["app"])
                    if not actions.same_app(now_front, a["app"]):
                        line += f" — but {now_front or 'another app'} is still in front"
                        row["front_after"] = now_front
                history.append({"line": line, "changed": None})
                prev_sig = sig
        except Stopped:
            result, reason = "stopped", "stopped by user (Esc)"
        except pyautogui.FailSafeException:
            result, reason = "stopped", "mouse moved to a screen corner (failsafe)"
        except Exception as e:  # keep a report even when something breaks
            result, reason = "fail", f"{type(e).__name__}: {e}"
        try:
            final = screen.capture(s.path("final.png"))
            final.resize((1512, round(final.height * 1512 / final.width))).save(s.path("final.jpg"), quality=85)
        except Exception:
            pass
        if not SETTINGS.get("save_screenshots"):
            s.drop_media()  # keep only the text: steps, reasons, model replies
        summary = s.finish(result, reason)
        # Learn in the background: faults and recipes are instant, the model reflection takes a few seconds.
        if not self.dry_run and self.use_memory:  # a dry run never acts, so it proves nothing about the Mac
            threading.Thread(target=memory.learn, args=(task, result, reason, rows, summary["dir"]),
                             kwargs={"used_ids": mem_ids, "protected_apps": protected, "reflect": CONFIG.get("reflect", True)}).start()
        if result != "stopped":  # on Esc stay silent: the user wants it to stop, not to talk
            say("Done." if result == "done" else "Sorry, I could not finish.", interrupt=True)
        self.hooks.status(result, f"{result.upper()}: {reason}"[:200])
        return summary

    def refine(self, img, a):
        """Zoom check: crop around the planned click at full resolution and ask again for the exact point."""
        W, H = img.size
        cw, ch = W * 0.24, H * 0.24
        cx, cy = a["x"] / 1000 * W, a["y"] / 1000 * H
        x0, y0 = min(max(cx - cw / 2, 0), W - cw), min(max(cy - ch / 2, 0), H - ch)
        crop = img.crop((round(x0), round(y0), round(x0 + cw), round(y0 + ch)))
        q = (f"This is a zoomed-in part of a Mac screen.\nFind: {a.get('target') or 'the element to click'}\n"
             "Return its center as {\"x\": <0-1000>, \"y\": <0-1000>} on your normalized grid of THIS image. "
             "If it is not in this image, return {\"x\": -1, \"y\": -1}. Reply with ONE JSON object only.")
        try:
            r = self.call(chat, [{"role": "user", "content": q}], image=crop, max_tokens=200,
                     response_format={"type": "json_object"}, retries=2)
        except RuntimeError as e:
            return {"error": str(e)}
        p = parse_point(r["content"])
        info = {"reply": r["content"], "secs": r["secs"], "before": [a["x"], a["y"]]}
        if p and 0 <= p[0] <= 1000 and 0 <= p[1] <= 1000:
            nx = round((x0 + p[0] / 1000 * cw) / W * 1000, 1)
            ny = round((y0 + p[1] / 1000 * ch) / H * 1000, 1)
            # Only a nudge is trusted. In a row of similar buttons the crop can pick a neighbor
            # (Calculator: "1" moved onto "2"); the Dock fix it is meant for was under 10 units.
            if abs(nx - a["x"]) <= 15 and abs(ny - a["y"]) <= 15:
                a["x"], a["y"] = nx, ny
            else:
                info["rejected"] = [nx, ny]
        info["after"] = [a["x"], a["y"]]
        return info

    def user_prompt(self, task, history, blocked, notes, changed, mouse, screen_pts, front=None):
        lines = [f"TASK: {task}", ""]
        if notes:
            lines += ["ANSWERS FROM THE USER:"] + notes + [""]
        recent = history[-CONFIG["history_steps"]:]
        if recent:
            lines.append("STEPS SO FAR (oldest first):")
            start = len(history) - len(recent) + 1
            for i, h in enumerate(recent, start):
                c = "" if h["changed"] is None else ("  [screen changed]" if h["changed"] else "  [screen did NOT change]")
                lines.append(f"{i}. {h['line']}{c}")
        else:
            lines.append("STEPS SO FAR: none, this is the first step.")
        if changed is False:
            lines.append("\nThe last action did NOT change the screen. Try a different approach.")
        if blocked:
            lines.append("\nBLOCKED (tried already without success; choose a different approach, e.g. a keyboard key, double_click, or open_app): " +
                         "; ".join(f"{b[0]} {b[1]}" for b in blocked))
        mx, my = round(mouse.x / screen_pts[0] * 1000), round(mouse.y / screen_pts[1] * 1000)
        lines.append(f"\nFront app (receives keys and typing): {front or 'unknown'}")
        lines.append(f"The mouse is at ({mx},{my}) on the grid. Return the next ONE action as JSON.")
        return "\n".join(lines)


DEFAULT_SETUP = "osascript -e 'tell app \"Terminal\" to activate'"   # the trap from the failed Bayhouse run


def run_evals(only=None, dry_run=False, suite="tasks", use_memory=None, tag=""):
    """Run a task file. A task passes when its `check` command exits 0 (or, without a check, when the model says done)."""
    import yaml
    tasks = yaml.safe_load(open(os.path.join(paths.ROOT, "evals", f"{suite}.yaml")))
    out = os.path.join(paths.DATA, f"eval-results-{suite}.jsonl")
    agent = Agent(dry_run=dry_run, use_memory=use_memory)
    mem = "on" if agent.use_memory else "off"
    passed = 0
    for t in tasks:
        if only and t["id"] not in only:
            continue
        print(f"\n=== eval {t['id']} (memory {mem}): {t['task']}", flush=True)
        subprocess.run(t.get("setup", DEFAULT_SETUP if suite != "tasks" else "true"), shell=True)
        time.sleep(1)
        s = agent.run(t["task"], source="eval")
        if t.get("check"):
            time.sleep(1)
            ok = subprocess.run(t["check"], shell=True, capture_output=True).returncode == 0
        else:
            ok = s["result"] == "done"
        passed += ok
        print(f"--> {'PASS' if ok else 'FAIL'}  (model said {s['result']}, {s['steps']} steps, {s['secs']} s)", flush=True)
        with open(out, "a") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "tag": tag, "memory": mem, "id": t["id"],
                                "task": t["task"], "pass": ok, "model_result": s["result"], "steps": s["steps"],
                                "secs": s["secs"], "dir": s["dir"]}) + "\n")
        if t.get("cleanup"):
            subprocess.run(t["cleanup"], shell=True)
            time.sleep(1)
        if not dry_run and agent.use_memory:
            time.sleep(8)  # let the background memory reflection finish before the next task recalls
    print(f"\nscore: {passed}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-steps", type=int)
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--only", nargs="*", type=int)
    ap.add_argument("--suite", default="tasks", help="task file in evals/ (tasks or suite10)")
    ap.add_argument("--no-memory", action="store_true", help="no recall and no learning")
    ap.add_argument("--tag", default="", help="label stored with each eval result")
    a = ap.parse_args()
    if a.eval:
        run_evals(a.only, a.dry_run, a.suite, False if a.no_memory else None, a.tag)
        sys.exit(0)
    if not a.task:
        ap.error("give a task, e.g. python agent.py \"open Calculator\"")
    if memory.is_memory_command(" ".join(a.task)):
        ops, said = memory.remember(" ".join(a.task))
        print(said, json.dumps(ops, ensure_ascii=False))
        sys.exit(0)
    summary = Agent(dry_run=a.dry_run or None, max_steps=a.max_steps,
                    use_memory=False if a.no_memory else None).run(" ".join(a.task))
    print(json.dumps(summary, indent=2))
    print("report:", os.path.join(paths.SESSIONS, summary["dir"], "report.html"))
