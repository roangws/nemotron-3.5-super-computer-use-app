"""GMI client for Nemotron 3.5 Super VL: headers, retries, JSON parsing, thinking switch."""
import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request

import settings

MODEL = "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-BF16"
USER_AGENT = "nemotron-agent/1.0"  # default urllib UA gets Cloudflare 403 (error 1010)


class NoKey(RuntimeError):
    """No GMI API key yet."""


def api_key():
    key = settings.api_key()
    if not key:
        raise NoKey(f"No GMI API key. Open Settings (menu bar) or run: python src/settings.py --key YOUR_KEY "
                    f"(get one at {settings.KEY_URL})")
    return key


def image_to_data_url(img, fmt="PNG"):
    buf = io.BytesIO()
    img.save(buf, format=fmt, **({"quality": 90} if fmt == "JPEG" else {}))
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode()


def chat(messages, image=None, thinking=False, max_tokens=None, timeout=None, retries=4, image_format="PNG", **extra):
    """One chat call. `image` (PIL) is attached to the last user message. Only one image per request.

    Returns dict: content, reasoning, finish_reason, usage, secs.
    """
    if max_tokens is None:
        max_tokens = 20000 if thinking else 1500
    if timeout is None:
        timeout = 180 if thinking else 60
    messages = [dict(m) for m in messages]
    if image is not None:
        last = messages[-1]
        last["content"] = [
            {"type": "text", "text": last["content"]},
            {"type": "image_url", "image_url": {"url": image_to_data_url(image, image_format)}},
        ]
    body = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": thinking},
        **extra,
    }
    data = json.dumps(body).encode()
    url = settings.base_url().rstrip("/") + "/chat/completions"
    key = api_key()
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": USER_AGENT,
        })
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
            choice = resp["choices"][0]
            msg = choice.get("message", {})
            return {
                "content": msg.get("content") or "",
                "reasoning": msg.get("reasoning_content") or msg.get("reasoning") or "",
                "finish_reason": choice.get("finish_reason"),
                "usage": resp.get("usage") or {},
                "secs": round(time.time() - t0, 2),
            }
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read()[:300]!r}"
            if e.code in (401, 403):  # wrong key: retrying will not help
                raise RuntimeError(f"GMI refused the API key ({last_err}). Check it in Settings.")
            if e.code == 429:  # GMI "all endpoints overloaded": wait longer
                time.sleep(10 * (attempt + 1))
        except Exception as e:  # fetch failed, IncompleteRead, timeout
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(2 ** attempt)
    raise RuntimeError(f"model call failed after {retries} tries: {last_err}")


def parse_json(text):
    """Parse the first '{' to the last '}'. Returns dict or None."""
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None


def parse_point(text):
    """Lenient {x, y} reader for slightly broken replies such as '{"x": 186, y=87}'. Returns (x, y) or None."""
    j = parse_json(text)
    if j and "x" in j and "y" in j:
        return float(j["x"]), float(j["y"])
    a, b = text.find("{"), text.rfind("}")
    nums = re.findall(r"-?\d+(?:\.\d+)?", text[a:b + 1] if 0 <= a < b else text)
    if len(nums) >= 2:
        return float(nums[0]), float(nums[1])
    return None


if __name__ == "__main__":
    r = chat([{"role": "user", "content": "Reply with the single word OK."}], max_tokens=20)
    print(repr(r["content"]), r["finish_reason"], r["usage"], f"{r['secs']}s")
