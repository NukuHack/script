"""
Screen Change Detector
======================

A real-time screen monitoring tool that detects and highlights changes occurring on 
the display outside of a designated GUI window.

The script creates a Tkinter window whose position and size define an "exclusion zone."
Any significant pixel change outside that zone is reported in the console and marked 
with a red rectangle overlay on the live preview.

All configuration is optional. If no command-line arguments are supplied, sensible
defaults (defined below as module-level constants) are used. Environment variables
may also be used for the save path.

Usage:
    python screen.py
    python screen.py --threshold 5000 --interval 0.05 --window 600
    python screen.py --save-path ./snapshots

Environment Variables:
    SCREEN_MONITOR_SAVE_PATH   Fallback for --save-path if the flag is omitted.

Requirements:
    - Python 3.8+
    - Pillow
    - mss
    - numpy
    - matplotlib
    - pynput (see note below)

Note on pynput:
    Versions 1.8.0 and 1.8.1 contained bugs in keyboard event handling on certain 
    platforms. Use a recent version (1.8.2+) or fallback to 1.7.8 if you experience
    issues with the global hotkey.
"""

import argparse
import os
import queue
import sys
import threading
import time
import tkinter as tk

import matplotlib.pyplot as plt
import mss
import numpy as np
from PIL import Image, ImageDraw
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from pynput import keyboard


# ---------------------------------------------------------------------------
# Fallback defaults (used when CLI args are not supplied or are invalid)
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLD = 10000      # Cumulative pixel-difference threshold
DEFAULT_INTERVAL = 0.1         # Seconds between captures
DEFAULT_WINDOW_SIZE = 400      # Initial exclusion window size (px)
DEFAULT_MAX_TIME_WINDOW = 3.0  # Seconds of frame history kept for comparison
DEFAULT_SAVE_PATH = os.environ.get("SCREEN_MONITOR_SAVE_PATH")  # Optional


def positive_int(value):
    """argparse type: integer > 0, else fall back to the default."""
    try:
        ivalue = int(value)
        if ivalue <= 0:
            raise ValueError
        return ivalue
    except (TypeError, ValueError):
        print(f"[warn] Invalid threshold '{value}'. Using default {DEFAULT_THRESHOLD}.")
        return DEFAULT_THRESHOLD


def positive_float(value):
    """argparse type: float > 0, else fall back to the default."""
    try:
        fvalue = float(value)
        if fvalue <= 0:
            raise ValueError
        return fvalue
    except (TypeError, ValueError):
        print(f"[warn] Invalid interval '{value}'. Using default {DEFAULT_INTERVAL}.")
        return DEFAULT_INTERVAL


class ScreenMonitor:
    """
    A GUI application to monitor screen changes and visualize them in real-time.
    """

    def __init__(self, args):
        """
        Initialize the ScreenMonitor.

        Args:
            args (argparse.Namespace): Parsed CLI arguments. All fields are 
                guaranteed to have valid fallback values.
        """
        self.running = False
        self.frames = []  # Stores tuples of (time, numpy array)
        self.image_queue = queue.Queue()
        self.root = tk.Tk()
        self.root.protocol("WM_DELETE_WINDOW", self.exit)
        self.root.title("Screen Change Detector")
        self.root.geometry(f"{args.window}x{args.window}+100+100")

        # Configuration (all have fallbacks from argparse)
        self.time_interval = args.interval or DEFAULT_INTERVAL
        self.threshold = args.threshold or DEFAULT_THRESHOLD
        self.max_time_window = DEFAULT_MAX_TIME_WINDOW
        self.save_path = args.save_path or DEFAULT_SAVE_PATH

        if self.save_path:
            os.makedirs(self.save_path, exist_ok=True)

        # Window parameters and lock for thread safety
        self.window_params = {'x': 0, 'y': 0, 'width': 0, 'height': 0}
        self.window_params_lock = threading.Lock()
        self.root.bind("<Configure>", self.on_configure)

        # Matplotlib setup
        self.fig, self.ax = plt.subplots(figsize=(8, 6))
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(fill=tk.BOTH, expand=True)
        self.im = None
        self.ax.axis('off')

    def on_configure(self, event):
        """Update window parameters when the window is moved/resized."""
        with self.window_params_lock:
            self.window_params['x'] = self.root.winfo_x()
            self.window_params['y'] = self.root.winfo_y()
            self.window_params['width'] = self.root.winfo_width()
            self.window_params['height'] = self.root.winfo_height()

    def capture(self):
        """
        Continuously capture the screen, compare frames, and queue images for display.
        Runs in a separate daemon thread.
        """
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            while self.running:
                now = time.time()
                try:
                    sct_img = sct.grab(monitor)
                except Exception as e:
                    print(f"[warn] Screen grab failed: {e}")
                    time.sleep(self.time_interval)
                    continue

                pil_img = Image.frombytes(
                    'RGB',
                    (monitor['width'], monitor['height']),
                    sct_img.bgra,
                    'raw',
                    'BGRX',
                )
                arr = np.array(pil_img).astype(np.int32)

                # Get current window parameters
                with self.window_params_lock:
                    window_x = self.window_params['x']
                    window_y = self.window_params['y']
                    window_width = self.window_params['width']
                    window_height = self.window_params['height']

                # Mask the GUI window area so it is excluded from detection
                mask_window = np.zeros((monitor['height'], monitor['width']), dtype=bool)
                y1 = max(0, min(window_y, monitor['height']))
                y2 = max(0, min(window_y + window_height, monitor['height']))
                x1 = max(0, min(window_x, monitor['width']))
                x2 = max(0, min(window_x + window_width, monitor['width']))
                mask_window[y1:y2, x1:x2] = True

                if self.frames:
                    prev_time, prev_arr = self.frames[-1]
                    diff = np.abs(arr - prev_arr).sum(axis=2)
                    diff_total = diff[~mask_window].sum()

                    if diff_total > self.threshold:
                        mask_changes = (np.abs(arr - prev_arr).any(axis=2)) & (~mask_window)
                        coords = np.where(mask_changes)
                        if coords[0].size > 0:
                            tl = (coords[1].min(), coords[0].min())
                            br = (coords[1].max(), coords[0].max())
                            draw = ImageDraw.Draw(pil_img)
                            draw.rectangle([tl, br], outline='red', width=2)
                            print(f"Change detected! Difference: {diff_total}")

                            if self.save_path:
                                fname = os.path.join(
                                    self.save_path,
                                    f"change_{int(now * 1000)}.png",
                                )
                                try:
                                    pil_img.save(fname)
                                except Exception as e:
                                    print(f"[warn] Could not save snapshot: {e}")

                self.frames.append((now, arr))
                while self.frames and self.frames[0][0] < now - self.max_time_window:
                    self.frames.pop(0)

                self.image_queue.put(pil_img)
                time.sleep(self.time_interval)

    def update(self):
        """Update the Matplotlib canvas with images from the queue."""
        try:
            while True:
                img = self.image_queue.get_nowait()
                arr = np.array(img)
                if self.im is None:
                    self.im = self.ax.imshow(arr)
                else:
                    self.im.set_data(arr)
                self.canvas.draw_idle()
        except queue.Empty:
            pass
        finally:
            self.root.after(50, self.update)

    def toggle(self, key):
        """Global hotkey handler to start/stop monitoring."""
        if key == keyboard.Key.home:
            if not self.running:
                print("Monitoring started.")
                self.start()
            else:
                print("Monitoring stopped.")
                self.stop()

    def start(self):
        """Start the capture thread."""
        if not self.running:
            self.running = True
            threading.Thread(target=self.capture, daemon=True).start()
            if not self.frames:
                self.update()

    def stop(self):
        """Stop the capture thread and clear frame history."""
        self.running = False
        self.frames.clear()

    def exit(self):
        """Cleanly exit the application."""
        self.stop()
        self.root.destroy()
        sys.exit()

    def run(self):
        """Start the keyboard listener and the Tkinter main loop."""
        print("Screen Change Detector started.")
        print("Press 'Home' to toggle monitoring.")
        print(f"Threshold: {self.threshold}, Interval: {self.time_interval}s")
        if self.save_path:
            print(f"Snapshots will be saved to: {self.save_path}")
        keyboard.Listener(on_press=self.toggle, daemon=True).start()
        self.root.mainloop()


def parse_args(argv=None):
    """
    Parse CLI args with guaranteed fallbacks. If any value is missing or invalid,
    the module-level defaults are used instead.

    Args:
        argv (list[str] | None): Optional argv override (useful for tests).

    Returns:
        argparse.Namespace: Validated arguments with fallbacks applied.
    """
    parser = argparse.ArgumentParser(
        description="Monitor screen changes outside of a designated window.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--threshold",
        type=positive_int,
        default=DEFAULT_THRESHOLD,
        help="Cumulative pixel difference threshold to trigger a change event.",
    )
    parser.add_argument(
        "--interval",
        type=positive_float,
        default=DEFAULT_INTERVAL,
        help="Time interval in seconds between screen captures.",
    )
    parser.add_argument(
        "--window",
        type=positive_int,
        default=DEFAULT_WINDOW_SIZE,
        help="Initial size (width and height) of the exclusion window.",
    )
    parser.add_argument(
        "--save-path",
        type=str,
        default=DEFAULT_SAVE_PATH,
        help="Directory to save snapshots when changes are detected "
             "(falls back to $SCREEN_MONITOR_SAVE_PATH if set).",
    )

    args = parser.parse_args(argv)

    # Final safety net: if anything somehow ended up None/0, restore defaults.
    if not args.threshold:
        args.threshold = DEFAULT_THRESHOLD
    if not args.interval:
        args.interval = DEFAULT_INTERVAL
    if not args.window:
        args.window = DEFAULT_WINDOW_SIZE

    return args


if __name__ == "__main__":
    ScreenMonitor(parse_args()).run()