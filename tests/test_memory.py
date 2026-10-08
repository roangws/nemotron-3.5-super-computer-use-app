"""Memory, app-name and terminal-guard tests. No screen, mouse or network.

Run: .venv/bin/python -m pytest tests -q
The Bayhouse fixture is the real failed run (2026-10-06 21:03): open_app "Chrome" failed silently,
Terminal stayed in front, and the search text was typed into the user's terminal.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest

import actions
import memory

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "bayhouse")
APPS = {"Google Chrome", "Safari", "Claude", "System Settings", "Calculator", "TextEdit", "Visual Studio Code",
        "Microsoft Word", "Microsoft Excel", "Notes", "Terminal"}
PROTECTED = ["Terminal", "iTerm2", "Ghostty", "Warp"]


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """Point memory at an empty temp folder."""
    monkeypatch.setattr(memory, "DIR", str(tmp_path))
    for name, file in (("RULES", "rules.md"), ("FAULTS", "faults.jsonl"), ("FACTS", "facts.jsonl"), ("RECIPES", "recipes.jsonl")):
        monkeypatch.setattr(memory, name, str(tmp_path / file))
    return tmp_path


def bayhouse_rows():
    rows = [json.loads(l) for l in open(os.path.join(FIX, "ledger.jsonl"))]
    for r in rows:
        r["front_app"] = json.load(open(os.path.join(FIX, f"step-{r['step']:02d}.json")))["front_app"]
        a = r.get("action") or {}
        if a.get("action") == "open_app" and actions.resolve_app(a["app"], APPS)[0] != a["app"]:
            r["open_error"] = "no app named 'Chrome'"
    return rows


# ---------- app names ----------

def test_resolve_app_aliases_and_exact():
    assert actions.resolve_app("Chrome", APPS) == ("Google Chrome", [])
    assert actions.resolve_app("google chrome", APPS) == ("Google Chrome", [])
    assert actions.resolve_app("Claude", APPS) == ("Claude", [])
    assert actions.resolve_app("settings", APPS) == ("System Settings", [])
    assert actions.resolve_app("vscode", APPS) == ("Visual Studio Code", [])
    assert actions.resolve_app("Calculater", APPS)[0] is None            # typo: no guess...
    assert "Calculator" in actions.resolve_app("Calculater", APPS)[1]    # ...but a close-name hint for the model
    assert actions.resolve_app("Photoshop", APPS) == (None, [])


def test_resolve_app_on_this_mac():
    """The Bayhouse bug: 'Chrome' must map to the installed 'Google Chrome'."""
    if "Google Chrome" not in actions.installed_apps():
        pytest.skip("Google Chrome is not installed")
    assert actions.resolve_app("Chrome")[0] == "Google Chrome"


# ---------- terminal guard ----------

def test_guard_blocks_typing_into_terminal():
    a = {"action": "type", "text": "Bayhouse Creative\n", "target": "Chrome address bar"}
    assert actions.protected_front(a, "Terminal", "Open the browser and search for Bayhouse Creative", PROTECTED)
    assert actions.protected_front({"action": "key", "keys": "cmd+l"}, "Terminal", "search", PROTECTED)


def test_guard_allows_safe_cases():
    task = "Open the browser and search for Bayhouse Creative"
    assert actions.protected_front({"action": "type", "text": "x"}, "Google Chrome", task, PROTECTED) is None
    assert actions.protected_front({"action": "key", "keys": "cmd+space"}, "Terminal", task, PROTECTED) is None  # global key
    assert actions.protected_front({"action": "key", "keys": "cmd+tab"}, "Terminal", task, PROTECTED) is None
    assert actions.protected_front({"action": "open_app", "app": "Google Chrome"}, "Terminal", task, PROTECTED) is None
    assert actions.protected_front({"action": "type", "text": "ls"}, "Terminal", "open Terminal and type ls", PROTECTED) is None


# ---------- fault detection on the real failed run ----------

def test_bayhouse_faults_detected():
    rows = bayhouse_rows()
    codes = {f["code"] for f in memory.detect_faults(rows, "stopped", "stopped by user", PROTECTED)}
    assert {"APP_NOT_FOUND", "TYPED_INTO_WRONG_APP", "USER_STOPPED"} <= codes


def test_bayhouse_replay_with_guards():
    """Replay every action of the failed run through the new guards: none of the bad steps would run."""
    task = "Open the browser and search for Bayhouse Creative on Google."
    for r in bayhouse_rows():
        a = actions.normalize(r["action"])
        if a["action"] == "open_app":
            assert actions.resolve_app(a["app"], APPS)[0] == "Google Chrome"   # step 1 now opens the right app
        if a["action"] in ("type", "key") and r["front_app"] == "Terminal":
            assert actions.protected_front(a, r["front_app"], task, PROTECTED), f"step {r['step']} would still type into Terminal"


def test_clean_run_has_no_faults():
    rows = [{"step": 1, "action": {"action": "open_app", "app": "Safari"}, "label": "open_app Safari", "front_app": "Terminal"},
            {"step": 2, "action": {"action": "type", "text": "x\n"}, "label": 'type "x"', "front_app": "Safari"}]
    assert memory.detect_faults(rows, "done", "", PROTECTED) == []


# ---------- learning and recall ----------

def test_learn_then_recall_warns_about_the_mistake(mem):
    memory.learn("Open the browser and search for Bayhouse Creative on Google.", "stopped", "stopped by user",
                 bayhouse_rows(), "s1", protected_apps=PROTECTED, reflect=False)
    text, _ = memory.recall("open chrome and search for bayhouse creative")
    assert "TYPED_INTO_WRONG_APP" in text and "APP_NOT_FOUND" in text
    assert "USER_STOPPED" not in text   # not actionable, kept out of the prompt


def test_recall_unrelated_task_stays_quiet(mem):
    memory.learn("Open the browser and search for Bayhouse Creative on Google.", "stopped", "stopped by user",
                 bayhouse_rows(), "s1", protected_apps=PROTECTED, reflect=False)
    text, _ = memory.recall("open calculator and compute 3 times 4")
    assert "Bayhouse" not in text


def test_recipes_keep_shortest_real_path(mem):
    long = [{"action": {"action": "open_app", "app": "Safari"}, "label": "open_app Safari"},
            {"action": {"action": "click", "x": 500, "y": 80}, "label": 'click "address bar" (500.0,80.0)'},
            {"action": {"action": "type", "text": "apple.com\n"}, "label": 'type "apple.com"'}]
    short = [long[0], {"action": {"action": "key", "keys": "cmd+l"}, "label": "key cmd+l"}, long[2]]
    memory.learn("Open Safari and go to apple.com", "done", "", long, "s1", reflect=False)
    memory.learn("Open Safari and go to apple.com.", "done", "", short, "s2", reflect=False)
    memory.learn("Open Safari and go to apple.com", "done", "", [long[0]], "s3", reflect=False)  # 1 step: not a path
    recipes = memory._read(memory.RECIPES)
    assert len(recipes) == 1 and recipes[0]["steps"] == ["open_app Safari", "key cmd+l", 'type "apple.com"']
    text, _ = memory.recall("open safari and go to wikipedia.org")
    assert "key cmd+l" in text and "(500.0,80.0)" not in text


def test_facts_merge_ops():
    f = memory.apply_ops([], [{"op": "ADD", "app": "Google Chrome", "fact": "The app is named Google Chrome, not Chrome."}], "s1")
    f = memory.apply_ops(f, [{"op": "ADD", "app": "Google Chrome", "fact": "The app is named Google Chrome, not Chrome."}], "s2")
    assert len(f) == 1                                         # duplicate ADD ignored
    f = memory.apply_ops(f, [{"op": "UPDATE", "id": "f1", "fact": "open_app 'Google Chrome' opens Chrome."}], "s3")
    assert f[0]["fact"].startswith("open_app 'Google Chrome'")
    f = memory.apply_ops(f, [{"op": "DELETE", "id": "f1"}, {"op": "BOGUS"}], "s4")
    assert f == []


def test_bad_facts_are_pruned():
    facts = [{"id": "f1", "app": "Safari", "fact": "x", "wins": 0, "losses": 2},
             {"id": "f2", "app": "Safari", "fact": "y", "wins": 5, "losses": 2}]
    facts = memory.score_facts(facts, {"f1", "f2"}, "fail")
    assert [f["id"] for f in facts] == ["f2"]                  # f1 kept losing, gone; f2 still mostly wins


def test_recall_size_is_capped(mem):
    for i in range(200):
        memory._append(memory.FAULTS, {"code": "APP_NOT_FOUND", "app": f"App{i}", "task": "open chrome",
                                       "what": "x" * 200, "fix": "y"})
    text, _ = memory.recall("open chrome")
    assert len(text) <= memory.RECALL_CHARS + 2


def test_memory_command_detection():
    from memory import is_memory_command
    for t in ["My default browser is Chrome, save my memory.", "Update my memory: I use the GMI profile.",
              "Remember that my name is Sam.", "remember my Chrome has three profiles", "Forget that I use Safari."]:
        assert is_memory_command(t), t
    for t in ["Open Chrome and search Google for memory foam", "Open Safari and go to apple.com", "save the file"]:
        assert not is_memory_command(t), t


def test_pinned_facts_survive_reflection(tmp_path, monkeypatch):
    import memory
    facts = memory.apply_ops([], [{"op": "ADD", "app": "Google Chrome", "fact": "The user's default browser is Google Chrome."}], "user", pinned=True)
    fid = facts[0]["id"]
    after = memory.apply_ops(facts, [{"op": "DELETE", "id": fid}, {"op": "UPDATE", "id": fid, "fact": "Safari"}], "session-x")
    assert after == facts                                     # the agent's own reflection cannot change what the user said
    assert memory.score_facts([dict(facts[0], losses=9)], {fid}, "fail")   # never pruned
    assert memory.apply_ops(facts, [{"op": "DELETE", "id": fid}], "user", pinned=True) == []   # the user can
