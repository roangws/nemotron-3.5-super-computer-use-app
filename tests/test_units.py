"""Fast tests with no screen, mouse or network. Run: .venv/bin/python -m pytest tests -q"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from PIL import Image, ImageDraw

import actions
import screen
from model import parse_json, parse_point

WORDS = ["send", "delete", "password", "buy"]


def test_parse_json_and_point():
    assert parse_json('blah {"a": 1} tail') == {"a": 1}
    assert parse_json("no json") is None
    assert parse_point('{"x": 186, y=87}') == (186.0, 87.0)
    assert parse_point('{"x": 111, 91}') == (111.0, 91.0)


def test_normalize_and_validate():
    a = actions.normalize({"action": "Double-Click", "x": "500", "y": 20})
    assert a["action"] == "double_click" and a["x"] == 500.0
    assert actions.validate(a) is None
    assert actions.validate(actions.normalize({"action": "click", "x": 1200, "y": 5}))
    assert actions.validate(actions.normalize({"action": "fly"}))
    assert actions.validate(actions.normalize({"action": "type"}))
    assert actions.validate(actions.normalize({"action": "open_app", "app": "Safari; rm -rf /"}))
    assert actions.validate(actions.normalize({"action": "open_app", "app": "System Settings"})) is None
    assert actions.normalize({"action": "hotkey", "keys": "cmd+l"})["action"] == "key"


def test_risky():
    assert actions.risky_reason({"action": "click", "target": "Send button"}, WORDS)
    assert actions.risky_reason({"action": "key", "keys": "enter", "risky": True}, WORDS)
    assert actions.risky_reason({"action": "click", "target": "Search field"}, WORDS) is None
    assert actions.risky_reason({"action": "click", "target": "Sender name"}, WORDS) is None  # whole words only
    assert actions.risky_reason({"action": "done", "thought": "sent it"}, WORDS) is None
    assert actions.risky_reason({"action": "key", "keys": "esc", "thought": "close it, then delete"}, WORDS) is None
    assert actions.is_password_typing({"action": "type", "target": "Password field", "text": "x"})


def test_repeat_block():
    a = actions.normalize({"action": "click", "x": 500, "y": 300, "target": "OK"})
    b = actions.normalize({"action": "click", "x": 505, "y": 302, "target": "OK button"})  # same spot, new name
    c = actions.normalize({"action": "click", "x": 100, "y": 900, "target": "ok"})       # same name, new spot
    d = actions.normalize({"action": "click", "x": 100, "y": 900, "target": "Cancel"})
    blocked = [actions.signature(a)]
    assert actions.same(actions.signature(b), blocked)
    assert actions.same(actions.signature(c), blocked)
    assert not actions.same(actions.signature(d), blocked)


def test_mapping_retina():
    assert actions.to_points({"x": 500, "y": 500}, (1512, 982)) == (756, 491)
    assert actions.to_points({"x": 0, "y": 1000}, (1512, 982)) == (0, 982)


def test_changed():
    a = Image.new("RGB", (3024, 1964), "white")
    assert not screen.changed(a, a.copy())
    b = a.copy()
    ImageDraw.Draw(b).rectangle([1000, 800, 1400, 1100], fill="black")
    assert screen.changed(a, b)


def test_report(tmp_path, monkeypatch):
    import logbook
    monkeypatch.setattr(logbook, "SESSIONS", str(tmp_path))
    s = logbook.Session("open <Calculator>", "typed")
    img = Image.new("RGB", (3024, 1964), "gray")
    act = {"action": "click", "x": 500, "y": 500, "target": "Calc", "thought": "click it"}
    screen.annotate(img, act, s.path("step-01.jpg"))
    s.log_step({"step": 1, "action": act, "label": "click", "image": "step-01.jpg", "model_secs": 1.2}, {"prompt": "p"})
    summary = s.finish("done", "ok")
    page = open(s.path("report.html")).read()
    assert "&lt;Calculator&gt;" in page and "step-01.jpg" in page and "click it" in page
    assert summary["steps"] == 1
    assert json.loads(open(tmp_path / "index.jsonl").read())["result"] == "done"


def test_same_app_and_wrong_app_guard():
    assert actions.same_app("Chrome", "Google Chrome")
    assert actions.same_app("Visual Studio Code", "Code")
    assert not actions.same_app("Google Chrome", "Terminal")
    t = {"action": "type", "text": "Bayhouse Creative\n", "in_app": "Google Chrome"}
    assert actions.wrong_app(t, "Terminal")                  # the failed Bayhouse run
    assert actions.wrong_app(t, "Google Chrome") is None
    assert actions.wrong_app({"action": "key", "keys": "cmd+space", "in_app": "Safari"}, "Terminal") is None  # global key
    assert actions.wrong_app({"action": "type", "text": "x"}, "Terminal") is None   # no in_app: other guards decide
