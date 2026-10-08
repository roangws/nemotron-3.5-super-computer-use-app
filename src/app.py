"""Nemotron Agent menu bar app.

  python app.py

- Menu bar icon with an On/Off switch.
- When On: hold the talk key (default ` backtick; change it in Settings) and speak a task, release to run it.
  The key press does not type.
- While it listens, works or talks, the orb shows at the bottom center and pulses with the voice.
- Stop a running task: press Esc once, choose "Stop task" in the menu, or slam the mouse into a screen corner.
"""
import faulthandler
import glob
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import objc
from AppKit import (NSAlert, NSApp, NSApplication, NSApplicationActivationPolicyAccessory, NSButton, NSImage, NSMakeRect,
                    NSMenu, NSMenuItem, NSPopUpButton, NSSecureTextField, NSStatusBar, NSTextField, NSURL, NSView,
                    NSVariableStatusItemLength, NSWorkspace)
from Foundation import NSData, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer
from PIL import Image
from PyObjCTools import AppHelper
import Quartz

import memory
import screen
import settings
import voice
from orb import Orb
from agent import CONFIG, SETTINGS, Agent, Hooks, apply_settings, say, speaker
from paths import ASSETS, LOG, SESSIONS

ESC = 53
PREFETCH = ["Got it.", "Done.", "Stopped.", "I need your OK for this one.", "I have a question for you.",
            "Sorry, I could not finish."]
SAMPLE = "Hi, I am Nemo. Opening Safari for you."


def menubar_frames(step=3, px=36):
    """Small orb images for the menu bar (18 pt, Retina), taken from the orb animation frames."""
    out = []
    for f in sorted(glob.glob(os.path.join(ASSETS, "orb_frames", "*.png")))[::step]:
        im = Image.open(f).convert("RGBA")
        im = im.crop(im.getbbox()).resize((px, px), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        ns = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue())))
        ns.setSize_((18, 18))
        out.append(ns)
    return out


ICON = NSImage.alloc().initWithContentsOfFile_(os.path.join(ASSETS, "AppIcon-1024.png"))


def k():
    """Short name of the talk key, for messages: ` or right ⌥."""
    return settings.talk_key(SETTINGS)["short"]


def new_alert():
    """A dialog with the orb icon (without this it shows the Python rocket)."""
    alert = NSAlert.alloc().init()
    alert.setIcon_(ICON)
    return alert


FAKE_AUDIO = os.environ.get("AGENT_FAKE_AUDIO")  # tests: use this wav instead of the microphone


class AppDelegate(NSObject):
    # ---------- setup ----------
    def applicationDidFinishLaunching_(self, _):
        self.enabled = True
        self.state = "idle"           # idle | listening | transcribing | working | confirm
        self.status_text = "Ready"
        self.agent = Agent(hooks=AppHooks(self))
        self.agent_thread = None
        self.recorder = voice.Recorder()
        self.recording = False
        self.last_esc = 0.0
        self.pending = None           # (kind, threading.Event, holder) while confirm/ask is open
        self.last_report = None
        self.linger_until = 0.0

        NSApp.setApplicationIconImage_(ICON)
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.icon_frames = menubar_frames()
        self.icon_i = 0
        self.item.button().setImage_(self.icon_frames[0])
        self.item.button().setTitle_("")
        self.build_menu()

        self.orb = Orb(CONFIG.get("orb_position", "bottom"))
        screen.EXCLUDE_WINDOW = lambda: self.orb.window.windowNumber() if self.orb.visible else None
        self.last_tick = time.time()
        self.spk = speaker()
        if settings.api_key():
            self.spk.prefetch(PREFETCH)
        else:  # first run: ask for the GMI API key
            AppHelper.callLater(1.0, self.openSettings_, None)

        self.tap_ok = self.install_key_tap()
        t = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1 / 60, self, "tick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(t, NSRunLoopCommonModes)
        self.refresh()
        if not self.tap_ok:
            self.set_status("idle", f"Needs Accessibility + Input Monitoring permission for the {k()} key")
        threading.Thread(target=self.warm_whisper, daemon=True).start()
        print(f"Nemotron Agent running. Menu bar icon: top right. Hold {k()} to talk.", flush=True)
        print(f"key tap ({k()} and Esc):", "OK" if self.tap_ok else
              "NO PERMISSION - allow Nemotron Agent in System Settings > Privacy & Security > Accessibility and Input Monitoring",
              flush=True)

    @objc.python_method
    def warm_whisper(self):
        try:
            voice.warm_up(CONFIG["whisper_model"])
            print("speech model ready", flush=True)
        except Exception as e:
            print("speech model failed to load:", e, flush=True)

    @objc.python_method
    def build_menu(self):
        m = NSMenu.alloc().init()
        m.setAutoenablesItems_(False)

        def add(title, sel, key=""):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, key)
            it.setTarget_(self)
            m.addItem_(it)
            return it

        self.mi_status = add("Ready", None)
        self.mi_status.setEnabled_(False)
        m.addItem_(NSMenuItem.separatorItem())
        self.mi_toggle = add("Agent On", "toggle:")
        self.mi_type = add("Type a task…", "typeTask:")
        self.mi_stop = add("Stop task  (Esc)", "stopTask:")
        self.mi_dry = add("Dry run (plan only, no clicks)", "toggleDry:")
        m.addItem_(NSMenuItem.separatorItem())
        add("Settings…", "openSettings:", ",")
        add("Open last report", "openReport:")
        add("Open sessions folder", "openFolder:")
        add("Open log (crashes, errors)", "openLog:")
        m.addItem_(NSMenuItem.separatorItem())
        add("Quit Nemotron Agent", "quit:", "q")
        self.item.setMenu_(m)

    # ---------- UI state ----------
    @objc.python_method
    def set_status(self, state, text):
        """Thread-safe: may be called from the agent thread."""
        def apply():
            self.state, self.status_text = state, text
            if state in ("done", "fail", "stopped"):
                self.state = "idle"
                self.linger_until = time.time() + 2.5 if state != "stopped" else 0.0  # Esc: no lingering orb
            self.refresh()
        AppHelper.callAfter(apply)

    @objc.python_method
    def refresh(self):
        # menu bar: the orb; dimmed when off, animated while listening/working (see tick_)
        self.item.button().setAlphaValue_(1.0 if self.enabled else 0.35)
        self.item.button().setTitle_(" ?" if self.state == "confirm" else "")
        self.mi_status.setTitle_(self.status_text[:80])
        self.mi_toggle.setState_(1 if self.enabled else 0)
        self.mi_dry.setState_(1 if self.agent.dry_run else 0)
        self.mi_stop.setEnabled_(self.busy())
        self.mi_type.setEnabled_(self.enabled and not self.busy())
        self.orb.set_caption("" if self.state == "listening" else self.status_text)

    @objc.python_method
    def busy(self):
        return (self.agent_thread is not None and self.agent_thread.is_alive()
                and not self.agent.stop_event.is_set())

    def tick_(self, _):
        now = time.time()
        dt, self.last_tick = min(now - self.last_tick, 0.1), now
        active = (self.state in ("listening", "transcribing", "working", "confirm")
                  or self.spk.speaking or time.time() < self.linger_until)
        level = self.recorder.level if self.recording else self.spk.level
        self.orb.tick(dt, active, level)
        if self.state in ("listening", "transcribing", "working", "confirm"):
            self.icon_i += 1
            if self.icon_i % 3 == 0:  # ~20 fps
                self.item.button().setImage_(self.icon_frames[(self.icon_i // 3) % len(self.icon_frames)])
        elif self.icon_i:
            self.icon_i = 0
            self.item.button().setImage_(self.icon_frames[0])

    # ---------- keyboard ----------
    @objc.python_method
    def install_key_tap(self):
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
                | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))  # modifier talk keys (right Option, right Command)
        self._tap_cb = self.on_key_event  # keep a reference
        self.tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                           Quartz.kCGEventTapOptionDefault, mask, self._tap_cb, None)
        if self.tap is None:
            return False
        src = Quartz.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self.tap, True)
        return True

    @objc.python_method
    def on_key_event(self, proxy, etype, event, refcon):
        try:
            if etype in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                Quartz.CGEventTapEnable(self.tap, True)
                return event
            if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID) == os.getpid():
                return event  # our own synthetic keys (the agent typing)
            code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            if code == ESC and etype == Quartz.kCGEventKeyDown and self.busy() and not self.pending:
                self.stopTask_(None)  # one Esc stops the task at once
                return event
            tk = settings.talk_key(SETTINGS)
            if code != tk["code"] or not self.enabled:
                return event
            flags = Quartz.CGEventGetFlags(event)
            if tk.get("modifier"):
                if etype != Quartz.kCGEventFlagsChanged:
                    return event
                bit = Quartz.kCGEventFlagMaskAlternate if tk["modifier"] == "option" else Quartz.kCGEventFlagMaskCommand
                down = bool(flags & bit)
                if down and not self.recording and (not self.busy() or self.pending):
                    self.start_listening()
                elif not down and self.recording:
                    self.stop_listening()
                return event  # never swallow a modifier: other apps must see it go up
            if etype == Quartz.kCGEventFlagsChanged:
                return event
            mods = (Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskControl |
                    Quartz.kCGEventFlagMaskAlternate | Quartz.kCGEventFlagMaskShift)
            if flags & mods:
                return event  # cmd+` etc. keep working
            if etype == Quartz.kCGEventKeyDown:
                if not self.recording and (not self.busy() or self.pending):
                    self.start_listening()
            elif self.recording:
                self.stop_listening()
            return None  # swallow the talk key so it does not type
        except Exception:
            traceback.print_exc()
            return event

    # ---------- voice ----------
    @objc.python_method
    def start_listening(self):
        self.recording = True
        self.listen_t0 = time.time()
        if not FAKE_AUDIO:
            try:
                self.recorder.start()
            except Exception as e:
                self.recording = False
                self.set_status("idle", f"Microphone error: {e}")
                return
        self.state, self.status_text = "listening", f"Listening… release {k()} when done"
        self.refresh()

    @objc.python_method
    def stop_listening(self):
        self.recording = False
        if FAKE_AUDIO:
            audio = voice.load_wav(FAKE_AUDIO)
        else:
            audio = self.recorder.stop()
        answering = self.pending is not None
        self.state, self.status_text = ("confirm" if answering else "transcribing"), "Transcribing…"
        self.refresh()
        threading.Thread(target=self.handle_audio, args=(audio, answering), daemon=True).start()

    @objc.python_method
    def handle_audio(self, audio, answering):
        if not voice.is_speech(audio):
            self.set_status("confirm" if answering else "idle", f"Didn't hear anything. Hold {k()} and speak.")
            return
        wav = os.path.join(tempfile.gettempdir(), f"nemo-{int(time.time())}.wav")
        voice.save_wav(audio, wav)
        try:
            text = voice.transcribe(audio, CONFIG["whisper_model"])
        except Exception as e:
            self.set_status("idle", f"Speech-to-text error: {e}")
            return
        print("heard:", repr(text), flush=True)
        if answering:
            self.answer_by_voice(text)
            return
        if not text:
            self.set_status("idle", "Didn't catch that. Try again.")
            return
        AppHelper.callAfter(self.start_task, text, "voice", wav)

    # ---------- tasks ----------
    @objc.python_method
    def start_task(self, task, source="typed", wav=None):
        if self.busy():
            return
        if not settings.api_key():
            self.set_status("idle", "Add your GMI API key in Settings first")
            self.openSettings_(None)
            return
        if memory.is_memory_command(task):  # "...save my memory": store it, do not run anything
            self.state, self.status_text = "working", "Saving to memory…"
            self.refresh()
            threading.Thread(target=self.save_memory, args=(task,), daemon=True).start()
            return
        self.state, self.status_text = "working", f"Got it: {task}"
        self.refresh()

        def run():
            try:
                s = self.agent.run(task, source=source, audio_path=wav)
                self.last_report = os.path.join(SESSIONS, s["dir"], "report.html")
            except Exception as e:
                traceback.print_exc()
                self.set_status("fail", f"Error: {e}")
        self.agent_thread = threading.Thread(target=run, daemon=True)
        self.agent_thread.start()

    @objc.python_method
    def save_memory(self, text):
        try:
            ops, said = memory.remember(text)
            print("memory:", said, ops, flush=True)
            say(said, interrupt=True)
            self.set_status("done", said)
        except Exception as e:
            self.set_status("fail", f"Could not save: {e}")

    # ---------- confirm / ask (called from the agent thread) ----------
    @objc.python_method
    def modal(self, kind, message):
        done, holder = threading.Event(), {}
        self.pending = (kind, done, holder)
        AppHelper.callAfter(self.show_modal, kind, message, done, holder)
        done.wait()
        self.pending = None
        return holder.get("value")

    @objc.python_method
    def show_modal(self, kind, message, done, holder):
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        alert = new_alert()
        alert.setMessageText_("Nemotron Agent" + (" needs your OK" if kind == "confirm" else " has a question"))
        alert.setInformativeText_(message + f"\n\nOr hold {k()} and say " + ("yes or no." if kind == "confirm" else "your answer."))
        field = None
        if kind == "confirm":
            alert.addButtonWithTitle_("Allow")
            alert.addButtonWithTitle_("Stop")
        else:
            field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 24))
            alert.setAccessoryView_(field)
            alert.addButtonWithTitle_("Answer")
            alert.addButtonWithTitle_("Stop")
        self.alert, self.alert_field = alert, field
        NSApp.activateIgnoringOtherApps_(True)
        if field is not None:
            alert.window().setInitialFirstResponder_(field)
        t0 = time.time()
        code = alert.runModal()
        if "value" not in holder:  # not already answered by voice
            first = code == 1000
            holder["value"] = (first if kind == "confirm" else (field.stringValue().strip() if first else None))
        print(f"{kind} answered: {holder['value']!r} after {time.time() - t0:.1f}s", flush=True)
        self.alert = None
        if front is not None:
            front.activateWithOptions_(0)
        time.sleep(0.3)
        done.set()

    @objc.python_method
    def answer_by_voice(self, text):
        if self.pending is None:
            return
        kind, done, holder = self.pending
        t = text.lower().strip(" .!?")
        if kind == "confirm":
            holder["value"] = t.startswith(("yes", "yeah", "yep", "ok", "sure", "allow", "do it", "go"))
        else:
            holder["value"] = None if t in ("stop", "cancel", "") else text
        AppHelper.callAfter(lambda: NSApp.stopModalWithCode_(1001))

    # ---------- menu actions ----------
    def toggle_(self, _):
        self.enabled = not self.enabled
        if not self.enabled and self.busy():
            self.agent.stop()
        self.status_text = f"Ready — hold {k()} to talk" if self.enabled else "Off — the talk key works as normal"
        self.refresh()

    def toggleDry_(self, _):
        self.agent.dry_run = not self.agent.dry_run
        self.refresh()

    def typeTask_(self, _):
        alert = new_alert()
        alert.setMessageText_("What should Nemotron do?")
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 24))
        field.setPlaceholderString_("e.g. open Safari and go to apple.com")
        alert.setAccessoryView_(field)
        alert.addButtonWithTitle_("Run")
        alert.addButtonWithTitle_("Cancel")
        NSApp.activateIgnoringOtherApps_(True)
        alert.window().setInitialFirstResponder_(field)
        if alert.runModal() == 1000 and field.stringValue().strip():
            AppHelper.callLater(0.4, self.start_task, field.stringValue().strip(), "typed")

    def stopTask_(self, _):
        if self.busy():
            self.agent.stop()  # abandons the model call, cuts typing and speech mid-word
            self.state, self.status_text, self.linger_until = "idle", "Stopped", 0.0
            self.refresh()     # orb fades out now; the report is written in the background

    # ---------- settings ----------
    def openSettings_(self, _):
        """API key, voice and switches. Saved to settings.json; used at once, no restart."""
        s = settings.load()
        W, H = 400, 272
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))

        def label(text, y, size=13, x=0, w=W):
            t = NSTextField.labelWithString_(text)
            t.setFrame_(NSMakeRect(x, y, w, 18))
            view.addSubview_(t)
            return t

        label("GMI Cloud API key", H - 18)
        key = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, H - 46, W, 24))
        key.setStringValue_(s["gmi_api_key"])
        key.setPlaceholderString_("paste your key here")
        view.addSubview_(key)
        get = NSButton.buttonWithTitle_target_action_("Get a key at console.gmicloud.ai ↗", self, "openKeyPage:")
        get.setFrame_(NSMakeRect(-6, H - 80, 290, 30))
        view.addSubview_(get)

        label("Hold this key to talk", H - 108)
        kpop = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(-3, H - 136, 300, 26), False)
        keys = list(settings.TALK_KEYS)
        for n in keys:
            kpop.addItemWithTitle_(settings.TALK_KEYS[n]["label"])
        kpop.selectItemAtIndex_(keys.index(s["talk_key"]))
        view.addSubview_(kpop)

        label("Voice", H - 166)
        pop = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(-3, H - 194, 220, 26), False)
        names = list(settings.VOICES)
        for n in names:
            pop.addItemWithTitle_(settings.VOICES[n]["label"])
        pop.selectItemAtIndex_(names.index(s["voice"]))
        view.addSubview_(pop)
        play = NSButton.buttonWithTitle_target_action_("▶ Play sample", self, "playSample:")
        play.setFrame_(NSMakeRect(222, H - 196, 140, 30))
        view.addSubview_(play)

        def check(text, on, y):
            b = NSButton.checkboxWithTitle_target_action_(text, None, None)
            b.setFrame_(NSMakeRect(0, y, W, 20))
            b.setState_(1 if on else 0)
            view.addSubview_(b)
            return b

        speak = check("Say each step out loud", s["speak"], H - 228)
        shots = check("Keep screenshots of each task (off: text log only)", s["save_screenshots"], H - 254)

        alert = new_alert()
        alert.setMessageText_("Nemotron Agent Settings")
        alert.setInformativeText_("Nemotron 3.5 Super VL runs on GMI Cloud. Your key stays on this Mac.")
        alert.setAccessoryView_(view)
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        self.settings_pop = pop
        NSApp.activateIgnoringOtherApps_(True)
        alert.window().setInitialFirstResponder_(key)
        if alert.runModal() != 1000:
            return
        had_key = bool(settings.api_key())
        s.update(gmi_api_key=key.stringValue().strip(), voice=names[pop.indexOfSelectedItem()],
                 talk_key=keys[kpop.indexOfSelectedItem()],
                 speak=bool(speak.state()), save_screenshots=bool(shots.state()))
        settings.save(s)
        apply_settings(s)
        if settings.api_key():
            self.spk.prefetch(PREFETCH)
            self.set_status("idle", f"Ready — hold {k()} to talk" if self.enabled else "Off — the talk key works as normal")
            if not had_key:
                say(f"Hi, I am Nemo. Hold {settings.talk_key(s)['spoken']} and tell me what to do.")
        else:
            self.set_status("idle", "Add your GMI API key in Settings")

    def openKeyPage_(self, _):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(settings.KEY_URL))

    def playSample_(self, _):
        if not settings.api_key():
            self.set_status("idle", "Save your API key first, then play a sample")
            return
        voice_id = settings.VOICES[list(settings.VOICES)[self.settings_pop.indexOfSelectedItem()]]["voice"]

        def run():
            try:
                import tts
                audio, sr, _ = tts.synth(SAMPLE, CONFIG.get("tts_engine", "inworld-tts-2-flash"), voice_id)
                self.spk.play(audio, sr)
            except Exception as e:
                print("sample failed:", e, flush=True)
        threading.Thread(target=run, daemon=True).start()

    def openReport_(self, _):
        if self.last_report and os.path.exists(self.last_report):
            subprocess.Popen(["open", self.last_report])
        else:
            self.openFolder_(None)

    def openFolder_(self, _):
        os.makedirs(SESSIONS, exist_ok=True)
        subprocess.Popen(["open", SESSIONS])

    def openLog_(self, _):
        subprocess.Popen(["open", "-a", "Console", LOG])

    def quit_(self, _):
        if self.busy():
            self.agent.stop()
        NSApp.terminate_(None)


class AppHooks(Hooks):
    def __init__(self, app):
        self.app = app

    def status(self, state, text):
        print(text, flush=True)
        self.app.set_status(state, text)

    def confirm(self, question):
        return bool(self.app.modal("confirm", question))

    def ask(self, question):
        return self.app.modal("ask", question)


def log_crashes():
    """Everything that could kill the app goes to ~/Library/Logs/NemotronAgent.log (menu: Open log)."""
    f = open(LOG, "a", buffering=1)
    faulthandler.enable(file=f)  # hard crashes (segfaults) print their stack here

    def hook(kind):
        def h(*args):
            import traceback
            exc = args[0] if kind == "thread" else None
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] uncaught exception in {kind}\n")
            if exc is not None:
                traceback.print_exception(exc.exc_type, exc.exc_value, exc.exc_traceback, file=f)
            else:
                traceback.print_exception(*args, file=f)
        return h
    sys.excepthook = hook("main thread")
    threading.excepthook = hook("thread")
    f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Nemotron Agent starting (pid {os.getpid()})\n")
    return f


def main():
    logf = log_crashes()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    try:
        AppHelper.runEventLoop(installInterrupt=True)
    finally:
        import traceback
        logf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] event loop ended\n")
        traceback.print_stack(file=logf)


if __name__ == "__main__":
    main()
