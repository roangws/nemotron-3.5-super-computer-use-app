"""The orb: a looping, voice-reactive animation shown while the agent listens, works or talks.

Frames come from assets/orb_frames (made from assets/orb-loop.mp4; black turned transparent).
Call `tick(dt, active, level)` about 60 times a second from the main thread.
"""
import glob
import math
import os

from AppKit import NSBackingStoreBuffered, NSColor, NSFont, NSImage, NSMakeRect, NSScreen, NSTextField, NSView, NSWindow
from Quartz import CALayer, CATransaction, CATransform3DMakeScale, CATransform3DTranslate, CGColorCreateGenericRGB
import Quartz

from paths import ASSETS
SIZE = 120          # orb size in points
PAD = 60            # room around the orb for the pulse
BOTTOM = 24         # gap between the caption and the orb
CAPTION_H = 30
FPS = 30            # source video frame rate


def _ease(t):
    """Smoothstep-like ease in/out (0..1 -> 0..1)."""
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


class Orb:
    def __init__(self, position="bottom"):
        files = sorted(glob.glob(os.path.join(ASSETS, "orb_frames", "*.png")))
        self.frames = []
        for f in files:
            img = NSImage.alloc().initWithContentsOfFile_(f)
            cg = img.CGImageForProposedRect_context_hints_(None, None, None)[0]
            self.frames.append(cg)
        sf = NSScreen.mainScreen().frame()
        W = SIZE + 2 * PAD
        H = SIZE + PAD + BOTTOM + CAPTION_H
        x = (sf.size.width - W) / 2
        y = 86 if position == "bottom" else (sf.size.height - H) / 2   # bottom: just above the Dock
        w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H), 0, NSBackingStoreBuffered, False)
        w.setOpaque_(False)
        w.setBackgroundColor_(NSColor.clearColor())
        w.setIgnoresMouseEvents_(True)
        w.setLevel_(Quartz.CGShieldingWindowLevel())
        w.setHasShadow_(False)
        w.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 8))
        # Visible to screen recorders (Screen Studio, QuickTime). The agent's own screenshots skip this
        # window instead (screen.EXCLUDE_WINDOW), so the model still never sees the orb.
        w.setSharingType_(1)
        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        root.setWantsLayer_(True)
        w.setContentView_(root)
        self.window, self.root = w, root

        self.layer = CALayer.layer()
        self.layer.setFrame_(((PAD, BOTTOM + CAPTION_H), (SIZE, SIZE)))
        self.layer.setContentsGravity_("resizeAspect")
        if self.frames:
            self.layer.setContents_(self.frames[0])
        root.layer().addSublayer_(self.layer)

        # caption pill under the orb
        self.caption_bg = CALayer.layer()
        self.caption_bg.setBackgroundColor_(CGColorCreateGenericRGB(0.06, 0.06, 0.08, 0.82))
        self.caption_bg.setCornerRadius_(CAPTION_H / 2 - 2)
        root.layer().addSublayer_(self.caption_bg)
        self.caption = NSTextField.labelWithString_("")
        self.caption.setFont_(NSFont.systemFontOfSize_weight_(12, 0.3))
        self.caption.setTextColor_(NSColor.whiteColor())
        self.caption.setAlignment_(1)  # center
        root.addSubview_(self.caption)
        self.set_caption("")

        self.t = 0.0          # animation clock (s)
        self.show = 0.0       # 0 hidden .. 1 shown (eased)
        self.level = 0.0      # smoothed voice level
        self.visible = False

    def set_caption(self, text):
        text = (text or "")[:60]
        self.caption.setStringValue_(text)
        W = self.root.frame().size.width
        tw = min(W - 8, max(40, len(text) * 6.8 + 24)) if text else 0
        self.caption.setFrame_(NSMakeRect((W - tw) / 2, 7, tw, 16))
        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        self.caption_bg.setFrame_((((W - tw) / 2, 2), (tw, CAPTION_H - 4)))
        self.caption_bg.setHidden_(not text)
        CATransaction.commit()

    def tick(self, dt, active, level):
        """active: should the orb be on screen. level: 0..~0.3 loudness (mic or voice)."""
        # smooth show/hide over ~0.35 s
        target = 1.0 if active else 0.0
        step = dt / 0.35
        self.show = min(target, self.show + step) if target > self.show else max(target, self.show - step)
        if self.show <= 0.0:
            if self.visible:
                self.window.orderOut_(None)
                self.visible = False
            return
        if not self.visible:
            self.window.orderFrontRegardless()
            self.visible = True

        # voice level: fast attack, slow release, so it pulses with syllables but never jitters
        lv = min(1.0, level * 9.0)
        k = 1 - math.exp(-dt / (0.05 if lv > self.level else 0.22))
        self.level += (lv - self.level) * k

        self.t += dt
        if self.frames:
            self.layer.setContents_(self.frames[int(self.t * FPS) % len(self.frames)])
        e = _ease(self.show)
        breathe = 0.015 * math.sin(self.t * 2.2)                     # gentle idle breathing
        scale = (0.55 + 0.45 * e) * (1.0 + breathe + 0.28 * self.level)
        bob = 5.0 * self.level * math.sin(self.t * 17.0)              # slight "talking" wobble
        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        tr = CATransform3DMakeScale(scale, scale, 1)
        self.layer.setTransform_(CATransform3DTranslate(tr, 0, bob, 0))
        self.layer.setOpacity_(e)
        self.caption_bg.setOpacity_(e)
        CATransaction.commit()
        self.caption.setAlphaValue_(e)
