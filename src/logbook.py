"""Session folder, ledger, per-step model logs and report.html."""
import html
import json
import os
import re
import time

from paths import SESSIONS


class Session:
    def __init__(self, task, source="typed"):
        slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:40] or "task"
        self.dir = os.path.join(SESSIONS, time.strftime("%Y%m%d-%H%M%S") + "-" + slug)
        os.makedirs(os.path.join(self.dir, "model"), exist_ok=True)
        self.task, self.source, self.t0 = task, source, time.time()
        self.steps = []
        with open(self.path("task.txt"), "w") as f:
            f.write(f"{task}\n\nsource: {source}\n")

    def path(self, name):
        return os.path.join(self.dir, name)

    def log_step(self, row, model_record=None):
        row = {"t": round(time.time() - self.t0, 2), **row}
        self.steps.append(row)
        with open(self.path("ledger.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")
        if model_record is not None:
            with open(self.path(f"model/step-{row['step']:02d}.json"), "w") as f:
                json.dump(model_record, f, indent=2)

    def drop_media(self):
        """Delete the screenshots and the voice recording of this run (Settings: screenshots off)."""
        for name in os.listdir(self.dir):
            if name.endswith((".png", ".jpg", ".wav")):
                os.remove(self.path(name))

    def finish(self, result, reason=""):
        secs = round(time.time() - self.t0, 1)
        summary = {"task": self.task, "source": self.source, "result": result, "reason": reason,
                   "steps": len(self.steps), "secs": secs, "dir": os.path.basename(self.dir)}
        json.dump(summary, open(self.path("summary.json"), "w"), indent=2)
        with open(os.path.join(SESSIONS, "index.jsonl"), "a") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **summary}) + "\n")
        self.write_report(summary)
        return summary

    def write_report(self, s):
        e = html.escape
        color = {"done": "#1a7f37", "fail": "#cf222e", "stopped": "#9a6700"}.get(s["result"], "#57606a")
        rows = []
        for r in self.steps:
            a = r.get("action") or {}
            img = f'<img src="{e(r["image"])}" loading="lazy">' if r.get("image") and os.path.exists(self.path(r["image"])) else ""
            meta = " · ".join(x for x in [
                f'{r.get("model_secs", "-")} s model',
                "thinking" if r.get("thinking") else "",
                f'screen changed: {r.get("screen_changed")}' if r.get("screen_changed") is not None else "",
                f'tokens {r.get("prompt_tokens", "-")}/{r.get("completion_tokens", "-")}',
                f'confirmed: {r["confirmed"]}' if r.get("confirmed") is not None else "",
            ] if x)
            err = f'<p class="err">{e(str(r["error"]))}</p>' if r.get("error") else ""
            rows.append(f"""<section><h3>Step {r['step']} — {e(r.get('label', ''))}</h3>
<p class="thought">{e(str(a.get('thought', '')))}</p><p class="meta">{e(meta)}</p>{err}{img}</section>""")
        final = '<section><h3>Final screen</h3><img src="final.jpg"></section>' if os.path.exists(self.path("final.jpg")) else ""
        audio = '<audio controls src="audio.wav"></audio>' if os.path.exists(self.path("audio.wav")) else ""
        page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Agent run</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#fff;--fg:#1f2328;--mute:#57606a;--card:#f6f8fa;--line:#d0d7de}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--mute:#8d96a0;--card:#161b22;--line:#30363d}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px}}
h1{{font-size:22px;margin:0 0 4px}} .result{{color:#fff;background:{color};padding:2px 10px;border-radius:12px;font-size:13px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:14px 0}}
h3{{margin:0 0 4px;font-size:16px}} .thought{{margin:2px 0}} .meta{{color:var(--mute);font-size:13px;margin:2px 0 8px}}
.err{{color:#cf222e}} img{{width:100%;border-radius:6px;border:1px solid var(--line)}}
</style></head><body>
<h1>{e(s['task'])}</h1>
<p><span class="result">{e(s['result'])}</span> &nbsp;{s['steps']} steps · {s['secs']} s · {e(s['source'])} {('· ' + e(s['reason'])) if s['reason'] else ''}</p>
{audio}{''.join(rows)}{final}</body></html>"""
        open(self.path("report.html"), "w").write(page)
