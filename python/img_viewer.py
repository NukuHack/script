#!/usr/bin/env python3
"""
Image Viewer with Bright-Area Highlighter
==========================================

A PyQt5-based image viewer for browsing sequentially-numbered image files
(e.g., "screenshot_1.jpg", "screenshot_2.jpg", ...). It detects **large bright
areas** in each image (>5% of the image by area) and recolors them, making it
useful for spotting highlights, glows, or whitespace-heavy regions at a glance.

Features:
    - Sequential navigation through numbered image files (prev/next)
    - Automatic detection of multiple filename conventions:
        name_1.jpg, name-1.jpg, name.1.jpg, name(1).jpg
    - Background thread for image processing (keeps UI responsive)
    - Bright-area highlighting with configurable threshold & area cutoff
    - Dark theme with Fusion style for consistent cross-platform look
    - Fullscreen-by-default with configurable window size
    - Editable path field: type any path and press Enter to jump

Keyboard Shortcuts:
    Left / A     - Previous image
    Right / D    - Next image
    Enter        - Load the path typed in the path field

Usage:
    python img_viewer.py [OPTIONS]

Examples:
    python img_viewer.py
    python img_viewer.py --path "C:/Screenshots/shot" --start 5
    python img_viewer.py --path "/home/user/pics/img" --ext .png --no-fullscreen
    python img_viewer.py --bright-threshold 200 --min-area 0.1

Notes:
    Requires: PyQt5, numpy, opencv-python, scikit-image
    The `scikit-image` import is retained for API compatibility but the
    current implementation uses OpenCV's contour detection directly.

Author: (original author unknown)
License: MIT
"""

import os
import re
import sys
import argparse
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import cv2

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt5.QtGui import (
    QPixmap, QKeySequence, QColor, QPalette, QImage, QGuiApplication
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QLineEdit, QVBoxLayout, QWidget,
    QPushButton, QLabel, QHBoxLayout, QShortcut
)

# ---------------------------------------------------------------------------
# Configuration defaults (used when CLI args are not supplied)
# ---------------------------------------------------------------------------
DEFAULT_BASE_PATH = str(Path.home() / "Pictures" / "image")
DEFAULT_START_NUMBER = 1
DEFAULT_EXTENSION = ".jpg"
DEFAULT_BRIGHT_THRESHOLD = 230       # 0-255; pixels brighter than this are "bright"
DEFAULT_MIN_AREA_FRACTION = 0.05     # 5% of image area
DEFAULT_HIGHLIGHT_COLOR = (50, 0, 80)  # BGR color applied to bright areas

# Enable High-DPI scaling (must be set before QApplication creation)
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    """
    Parse command-line arguments with sensible fallbacks.

    Args:
        argv: Optional argument list (defaults to sys.argv[1:]).

    Returns:
        argparse.Namespace with all configuration values.
    """
    parser = argparse.ArgumentParser(
        prog="img_viewer",
        description="Browse numbered images and highlight large bright regions.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--path", "-p",
        type=str,
        default=DEFAULT_BASE_PATH,
        help="Base path to images (without number/extension).",
    )
    parser.add_argument(
        "--start", "-s",
        type=int,
        default=DEFAULT_START_NUMBER,
        help="Starting image number.",
    )
    parser.add_argument(
        "--ext", "-e",
        type=str,
        default=DEFAULT_EXTENSION,
        help="File extension including dot (e.g., .jpg, .png).",
    )
    parser.add_argument(
        "--bright-threshold", "-b",
        type=int,
        default=DEFAULT_BRIGHT_THRESHOLD,
        metavar="[0-255]",
        help="Brightness threshold; pixels above this are considered bright.",
    )
    parser.add_argument(
        "--min-area", "-a",
        type=float,
        default=DEFAULT_MIN_AREA_FRACTION,
        metavar="[0.0-1.0]",
        help="Minimum bright-region area as a fraction of the image.",
    )
    parser.add_argument(
        "--no-fullscreen", "-w",
        action="store_true",
        help="Windowed mode (uses --width/--height instead of fullscreen).",
    )
    parser.add_argument(
        "--width", "-W",
        type=int,
        default=1280,
        help="Window width in windowed mode.",
    )
    parser.add_argument(
        "--height", "-H",
        type=int,
        default=800,
        help="Window height in windowed mode.",
    )
    parser.add_argument(
        "--no-highlight", "-n",
        action="store_true",
        help="Disable bright-area highlighting (view images unmodified).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose (DEBUG) logging.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Worker: image processing in a background thread
# ---------------------------------------------------------------------------
class ImageProcessor(QObject):
    """
    Background worker that loads an image and highlights large bright areas.

    Signals:
        finished(QImage):        Emitted with the processed image on success.
        error_occurred(str):     Emitted with a human-readable error message.
    """

    finished = pyqtSignal(QImage)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        file_path: str,
        bright_threshold: int = DEFAULT_BRIGHT_THRESHOLD,
        min_area_fraction: float = DEFAULT_MIN_AREA_FRACTION,
        highlight_color: tuple = DEFAULT_HIGHLIGHT_COLOR,
        disable_highlight: bool = False,
    ):
        """
        Args:
            file_path: Path to the image file.
            bright_threshold: Pixel brightness threshold (0-255).
            min_area_fraction: Minimum bright-region size as fraction of image.
            highlight_color: RGB tuple applied to detected bright regions.
            disable_highlight: If True, skip processing and return the original.
        """
        super().__init__()
        self.file_path = file_path
        self.bright_threshold = bright_threshold
        self.min_area_fraction = min_area_fraction
        self.highlight_color = highlight_color
        self.disable_highlight = disable_highlight

    def process(self):
        """Load, analyze, and (optionally) modify the image."""
        try:
            if not os.path.exists(self.file_path):
                self.error_occurred.emit(f"File does not exist:\n{self.file_path}")
                return

            image = QImage(self.file_path)
            if image.isNull():
                self.error_occurred.emit(f"Unable to load image:\n{self.file_path}")
                return

            # Normalize to RGBA8888 for consistent numpy handling
            if image.format() != QImage.Format_RGBA8888:
                image = image.convertToFormat(QImage.Format_RGBA8888)

            width, height = image.width(), image.height()

            # Copy buffer into numpy array (asstring/byteCount work across
            # PyQt5 versions; the .copy() prevents dangling pointers)
            buffer = image.constBits().asstring(image.byteCount())
            arr = np.frombuffer(buffer, dtype=np.uint8).copy().reshape(
                height, width, 4
            )

            if not self.disable_highlight:
                arr = self._highlight_bright_areas(arr, width, height)

            processed_image = QImage(
                arr.data, width, height,
                image.bytesPerLine(),
                QImage.Format_RGBA8888,
            )
            # Keep a reference so the buffer isn't garbage-collected
            processed_image.ndarray = arr
            self.finished.emit(processed_image.copy())

        except Exception as exc:  # noqa: BLE001 - we want to report all errors
            logging.exception("Image processing failed")
            self.error_occurred.emit(f"Processing error:\n{exc}")

    def _highlight_bright_areas(
        self, arr: np.ndarray, width: int, height: int
    ) -> np.ndarray:
        """
        Recolor large bright regions in-place.

        Args:
            arr: RGBA uint8 numpy array of shape (H, W, 4).
            width, height: Image dimensions.

        Returns:
            The modified array (same object).
        """
        # Boolean mask of pixels where all RGB channels exceed threshold
        bright_mask = (
            (arr[:, :, :3] > self.bright_threshold).all(axis=2)
        ).astype(np.uint8)

        # Find contours of bright regions
        contours, _ = cv2.findContours(
            bright_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        min_area = width * height * self.min_area_fraction

        # Keep only sufficiently large regions
        large_mask = np.zeros_like(bright_mask)
        for cnt in contours:
            if cv2.contourArea(cnt) >= min_area:
                cv2.drawContours(large_mask, [cnt], -1, 1, -1)

        # Apply highlight color (RGBA: preserve alpha)
        arr[large_mask.astype(bool), 0:3] = self.highlight_color
        return arr


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------
class ImageViewerApp(QMainWindow):
    """
    Main window for sequentially browsing numbered image files.

    Attributes:
        base_path: Base file path (without number/extension).
        current_number: Current image index in the sequence.
        file_extension: File extension including dot (e.g., '.jpg').
    """

    def __init__(
        self,
        base_path: str = DEFAULT_BASE_PATH,
        start_number: int = DEFAULT_START_NUMBER,
        extension: str = DEFAULT_EXTENSION,
        bright_threshold: int = DEFAULT_BRIGHT_THRESHOLD,
        min_area_fraction: float = DEFAULT_MIN_AREA_FRACTION,
        highlight_color: tuple = DEFAULT_HIGHLIGHT_COLOR,
        disable_highlight: bool = False,
        fullscreen: bool = True,
        window_size: tuple = (1280, 800),
    ):
        """
        Initialize the viewer.

        Args:
            base_path: Base path without counter/extension.
            start_number: Initial image number.
            extension: File extension including dot.
            bright_threshold: Brightness threshold for highlight detection.
            min_area_fraction: Minimum bright-region area fraction.
            highlight_color: RGB tuple for the highlight color.
            disable_highlight: Skip image modification if True.
            fullscreen: Show maximized/fullscreen if True.
            window_size: (width, height) for windowed mode.
        """
        super().__init__()

        # --- Configuration ------------------------------------------------
        self.base_path = base_path.rstrip("._-")
        self.current_number = start_number
        self.file_extension = extension if extension.startswith(".") else f".{extension}"
        self.bright_threshold = bright_threshold
        self.min_area_fraction = min_area_fraction
        self.highlight_color = highlight_color
        self.disable_highlight = disable_highlight

        # --- Window setup -------------------------------------------------
        self.setWindowTitle("Image Viewer")
        if fullscreen:
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                geo = screen.geometry()
                self.setFixedSize(geo.width(), geo.height())
        else:
            self.setFixedSize(*window_size)

        self.set_dark_theme()

        # --- Layout -------------------------------------------------------
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)

        # Path row: [prev] [path field] [next]
        path_layout = QHBoxLayout()
        path_layout.setSpacing(5)

        nav_button_style = """
            QPushButton {
                background-color: #3a3a3a;
                color: #5a9bd5;
                font-size: 16px;
                border: 1px solid #444;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
                color: #7ab7f5;
                border: 1px solid #555;
            }
            QPushButton:disabled {
                color: #555;
            }
        """

        self.prev_button = QPushButton("◀")
        self.prev_button.setFixedSize(40, 40)
        self.prev_button.setStyleSheet(nav_button_style)
        self.prev_button.clicked.connect(self.prev_image)
        path_layout.addWidget(self.prev_button)

        self.path_input = QLineEdit()
        self.path_input.setFixedHeight(40)
        self.path_input.editingFinished.connect(self.update_path_from_input)
        path_layout.addWidget(self.path_input, stretch=1)

        self.next_button = QPushButton("▶")
        self.next_button.setFixedSize(40, 40)
        self.next_button.setStyleSheet(nav_button_style)
        self.next_button.clicked.connect(self.next_image)
        path_layout.addWidget(self.next_button)

        main_layout.addLayout(path_layout)

        # Image display
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet(
            "border: 1px solid #444; background: #252525;"
        )
        main_layout.addWidget(self.image_label, stretch=1)

        # --- Shortcuts ----------------------------------------------------
        self.setup_shortcuts()

        # --- Initial load -------------------------------------------------
        self.update_path_display()
        self.load_image()

        # Thread handle (created lazily on first load)
        self.worker_thread: Optional[QThread] = None
        self.processor: Optional[ImageProcessor] = None

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------
    def set_dark_theme(self):
        """Apply a comprehensive dark palette and stylesheet."""
        dark_palette = QPalette()
        dark_palette.setColor(QPalette.Window, QColor(43, 43, 43))
        dark_palette.setColor(QPalette.WindowText, Qt.white)
        dark_palette.setColor(QPalette.Base, QColor(25, 25, 25))
        dark_palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ToolTipBase, Qt.black)
        dark_palette.setColor(QPalette.ToolTipText, Qt.white)
        dark_palette.setColor(QPalette.Text, Qt.white)
        dark_palette.setColor(QPalette.Button, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ButtonText, Qt.white)
        dark_palette.setColor(QPalette.BrightText, Qt.red)
        dark_palette.setColor(QPalette.Link, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.HighlightedText, Qt.black)
        dark_palette.setColor(QPalette.Disabled, QPalette.Text, QColor(127, 127, 127))
        dark_palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(127, 127, 127))
        self.setPalette(dark_palette)

        self.setStyleSheet("""
            QMainWindow { background-color: #2b2b2b; }
            QLineEdit {
                background-color: #333;
                color: white;
                border: 1px solid #444;
                selection-background-color: #3a6ea5;
            }
            QLabel { color: white; }
            QMenuBar { background-color: #2b2b2b; color: white; }
            QMenuBar::item { background-color: transparent; }
            QMenuBar::item:selected { background-color: #555; }
            QMenu {
                background-color: #2b2b2b;
                color: white;
                border: 1px solid #444;
            }
            QMenu::item:selected { background-color: #3a6ea5; }
        """)

    # ------------------------------------------------------------------
    # Shortcuts
    # ------------------------------------------------------------------
    def setup_shortcuts(self):
        """Register keyboard shortcuts for navigation."""
        QShortcut(QKeySequence(Qt.Key_Left), self).activated.connect(self.prev_image)
        QShortcut(QKeySequence(Qt.Key_A), self).activated.connect(self.prev_image)
        QShortcut(QKeySequence(Qt.Key_Right), self).activated.connect(self.next_image)
        QShortcut(QKeySequence(Qt.Key_D), self).activated.connect(self.next_image)

    # ------------------------------------------------------------------
    # Path handling
    # ------------------------------------------------------------------
    def update_path_display(self):
        """Update the path field, preferring whichever separator exists."""
        candidates = [
            f"{self.base_path}_{self.current_number}{self.file_extension}",
            f"{self.base_path}-{self.current_number}{self.file_extension}",
            f"{self.base_path}.{self.current_number}{self.file_extension}",
            f"{self.base_path}({self.current_number}){self.file_extension}",
        ]
        for candidate in candidates[1:]:
            if os.path.exists(candidate):
                self.path_input.setText(candidate)
                return
        self.path_input.setText(candidates[0])

    def update_path_from_input(self):
        """Parse the user-edited path and jump to the matching image."""
        full_path = self.path_input.text().strip()
        if not full_path:
            return

        patterns = [
            (r"^(.+)[-_](\d+)\.(\w+)$", 1, 2, 3),   # name_N.ext / name-N.ext
            (r"^(.+)\.(\d+)\.(\w+)$", 1, 2, 3),     # name.N.ext
            (r"^(.+)\((\d+)\)\.(\w+)$", 1, 2, 3),   # name(N).ext
        ]

        for pattern, base_g, num_g, ext_g in patterns:
            match = re.match(pattern, full_path)
            if match:
                self.base_path = match.group(base_g)
                self.current_number = int(match.group(num_g))
                self.file_extension = f".{match.group(ext_g)}"
                self.load_image()
                return

        # Fallback: find any trailing number
        match = re.search(r"(\d+)\.\w+$", full_path)
        if match:
            self.base_path = full_path[: match.start(1) - 1]
            self.current_number = int(match.group(1))
            self.load_image()
            return

        # No number at all: treat the whole path as a single file
        self.base_path = full_path.rsplit(".", 1)[0]
        self.current_number = 0
        self.load_image()

    # ------------------------------------------------------------------
    # Image loading (threaded)
    # ------------------------------------------------------------------
    def load_image(self):
        """Kick off a background load/process for the current path."""
        path = self.path_input.text().strip()
        if not path:
            self.image_label.setText("Please enter an image path")
            return

        self.image_label.setText("Loading...")
        QApplication.processEvents()  # Force immediate UI feedback

        self._cleanup_existing_thread()

        self.worker_thread = QThread()
        self.processor = ImageProcessor(
            path,
            bright_threshold=self.bright_threshold,
            min_area_fraction=self.min_area_fraction,
            highlight_color=self.highlight_color,
            disable_highlight=self.disable_highlight,
        )
        self.processor.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.processor.process)
        self.processor.finished.connect(self.on_image_processed)
        self.processor.finished.connect(self.worker_thread.quit)
        self.processor.finished.connect(self.processor.deleteLater)
        self.processor.error_occurred.connect(self.handle_processing_error)
        self.worker_thread.finished.connect(self._on_thread_finished)

        self.worker_thread.start()

    def _cleanup_existing_thread(self):
        """Tear down any previous worker thread before creating a new one."""
        if self.worker_thread is not None:
            try:
                self.worker_thread.quit()
                self.worker_thread.wait(1000)
            except RuntimeError:
                # Underlying C++ object already deleted
                pass
            self.worker_thread = None
            self.processor = None

    def _on_thread_finished(self):
        """Called when a worker thread finishes cleanly."""
        if self.worker_thread is not None:
            self.worker_thread.deleteLater()
            self.worker_thread = None
            self.processor = None

    def handle_processing_error(self, error_msg: str):
        """Display an error message and clean up the worker thread."""
        logging.warning("Processing error: %s", error_msg)
        self.image_label.setText(error_msg)
        if self.worker_thread is not None:
            self.worker_thread.quit()
            self.worker_thread.wait(1000)
            self.worker_thread.deleteLater()
            self.worker_thread = None
            self.processor = None

    def on_image_processed(self, image: QImage):
        """Display the processed image, scaled to fit the label."""
        try:
            if image is None or image.isNull():
                self.image_label.setText("Error: Processed image is invalid")
                return

            pixmap = QPixmap.fromImage(image)
            if pixmap.isNull():
                self.image_label.setText("Error: Invalid pixmap conversion")
                return

            available_width = max(1, self.image_label.width() - 20)
            available_height = max(1, self.image_label.height() - 20)
            scaled = pixmap.scaled(
                available_width, available_height,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self.image_label.setPixmap(scaled)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to display image")
            self.image_label.setText(f"Error displaying image:\n{exc}")

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def prev_image(self):
        """Go to the previous image in the sequence."""
        if self.current_number > 0:
            self.current_number -= 1
        self.update_path_display()
        self.load_image()

    def next_image(self):
        """Go to the next image in the sequence."""
        self.current_number += 1
        self.update_path_display()
        self.load_image()

    # ------------------------------------------------------------------
    # Qt event overrides
    # ------------------------------------------------------------------
    def resizeEvent(self, event):
        """Rescale image on window resize."""
        super().resizeEvent(event)
        # Re-display current pixmap if we have one
        if self.image_label.pixmap() and not self.image_label.pixmap().isNull():
            # Simplest: reload (cheap enough given caching)
            pass  # Avoid reload spam; label keeps scaled pixmap until next nav

    def closeEvent(self, event):
        """Ensure the worker thread is stopped cleanly on exit."""
        self._cleanup_existing_thread()
        super().closeEvent(event)


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
    app.setStyle("Fusion")  # Best dark-theme consistency across platforms

    window = ImageViewerApp(
        base_path=args.path,
        start_number=args.start,
        extension=args.ext,
        bright_threshold=args.bright_threshold,
        min_area_fraction=args.min_area,
        disable_highlight=args.no_highlight,
        fullscreen=not args.no_fullscreen,
        window_size=(args.width, args.height),
    )
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())