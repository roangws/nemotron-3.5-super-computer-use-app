"""Screen capture, mouse marker, change detection and step annotation."""
import subprocess

import pyautogui
from PIL import Image, ImageChops, ImageDraw, ImageFont

try:
    FONT = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 34)
except OSError:
    FONT = ImageFont.load_default()


def points_size():
    """Logical screen size in points (what pyautogui clicks in)."""
    s = pyautogui.size()
    return s.width, s.height


def spotlight_open():
    """Spotlight is a floating panel: macOS keeps reporting the previous app as frontmost while it is open."""
    import Quartz
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
        if w.get("kCGWindowOwnerName") == "Spotlight" and w.get("kCGWindowBounds", {}).get("Width", 0) > 100:
            return True
    return False


def frontmost_app():
    """App with keyboard focus. Keys and typing go to this app.

    System Events is used because NSWorkspace goes stale in a process without a run loop,
    and the Accessibility focused-app call returned kAXErrorCannotComplete on this Mac.
    """
    if spotlight_open():
        return "Spotlight"  # typing goes to the Spotlight field, not to the app behind it
    r = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of first process whose frontmost is true'],
                       capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


# The app sets this to a function returning the orb's window number (or None when it is hidden).
# Screenshots for the model are then taken *below* that window, so the orb is visible in screen
# recordings (Screen Studio etc.) but never in what the model sees.
EXCLUDE_WINDOW = None


def _grab_below(window_id):
    """Screenshot of everything under one window, as a PIL image (about 0.03 s)."""
    import Quartz
    opt = Quartz.kCGWindowListOptionOnScreenBelowWindow if window_id else Quartz.kCGWindowListOptionOnScreenOnly
    cg = Quartz.CGWindowListCreateImage(Quartz.CGRectInfinite, opt, window_id or Quartz.kCGNullWindowID,
                                        Quartz.kCGWindowImageDefault)
    if cg is None:
        return None
    w, h, bpr = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg), Quartz.CGImageGetBytesPerRow(cg)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cg))
    return Image.frombuffer("RGBA", (w, h), bytes(data), "raw", "BGRA", bpr, 1).convert("RGB")


def capture(path):
    """Full-size screenshot of the main display (Retina pixels). M1: full size gave 20/20 hits."""
    wid = EXCLUDE_WINDOW() if callable(EXCLUDE_WINDOW) else None
    if wid:
        try:
            img = _grab_below(wid)
            if img is not None:
                img.save(path, compress_level=1)
                return img
        except Exception:
            pass  # fall back to screencapture
    subprocess.run(["screencapture", "-x", "-m", "-t", "png", path], check=True)
    img = Image.open(path)
    img.load()
    return img.convert("RGB")


def settle(path, stop_event=None, min_wait=0.15, max_wait=1.5, poll=0.12, quiet_pixels=800):
    """Wait until the screen stops changing (instead of a fixed pause). Returns the settled screenshot, saved at `path`.

    The fixed 0.8 s pause (1.8 s after open_app) was mostly waiting for nothing; most screens settle in 0.3-0.5 s.
    """
    import time
    end = time.time() + max_wait
    if stop_event is not None and stop_event.wait(min_wait):
        return None
    a = capture(path)
    while time.time() < end:
        if stop_event is not None and stop_event.wait(poll):
            return None
        b = capture(path)
        if not changed(a, b, quiet_pixels):  # small motion (a spinner, a blinking cursor) counts as settled
            return b
        a = b
    return a


def for_model(img, width, mouse_pts, screen_pts):
    """The image the model sees: resized to `width` (1512 = the screen in points) with the mouse ring drawn."""
    small = img if not width or img.width <= width else img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    return mark_mouse(small, mouse_pts, screen_pts)


def mark_mouse(img, mouse_pts, screen_pts):
    """Copy of img with a red ring at the mouse position (the model's view of the cursor)."""
    out = img.copy()
    sx, sy = img.width / screen_pts[0], img.height / screen_pts[1]
    x, y = mouse_pts[0] * sx, mouse_pts[1] * sy
    r = 22 * sx / 2
    ImageDraw.Draw(out).ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0), width=max(3, int(sx * 2)))
    return out


def changed(a, b, min_pixels=30):
    """True unless the two screenshots are nearly identical.

    Says "no change" only when almost nothing moved: a wrong "no change" makes the model abandon
    a click that worked (seen with Calculator digits). The menu bar is ignored (clock).
    """
    if a is None or b is None:
        return True
    size = (756, 491)
    ga, gb = a.convert("L").resize(size), b.convert("L").resize(size)
    box = (0, 16, size[0], size[1])
    diff = ImageChops.difference(ga.crop(box), gb.crop(box)).point(lambda v: 255 if v > 24 else 0)
    return diff.histogram()[255] >= min_pixels


def annotate(img, action, out_path, width=1512):
    """Smaller JPEG of what the model saw, with the planned click marked and the action labeled."""
    out = img.copy()
    d = ImageDraw.Draw(out)
    W, H = out.size
    label = describe(action)
    if action.get("action") in ("click", "double_click", "right_click") and "x" in action:
        px, py = action["x"] / 1000 * W, action["y"] / 1000 * H
        d.ellipse([px - 30, py - 30, px + 30, py + 30], outline=(0, 200, 255), width=6)
        d.line([px - 50, py, px + 50, py], fill=(0, 200, 255), width=4)
        d.line([px, py - 50, px, py + 50], fill=(0, 200, 255), width=4)
        d.text((px + 40, py + 20), label, fill=(0, 200, 255), font=FONT, stroke_width=4, stroke_fill=(0, 0, 0))
    else:
        d.text((40, H - 90), label, fill=(0, 200, 255), font=FONT, stroke_width=4, stroke_fill=(0, 0, 0))
    out = out.resize((width, round(H * width / W)), Image.LANCZOS)
    out.save(out_path, quality=85)


def describe(a):
    t = a.get("action", "?")
    if t in ("click", "double_click", "right_click"):
        return f'{t} "{a.get("target", "")}" ({a.get("x")},{a.get("y")})'
    if t == "type":
        return f'type "{str(a.get("text", ""))[:40]}"'
    if t == "key":
        return f'key {a.get("keys")}'
    if t == "scroll":
        return f'scroll {a.get("dy")}'
    if t == "open_app":
        return f'open_app {a.get("app")}'
    return t
