#!/usr/bin/env python3
"""
App Memory Monitor
==================

A PyQt5-based GUI utility for tracking the real-time memory (RSS) usage of
one or more running processes on the local machine. Useful for profiling
browsers, games, dev servers, or any misbehaving application that tends to
leak memory over time.

Features:
    - Live memory readout (GB / MB / KB / B) updated on a timer
    - Aggregate RSS across all matching processes (e.g., all Chrome tabs)
    - Process-count display for multi-process apps
    - Persistent "Monitored Apps" watchlist saved to JSON
    - Double-click a watchlist entry to start monitoring it
    - Auto-wait mode: keeps polling if the target app isn't running yet
    - Cross-platform: works wherever psutil works (Windows/macOS/Linux)

Usage:
    python memory_check.py [OPTIONS]

Examples:
    python memory_check.py
    python memory_check.py --watch "chrome.exe" --interval 500
    python memory_check.py --storage "C:/Users/me/monitored.json" --auto-start firefox
    python memory_check.py --no-persist          # ephemeral session

Keyboard shortcuts:
    Enter (in name field) - Start monitoring the typed app

Notes:
    Requires: PyQt5, psutil
    On Windows, process names usually end with ".exe" (e.g., "chrome.exe").
    On Linux/macOS, use the bare process name (e.g., "chrome").
    Process matching is case-insensitive substring matching, so "chrome"
    will match "chrome.exe", "chrome_crashpad_handler", etc.

Author: (original author unknown)
License: MIT
"""

import sys
import json
import argparse
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

import psutil

from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QListWidget, QTextEdit,
    QMessageBox, QGroupBox
)

# ---------------------------------------------------------------------------
# Configuration defaults (used when CLI args are not supplied)
# ---------------------------------------------------------------------------
DEFAULT_STORAGE_FILE = "monitored_apps.json"
DEFAULT_UPDATE_INTERVAL_MS = 1000       # Live-update cadence while monitoring
DEFAULT_WAIT_INTERVAL_MS = 2000         # Poll cadence while waiting for app
DEFAULT_WINDOW_SIZE = (600, 500)

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
        argv: Optional argument list (defaults to sys.argv[1:]).

    Returns:
        argparse.Namespace with all configuration values.
    """
    parser = argparse.ArgumentParser(
        prog="memory_check",
        description="Monitor RSS memory usage of running applications.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--storage", "-S",
        type=str,
        default=DEFAULT_STORAGE_FILE,
        help="Path to the JSON file storing the monitored-apps watchlist.",
    )
    parser.add_argument(
        "--watch", "-w",
        type=str,
        default=None,
        help="App name to begin monitoring immediately on launch.",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=DEFAULT_UPDATE_INTERVAL_MS,
        metavar="MS",
        help="Memory refresh interval in milliseconds.",
    )
    parser.add_argument(
        "--wait-interval",
        type=int,
        default=DEFAULT_WAIT_INTERVAL_MS,
        metavar="MS",
        help="Polling interval (ms) while waiting for an app to appear.",
    )
    parser.add_argument(
        "--width", "-W",
        type=int,
        default=DEFAULT_WINDOW_SIZE[0],
        help="Initial window width.",
    )
    parser.add_argument(
        "--height", "-H",
        type=int,
        default=DEFAULT_WINDOW_SIZE[1],
        help="Initial window height.",
    )
    parser.add_argument(
        "--no-persist", "-n",
        action="store_true",
        help="Do not load or save the watchlist to disk.",
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
class MemoryMonitorApp(QMainWindow):
    """
    Main window: watchlist management + live memory readout.

    Attributes:
        storage_file: Path to the JSON watchlist file (or None if disabled).
        monitored_apps: List of app names in the watchlist.
        currently_monitoring: Name of the app currently being tracked (or None).
    """

    def __init__(
        self,
        storage_file: Optional[str] = DEFAULT_STORAGE_FILE,
        update_interval_ms: int = DEFAULT_UPDATE_INTERVAL_MS,
        wait_interval_ms: int = DEFAULT_WAIT_INTERVAL_MS,
        window_size: tuple = DEFAULT_WINDOW_SIZE,
        auto_start: Optional[str] = None,
    ):
        """
        Initialize the monitor window.

        Args:
            storage_file: JSON path for the watchlist; None disables persistence.
            update_interval_ms: Refresh rate for live memory updates.
            wait_interval_ms: Poll rate while waiting for the target app.
            window_size: (width, height) tuple for the window.
            auto_start: Optional app name to start monitoring immediately.
        """
        super().__init__()

        # --- Configuration ------------------------------------------------
        self.storage_file: Optional[Path] = (
            Path(storage_file) if storage_file else None
        )
        self.update_interval_ms = update_interval_ms
        self.wait_interval_ms = wait_interval_ms

        self.currently_monitoring: Optional[str] = None
        self.monitoring_timer = QTimer()
        self.monitoring_timer.timeout.connect(self.update_memory_usage)

        # --- Window setup -------------------------------------------------
        self.setWindowTitle("App Memory Monitor")
        self.setGeometry(100, 100, *window_size)

        self.main_widget = QWidget()
        self.setCentralWidget(self.main_widget)
        self.main_layout = QHBoxLayout(self.main_widget)

        self._build_left_panel()
        self._build_right_panel()

        self.main_layout.addWidget(self.left_panel, 1)
        self.main_layout.addWidget(self.right_panel, 2)

        # --- Load watchlist ----------------------------------------------
        self.monitored_apps: List[str] = []
        self.load_monitored_apps()
        self.update_monitored_apps_list()

        # --- Auto-start (if requested) -----------------------------------
        if auto_start:
            self.app_name_input.setText(auto_start)
            self.start_monitoring()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_left_panel(self):
        """Build the left panel: app input + watchlist."""
        self.left_panel = QWidget()
        self.left_layout = QVBoxLayout(self.left_panel)

        # --- App input ---------------------------------------------------
        self.app_input_group = QGroupBox("Monitor Application")
        self.app_input_layout = QVBoxLayout()

        self.app_name_label = QLabel("Enter application name:")
        self.app_name_input = QLineEdit()
        self.app_name_input.setPlaceholderText("e.g., chrome.exe")
        # Pressing Enter in the field starts monitoring
        self.app_name_input.returnPressed.connect(self.start_monitoring)

        self.monitor_button = QPushButton("Start Monitoring")
        self.monitor_button.clicked.connect(self.start_monitoring)

        self.add_to_monitored_button = QPushButton("Add to Monitored Apps")
        self.add_to_monitored_button.clicked.connect(self.add_to_monitored)
        self.add_to_monitored_button.setEnabled(False)

        self.app_input_layout.addWidget(self.app_name_label)
        self.app_input_layout.addWidget(self.app_name_input)
        self.app_input_layout.addWidget(self.monitor_button)
        self.app_input_layout.addWidget(self.add_to_monitored_button)
        self.app_input_group.setLayout(self.app_input_layout)

        # --- Watchlist ---------------------------------------------------
        self.monitored_apps_group = QGroupBox("Monitored Apps")
        self.monitored_apps_layout = QVBoxLayout()

        self.monitored_apps_list = QListWidget()
        self.monitored_apps_list.itemDoubleClicked.connect(self.monitor_selected_app)

        self.remove_button = QPushButton("Remove Selected")
        self.remove_button.clicked.connect(self.remove_monitored_app)

        self.monitored_apps_layout.addWidget(self.monitored_apps_list)
        self.monitored_apps_layout.addWidget(self.remove_button)
        self.monitored_apps_group.setLayout(self.monitored_apps_layout)

        self.left_layout.addWidget(self.app_input_group)
        self.left_layout.addWidget(self.monitored_apps_group)

    def _build_right_panel(self):
        """Build the right panel: memory readout + controls."""
        self.right_panel = QWidget()
        self.right_layout = QVBoxLayout(self.right_panel)

        self.memory_display_group = QGroupBox("Memory Usage")
        self.memory_display_layout = QVBoxLayout()

        self.app_name_display = QLabel("Not currently monitoring any app")
        self.app_name_display.setAlignment(Qt.AlignCenter)
        self.app_name_display.setStyleSheet(
            "font-weight: bold; font-size: 16px;"
        )

        self.memory_usage_display = QLabel()
        self.memory_usage_display.setAlignment(Qt.AlignCenter)
        self.memory_usage_display.setStyleSheet(
            "font-size: 24px; color: #2E86C1;"
        )

        self.detailed_memory_display = QTextEdit()
        self.detailed_memory_display.setReadOnly(True)
        self.detailed_memory_display.setStyleSheet("font-family: monospace;")

        self.process_count_display = QLabel()
        self.process_count_display.setAlignment(Qt.AlignCenter)

        self.stop_button = QPushButton("Stop Monitoring")
        self.stop_button.clicked.connect(self.stop_monitoring)
        self.stop_button.setEnabled(False)

        self.memory_display_layout.addWidget(self.app_name_display)
        self.memory_display_layout.addWidget(self.memory_usage_display)
        self.memory_display_layout.addWidget(self.detailed_memory_display)
        self.memory_display_layout.addWidget(self.process_count_display)
        self.memory_display_layout.addWidget(self.stop_button)
        self.memory_display_group.setLayout(self.memory_display_layout)

        self.right_layout.addWidget(self.memory_display_group)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------
    @staticmethod
    def convert_bytes(bytes_num: int) -> tuple:
        """
        Split a byte count into (GB, MB, KB, B) components.

        Args:
            bytes_num: Total number of bytes.

        Returns:
            Tuple (gb, mb, kb, b) of integers.
        """
        gb = bytes_num // (1024 ** 3)
        remaining = bytes_num % (1024 ** 3)
        mb = remaining // (1024 ** 2)
        remaining %= (1024 ** 2)
        kb = remaining // 1024
        b = remaining % 1024
        return gb, mb, kb, b

    @staticmethod
    def format_memory(gb: int, mb: int, kb: int, b: int) -> str:
        """
        Human-readable rendering of byte components.

        Args:
            gb, mb, kb, b: Byte components from `convert_bytes`.

        Returns:
            A string like "1 GB 234 MB 5 KB 12 B", skipping zero components.
        """
        parts = []
        if gb > 0:
            parts.append(f"{gb} GB")
        if mb > 0:
            parts.append(f"{mb} MB")
        if kb > 0:
            parts.append(f"{kb} KB")
        if b > 0 or not parts:
            parts.append(f"{b} B")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Process discovery
    # ------------------------------------------------------------------
    def find_process_by_name(self, name: str) -> List[psutil.Process]:
        """
        Find all running processes whose name contains `name` (case-insensitive).

        Args:
            name: Substring to match against process names.

        Returns:
            List of matching psutil.Process objects.
        """
        matching: List[psutil.Process] = []
        name_lower = name.lower()
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                proc_name = proc.info.get("name") or ""
                if name_lower in proc_name.lower():
                    matching.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                continue
        return matching

    @staticmethod
    def get_total_memory(processes: List[psutil.Process]) -> int:
        """
        Sum the RSS of a list of processes (skipping any that disappear).

        Args:
            processes: List of psutil.Process objects.

        Returns:
            Total resident set size in bytes.
        """
        total = 0
        for proc in processes:
            try:
                total += proc.info["memory_info"].rss
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess, KeyError):
                continue
        return total

    # ------------------------------------------------------------------
    # Watchlist persistence
    # ------------------------------------------------------------------
    def load_monitored_apps(self):
        """Load the watchlist from JSON (no-op if disabled or missing)."""
        self.monitored_apps = []
        if self.storage_file is None or not self.storage_file.exists():
            return
        try:
            with open(self.storage_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.monitored_apps = [str(x) for x in data]
            else:
                logging.warning("Watchlist file did not contain a list; ignoring.")
        except (json.JSONDecodeError, OSError) as exc:
            logging.warning("Could not load watchlist: %s", exc)

    def save_monitored_apps(self):
        """Persist the watchlist to JSON (no-op if persistence disabled)."""
        if self.storage_file is None:
            return
        try:
            with open(self.storage_file, "w", encoding="utf-8") as f:
                json.dump(self.monitored_apps, f, indent=2)
        except OSError as exc:
            logging.error("Could not save watchlist: %s", exc)

    def update_monitored_apps_list(self):
        """Refresh the watchlist widget from `self.monitored_apps`."""
        self.monitored_apps_list.clear()
        for app in self.monitored_apps:
            self.monitored_apps_list.addItem(app)

    # ------------------------------------------------------------------
    # Watchlist actions
    # ------------------------------------------------------------------
    def add_to_monitored(self):
        """Add the currently-typed app name to the watchlist."""
        app_name = self.app_name_input.text().strip()
        if not app_name:
            QMessageBox.warning(self, "Error", "Please enter an application name")
            return

        if app_name in self.monitored_apps:
            QMessageBox.information(
                self, "Info", f"'{app_name}' is already being monitored."
            )
            return

        self.monitored_apps.append(app_name)
        self.save_monitored_apps()
        self.update_monitored_apps_list()
        QMessageBox.information(
            self, "Success", f"'{app_name}' has been added to monitored apps."
        )

    def remove_monitored_app(self):
        """Remove the selected watchlist entry (stops monitoring it if active)."""
        selected_items = self.monitored_apps_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "Error", "Please select an app to remove")
            return

        app_name = selected_items[0].text()
        if app_name in self.monitored_apps:
            self.monitored_apps.remove(app_name)
            self.save_monitored_apps()
            self.update_monitored_apps_list()

        if self.currently_monitoring == app_name:
            self.stop_monitoring()

    def monitor_selected_app(self, item):
        """Double-click handler: start monitoring the selected watchlist entry."""
        app_name = item.text()
        self.app_name_input.setText(app_name)
        self.start_monitoring()

    # ------------------------------------------------------------------
    # Monitoring control
    # ------------------------------------------------------------------
    def start_monitoring(self):
        """Begin monitoring the app named in the input field."""
        app_name = self.app_name_input.text().strip()
        if not app_name:
            QMessageBox.warning(self, "Error", "Please enter an application name")
            return

        processes = self.find_process_by_name(app_name)

        if not processes:
            reply = QMessageBox.question(
                self, "App Not Found",
                f"No running processes found for '{app_name}'. "
                "Do you want to keep trying?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

            # Wait mode
            self.currently_monitoring = app_name
            self.app_name_display.setText(f"Waiting for '{app_name}'...")
            self.monitoring_timer.start(self.wait_interval_ms)
            self.stop_button.setEnabled(True)
            self.monitor_button.setEnabled(False)
            self.add_to_monitored_button.setEnabled(
                app_name not in self.monitored_apps
            )
            return

        # Normal monitoring mode
        self.currently_monitoring = app_name
        self.app_name_display.setText(f"Monitoring: {app_name}")
        self.monitoring_timer.start(self.update_interval_ms)
        self.stop_button.setEnabled(True)
        self.monitor_button.setEnabled(False)
        self.add_to_monitored_button.setEnabled(
            app_name not in self.monitored_apps
        )

        self.update_memory_usage()

    def stop_monitoring(self):
        """Stop the current monitoring session and reset the display."""
        self.monitoring_timer.stop()
        self.currently_monitoring = None
        self.app_name_display.setText("Not currently monitoring any app")
        self.memory_usage_display.clear()
        self.detailed_memory_display.clear()
        self.process_count_display.clear()
        self.stop_button.setEnabled(False)
        self.monitor_button.setEnabled(True)

    def update_memory_usage(self):
        """Refresh the memory readout from the current process list."""
        if not self.currently_monitoring:
            return

        processes = self.find_process_by_name(self.currently_monitoring)

        if not processes:
            # Target disappeared; switch back to wait mode
            if self.monitoring_timer.isActive():
                self.app_name_display.setText(
                    f"Waiting for '{self.currently_monitoring}'..."
                )
                self.memory_usage_display.setText("App not currently running")
                self.detailed_memory_display.clear()
                self.process_count_display.clear()
                # Slower polling while waiting
                self.monitoring_timer.start(self.wait_interval_ms)
            return

        # If we were in wait mode but the app appeared, resume normal cadence
        if self.monitoring_timer.interval() != self.update_interval_ms:
            self.monitoring_timer.start(self.update_interval_ms)

        total_memory = self.get_total_memory(processes)
        gb, mb, kb, b = self.convert_bytes(total_memory)

        self.app_name_display.setText(f"Monitoring: {self.currently_monitoring}")
        self.memory_usage_display.setText(self.format_memory(gb, mb, kb, b))

        detailed_text = (
            f"Memory Breakdown:\n"
            f"{gb} GB\n"
            f"{mb} MB\n"
            f"{kb} KB\n"
            f"{b} B\n\n"
            f"Total Bytes: {total_memory:,}"
        )
        self.detailed_memory_display.setText(detailed_text)
        self.process_count_display.setText(f"Processes: {len(processes)}")

        # Keep add button consistent with watchlist state
        self.add_to_monitored_button.setEnabled(
            self.currently_monitoring not in self.monitored_apps
        )

    # ------------------------------------------------------------------
    # Qt event overrides
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        """Stop timers and persist watchlist before exit."""
        self.monitoring_timer.stop()
        self.save_monitored_apps()
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

    window = MemoryMonitorApp(
        storage_file=None if args.no_persist else args.storage,
        update_interval_ms=args.interval,
        wait_interval_ms=args.wait_interval,
        window_size=(args.width, args.height),
        auto_start=args.watch,
    )
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())