#!/usr/bin/env python3
"""
Image Display & Save Utility
=============================

A PyQt5-based GUI application for pasting, displaying, and saving images from the clipboard.
Designed for efficient screenshot/image capture workflows where images need to be saved
with sequential naming conventions.

Features:
    - Paste images directly from clipboard (Ctrl+V)
    - Display pasted images in a resizable preview area
    - Save images with automatic sequential numbering (Ctrl+S)
    - Editable save path with automatic counter detection
    - Spinbox for manual counter adjustment
    - Configurable base path, file extension, and starting counter via CLI args

Keyboard Shortcuts:
    Ctrl+V  - Paste image from clipboard
    Ctrl+S  - Save current image (auto-increments counter)

Usage:
    python image_saver.py [OPTIONS]

Examples:
    python image_saver.py
    python image_saver.py --path "C:/Screenshots/capture" --ext png
    python image_saver.py --path "/home/user/pics/shot" --counter 42 --quality 95

Author: (original author unknown)
License: MIT
"""

import sys
import argparse
import logging
from pathlib import Path
from typing import Optional

# PyQt5 imports - works with PyQt5 >= 5.15
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QLineEdit, QVBoxLayout, QWidget,
    QShortcut, QLabel, QHBoxLayout, QSpinBox, QMessageBox
)
from PyQt5.QtGui import QPixmap, QImage, QKeySequence
from PyQt5.QtCore import Qt

# ---------------------------------------------------------------------------
# Configuration defaults (used when CLI args are not supplied)
# ---------------------------------------------------------------------------
DEFAULT_BASE_PATH = str(Path.home() / "Pictures" / "image")
DEFAULT_EXTENSION = "jpg"
DEFAULT_COUNTER = 0
DEFAULT_QUALITY = 90
DEFAULT_WINDOW_WIDTH = 800
DEFAULT_WINDOW_HEIGHT = 600

# Enable High-DPI scaling (must be set before QApplication is created)
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    """
    Parse command-line arguments with sensible fallbacks.

    Args:
        argv: Optional list of arguments (defaults to sys.argv[1:]).

    Returns:
        argparse.Namespace containing all configuration values.
    """
    parser = argparse.ArgumentParser(
        prog="image_saver",
        description="Paste, display, and save clipboard images with sequential naming.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--path", "-p",
        type=str,
        default=DEFAULT_BASE_PATH,
        help="Base path (without counter or extension) for saved images.",
    )
    parser.add_argument(
        "--ext", "-e",
        type=str,
        default=DEFAULT_EXTENSION,
        choices=["jpg", "jpeg", "png", "bmp", "webp"],
        help="Image file extension/format to save as.",
    )
    parser.add_argument(
        "--counter", "-c",
        type=int,
        default=DEFAULT_COUNTER,
        help="Initial save counter value. 0 means no numeric suffix.",
    )
    parser.add_argument(
        "--quality", "-q",
        type=int,
        default=DEFAULT_QUALITY,
        choices=range(1, 101),
        metavar="[1-100]",
        help="JPEG/WebP save quality (ignored for lossless formats).",
    )
    parser.add_argument(
        "--width", "-W",
        type=int,
        default=DEFAULT_WINDOW_WIDTH,
        help="Initial window width in pixels.",
    )
    parser.add_argument(
        "--height", "-H",
        type=int,
        default=DEFAULT_WINDOW_HEIGHT,
        help="Initial window height in pixels.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose (DEBUG) logging.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------
class ImageDisplayApp(QMainWindow):
    """
    Main application window for pasting, displaying, and saving images.

    Attributes:
        base_path: Base filesystem path (without counter/extension).
        extension: File extension used when saving (e.g., 'jpg').
        save_counter: Current counter value for sequential file naming.
        quality: Save quality for lossy formats.
        current_image: The QImage currently displayed/saved.
    """

    def __init__(
        self,
        base_path: str = DEFAULT_BASE_PATH,
        extension: str = DEFAULT_EXTENSION,
        counter: int = DEFAULT_COUNTER,
        quality: int = DEFAULT_QUALITY,
        window_size: tuple = (DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT),
    ):
        """
        Initialize the application window.

        Args:
            base_path: Base filesystem path for saved images.
            extension: File extension ('jpg', 'png', etc.).
            counter: Initial counter value.
            quality: Save quality for lossy formats (1-100).
            window_size: (width, height) tuple for the window size.
        """
        super().__init__()

        # --- Configuration -------------------------------------------------
        self.base_path = base_path.rstrip("._")
        self.extension = extension.lstrip(".").lower()
        self.save_counter = counter
        self.quality = quality

        # Ensure base directory exists (best-effort; user may edit path later)
        try:
            Path(self.base_path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logging.warning("Could not create base directory: %s", exc)

        # --- Window setup --------------------------------------------------
        self.setWindowTitle("Image Display & Save")
        self.setFixedSize(*window_size)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)

        # --- Path + counter row -------------------------------------------
        path_layout = QHBoxLayout()
        path_layout.setSpacing(10)

        self.path_input = QLineEdit(self._build_path_string(self.save_counter))
        self.path_input.setStyleSheet(
            "QLineEdit { font-size: 14px; border: 1px solid #ccc; "
            "background: #f0f0f0; padding: 2px; }"
        )
        self.path_input.setFixedHeight(40)
        self.path_input.editingFinished.connect(self.update_base_path)
        self.path_input.setFocusPolicy(Qt.StrongFocus)
        path_layout.addWidget(self.path_input, stretch=1)

        counter_label = QLabel("Save #:")
        counter_label.setStyleSheet("font-size: 14px;")
        path_layout.addWidget(counter_label)

        self.counter_input = QSpinBox()
        self.counter_input.setStyleSheet(
            "QSpinBox { font-size: 14px; border: 1px solid #ccc; "
            "background: #f0f0f0; padding: 2px; }"
        )
        self.counter_input.setFixedWidth(70)
        self.counter_input.setFixedHeight(40)
        self.counter_input.setRange(0, 999999)
        self.counter_input.setValue(self.save_counter)
        self.counter_input.setFocusPolicy(Qt.StrongFocus)
        self.counter_input.valueChanged.connect(self.update_counter)
        path_layout.addWidget(self.counter_input)

        main_layout.addLayout(path_layout)

        # --- Image display -------------------------------------------------
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet(
            "border: 1px solid #ccc; background: #f8f8f8;"
        )
        self.image_label.setFocusPolicy(Qt.StrongFocus)
        self.image_label.setMinimumSize(200, 200)
        main_layout.addWidget(self.image_label, stretch=1)

        # --- Clipboard & shortcuts ----------------------------------------
        self.clipboard = QApplication.clipboard()

        self.paste_shortcut = QShortcut(QKeySequence("Ctrl+V"), self)
        self.paste_shortcut.activated.connect(self.handle_paste)

        self.save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        self.save_shortcut.activated.connect(self.save_current_image)

        # --- State ---------------------------------------------------------
        self.current_image: Optional[QImage] = None

        self.image_label.setFocus()
        logging.debug(
            "Initialized: base_path=%s ext=%s counter=%d quality=%d",
            self.base_path, self.extension, self.save_counter, self.quality,
        )

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------
    def _build_path_string(self, counter: int) -> str:
        """Build the display path string for a given counter value."""
        if counter == 0:
            return f"{self.base_path}.{self.extension}"
        return f"{self.base_path}_{counter}.{self.extension}"

    def update_base_path(self):
        """
        Parse the user-edited path field to extract base path and counter.

        Recognizes patterns like '/foo/bar_42.jpg' and splits them into
        base path '/foo/bar' and counter 42.
        """
        full_path = self.path_input.text().strip()
        if not full_path:
            return

        # Strip known extension
        stem = full_path
        for ext in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                self.extension = ext.lstrip(".")
                break

        # Try to split on trailing _<digits>
        if "_" in stem:
            head, _, tail = stem.rpartition("_")
            if tail.isdigit():
                self.base_path = head
                self.save_counter = int(tail)
                # Block signals to avoid recursive updates
                self.counter_input.blockSignals(True)
                self.counter_input.setValue(self.save_counter)
                self.counter_input.blockSignals(False)
                logging.debug("Parsed path: base=%s counter=%d",
                              self.base_path, self.save_counter)
                return

        # No counter found
        self.base_path = stem
        self.update_path_display()

    def update_counter(self, value: int):
        """Update the save counter from the spinbox."""
        self.save_counter = value
        self.update_path_display()

    def update_path_display(self):
        """Refresh the path input field to reflect current state."""
        self.path_input.setText(self._build_path_string(self.save_counter))

    # ------------------------------------------------------------------
    # Clipboard / image handling
    # ------------------------------------------------------------------
    def handle_paste(self):
        """Route Ctrl+V based on current focus."""
        if self.counter_input.hasFocus():
            # Let the spinbox paste text normally
            return
        if self.path_input.hasFocus():
            # Let the line edit paste text normally
            return
        self.paste_from_clipboard()

    def paste_from_clipboard(self):
        """Paste image data from the system clipboard."""
        clipboard_image = self.clipboard.image()
        if clipboard_image.isNull():
            logging.info("Clipboard does not contain an image.")
            return

        self.current_image = clipboard_image
        self.display_image()
        # Reset counter for new image
        self.save_counter = 0
        self.counter_input.blockSignals(True)
        self.counter_input.setValue(0)
        self.counter_input.blockSignals(False)
        self.update_path_display()
        logging.debug("Pasted image: %dx%d",
                      clipboard_image.width(), clipboard_image.height())

    def save_current_image(self):
        """Save the currently displayed image to disk."""
        if self.path_input.hasFocus() or self.counter_input.hasFocus():
            return  # Don't hijack Ctrl+S while editing fields

        if not self.current_image or self.current_image.isNull():
            logging.info("No image to save.")
            return

        counter = self.counter_input.value()
        save_path = self._build_path_string(counter)

        # Ensure directory exists
        try:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Save Error",
                                 f"Could not create directory:\n{exc}")
            return

        # Save with appropriate format
        fmt = self.extension.upper()
        save_kwargs = {}
        if self.extension in ("jpg", "jpeg", "webp"):
            save_kwargs["quality"] = self.quality

        if self.current_image.save(save_path, fmt, **save_kwargs):
            logging.info("Image saved to %s", save_path)
            # Auto-increment counter for next save
            self.counter_input.setValue(counter + 1)
        else:
            QMessageBox.warning(self, "Save Failed",
                                f"Failed to save image to:\n{save_path}")
            logging.error("Failed to save image to %s", save_path)

    def display_image(self):
        """Render the current image scaled to fit the label."""
        if not self.current_image or self.current_image.isNull():
            return
        scaled = self.current_image.scaled(
            self.image_label.width(),
            self.image_label.height(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.image_label.setPixmap(QPixmap.fromImage(scaled))

    # ------------------------------------------------------------------
    # Qt event overrides
    # ------------------------------------------------------------------
    def resizeEvent(self, event):
        """Redisplay image when the window is resized."""
        super().resizeEvent(event)
        self.display_image()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: Optional[list] = None) -> int:
    """Application entry point."""
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    app = QApplication(sys.argv)
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