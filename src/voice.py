"""Push-to-talk recording and local speech-to-text (mlx-whisper)."""
import os
import threading
import wave

import numpy as np
import sounddevice as sd

RATE = 16000
_model_lock = threading.Lock()
_warm = False


class Recorder:
    """Start on key down, stop on key up. Collects 16 kHz mono float32 audio."""

    def __init__(self):
        self.frames, self.stream = [], None
        self.level = 0.0  # live loudness, drives the orb

    def _cb(self, data, n, t, s):
        self.frames.append(data.copy())
        self.level = float(np.sqrt(np.mean(data ** 2)))

    def start(self):
        self.frames = []
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", callback=self._cb)
        self.stream.start()

    def stop(self):
        if self.stream is None:
            return np.zeros(0, dtype=np.float32)
        self.stream.stop()
        self.stream.close()
        self.stream = None
        self.level = 0.0
        return np.concatenate(self.frames)[:, 0] if self.frames else np.zeros(0, dtype=np.float32)


def save_wav(audio, path):
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def load_wav(path):
    """Read a 16 kHz mono 16-bit wav (as written by save_wav or `afconvert -f WAVE -d LEI16@16000 -c 1`)."""
    with wave.open(path) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1, "need 16 kHz mono wav"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32767


def is_speech(audio):
    """Skip taps and silence (whisper invents text like 'Thank you.' on silence)."""
    return len(audio) >= RATE * 0.4 and float(np.sqrt(np.mean(audio ** 2))) > 0.004


def transcribe(audio, model):
    import mlx_whisper
    with _model_lock:
        r = mlx_whisper.transcribe(audio, path_or_hf_repo=model, language="en",
                                   condition_on_previous_text=False, fp16=True)
    return r["text"].strip()


def warm_up(model):
    """Load the model once in the background so the first real task is fast."""
    global _warm
    if not _warm:
        transcribe(np.zeros(RATE, dtype=np.float32), model)
        _warm = True


def mic_ok():
    try:
        sd.query_devices(kind="input")
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    import time
    model = "mlx-community/whisper-large-v3-turbo"
    if len(sys.argv) > 1:
        t = time.time()
        print(repr(transcribe(load_wav(sys.argv[1]), model)), f"{time.time() - t:.1f}s")
    else:
        r = Recorder()
        input("Press Enter, speak, press Enter again.")
        r.start()
        input()
        a = r.stop()
        print(len(a) / RATE, "s", "speech" if is_speech(a) else "no speech")
        print(transcribe(a, model))
