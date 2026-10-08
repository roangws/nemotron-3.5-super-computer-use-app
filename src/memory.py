"""Long-term memory so the agent does not repeat its mistakes.

Design:
- `faults.jsonl` (code, what, fix) + a hand-edited `rules.md` carried into every call.
- Remember what failed so it is not retried.
- Facts are merged with ADD / UPDATE / DELETE / NOOP (as in Mem0) instead of piling up.
- Stable text first (rules, facts) so the server can reuse its prompt cache; small, relevant recall only.
- Lessons from bad runs can pollute later runs, so facts keep a win/loss count and get pruned.

Files in memory/ (under ~/Library/Application Support/Nemotron Agent/):
  rules.md        always loaded; edit by hand
  faults.jsonl    one line per detected fault (append only, written by code, not by the model)
  facts.jsonl     current per-app facts (rewritten on every change)
  recipes.jsonl   action sequences of tasks that finished "done"

  python memory.py --show               print what is stored
  python memory.py --recall "open chrome and search for x"
  python memory.py --backfill           learn from all past sessions (no model calls)
"""
import json
import os
import re
import threading
import time

import paths

DIR = paths.MEMORY
RULES = os.path.join(DIR, "rules.md")
FAULTS = os.path.join(DIR, "faults.jsonl")
FACTS = os.path.join(DIR, "facts.jsonl")
RECIPES = os.path.join(DIR, "recipes.jsonl")

# Fault codes detected by code from the step log. Each has a default fix the agent is told next time.
FIXES = {
    "APP_NOT_FOUND": "Use the exact app name from /Applications (for example 'Google Chrome', not 'Chrome').",
    "TYPED_INTO_WRONG_APP": "Check the front app before typing; use open_app and wait until that app is in front.",
    "REPEATED_NO_CHANGE": "When an action does not change the screen twice, switch approach (keyboard shortcut, open_app, or a different element).",
    "MAX_STEPS": "The task ran out of steps; plan shorter paths with keyboard shortcuts.",
    "USER_STOPPED": "The user stopped this task; it was going the wrong way.",
    "INVALID_REPLY": "Reply with exactly one JSON object.",
}
RECALL_CHARS = 2500          # cap on the memory block added to the system prompt
STOP = set("a an and the to of in on for with then it my me please can you hey open go".split())


# ---------------- storage ----------------

def _read(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _append(path, row):
    os.makedirs(DIR, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write(path, rows):
    os.makedirs(DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def words(text):
    return {w.strip(".") for w in re.findall(r"[a-z0-9.]+", str(text).lower()) if w.strip(".") not in STOP and len(w.strip(".")) > 1}


def similar(a, b):
    wa, wb = words(a), words(b)
    return len(wa & wb) / max(1, len(wa | wb))


# ---------------- facts (Mem0-style merge) ----------------

def load_facts():
    return _read(FACTS)


def apply_ops(facts, ops, source, pinned=False):
    """ops: [{"op": "ADD"|"UPDATE"|"DELETE"|"NOOP", "id": ..., "app": ..., "fact": ...}]. Returns new list."""
    facts = [dict(f) for f in facts]
    by_id = {f["id"]: f for f in facts}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for op in ops or []:
        kind = str(op.get("op", "")).upper()
        if kind == "ADD" and op.get("fact"):
            fact = str(op["fact"]).strip()[:240]
            dup = next((f for f in facts if similar(f["fact"], fact) > 0.8), None)
            if dup:
                continue
            new_id = f"f{max([int(f['id'][1:]) for f in facts if f['id'][1:].isdigit()] + [0]) + 1}"
            row = {"id": new_id, "app": str(op.get("app") or "general")[:60], "fact": fact, "source": source,
                   "created": now, "updated": now, "wins": 0, "losses": 0}
            if pinned:
                row["pinned"] = True  # told by the user: always recalled, never pruned or deleted by reflection
            facts.append(row)
            by_id[new_id] = row
        elif kind in ("UPDATE", "DELETE") and by_id.get(op.get("id"), {}).get("pinned") and not pinned:
            continue  # only the user can change what the user said
        elif kind == "UPDATE" and op.get("id") in by_id and op.get("fact"):
            by_id[op["id"]].update(fact=str(op["fact"]).strip()[:240], updated=now, source=source)
        elif kind == "DELETE" and op.get("id") in by_id:
            facts = [f for f in facts if f["id"] != op["id"]]
            by_id.pop(op["id"])
    return facts


def score_facts(facts, used_ids, result):
    """Facts shown in a session get a win if it finished, a loss if not. Facts that keep losing are dropped."""
    for f in facts:
        if f["id"] in used_ids:
            if result == "done":
                f["wins"] = f.get("wins", 0) + 1
            elif result in ("fail", "stopped"):
                f["losses"] = f.get("losses", 0) + 1
    return [f for f in facts if f.get("pinned") or not (f.get("losses", 0) >= 3 and f["losses"] > 2 * f.get("wins", 0))]


# ---------------- fault detection (code, not model) ----------------

def detect_faults(rows, result, reason, protected_apps=()):
    """rows: ledger rows of one session. Returns [{code, app, what}]. Deterministic."""
    faults = []
    seen = set()

    def add(code, app, what):
        if (code, app) not in seen:
            seen.add((code, app))
            faults.append({"code": code, "app": app or "", "what": what})

    for r in rows:
        a = r.get("action") or {}
        t = a.get("action")
        if r.get("open_error") or (t == "open_app" and r.get("app_found") is False):
            add("APP_NOT_FOUND", a.get("app"), f"open_app {a.get('app')!r} failed: {r.get('open_error') or 'no such app'}")
        front = r.get("front_app") or ""
        if t in ("type", "key") and (r.get("guard") in ("protected_front_app", "wrong_front_app") or (front in protected_apps and not r.get("guard"))):
            add("TYPED_INTO_WRONG_APP", front, f"{t} {a.get('text') or a.get('keys')!r} aimed at {a.get('target') or 'the task app'} but {front} was in front")
        if str(r.get("error", "")).startswith("blocked"):
            add("REPEATED_NO_CHANGE", front, f"{r.get('label')} was repeated without effect")
        if r.get("label") in ("invalid reply", "invalid action"):
            add("INVALID_REPLY", "", str(r.get("error", ""))[:120])
    if result == "fail" and "max steps" in str(reason):
        add("MAX_STEPS", "", f"ran out of steps after: {' / '.join(r.get('label', '?') for r in rows[-3:])}")
    if result == "stopped" and "user" in str(reason):
        add("USER_STOPPED", "", reason)
    return faults


# ---------------- recipes ----------------

def recipe_from(rows):
    steps = []
    for r in rows:
        a = r.get("action") or {}
        if r.get("error") or a.get("action") in (None, "done", "fail", "ask_human", "wait"):
            continue
        label = re.sub(r"\s*\([\d.]+,\s*[\d.]+\)$", "", r.get("label") or a.get("action")).replace("\n", "⏎")  # positions change, names do not
        if not steps or steps[-1] != label:
            steps.append(label)
    return steps


def cost(steps):
    return (len(steps), sum(s.startswith(("click", "double_click", "right_click")) for s in steps))


def save_recipe(task, rows, session):
    steps = recipe_from(rows)
    if len(steps) < 2:   # 0-1 steps means the screen already showed the answer: not a reusable path
        return
    typed = " ".join(str((r.get("action") or {}).get("text", "")) for r in rows)
    if any(n not in typed for n in re.findall(r"\d+", task)):
        return           # the task's numbers were never typed: the result was already on screen
    recipes = _read(RECIPES)
    for i, r in enumerate(recipes):
        if similar(r["task"], task) > 0.85:
            if cost(steps) < cost(r["steps"]):      # keep the shortest real path; on a tie, the one with fewer clicks
                recipes[i] = {"task": task, "steps": steps, "session": session}
                _write(RECIPES, recipes)
            return
    _append(RECIPES, {"task": task, "steps": steps, "session": session})


# ---------------- recall ----------------

def apps_in(text, facts):
    t = str(text).lower()
    return {f["app"] for f in facts if f["app"] != "general" and (f["app"].lower() in t or f["app"].lower().split()[-1] in t)}


def recall(task, front_app=""):
    """Returns (text block for the system prompt, ids of facts used)."""
    parts, used = [], []
    if os.path.exists(RULES):
        parts.append("RULES FROM MEMORY (learned from past runs):\n" + open(RULES).read().strip())
    facts = load_facts()
    told = [f for f in facts if f.get("pinned")]
    if told:
        parts.append("WHAT THE USER TOLD YOU (always follow these):\n" + "\n".join(f"- {f['fact']}" for f in told[-15:]))
        used += [f["id"] for f in told[-15:]]
    facts = [f for f in facts if not f.get("pinned")]
    apps = apps_in(task, facts) | ({front_app} if front_app else set())
    rel = [f for f in facts if f["app"] in apps or f["app"] == "general" or similar(f["fact"], task) > 0.25]
    if rel:
        rel.sort(key=lambda f: (f.get("wins", 0) - f.get("losses", 0)), reverse=True)
        parts.append("FACTS ABOUT THIS MAC:\n" + "\n".join(f"- [{f['app']}] {f['fact']}" for f in rel[:12]))
        used += [f["id"] for f in rel[:12]]
    faults = _read(FAULTS)
    if faults:
        tw = words(task)
        hits, seen = [], set()
        for f in reversed(faults):
            key = (f["code"], f.get("app", ""))
            if key in seen or f["code"] in ("USER_STOPPED", "INVALID_REPLY"):
                continue
            if words(f.get("task", "")) & tw or (f.get("app") and f["app"] in apps):
                seen.add(key)
                hits.append(f"- {f['code']}: {f['what']} -> fix: {f.get('fix') or FIXES.get(f['code'], '')}")
        if hits:
            parts.append("MISTAKES MADE BEFORE ON SIMILAR TASKS (do not repeat):\n" + "\n".join(hits[:5]))
    recipes = [(similar(r["task"], task), r) for r in _read(RECIPES)]
    top = max((sim for sim, _ in recipes), default=0)
    near = [r for sim, r in recipes if sim >= 0.3 and sim >= top - 0.1]   # equally good matches: take the cheapest path
    best = min(near, key=lambda r: cost(r["steps"]), default=None)
    if best:
        parts.append(f"A SIMILAR TASK THAT WORKED (\"{best['task']}\"):\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(best["steps"], 1)))
    text = "\n\n".join(parts)
    if len(text) > RECALL_CHARS:
        text = text[:RECALL_CHARS].rsplit("\n", 1)[0] + "\n…"
    return text, used


# ---------------- learning after a session ----------------

REFLECT = """You review one run of a Mac automation agent and update its memory of facts about this Mac.
Facts are short, reusable and specific (app names, shortcuts that worked, where things are, what failed and what to do instead).
Use ops: ADD a new fact, UPDATE an existing fact by id when it is wrong or incomplete, DELETE a fact by id that this run proved wrong.
Only add facts this run actually shows. Write facts about the Mac and its apps (names, shortcuts, where things are),
not about how this run ended. Never store screen coordinates (windows move). Never store obvious facts such as "X can be launched with open_app" or "typing works when the app is in front"; store only what would have saved steps or avoided a mistake in this run. A run that stopped early or hit its step limit proves nothing about what it did not try. Max 3 ops. If nothing new was learned, return {"ops": []}.
Reply with ONE JSON object: {"ops": [{"op": "ADD"|"UPDATE"|"DELETE", "id": "<for UPDATE/DELETE>", "app": "<app name or general>", "fact": "<one sentence>"}]}"""


def reflect_ops(task, result, reason, rows, facts):
    from model import chat, parse_json
    log = "\n".join(f"{r.get('step')}. {r.get('label') or r.get('error')}"
                    + (f" [front app: {r['front_app']}]" if r.get("front_app") else "")
                    + (f" [screen changed: {r['screen_changed_before']}]" if r.get("screen_changed_before") is not None else "")
                    + (f" ERROR: {r['error']}" if r.get("error") else "")
                    + (f" OPEN FAILED: {r['open_error']}" if r.get("open_error") else "")
                    for r in rows)
    known = "\n".join(f"{f['id']} [{f['app']}] {f['fact']}" for f in facts) or "(none)"
    msg = f"TASK: {task}\nRESULT: {result} ({reason})\n\nSTEPS:\n{log}\n\nKNOWN FACTS:\n{known}"
    r = chat([{"role": "system", "content": REFLECT}, {"role": "user", "content": msg}],
             thinking=True, max_tokens=8000, timeout=120, retries=2, response_format={"type": "json_object"})
    out = parse_json(r["content"]) or {}
    return out.get("ops", [])


_lock = threading.Lock()


def learn(task, result, reason, rows, session, used_ids=(), protected_apps=(), reflect=True):
    """Write faults, recipes and fact scores now; run the model reflection (slow) unless reflect=False."""
    with _lock:
        for f in detect_faults(rows, result, reason, protected_apps):
            _append(FAULTS, {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "session": session, "task": task,
                             **f, "fix": FIXES.get(f["code"], "")})
        if result == "done":
            save_recipe(task, rows, session)
        _write(FACTS, score_facts(load_facts(), set(used_ids), result))
    if reflect:
        try:
            ops = reflect_ops(task, result, reason, rows, load_facts())
            with _lock:
                _write(FACTS, apply_ops(load_facts(), ops, session))
            return ops
        except Exception as e:  # memory must never break the agent
            print(f"[memory] reflection skipped: {e}", flush=True)
    return []


# ---------------- "save my memory": facts told by the user ----------------

MEMORY_COMMAND = re.compile(
    r"\b(save|update|add)\b.{0,15}\b(my|your|the|to)\s+memory\b|\bremember\s+(that|this)\b|^\W*remember\b|"
    r"\bforget\s+(that|about|this)\b|\b(save|store)\s+(this|that)\b.{0,12}\bmemory\b", re.I)


def is_memory_command(text):
    """'My default browser is Chrome, save my memory' -> True. These are stored, never executed."""
    return bool(MEMORY_COMMAND.search(str(text)))


REMEMBER = """The user is telling their Mac assistant something to remember (or forget). Turn it into memory operations.
Facts are short, standalone sentences about the user, their preferences, or their Mac (for example "The user's default browser is Google Chrome.").
Use ops: ADD a new fact; UPDATE an existing fact by id when the user corrects it; DELETE a fact by id when the user asks to forget it.
Ignore the command words themselves ("save my memory"). Max 4 ops.
Reply with ONE JSON object: {"ops": [{"op": "ADD"|"UPDATE"|"DELETE", "id": "<for UPDATE/DELETE>", "app": "<app name or general>", "fact": "<one sentence>"}],
 "say": "<one short sentence confirming what you saved, e.g. 'Got it, Chrome is your default browser.'>"}"""


def remember(text):
    """Store what the user said (no task is run). Returns (ops, sentence to speak)."""
    from model import chat, parse_json
    facts = load_facts()
    known = "\n".join(f"{f['id']} [{f['app']}] {f['fact']}" for f in facts) or "(none)"
    r = chat([{"role": "system", "content": REMEMBER},
              {"role": "user", "content": f"KNOWN FACTS:\n{known}\n\nTHE USER SAID: {text}"}],
             max_tokens=600, timeout=60, response_format={"type": "json_object"})
    out = parse_json(r["content"]) or {}
    ops = out.get("ops", [])
    with _lock:
        _write(FACTS, apply_ops(load_facts(), ops, "user", pinned=True))
    return ops, out.get("say") or ("Saved to memory." if ops else "I did not find anything to save.")


# ---------------- backfill from old sessions ----------------

def backfill(protected_apps=("Terminal", "iTerm2", "Ghostty", "Warp")):
    """Learn faults and recipes from sessions/ (no model calls). Old ledgers have no front_app, so read it from the prompt."""
    import actions
    root = paths.SESSIONS
    index = {r["dir"]: r for r in _read(os.path.join(root, "index.jsonl"))}
    n = 0
    for d, meta in sorted(index.items()):
        rows = _read(os.path.join(root, d, "ledger.jsonl"))
        for r in rows:
            rec = os.path.join(root, d, "model", f"step-{r.get('step', 0):02d}.json")
            if os.path.exists(rec) and not r.get("front_app"):
                m = re.search(r"Front app \(receives keys and typing\): (.+)", json.load(open(rec)).get("prompt", ""))
                if m:
                    r["front_app"] = m.group(1).strip()
            a = r.get("action") or {}
            if a.get("action") == "open_app":
                found, _ = actions.resolve_app(a.get("app", ""))
                if found is None or found.lower() != str(a.get("app")).lower():
                    r["open_error"] = f"no app named {a.get('app')!r}" + (f"; the real name is {found!r}" if found else "")
        learn(meta["task"], meta["result"], meta.get("reason", ""), rows, d, protected_apps=protected_apps, reflect=False)
        n += 1
    return n


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--recall")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--remember", help='store something the user said, e.g. "my default browser is Chrome"')
    a = ap.parse_args()
    if a.remember:
        ops, said = remember(a.remember)
        print(said, json.dumps(ops, ensure_ascii=False))
    if a.backfill:
        print(f"learned from {backfill()} sessions")
    if a.recall:
        print(recall(a.recall)[0])
    if a.show:
        for name, path in (("facts", FACTS), ("faults", FAULTS), ("recipes", RECIPES)):
            rows = _read(path)
            print(f"\n== {name} ({len(rows)})")
            for r in rows[-15:]:
                print(" ", json.dumps(r, ensure_ascii=False)[:200])
