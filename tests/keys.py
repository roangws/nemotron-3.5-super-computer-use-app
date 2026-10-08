"""Post real key events from a separate process (to test the app's key tap). Usage: keys.py hold|tap <keycode> [secs]"""
import sys
import time

import Quartz


def post(code, down):
    e = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


if __name__ == "__main__":
    mode, code = sys.argv[1], int(sys.argv[2])
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 0.05
    post(code, True)
    time.sleep(secs)
    post(code, False)
