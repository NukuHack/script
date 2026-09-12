#!/usr/bin/env python3
"""
Arrow keys -> mouse move, PageUp -> left click, PageDown -> right click.

Install:  pip install pynput
Wayland:  also install `ydotool` (and run its daemon: `ydotoold &`).
"""

import shutil
import subprocess
import sys
import threading
import time

from pynput import keyboard
from pynput.mouse import Button, Controller

# ---------- tuning ----------
STEP  = 12       # pixels per tick
TICK  = 0.015    # seconds between ticks (~800 px/s with STEP=12)
# ----------------------------

# ---- Detect whether pynput's mouse controller actually works ----
# On Wayland, pynput can *listen* but can't *control*; fall back to ydotool.
USE_YDOTOOL = False
if sys.platform.startswith("linux"):
    # Crude but effective: try moving the pointer 1px and back.
    _m = Controller()
    try:
        _before = _m.position
        _m.move(1, 0)
        _after = _m.position
        _m.move(-1, 0)
        if _before == _after:
            raise RuntimeError("mouse didn't move")
    except Exception:
        if shutil.which("ydotool"):
            USE_YDOTOOL = True
        else:
            print("Warning: pynput can't control the mouse and ydotool "
                  "isn't installed. Mouse actions will do nothing.",
                  file=sys.stderr)

mouse = Controller()

# ---- Normalise pynput keys to stable lowercase strings ----
def key_name(key):
    # Key.up / Key.page_up etc. -> "up" / "page_up"
    name = getattr(key, "name", None)
    if name:
        return name.lower()
    # Character keys (not used here, but harmless)
    char = getattr(key, "char", None)
    if char:
        return char.lower()
    return None

MOVE_MAP = {
    "up":    (0, -STEP),
    "down":  (0,  STEP),
    "left":  (-STEP, 0),
    "right": ( STEP, 0),
}

CLICK_MAP = {
    "page_up":   Button.left,
    "page_down": Button.right,
}

pressed = set()
lock = threading.Lock()


def do_move(dx, dy):
    if USE_YDOTOOL:
        # ydotool mousemove --relative dx dy
        subprocess.run(["ydotool", "mousemove", "--", str(dx), str(dy)],
                       check=False)
    else:
        mouse.move(dx, dy)


def do_click(button):
    if USE_YDOTOOL:
        # 0xC0 = left click, 0xC1 = right click (ydotool key codes)
        code = "0xC0" if button == Button.left else "0xC1"
        subprocess.run(["ydotool", "click", code], check=False)
    else:
        mouse.click(button, 1)


def worker():
    while True:
        with lock:
            active = list(pressed)
        for name in active:
            if name in MOVE_MAP:
                dx, dy = MOVE_MAP[name]
                do_move(dx, dy)
            elif name in CLICK_MAP:
                do_click(CLICK_MAP[name])
        time.sleep(TICK)


def on_press(key):
    name = key_name(key)
    if name in MOVE_MAP or name in CLICK_MAP:
        with lock:
            pressed.add(name)


def on_release(key):
    name = key_name(key)
    with lock:
        pressed.discard(name)


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    mode = "ydotool" if USE_YDOTOOL else "pynput"
    print(f"listening… ({mode} backend, Ctrl-C to quit)")
    try:
        with keyboard.Listener(on_press=on_press,
                               on_release=on_release) as listener:
            listener.join()
    except KeyboardInterrupt:
        pass