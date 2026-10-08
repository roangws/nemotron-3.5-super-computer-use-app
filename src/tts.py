"""Text-to-speech through the GMI request queue (Inworld and MiniMax), plus playback with a live level meter.

  python tts.py "Opening Safari"              # speak with the configured engine
  python tts.py --bench                       # latency benchmark of all engines
"""
import io
import json
import os
import queue
import threading
import time
import urllib.request

import numpy as np
import sounddevice as sd
import soundfile as sf

import model
import paths
import settings

QUEUE = "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey/requests"

# engine name -> (model id, payload extras). The voice comes from Settings (female Ashley or male Dennis).
ENGINES = {
    "inworld-tts-2-flash": ("inworld-tts-2-flash", {"voice_id": "Ashley", "audio_encoding": "WAV", "sample_rate_hertz": "24000"}),
    "inworld-tts-1.5-mini": ("inworld-tts-1.5-mini", {"voice_id": "Ashley", "audio_encoding": "WAV", "sample_rate_hertz": "24000"}),
    "inworld-tts-2": ("inworld-tts-2", {"voice_id": "Ashley", "audio_encoding": "WAV", "sample_rate_hertz": "24000"}),
    "minimax-2.8-turbo": ("minimax-tts-speech-2.8-turbo", {"voice_id": "English_ConfidentWoman", "format": "mp3", "audio_sample_rate": "24000", "channel": "1"}),
    "minimax-2.6-turbo": ("minimax-tts-speech-2.6-turbo", {"voice_id": "English_ConfidentWoman", "format": "mp3", "audio_sample_rate": "24000", "channel": "1"}),
}


def _req(url, body=None, timeout=30):
    r = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers={
        "Authorization": "Bearer " + model.api_key(),
        "Content-Type": "application/json",
        "User-Agent": model.USER_AGENT,
    })
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.read()


def _download(url, tries=3):
    """Fetch the audio file (a signed URL: no API key). Retries: the download sometimes ends early."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": model.USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(0.3)


def synth(text, engine, voice=None, poll=0.1, timeout=30):
    """Returns (audio float32 mono, sample_rate, timing dict)."""
    model_id, extra = ENGINES[engine]
    payload = {"text": text, **extra}
    if voice:
        payload["voice_id"] = voice
    t0 = time.time()
    job = json.loads(_req(QUEUE, {"model": model_id, "payload": payload}))
    t_submit = time.time() - t0
    rid = job["request_id"]
    while job.get("status") not in ("success", "failed", "error", "cancelled"):
        if time.time() - t0 > timeout:
            raise TimeoutError(f"{engine}: no result after {timeout}s")
        time.sleep(poll)
        job = json.loads(_req(f"{QUEUE}/{rid}"))
    if job["status"] != "success":
        raise RuntimeError(f"{engine}: {job.get('status')} {str(job.get('outcome'))[:200]}")
    t_ready = time.time() - t0
    o = job["outcome"]
    url = o.get("audio_url") or o["media_urls"][0]["url"]  # MiniMax: audio_url only; Inworld: both
    data = _download(url)
    audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    t_total = time.time() - t0
    return audio.mean(axis=1), sr, {"submit": round(t_submit, 2), "ready": round(t_ready, 2),
                                    "total": round(t_total, 2), "audio_secs": round(len(audio) / sr, 2)}


class Speaker:
    """Speaks status lines one at a time in the background. `level` is the live loudness (0..1) for the orb."""

    def __init__(self, engine, voice=None, fallback_say=True):
        self.engine, self.voice, self.fallback_say = engine, voice, fallback_say
        self.q = queue.Queue()
        self.level = 0.0
        self.speaking = False
        self.cache = {}  # text -> (audio, sr) for fixed phrases, so they play with no delay
        threading.Thread(target=self._run, daemon=True).start()

    def prefetch(self, phrases):
        def run():
            for p in phrases:
                try:
                    a, sr, _ = synth(p, self.engine, self.voice)
                    self.cache[p] = (a, sr)
                except Exception as e:
                    print("tts prefetch failed:", e, flush=True)
        threading.Thread(target=run, daemon=True).start()

    def say(self, text, interrupt=False):
        if interrupt:
            self.clear()
        self.q.put(text)

    def stop_now(self):
        """Silence at once: drop queued lines and cut the one playing."""
        self.clear()
        self._abort = True

    def clear(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def _run(self):
        while True:
            text = self.q.get()
            if self.q.qsize() > 1:  # behind: skip stale lines, keep the newest
                continue
            try:
                t0 = time.time()
                cached = text in self.cache
                audio, sr = self.cache.get(text) or synth(text, self.engine, self.voice)[:2]
                print(f"speaking: {text!r} ({'cached' if cached else f'{time.time() - t0:.2f}s'})", flush=True)
                self.play(audio, sr)
            except Exception as e:
                print("tts error:", e, flush=True)
                if self.fallback_say:
                    os.system(f"say {json.dumps(text)}")

    def play(self, audio, sr):
        self._abort = False
        self.speaking = True
        pos = [0]
        block = int(sr * 0.02)

        def cb(out, frames, t, status):
            chunk = audio[pos[0]:pos[0] + frames]
            out[:len(chunk), 0] = chunk
            out[len(chunk):, 0] = 0
            pos[0] += frames
            if getattr(self, "_abort", False):
                raise sd.CallbackStop
            self.level = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
            if pos[0] >= len(audio):
                raise sd.CallbackStop

        done = threading.Event()
        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32", blocksize=block, callback=cb,
                             finished_callback=done.set):
            done.wait(len(audio) / sr + 2)
        self.level = 0.0
        self.speaking = False


PHRASES = ["Opening Safari.", "Typing apple dot com in the address bar.", "Done. The page is open."]


def bench(rounds=2):
    out = os.path.join(paths.DATA, "tts-bench")
    os.makedirs(out, exist_ok=True)
    rows = []
    for engine in ENGINES:
        for r in range(rounds):
            for i, text in enumerate(PHRASES):
                try:
                    audio, sr, t = synth(text, engine)
                    if r == 0:
                        sf.write(os.path.join(out, f"{engine}-{i}.wav"), audio, sr)
                    rows.append({"engine": engine, "text": text, **t})
                    print(f"{engine:22} {t['total']:5.2f}s total ({t['ready']:.2f}s ready)  {text}", flush=True)
                except Exception as e:
                    rows.append({"engine": engine, "text": text, "error": str(e)})
                    print(f"{engine:22} ERROR {e}", flush=True)
    json.dump(rows, open(os.path.join(out, "results.json"), "w"), indent=2)
    print("\nmedian total seconds:")
    for engine in ENGINES:
        ts = sorted(x["total"] for x in rows if x["engine"] == engine and "total" in x)
        if ts:
            print(f"  {engine:22} median {ts[len(ts) // 2]:.2f}s  min {ts[0]:.2f}s  max {ts[-1]:.2f}s  ({len(ts)} ok)")


if __name__ == "__main__":
    import sys
    if "--bench" in sys.argv:
        bench()
    else:
        args = [a for a in sys.argv[1:] if not a.startswith("--")]
        eng = sys.argv[sys.argv.index("--engine") + 1] if "--engine" in sys.argv else "inworld-tts-2-flash"
        args = [a for a in args if a != eng]
        text = " ".join(args) or "Hi, I am Nemo. What should I do?"
        audio, sr, t = synth(text, eng, settings.voice_id())
        print(t)
        Speaker(eng).play(audio, sr)
