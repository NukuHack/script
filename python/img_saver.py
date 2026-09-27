#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Image Display & Save Utility
=============================

A GUI utility for pasting images from the clipboard, previewing them,
and saving them with automatic sequential naming.

Works on Python 2.7 and Python 3.x.
The only external dependency is PyQt5.

Features
--------
* Ctrl+V : paste an image from the system clipboard
* Ctrl+S : save the current image, then auto-increment the counter
* Editable save path with automatic counter/extension detection
* Spinbox for manual counter adjustment
* "Next free counter" auto-detection on paste (never overwrites silently)
* Status bar with feedback
* Configurable via command-line arguments

Usage
-----
    python img_saver.py [OPTIONS]

Examples
--------
    python img_saver.py
    python img_saver.py --path "C:/Screenshots/capture" --ext png
    python img_saver.py --path "/home/user/pics/shot" --counter 42 --quality 95

License: MIT
"""

from __future__ import absolute_import, division, print_function, unicode_literals

import argparse
import logging
import os
import sys

# ---------------------------------------------------------------------------
# PyQt5 is the only external dependency.  Fail with a clear message if missing.
# ---------------------------------------------------------------------------
try:
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap, QKeySequence
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QLineEdit, QVBoxLayout, QWidget,
        QShortcut, QLabel, QHBoxLayout, QSpinBox, QMessageBox,
    )
except ImportError:
    sys.stderr.write(
        "ERROR: PyQt5 is required.\n"
        "       Install it with:  pip install PyQt5\n"
        "       (On Python 2.7 you need PyQt5 <= 5.15.)\n"
    )
    raise


# ---------------------------------------------------------------------------
# Cross-version helpers
# ---------------------------------------------------------------------------
PY2 = sys.version_info[0] == 2


def _fs_unicode(value):
    """Return *value* as a unicode string, decoding bytes on Python 2."""
    if PY2 and isinstance(value, bytes):
        enc = sys.getfilesystemencoding() or 'utf-8'
        try:
            return value.decode(enc)
        except (LookupError, UnicodeDecodeError):
            return value.decode('utf-8', 'replace')
    return value


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
DEFAULT_BASE_PATH = os.path.join(os.path.expanduser("~"), "Pictures", "image")
DEFAULT_EXTENSION = "jpg"
DEFAULT_COUNTER = 0
DEFAULT_QUALITY = 90
DEFAULT_WINDOW_WIDTH = 800
DEFAULT_WINDOW_HEIGHT = 600

SUPPORTED_EXTENSIONS = ("jpg", "jpeg", "png", "bmp", "webp")
LOSSY_EXTENSIONS = ("jpg", "jpeg", "webp")


# Enable High-DPI scaling (must happen before QApplication is constructed).
for _attr in ("AA_EnableHighDpiScaling", "AA_UseHighDpiPixmaps"):
    if hasattr(Qt, _attr):
        try:
            QApplication.setAttribute(getattr(Qt, _attr), True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    """Parse command-line arguments.  *argv* defaults to sys.argv[1:]."""
    parser = argparse.ArgumentParser(
        prog="img_saver",
        description="Paste, display, and save clipboard images with sequential naming.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--path", "-p", default=DEFAULT_BASE_PATH,
                        help="Base path (without counter or extension).")
    parser.add_argument("--ext", "-e", default=DEFAULT_EXTENSION,
                        choices=list(SUPPORTED_EXTENSIONS),
                        help="Image format to save as.")
    parser.add_argument("--counter", "-c", type=int, default=DEFAULT_COUNTER,
                        help="Initial save counter (0 = no numeric suffix).")
    parser.add_argument("--quality", "-q", type=int, default=DEFAULT_QUALITY,
                        metavar="1-100",
                        help="Save quality for lossy formats (jpg/jpeg/webp).")
    parser.add_argument("--width", "-W", type=int, default=DEFAULT_WINDOW_WIDTH,
                        help="Initial window width.")
    parser.add_argument("--height", "-H", type=int, default=DEFAULT_WINDOW_HEIGHT,
                        help="Initial window height.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable DEBUG logging.")

    args = parser.parse_args(argv)

    if not (1 <= args.quality <= 100):
        parser.error("--quality must be between 1 and 100")
    if args.counter < 0:
        parser.error("--counter must be >= 0")

    # Normalize strings on Python 2 (argparse gives bytes there).
    args.path = _fs_unicode(args.path)
    args.ext = _fs_unicode(args.ext)

    return args


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------
class ImageDisplayApp(QMainWindow):
    """Main application window for pasting, displaying, and saving images."""

    def __init__(self,
                 base_path=DEFAULT_BASE_PATH,
                 extension=DEFAULT_EXTENSION,
                 counter=DEFAULT_COUNTER,
                 quality=DEFAULT_QUALITY,
                 window_size=(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)):
        super(ImageDisplayApp, self).__init__()

        # ---- configuration ------------------------------------------------
        self.base_path = base_path.rstrip("._") or DEFAULT_BASE_PATH
        self.extension = extension.lstrip(".").lower()
        self.save_counter = max(0, int(counter))
        self.quality = int(quality)
        self.current_image = None

        self._ensure_parent_dir(self._build_path_string(self.save_counter))

        # ---- window -------------------------------------------------------
        self.setWindowTitle("Image Display & Save")
        self.resize(*window_size)
        self.setMinimumSize(320, 240)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)

        # ---- path + counter row ------------------------------------------
        path_layout = QHBoxLayout()
        path_layout.setSpacing(8)

        self.path_input = QLineEdit(self._build_path_string(self.save_counter))
        self.path_input.setStyleSheet(
            "QLineEdit { font-size: 13px; border: 1px solid #ccc;"
            " background: #fafafa; padding: 4px; }"
        )
        self.path_input.setMinimumHeight(32)
        self.path_input.editingFinished.connect(self._on_path_edited)
        path_layout.addWidget(self.path_input, 1)

        counter_label = QLabel("Save #:")
        path_layout.addWidget(counter_label)

        self.counter_input = QSpinBox()
        self.counter_input.setStyleSheet(
            "QSpinBox { font-size: 13px; border: 1px solid #ccc;"
            " background: #fafafa; padding: 4px; }"
        )
        self.counter_input.setRange(0, 999999)
        self.counter_input.setValue(self.save_counter)
        self.counter_input.setFixedWidth(80)
        self.counter_input.setMinimumHeight(32)
        self.counter_input.valueChanged.connect(self._on_counter_changed)
        path_layout.addWidget(self.counter_input)

        main_layout.addLayout(path_layout)

        # ---- image preview ------------------------------------------------
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet(
            "border: 1px solid #ccc; background: #f8f8f8; color: #888;"
        )
        self.image_label.setText("Paste an image with Ctrl+V")
        self.image_label.setMinimumSize(200, 200)
        main_layout.addWidget(self.image_label, 1)

        # ---- shortcuts ----------------------------------------------------
        self.paste_shortcut = QShortcut(QKeySequence("Ctrl+V"), self)
        self.paste_shortcut.activated.connect(self.handle_paste)

        self.save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        self.save_shortcut.activated.connect(self.save_current_image)

        # ---- status bar ---------------------------------------------------
        self.statusBar().showMessage(
            "Ready.  Ctrl+V to paste,  Ctrl+S to save."
        )

        # ---- clipboard ----------------------------------------------------
        self.clipboard = QApplication.clipboard()

        self.image_label.setFocus()
        logging.debug(
            "Initialized: base_path=%s ext=%s counter=%d quality=%d",
            self.base_path, self.extension, self.save_counter, self.quality,
        )

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------
    def _build_path_string(self, counter):
        """Build the display path string for a given counter value."""
        if counter and counter > 0:
            return "{0}_{1}.{2}".format(self.base_path, counter, self.extension)
        return "{0}.{1}".format(self.base_path, self.extension)

    @staticmethod
    def _ensure_parent_dir(path):
        """Best-effort creation of the parent directory of *path*."""
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent)
            except OSError:
                pass

    @staticmethod
    def _split_path(text, default_ext):
        """
        Split a full path string into (base, counter_or_None, extension).

        Recognizes patterns like '/foo/bar_42.jpg'.
        """
        stem = text
        ext = default_ext
        lower = stem.lower()
        for e in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
            if lower.endswith(e):
                stem = stem[: -len(e)]
                ext = e[1:]
                break

        stem = stem.rstrip("._")

        if "_" in stem:
            head, _, tail = stem.rpartition("_")
            if head and tail.isdigit():
                return head, int(tail), ext

        return stem, None, ext

    def _on_path_edited(self):
        """Parse the user-edited path field to extract base, counter, extension."""
        text = self.path_input.text().strip()
        if not text:
            self._refresh_path_display()
            return

        base, counter, ext = self._split_path(text, self.extension)

        if base:
            self.base_path = base
        self.extension = ext

        if counter is not None:
            self.save_counter = counter
            self._set_counter_silently(counter)

        self._refresh_path_display()
        logging.debug("Parsed path: base=%s counter=%s ext=%s",
                      self.base_path, self.save_counter, self.extension)

    def _set_counter_silently(self, value):
        """Set the spinbox value without emitting valueChanged."""
        self.counter_input.blockSignals(True)
        try:
            self.counter_input.setValue(value)
        finally:
            self.counter_input.blockSignals(False)

    def _on_counter_changed(self, value):
        self.save_counter = value
        self._refresh_path_display()

    def _refresh_path_display(self):
        self.path_input.setText(self._build_path_string(self.save_counter))

    # ------------------------------------------------------------------
    # Clipboard / image handling
    # ------------------------------------------------------------------
    def handle_paste(self):
        """
        Route Ctrl+V.

        If a text widget is focused we forward the paste to it so that
        Ctrl+V keeps working inside the path field and the spinbox.
        """
        if self.path_input.hasFocus():
            self.path_input.paste()
            return
        if self.counter_input.hasFocus():
            editor = self.counter_input.lineEdit()
            if editor is not None:
                editor.paste()
            return
        self.paste_from_clipboard()

    def paste_from_clipboard(self):
        """Paste image data from the system clipboard."""
        image = self.clipboard.image()
        if image.isNull():
            self.statusBar().showMessage("Clipboard contains no image.", 3000)
            logging.info("Clipboard contains no image.")
            return

        self.current_image = image
        self.display_image()

        # Reset to the next available counter for the current base path so
        # we never silently overwrite an existing file.
        next_counter = self._next_free_counter()
        self.save_counter = next_counter
        self._set_counter_silently(next_counter)
        self._refresh_path_display()

        self.statusBar().showMessage(
            "Pasted {0}×{1} image.  Ctrl+S to save.".format(
                image.width(), image.height()),
            4000,
        )
        logging.debug("Pasted image %dx%d", image.width(), image.height())

    def _next_free_counter(self, start=0, limit=999999):
        """Return the first counter whose target file does not yet exist."""
        c = start
        while c <= limit:
            if not os.path.exists(self._build_path_string(c)):
                return c
            c += 1
        return start

    def save_current_image(self):
        """Save the currently displayed image to disk."""
        # Don't hijack Ctrl+S while the user is editing a text field.
        if self.path_input.hasFocus() or self.counter_input.hasFocus():
            return

        if self.current_image is None or self.current_image.isNull():
            self.statusBar().showMessage("Nothing to save.", 3000)
            logging.info("No image to save.")
            return

        counter = self.counter_input.value()
        save_path = self._build_path_string(counter)
        self._ensure_parent_dir(save_path)

        fmt = self.extension.upper()
        try:
            if self.extension in LOSSY_EXTENSIONS:
                ok = self.current_image.save(save_path, fmt, self.quality)
            else:
                ok = self.current_image.save(save_path, fmt)
        except Exception as exc:
            ok = False
            logging.exception("Exception during save: %s", exc)

        if ok:
            logging.info("Saved %s", save_path)
            self.statusBar().showMessage(
                "Saved: {0}".format(save_path), 4000)
            # Auto-increment for next save (emits valueChanged -> refresh).
            self.counter_input.setValue(counter + 1)
        else:
            QMessageBox.warning(
                self, "Save Failed",
                "Failed to save image to:\n{0}".format(save_path))
            self.statusBar().showMessage("Save failed.", 4000)
            logging.error("Save failed for %s", save_path)

    def display_image(self):
        """Render the current image, scaling down to fit the label if needed."""
        if self.current_image is None or self.current_image.isNull():
            return

        label_w = max(1, self.image_label.width())
        label_h = max(1, self.image_label.height())
        img = self.current_image

        # Only scale *down*; never upscale a small image.
        if img.width() > label_w or img.height() > label_h:
            img = img.scaled(label_w, label_h,
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)

        self.image_label.setPixmap(QPixmap.fromImage(img))

    # ------------------------------------------------------------------
    # Qt event overrides
    # ------------------------------------------------------------------
    def resizeEvent(self, event):
        super(ImageDisplayApp, self).resizeEvent(event)
        self.display_image()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv=None):
    """Application entry point."""
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Qt's QApplication wants argv; don't leak our custom flags into it.
    qargv = sys.argv[:1] if sys.argv else ["img_saver"]
    app = QApplication(qargv)

    window = ImageDisplayApp(
        base_path=args.path,
        extension=args.ext,
        counter=args.counter,
        quality=args.quality,
        window_size=(args.width, args.height),
    )
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
