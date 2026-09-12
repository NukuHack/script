#!/usr/bin/env python3
"""
Bitburner Remote API sync tool -- combined CLI + GUI version.

Important: Bitburner's Remote API is a WebSocket *client*. When you click
"Connect" in Options -> Remote API, the game tries to connect OUT to
ws://<hostname>:<port>. That means something on your machine must already
be listening on that port. This script IS that listener: it starts a
WebSocket server and waits for Bitburner to connect into it. Once connected,
you can either use an interactive console prompt (or --run for one-shot
commands) or a Tkinter GUI to pull/push/delete files over that connection.

Setup:
  pip install websockets

Usage:
  GUI (default):
       python bitburner_tool.py
    or
       python bitburner_tool.py --gui

  CLI:
       python bitburner_tool.py --cli --port 12525
    It will print "Listening on ws://0.0.0.0:12525 ... waiting for Bitburner"

  Then in Bitburner: Options -> Remote API -> hostname "localhost",
  port 12525 -> click Connect.

  In CLI mode, once connected you get an interactive prompt:
       > servers
       > files home
       > pull home ./home_scripts
       > push home ./home_scripts/hack.js
       > get home /hack.js ./hack.js
       > delete home /old.js
       > ram home /hack.js
       > watch home ./home_scripts
       > help
       > quit

  You can also run one-shot CLI commands directly:
       python bitburner_tool.py --cli --port 12525 --run "pull home ./home_scripts"
  (this still waits for Bitburner to connect first, runs the one command, then exits)

  In GUI mode, click "Start Listening" then connect from Bitburner and use
  the buttons to browse servers, list files, pull/push/delete, calc RAM, or
  auto-watch a local folder and push changes live.
"""

import argparse
import asyncio
import json
import os
import shlex
import sys
import threading
import time

try:
    import websockets
except ImportError:
    print("Missing dependency. Install it with:\n  pip install websockets")
    sys.exit(1)


# ==========================================================================
# Shared core: JSON-RPC link + file path helpers (used by both CLI and GUI)
# ==========================================================================

class RpcError(Exception):
    pass


class GameLink:
    """Wraps the single active connection from Bitburner and does JSON-RPC over it."""

    def __init__(self, on_status_change=None):
        self.ws = None
        self._id = 0
        self._pending = {}
        self._connected_event = asyncio.Event()
        # on_status_change(str) is called with "connected" / "disconnected" -- used by the GUI.
        self.on_status_change = on_status_change

    async def on_connect(self, ws):
        self.ws = ws
        self._connected_event.set()
        if self.on_status_change:
            self.on_status_change("connected")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mid = msg.get("id")
                fut = self._pending.pop(mid, None)
                if fut and not fut.done():
                    if msg.get("error"):
                        fut.set_exception(RpcError(str(msg["error"])))
                    else:
                        fut.set_result(msg.get("result"))
        finally:
            self.ws = None
            self._connected_event.clear()
            if self.on_status_change:
                self.on_status_change("disconnected")

    async def wait_connected(self):
        await self._connected_event.wait()

    def is_connected(self):
        return self.ws is not None

    async def call(self, method, params=None, timeout=15):
        if not self.ws:
            raise ConnectionError("Bitburner is not connected.")
        self._id += 1
        mid = self._id
        req = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            req["params"] = params
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps(req))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise RpcError(f"{method} timed out after {timeout}s")

    # convenience wrappers
    async def get_all_servers(self):
        return await self.call("getAllServers")

    async def get_file_names(self, server):
        return await self.call("getFileNames", {"server": server})

    async def get_file(self, filename, server):
        return await self.call("getFile", {"filename": filename, "server": server})

    async def get_all_files(self, server):
        return await self.call("getAllFiles", {"server": server})

    async def push_file(self, filename, content, server):
        return await self.call("pushFile", {"filename": filename, "content": content, "server": server})

    async def delete_file(self, filename, server):
        return await self.call("deleteFile", {"filename": filename, "server": server})

    async def calculate_ram(self, filename, server):
        return await self.call("calculateRam", {"filename": filename, "server": server})


def local_to_remote_name(local_path, base_dir):
    rel = os.path.relpath(local_path, base_dir).replace(os.sep, "/")
    return rel if rel.startswith("/") else "/" + rel


def remote_to_local_path(remote_name, base_dir):
    rel = remote_name.lstrip("/")
    return os.path.join(base_dir, *rel.split("/"))


# ==========================================================================
# CLI mode
# ==========================================================================

async def do_servers(link, args):
    servers = await link.get_all_servers()
    if not servers:
        print("No servers found.")
        return
    print(f"{'HOSTNAME':<30} {'ROOT':<6} {'PURCHASED'}")
    for s in servers:
        print(f"{s['hostname']:<30} {'yes' if s.get('hasAdminRights') else 'no':<6} "
              f"{'yes' if s.get('purchasedByPlayer') else 'no'}")


async def do_files(link, args):
    names = await link.get_file_names(args[0])
    if not names:
        print(f"No files on {args[0]}.")
        return
    for n in sorted(names):
        print(n)


async def do_pull(link, args):
    server, outdir = args[0], args[1]
    os.makedirs(outdir, exist_ok=True)
    files = await link.get_all_files(server)
    if not files:
        print(f"No files on {server}.")
        return
    for f in files:
        local_path = remote_to_local_path(f["filename"], outdir)
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "w", encoding="utf-8") as fh:
            fh.write(f["content"])
        print(f"  pulled {f['filename']} -> {local_path}")
    print(f"Done. Pulled {len(files)} file(s) from {server} into {outdir}")


async def do_get(link, args):
    server, remote = args[0], args[1]
    out = args[2] if len(args) > 2 else os.path.basename(remote)
    content = await link.get_file(remote, server)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"Saved {remote} -> {out}")


async def do_push(link, args):
    server, path = args[0], args[1]
    remote = args[2] if len(args) > 2 else ("/" + os.path.basename(path))
    if not os.path.isfile(path):
        print(f"Local file not found: {path}")
        return
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    await link.push_file(remote, content, server)
    print(f"Pushed {path} -> {server}:{remote}")


async def do_push_all(link, args):
    server, directory = args[0], args[1]
    if not os.path.isdir(directory):
        print(f"Directory not found: {directory}")
        return
    count = 0
    for root, _, filenames in os.walk(directory):
        for fn in filenames:
            local_path = os.path.join(root, fn)
            remote_name = local_to_remote_name(local_path, directory)
            with open(local_path, "r", encoding="utf-8") as fh:
                content = fh.read()
            await link.push_file(remote_name, content, server)
            print(f"  pushed {local_path} -> {server}:{remote_name}")
            count += 1
    print(f"Done. Pushed {count} file(s) to {server}")


async def do_delete(link, args):
    server, remote = args[0], args[1]
    await link.delete_file(remote, server)
    print(f"Deleted {remote} from {server}")


async def do_ram(link, args):
    server, remote = args[0], args[1]
    cost = await link.calculate_ram(remote, server)
    print(f"{remote} on {server}: {cost} GB")


async def do_watch(link, args):
    server = args[0]
    directory = args[1]
    interval = float(args[2]) if len(args) > 2 else 1.5
    os.makedirs(directory, exist_ok=True)
    print(f"Watching {directory} -> pushing changes to '{server}' every {interval}s. "
          f"Press Ctrl+C to stop watching (won't close the connection).")

    def scan():
        current = {}
        for root, _, filenames in os.walk(directory):
            for fn in filenames:
                path = os.path.join(root, fn)
                try:
                    current[path] = os.path.getmtime(path)
                except OSError:
                    pass
        return current

    mtimes = scan()
    for path in mtimes:
        remote_name = local_to_remote_name(path, directory)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            await link.push_file(remote_name, content, server)
            print(f"  [init] pushed {remote_name}")
        except Exception as e:
            print(f"  [init] failed to push {path}: {e}")

    try:
        while True:
            await asyncio.sleep(interval)
            if not link.is_connected():
                print("Bitburner disconnected, stopping watch.")
                return
            current = scan()
            for path, mtime in current.items():
                if path not in mtimes or mtimes[path] != mtime:
                    remote_name = local_to_remote_name(path, directory)
                    try:
                        with open(path, "r", encoding="utf-8") as fh:
                            content = fh.read()
                        await link.push_file(remote_name, content, server)
                        print(f"  [{time.strftime('%H:%M:%S')}] pushed {remote_name}")
                    except Exception as e:
                        print(f"  failed to push {path}: {e}")
            mtimes = current
    except KeyboardInterrupt:
        print("\nStopped watching.")


COMMANDS = {
    "servers": (do_servers, 0, "servers                          - list all servers"),
    "files": (do_files, 1, "files <server>                   - list files on a server"),
    "pull": (do_pull, 2, "pull <server> <outdir>           - download all files from server"),
    "get": (do_get, 2, "get <server> <remote> [out]      - download a single file"),
    "push": (do_push, 2, "push <server> <localfile> [remote] - upload a single file"),
    "push-all": (do_push_all, 2, "push-all <server> <dir>          - upload every file in a dir"),
    "delete": (do_delete, 2, "delete <server> <remote>         - delete a remote file"),
    "ram": (do_ram, 2, "ram <server> <remote>            - calculate RAM cost"),
    "watch": (do_watch, 2, "watch <server> <dir> [interval]  - auto-push local changes"),
}


async def run_one(link, line):
    parts = shlex.split(line)
    if not parts:
        return
    cmd, args = parts[0], parts[1:]
    if cmd in ("quit", "exit"):
        raise SystemExit
    if cmd == "help":
        print("Commands:")
        for _, (_, _, help_text) in COMMANDS.items():
            print("  " + help_text)
        print("  help                             - show this message")
        print("  quit / exit                      - exit the tool")
        return
    if cmd not in COMMANDS:
        print(f"Unknown command '{cmd}'. Type 'help' for a list.")
        return
    func, min_args, help_text = COMMANDS[cmd]
    if len(args) < min_args:
        print(f"Usage: {help_text}")
        return
    try:
        await func(link, args)
    except RpcError as e:
        print(f"RPC error: {e}")
    except ConnectionError as e:
        print(f"Connection error: {e}")


async def interactive_loop(link):
    print("Type 'help' for a list of commands, 'quit' to exit.")
    loop = asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("> "))
        except EOFError:
            break
        try:
            await run_one(link, line)
        except SystemExit:
            break


async def cli_main_async(args):
    link = GameLink()

    async def handler(ws):
        print("Bitburner connected.")
        try:
            await link.on_connect(ws)
        finally:
            print("Bitburner disconnected.")

    server = await websockets.serve(handler, args.host, args.port)
    print(f"Listening on ws://{args.host}:{args.port} ... waiting for Bitburner to connect.")
    print("In Bitburner: Options -> Remote API -> set the same hostname/port -> Connect.")

    await link.wait_connected()

    try:
        if args.run:
            await run_one(link, args.run)
        else:
            await interactive_loop(link)
    finally:
        server.close()
        await server.wait_closed()


def run_cli(args):
    try:
        asyncio.run(cli_main_async(args))
    except KeyboardInterrupt:
        print("\nExiting.")


# ==========================================================================
# GUI mode
# ==========================================================================

class AsyncLoopThread:
    """Runs an asyncio event loop on its own thread so the Tk mainloop stays free."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_coro(self, coro, on_done=None, on_error=None):
        """Schedule coro on the loop thread; call on_done/on_error back on whichever
        thread they run (caller is responsible for marshalling to Tk if needed)."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _cb(f):
            try:
                result = f.result()
                if on_done:
                    on_done(result)
            except Exception as e:
                if on_error:
                    on_error(e)

        fut.add_done_callback(_cb)
        return fut

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


class BitburnerGUI:
    def __init__(self, root):
        import tkinter as tk  # noqa: F401 (imported lazily so --cli doesn't require tkinter)
        self.root = root
        root.title("Bitburner Remote API")
        root.geometry("880x600")

        self.async_thread = AsyncLoopThread()
        self.link = GameLink(on_status_change=self._on_status_change)
        self.ws_server = None
        self.watch_stop_event = None
        self.watch_thread_running = False

        self._build_widgets()
        self._log("Ready. Click 'Start Listening' then connect from Bitburner's Options -> Remote API.")

    # ---------------- UI construction ----------------

    def _build_widgets(self):
        import tkinter as tk
        from tkinter import ttk

        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Host:").grid(row=0, column=0, sticky="w")
        self.host_var = tk.StringVar(value="localhost")
        ttk.Entry(top, textvariable=self.host_var, width=14).grid(row=0, column=1, padx=(0, 10))

        ttk.Label(top, text="Port:").grid(row=0, column=2, sticky="w")
        self.port_var = tk.StringVar(value="12525")
        ttk.Entry(top, textvariable=self.port_var, width=8).grid(row=0, column=3, padx=(0, 10))

        self.start_btn = ttk.Button(top, text="Start Listening", command=self.start_listening)
        self.start_btn.grid(row=0, column=4, padx=(0, 10))

        self.status_var = tk.StringVar(value="Not listening")
        self.status_label = ttk.Label(top, textvariable=self.status_var, foreground="gray")
        self.status_label.grid(row=0, column=5, padx=(0, 10))

        # Server selection row
        mid = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        mid.pack(fill="x")

        ttk.Label(mid, text="Server:").grid(row=0, column=0, sticky="w")
        self.server_var = tk.StringVar()
        self.server_combo = ttk.Combobox(mid, textvariable=self.server_var, width=28, state="readonly")
        self.server_combo.grid(row=0, column=1, padx=(0, 10))
        self.server_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_files())

        ttk.Button(mid, text="Refresh Servers", command=self.refresh_servers).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(mid, text="Refresh Files", command=self.refresh_files).grid(row=0, column=3, padx=(0, 6))

        # Main body: file list on the left, action buttons on the right
        body = ttk.Frame(self.root, padding=8)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="Files on selected server:").pack(anchor="w")
        self.file_listbox = tk.Listbox(left, selectmode="extended")
        self.file_listbox.pack(fill="both", expand=True, side="left")
        scrollbar = ttk.Scrollbar(left, command=self.file_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.file_listbox.config(yscrollcommand=scrollbar.set)

        right = ttk.Frame(body, padding=(10, 0, 0, 0))
        right.pack(side="left", fill="y")

        ttk.Label(right, text="Actions", font=("", 10, "bold")).pack(anchor="w", pady=(0, 6))

        ttk.Button(right, text="Pull ALL files to folder...", command=self.pull_all).pack(fill="x", pady=2)
        ttk.Button(right, text="Download selected file(s)...", command=self.download_selected).pack(fill="x", pady=2)
        ttk.Button(right, text="Push single file...", command=self.push_single).pack(fill="x", pady=2)
        ttk.Button(right, text="Push entire folder...", command=self.push_folder).pack(fill="x", pady=2)
        ttk.Button(right, text="Delete selected file(s)", command=self.delete_selected).pack(fill="x", pady=2)
        ttk.Button(right, text="Calculate RAM of selected", command=self.calc_ram_selected).pack(fill="x", pady=2)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=8)

        ttk.Label(right, text="Auto-Watch Folder", font=("", 10, "bold")).pack(anchor="w")
        self.watch_dir_var = tk.StringVar(value="(none selected)")
        ttk.Label(right, textvariable=self.watch_dir_var, wraplength=200, foreground="gray").pack(anchor="w", pady=(2, 4))
        self.watch_btn = ttk.Button(right, text="Choose Folder & Start Watching", command=self.toggle_watch)
        self.watch_btn.pack(fill="x", pady=2)

        # Log area
        log_frame = ttk.Frame(self.root, padding=8)
        log_frame.pack(fill="both", expand=False)
        ttk.Label(log_frame, text="Log:").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    # ---------------- helpers ----------------

    def _log(self, msg):
        def _do():
            self.log_text.config(state="normal")
            self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.root.after(0, _do)

    def _on_status_change(self, status):
        def _do():
            if status == "connected":
                self.status_var.set("Bitburner connected")
                self.status_label.config(foreground="green")
                self._log("Bitburner connected.")
                self.refresh_servers()
            else:
                self.status_var.set("Listening... waiting for Bitburner")
                self.status_label.config(foreground="orange")
                self._log("Bitburner disconnected.")
        self.root.after(0, _do)

    def _require_server(self):
        from tkinter import messagebox
        s = self.server_var.get().strip()
        if not s:
            messagebox.showwarning("No server", "Pick a server first (Refresh Servers).")
            return None
        return s

    def _require_connected(self):
        from tkinter import messagebox
        if not self.link.is_connected():
            messagebox.showwarning("Not connected", "Bitburner isn't connected yet.")
            return False
        return True

    def _selected_files(self):
        return [self.file_listbox.get(i) for i in self.file_listbox.curselection()]

    # ---------------- server lifecycle ----------------

    def start_listening(self):
        from tkinter import messagebox
        host = self.host_var.get().strip() or "localhost"
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid port", "Port must be a number.")
            return

        self.start_btn.config(state="disabled")

        async def handler(ws):
            await self.link.on_connect(ws)

        async def _serve():
            self.ws_server = await websockets.serve(handler, host, port)

        def on_done(_):
            self.status_var.set(f"Listening on {host}:{port} - waiting for Bitburner")
            self.status_label.config(foreground="orange")
            self._log(f"Listening on ws://{host}:{port}. In Bitburner: Options -> Remote API -> Connect.")

        def on_error(e):
            self.start_btn.config(state="normal")
            self._log(f"Failed to start listening: {e}")
            messagebox.showerror("Error", f"Failed to start listening:\n{e}")

        self.async_thread.run_coro(_serve(), on_done=on_done, on_error=on_error)

    # ---------------- server/file browsing ----------------

    def refresh_servers(self):
        if not self._require_connected():
            return

        def on_done(servers):
            def _do():
                names = sorted(s["hostname"] for s in servers) if servers else []
                self.server_combo["values"] = names
                if names and not self.server_var.get():
                    self.server_var.set(names[0])
                    self.refresh_files()
                self._log(f"Found {len(names)} server(s).")
            self.root.after(0, _do)

        def on_error(e):
            self._log(f"Error listing servers: {e}")

        self.async_thread.run_coro(self.link.get_all_servers(), on_done, on_error)

    def refresh_files(self):
        server = self._require_server()
        if not server or not self._require_connected():
            return

        def on_done(names):
            def _do():
                self.file_listbox.delete(0, "end")
                for n in sorted(names or []):
                    self.file_listbox.insert("end", n)
                self._log(f"{server}: {len(names or [])} file(s).")
            self.root.after(0, _do)

        def on_error(e):
            self._log(f"Error listing files on {server}: {e}")

        self.async_thread.run_coro(self.link.get_file_names(server), on_done, on_error)

    # ---------------- pull / download ----------------

    def pull_all(self):
        from tkinter import filedialog, messagebox
        server = self._require_server()
        if not server or not self._require_connected():
            return
        outdir = filedialog.askdirectory(title=f"Choose folder to save ALL files from {server}")
        if not outdir:
            return

        async def _pull():
            files = await self.link.get_all_files(server)
            os.makedirs(outdir, exist_ok=True)
            for f in files or []:
                local_path = remote_to_local_path(f["filename"], outdir)
                os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
                with open(local_path, "w", encoding="utf-8") as fh:
                    fh.write(f["content"])
            return len(files or [])

        def on_done(count):
            self._log(f"Pulled {count} file(s) from {server} into {outdir}")
            messagebox.showinfo("Done", f"Pulled {count} file(s) from {server} into:\n{outdir}")

        def on_error(e):
            self._log(f"Pull failed: {e}")
            messagebox.showerror("Error", f"Pull failed:\n{e}")

        self.async_thread.run_coro(_pull(), on_done, on_error)

    def download_selected(self):
        from tkinter import filedialog, messagebox
        server = self._require_server()
        if not server or not self._require_connected():
            return
        files = self._selected_files()
        if not files:
            messagebox.showwarning("Nothing selected", "Select one or more files in the list first.")
            return
        outdir = filedialog.askdirectory(title="Choose folder to save selected file(s)")
        if not outdir:
            return

        async def _get_all():
            saved = []
            for remote in files:
                content = await self.link.get_file(remote, server)
                local_path = remote_to_local_path(remote, outdir)
                os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
                with open(local_path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                saved.append(local_path)
            return saved

        def on_done(saved):
            self._log(f"Downloaded {len(saved)} file(s) to {outdir}")

        def on_error(e):
            self._log(f"Download failed: {e}")
            messagebox.showerror("Error", f"Download failed:\n{e}")

        self.async_thread.run_coro(_get_all(), on_done, on_error)

    # ---------------- push ----------------

    def push_single(self):
        from tkinter import filedialog, messagebox, simpledialog
        server = self._require_server()
        if not server or not self._require_connected():
            return
        path = filedialog.askopenfilename(title="Choose local file to push")
        if not path:
            return
        default_remote = "/" + os.path.basename(path)
        remote = simpledialog.askstring("Remote filename", "Remote path on server:", initialvalue=default_remote)
        if not remote:
            return
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()

        def on_done(_):
            self._log(f"Pushed {path} -> {server}:{remote}")
            self.refresh_files()

        def on_error(e):
            self._log(f"Push failed: {e}")
            messagebox.showerror("Error", f"Push failed:\n{e}")

        self.async_thread.run_coro(self.link.push_file(remote, content, server), on_done, on_error)

    def push_folder(self):
        from tkinter import filedialog, messagebox
        server = self._require_server()
        if not server or not self._require_connected():
            return
        directory = filedialog.askdirectory(title="Choose folder to push (all files inside it)")
        if not directory:
            return

        async def _push_all():
            count = 0
            for root, _, filenames in os.walk(directory):
                for fn in filenames:
                    local_path = os.path.join(root, fn)
                    remote_name = local_to_remote_name(local_path, directory)
                    with open(local_path, "r", encoding="utf-8") as fh:
                        content = fh.read()
                    await self.link.push_file(remote_name, content, server)
                    count += 1
            return count

        def on_done(count):
            self._log(f"Pushed {count} file(s) from {directory} to {server}")
            self.refresh_files()

        def on_error(e):
            self._log(f"Push folder failed: {e}")
            messagebox.showerror("Error", f"Push folder failed:\n{e}")

        self.async_thread.run_coro(_push_all(), on_done, on_error)

    # ---------------- delete / ram ----------------

    def delete_selected(self):
        from tkinter import messagebox
        server = self._require_server()
        if not server or not self._require_connected():
            return
        files = self._selected_files()
        if not files:
            messagebox.showwarning("Nothing selected", "Select one or more files in the list first.")
            return
        if not messagebox.askyesno("Confirm delete", f"Delete {len(files)} file(s) from {server}?"):
            return

        async def _delete_all():
            for remote in files:
                await self.link.delete_file(remote, server)
            return len(files)

        def on_done(count):
            self._log(f"Deleted {count} file(s) from {server}")
            self.refresh_files()

        def on_error(e):
            self._log(f"Delete failed: {e}")
            messagebox.showerror("Error", f"Delete failed:\n{e}")

        self.async_thread.run_coro(_delete_all(), on_done, on_error)

    def calc_ram_selected(self):
        from tkinter import messagebox
        server = self._require_server()
        if not server or not self._require_connected():
            return
        files = self._selected_files()
        if not files:
            messagebox.showwarning("Nothing selected", "Select one or more files in the list first.")
            return

        async def _calc_all():
            results = []
            for remote in files:
                cost = await self.link.calculate_ram(remote, server)
                results.append((remote, cost))
            return results

        def on_done(results):
            lines = "\n".join(f"{name}: {cost} GB" for name, cost in results)
            self._log("RAM costs:\n" + lines)
            messagebox.showinfo("RAM cost", lines)

        def on_error(e):
            self._log(f"RAM calc failed: {e}")
            messagebox.showerror("Error", f"RAM calc failed:\n{e}")

        self.async_thread.run_coro(_calc_all(), on_done, on_error)

    # ---------------- watch ----------------

    def toggle_watch(self):
        from tkinter import filedialog
        if self.watch_thread_running:
            self.watch_stop_event.set()
            self.watch_btn.config(text="Choose Folder & Start Watching")
            self.watch_thread_running = False
            self._log("Stopped watching.")
            return

        server = self._require_server()
        if not server or not self._require_connected():
            return
        directory = filedialog.askdirectory(title="Choose folder to auto-watch and push on change")
        if not directory:
            return

        self.watch_dir_var.set(directory)
        self.watch_stop_event = threading.Event()
        self.watch_thread_running = True
        self.watch_btn.config(text="Stop Watching")

        def scan():
            current = {}
            for root, _, filenames in os.walk(directory):
                for fn in filenames:
                    path = os.path.join(root, fn)
                    try:
                        current[path] = os.path.getmtime(path)
                    except OSError:
                        pass
            return current

        async def push_one(path):
            remote_name = local_to_remote_name(path, directory)
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            await self.link.push_file(remote_name, content, server)
            self._log(f"[watch] pushed {remote_name}")

        def watch_loop():
            mtimes = scan()
            for path in mtimes:
                self.async_thread.run_coro(push_one(path), on_error=lambda e, p=path: self._log(f"[watch] failed {p}: {e}"))
            while not self.watch_stop_event.is_set():
                time.sleep(1.5)
                if not self.link.is_connected():
                    self._log("[watch] Bitburner disconnected, stopping watch.")
                    self.root.after(0, lambda: (self.watch_btn.config(text="Choose Folder & Start Watching")))
                    self.watch_thread_running = False
                    return
                current = scan()
                for path, mtime in current.items():
                    if path not in mtimes or mtimes[path] != mtime:
                        self.async_thread.run_coro(push_one(path), on_error=lambda e, p=path: self._log(f"[watch] failed {p}: {e}"))
                mtimes = current

        threading.Thread(target=watch_loop, daemon=True).start()
        self._log(f"Watching {directory} -> pushing changes to '{server}'.")


def run_gui():
    import tkinter as tk
    root = tk.Tk()
    app = BitburnerGUI(root)  # noqa: F841
    root.mainloop()


# ==========================================================================
# Entry point
# ==========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Bitburner Remote API sync tool (GUI by default, or --cli for console mode)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--gui", action="store_true", help="Run the Tkinter GUI (default).")
    mode.add_argument("--cli", action="store_true", help="Run the console/interactive CLI instead of the GUI.")

    parser.add_argument("--host", default="localhost", help="[CLI] Interface to listen on (default: localhost)")
    parser.add_argument("--port", type=int, default=12525, help="Port to listen on (default: 12525)")
    parser.add_argument("--run", help="[CLI] Run a single command non-interactively then exit, "
                                       "e.g. --run \"pull home ./scripts\"")
    args = parser.parse_args()

    if args.cli:
        run_cli(args)
    else:
        run_gui()


if __name__ == "__main__":
    main()
