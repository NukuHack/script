#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Image Display & Save Utility
=============================

Two modes:

  * GUI mode   :  img_saver.py --gui [--path <base>] [--ext jpg] [...]
                  Paste clipboard images, preview them, save with
                  sequential naming.

  * Save mode  :  img_saver.py <save_path> [--quality N]
                  Grab the current clipboard image and write it straight
                  to <save_path>.  No window, no questions.

  * No args    :  img_saver.py
                  Prints this help.

Works on Python 2.7 and Python 3.x.
Only external dependency: PyQt5.

License: MIT
"""

from __future__ import absolute_import, division, print_function, unicode_literals

import argparse
import logging
import os
import sys

# ---------------------------------------------------------------------------
# PyQt5 is the only external dependency.
# ---------------------------------------------------------------------------
try:
    from PyQt5.QtCore import Qt, QTimer
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
    """Return *value* as unicode, decoding bytes on Python 2."""
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

# Qt format names differ slightly from file extensions.
_QT_FORMAT = {
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "png": "PNG",
    "bmp": "BMP",
    "webp": "WEBP",
}


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
def build_parser():
    parser = argparse.ArgumentParser(
        prog="img_saver",
        description=(
            "Paste, display, and save clipboard images.\n"
            "With --gui you get an interactive window.\n"
            "Without --gui you must supply a <save_path>; the clipboard\n"
            "image is written there and the program exits."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  img_saver.py --gui\n"
            "  img_saver.py --gui --path ~/Pictures/shot --ext png\n"
            "  img_saver.py ~/Pictures/clip.png\n"
            "  img_saver.py /tmp/shot.jpg --quality 95\n"
        ),
    )

    parser.add_argument(
        "path", nargs="?", default=None,
        help="Target image file (save mode) or base path (GUI mode).",
    )
    parser.add_argument(
        "--gui", action="store_true",
        help="Launch the interactive GUI instead of saving directly.",
    )
    parser.add_argument(
        "--ext", "-e", default=DEFAULT_EXTENSION,
        choices=list(SUPPORTED_EXTENSIONS),
        help="Extension used in GUI mode (default: %(default)s).",
    )
    parser.add_argument(
        "--counter", "-c", type=int, default=DEFAULT_COUNTER,
        help="Initial counter in GUI mode (default: %(default)s).",
    )
    parser.add_argument(
        "--quality", "-q", type=int, default=DEFAULT_QUALITY,
        metavar="1-100",
        help="Save quality for jpg/jpeg/webp (default: %(default)s).",
    )
    parser.add_argument(
        "--width", "-W", type=int, default=DEFAULT_WINDOW_WIDTH,
        help="Initial GUI window width (default: %(default)s).",
    )
    parser.add_argument(
        "--height", "-H", type=int, default=DEFAULT_WINDOW_HEIGHT,
        help="Initial GUI window height (default: %(default)s).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser


def parse_args(argv):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not (1 <= args.quality <= 100):
        parser.error("--quality must be between 1 and 100")
    if args.counter < 0:
        parser.error("--counter must be >= 0")

    if args.path is not None:
        args.path = _fs_unicode(args.path)
    args.ext = _fs_unicode(args.ext)

    return args


# ---------------------------------------------------------------------------
# GUI class (unchanged in spirit)
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

        self.base_path = base_path.rstrip("._") or DEFAULT_BASE_PATH
        self.extension = extension.lstrip(".").lower()
        self.save_counter = max(0, int(counter))
        self.quality = int(quality)
        self.current_image = None

        self._ensure_parent_dir(self._build_path_string(self.save_counter))

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
        if counter and counter > 0:
            return "{0}_{1}.{2}".format(self.base_path, counter, self.extension)
        return "{0}.{1}".format(self.base_path, self.extension)

    @staticmethod
    def _ensure_parent_dir(path):
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent)
            except OSError:
                pass

    @staticmethod
    def _split_path(text, default_ext):
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
        image = self.clipboard.image()
        if image.isNull():
            self.statusBar().showMessage("Clipboard contains no image.", 3000)
            logging.info("Clipboard contains no image.")
            return

        self.current_image = image
        self.display_image()

        next_counter = self._next_free_counter()
        self.save_counter = next_counter
        self._set_counter_silently(next_counter)
        self._refresh_path_display()

        self.statusBar().showMessage(
            "Pasted {0}x{1} image.  Ctrl+S to save.".format(
                image.width(), image.height()),
            4000,
        )
        logging.debug("Pasted image %dx%d", image.width(), image.height())

    def _next_free_counter(self, start=0, limit=999999):
        c = start
        while c <= limit:
            if not os.path.exists(self._build_path_string(c)):
                return c
            c += 1
        return start

    def save_current_image(self):
        if self.path_input.hasFocus() or self.counter_input.hasFocus():
            return

        if self.current_image is None or self.current_image.isNull():
            self.statusBar().showMessage("Nothing to save.", 3000)
            logging.info("No image to save.")
            return

        counter = self.counter_input.value()
        save_path = self._build_path_string(counter)
        self._ensure_parent_dir(save_path)

        fmt = _QT_FORMAT.get(self.extension, self.extension.upper())
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
            self.statusBar().showMessage("Saved: {0}".format(save_path), 4000)
            self.counter_input.setValue(counter + 1)
        else:
            QMessageBox.warning(
                self, "Save Failed",
                "Failed to save image to:\n{0}".format(save_path))
            self.statusBar().showMessage("Save failed.", 4000)
            logging.error("Save failed for %s", save_path)

    def display_image(self):
        if self.current_image is None or self.current_image.isNull():
            return

        label_w = max(1, self.image_label.width())
        label_h = max(1, self.image_label.height())
        img = self.current_image

        if img.width() > label_w or img.height() > label_h:
            img = img.scaled(label_w, label_h,
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)

        self.image_label.setPixmap(QPixmap.fromImage(img))

    def resizeEvent(self, event):
        super(ImageDisplayApp, self).resizeEvent(event)
        self.display_image()


# ---------------------------------------------------------------------------
# Save-mode (headless) helper
# ---------------------------------------------------------------------------
def _pick_extension(target_path):
    """Return (final_path, extension) — adds .png if user gave no extension."""
    ext = os.path.splitext(target_path)[1].lstrip(".").lower()
    if ext in SUPPORTED_EXTENSIONS:
        return target_path, ext
    if ext == "":
        return target_path + ".png", "png"
    # Unknown extension: still try, but treat as given format string.
    return target_path, ext


def _do_save_clipboard(target_path, quality):
    """
    Read the clipboard image and write it to *target_path*.

    Returns 0 on success, non-zero on failure.
    Called from within a QApplication event loop.
    """
    target_path, ext = _pick_extension(target_path)

    parent = os.path.dirname(os.path.abspath(target_path))
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent)
        except OSError as exc:
            sys.stderr.write(
                "ERROR: could not create directory {0}: {1}\n".format(
                    parent, exc))
            return 3

    clipboard = QApplication.clipboard()
    image = clipboard.image()
    if image is NullImage():
        sys.stderr.write("ERROR: clipboard contains no image.\n")
        return 2

    fmt = _QT_FORMAT.get(ext, ext.upper() or "PNG")
    try:
        if ext in LOSSY_EXTENSIONS:
            ok = image.save(target_path, fmt, int(quality))
        else:
            ok = image.save(target_path, fmt)
    except Exception as exc:
        sys.stderr.write("ERROR: exception while saving: {0}\n".format(exc))
        return 4

    if not ok:
        sys.stderr.write(
            "ERROR: Qt failed to save image to {0} (format={1}).\n".format(
                target_path, fmt))
        return 4

    sys.stdout.write("Saved: {0}\n".format(target_path))
    return 0


def NullImage():
    """Lazy accessor so we don't import QImage at module import time."""
    from PyQt5.QtGui import QImage
    return QImage()


def run_save_mode(target_path, quality):
    """
    Headless save: create a QApplication, schedule the save, run the event
    loop briefly so clipboard transfers on X11/Wayland can complete.
    """
    app = QApplication(sys.argv[:1])
    result = [1]

    def _run():
        try:
            result[0] = _do_save_clipboard(target_path, quality)
        finally:
            app.quit()

    QTimer.singleShot(0, _run)
    app.exec_()
    return result[0]


def run_gui_mode(args):
    """Launch the interactive GUI."""
    qargv = sys.argv[:1] if sys.argv else ["img_saver"]
    app = QApplication(qargv)

    base_path = args.path if args.path else DEFAULT_BASE_PATH
    window = ImageDisplayApp(
        base_path=base_path,
        extension=args.ext,
        counter=args.counter,
        quality=args.quality,
        window_size=(args.width, args.height),
    )
    window.show()
    return app.exec_()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()

    # No arguments at all -> print help, exit non-zero.
    if not argv:
        parser.print_help()
        return 1

    args = parser.parse_args(argv)

    if not (1 <= args.quality <= 100):
        parser.error("--quality must be between 1 and 100")
    if args.counter < 0:
        parser.error("--counter must be >= 0")

    if args.path is not None:
        args.path = _fs_unicode(args.path)
    args.ext = _fs_unicode(args.ext)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.gui:
        return run_gui_mode(args)

    # Save mode requires a target path.
    if not args.path:
        parser.error(
            "a <save_path> is required unless --gui is used.\n"
            "       Run 'img_saver.py' with no arguments for help."
        )

    return run_save_mode(args.path, args.quality)


if __name__ == "__main__":
    sys.exit(main())

