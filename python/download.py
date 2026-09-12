#!/usr/bin/env python3
"""
downloader.py - Simple resumable file downloader (console or GUI).

Usage:
  downloader.py --gui                                Launch the graphical interface
  downloader.py <url> [-o FILE] [-r RETRIES] [-d DELAY]   Download from the console

Examples:
  downloader.py --gui
  downloader.py https://example.com/file.zip
  downloader.py https://example.com/file.zip?filename=movie.mp4
  downloader.py https://example.com/file.zip -o myfile.zip -r 5 -d 10

Resume behavior:
  Before downloading, the script looks for the output file itself, a ".part"
  file, or any other unfinished-looking variant (*.part*, *.tmp, *.crdownload,
  *.download) that matches the target filename. Whichever candidate is
  largest is COPIED (not moved) to the target name and used as the resume
  point via `wget -c`. The original partial file is left in place, so if
  anything goes wrong mid-download you can just run the script again -- the
  untouched partial is still there to resume from. This only works if the
  server supports HTTP range requests.
"""

import sys
import os
import glob
import time
import shutil
import argparse
import subprocess
import threading
from urllib.parse import urlparse, parse_qs, unquote


# ---------------------------------------------------------------------------
# Shared logic (used by both console and GUI modes)
# ---------------------------------------------------------------------------

def extract_filename(url):
    """Prefer a 'filename' query param; otherwise fall back to the URL's basename."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if "filename" in params and params["filename"]:
            return unquote(params["filename"][0])
        name = os.path.basename(parsed.path)
        if name:
            return unquote(name)
    except Exception:
        pass
    return "downloaded_file"


def find_partial(output):
    """Find the best candidate partial/unfinished file for resuming (largest wins)."""
    candidates = {output, output + ".part"}
    for pattern in (output + ".part*", output + "*.part",
                    output + ".tmp", output + ".crdownload", output + ".download"):
        candidates.update(glob.glob(pattern))

    best, best_size = None, -1
    for c in candidates:
        if os.path.isfile(c):
            size = os.path.getsize(c)
            if size > best_size:
                best, best_size = c, size
    return best


def prepare_resume(output, log=print):
    """Copy the best partial file found (if any) to the target output name.

    Uses a copy rather than a rename/move on purpose: the original partial
    file (e.g. "file.zip.part") is left untouched. If wget, the network, or
    anything else fails or misbehaves during this run, the untouched partial
    is still there and the next run can simply try again from it, instead of
    having lost or corrupted the only copy of the partial data.
    """
    partial = find_partial(output)
    if partial is None:
        log("No partial file found. Starting fresh download.")
        return
    if partial == output:
        log(f"Found existing file '{output}'. Will attempt to resume.")
        return
    # A differently-named partial file was found and is the best (or only) candidate.
    if os.path.exists(output) and os.path.getsize(output) >= os.path.getsize(partial):
        log(f"Existing '{output}' is already as large or larger; keeping it.")
        return
    try:
        shutil.copy2(partial, output)
        log(f"Copied partial file '{partial}' -> '{output}' (original left in place)")
    except Exception as e:
        log(f"Could not copy partial file '{partial}': {e}. Starting fresh.")


def build_wget_cmd(url, output):
    return ["wget", "-c", "-t", "0", "-T", "30", "-O", output, url]


# ---------------------------------------------------------------------------
# Console mode
# ---------------------------------------------------------------------------

def download_console(url, output=None, max_retries=10, retry_delay=5):
    output = output or extract_filename(url)
    prepare_resume(output)

    for attempt in range(1, max_retries + 1):
        print(f"Attempt {attempt}/{max_retries}: downloading '{output}'...")
        result = subprocess.run(build_wget_cmd(url, output))
        if result.returncode == 0:
            print(f"Download completed successfully: {output}")
            return True
        print(f"Attempt {attempt} failed.")
        if attempt < max_retries:
            print(f"Waiting {retry_delay}s before retrying...")
            time.sleep(retry_delay)

    print(f"Download failed after {max_retries} attempts. Run again to resume.")
    return False


# ---------------------------------------------------------------------------
# GUI mode (tkinter, only imported if actually used)
# ---------------------------------------------------------------------------

def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    class DownloaderGUI:
        def __init__(self, root):
            self.root = root
            root.title("Resumable Downloader")
            root.geometry("560x380")

            self.url_var = tk.StringVar()
            self.output_var = tk.StringVar()
            self.retries_var = tk.StringVar(value="10")
            self.delay_var = tk.StringVar(value="5")
            self.downloading = False
            self.process = None

            frm = ttk.Frame(root, padding=10)
            frm.pack(fill=tk.BOTH, expand=True)

            ttk.Label(frm, text="URL:").grid(row=0, column=0, sticky=tk.W, pady=4)
            ttk.Entry(frm, textvariable=self.url_var, width=55).grid(
                row=0, column=1, columnspan=3, sticky=tk.EW, pady=4)

            ttk.Label(frm, text="Output filename (optional):").grid(
                row=1, column=0, sticky=tk.W, pady=4)
            ttk.Entry(frm, textvariable=self.output_var, width=55).grid(
                row=1, column=1, columnspan=3, sticky=tk.EW, pady=4)

            ttk.Label(frm, text="Max retries:").grid(row=2, column=0, sticky=tk.W, pady=4)
            ttk.Entry(frm, textvariable=self.retries_var, width=8).grid(
                row=2, column=1, sticky=tk.W, pady=4)
            ttk.Label(frm, text="Retry delay (s):").grid(row=2, column=2, sticky=tk.W, pady=4)
            ttk.Entry(frm, textvariable=self.delay_var, width=8).grid(
                row=2, column=3, sticky=tk.W, pady=4)

            btns = ttk.Frame(frm)
            btns.grid(row=3, column=0, columnspan=4, pady=10)
            self.start_btn = ttk.Button(btns, text="Start Download", command=self.start)
            self.start_btn.pack(side=tk.LEFT, padx=5)
            self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel, state=tk.DISABLED)
            self.cancel_btn.pack(side=tk.LEFT, padx=5)

            self.log = tk.Text(frm, height=12, wrap=tk.WORD)
            self.log.grid(row=4, column=0, columnspan=4, sticky=tk.NSEW, pady=4)
            frm.rowconfigure(4, weight=1)
            frm.columnconfigure(1, weight=1)

        def write(self, msg):
            self.root.after(0, self._write, msg)

        def _write(self, msg):
            self.log.insert(tk.END, msg + "\n")
            self.log.see(tk.END)

        def start(self):
            url = self.url_var.get().strip()
            if not url:
                messagebox.showerror("Missing URL", "Please enter a URL.")
                return
            output = self.output_var.get().strip() or extract_filename(url)
            self.output_var.set(output)
            try:
                retries = int(self.retries_var.get())
                delay = int(self.delay_var.get())
            except ValueError:
                messagebox.showerror("Invalid input", "Retries and delay must be numbers.")
                return

            self.downloading = True
            self.start_btn.config(state=tk.DISABLED)
            self.cancel_btn.config(state=tk.NORMAL)
            threading.Thread(target=self.worker, args=(url, output, retries, delay), daemon=True).start()

        def worker(self, url, output, retries, delay):
            prepare_resume(output, log=self.write)
            attempt = 0
            success = False
            while attempt < retries and self.downloading:
                attempt += 1
                self.write(f"Attempt {attempt}/{retries}: downloading '{output}'...")
                self.process = subprocess.Popen(build_wget_cmd(url, output),
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                while self.process.poll() is None and self.downloading:
                    time.sleep(0.2)
                if not self.downloading:
                    self.process.terminate()
                    self.write("Download cancelled.")
                    break
                if self.process.returncode == 0:
                    success = True
                    break
                self.write(f"Attempt {attempt} failed.")
                if attempt < retries and self.downloading:
                    self.write(f"Waiting {delay}s before retrying...")
                    time.sleep(delay)
            self.root.after(0, self.finish, success, output)

        def finish(self, success, output):
            self.downloading = False
            self.start_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(state=tk.DISABLED)
            if success:
                size = os.path.getsize(output) if os.path.exists(output) else 0
                self.write(f"Done. Saved to '{output}' ({size:,} bytes).")
                messagebox.showinfo("Download complete", f"Saved to '{output}'.")
            else:
                self.write("Download did not complete. Run again to resume.")

        def cancel(self):
            self.downloading = False
            self.cancel_btn.config(state=tk.DISABLED)
            self.write("Cancelling...")

    root = tk.Tk()
    DownloaderGUI(root)
    root.mainloop()


# ---------------------------------------------------------------------------
# Entry point / argument routing
# ---------------------------------------------------------------------------

def print_help():
    print(__doc__)


def main():
    argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        print_help()
        return

    if argv[0] == "--gui":
        run_gui()
        return

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("url")
    parser.add_argument("-o", "--output")
    parser.add_argument("-r", "--retries", type=int, default=10)
    parser.add_argument("-d", "--delay", type=int, default=5)

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print_help()
        return

    if not args.url.lower().startswith(("http://", "https://", "ftp://")):
        print("Error: not a valid URL.\n")
        print_help()
        return

    download_console(args.url, args.output, args.retries, args.delay)


if __name__ == "__main__":
    main()
