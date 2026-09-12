#!/usr/bin/env python3
"""
AI Chat HTTP Server
===================

A single-file HTTP server that exposes a chat API backed by the Hugging Face
Inference API and serves a self-contained web UI for interacting with it.

The server provides:
    - GET  /            → Serves the embedded chat UI (HTML/CSS/JS in one page)
    - POST /api/chat    → Accepts {"message": "..."} (JSON) or form data and
                          returns {"response": "..."} from the AI model
    - OPTIONS *         → CORS preflight handling

Features:
    - Per-IP rate limiting with automatic temporary bans
    - CORS support for allowed origins
    - Configurable model, provider, and generation parameters
    - API key loaded from a file or supplied directly
    - Request size limits to prevent abuse

Usage:
    python server.py [options]

Examples:
    # Run with defaults on port 1234
    python server.py

    # Run on a custom port with a custom key file
    python server.py --port 8080 --key-file /path/to/key.txt

    # Change model and generation parameters
    python server.py --model "deepseek-ai/DeepSeek-V3-0324" --temperature 0.7

    # Loosen rate limits
    python server.py --requests-per-minute 60 --ban-time 10
"""

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote

from huggingface_hub import InferenceClient


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "PORT": 1234,
    "HOST": "0.0.0.0",
    # Default: three levels up from this script, then resources/key.txt
    "API_KEY_FILE": _SCRIPT_DIR.parent.parent / "resources" / "key.txt",
    "MODEL_NAME": "deepseek-ai/DeepSeek-V3-0324",
    "PROVIDER": "nebius",
    "TEMPERATURE": 0.1,
    "MAX_TOKENS": 4096,
    "TOP_P": 0.5,
    "STREAM": True,
    "REQUESTS_PER_MINUTE": 30,
    "BAN_TIME": 30,
    "ALLOWED_ORIGINS": ["http://localhost", "http://127.0.0.1"],
    "MAX_REQUEST_SIZE": 1024 * 10,  # 10 KB
    "SYSTEM_CONTEXT": "",
}


# ---------------------------------------------------------------------------
# Embedded web UI
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Assistant</title>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        :root {
            --bg: #f4f4f4;
            --card: #e9e9e9;
            --accent: #4CAF50;
            --accent-hover: #45a049;
            --text: #222;
        }
        @media (prefers-color-scheme: dark) {
            :root {
                --bg: #1e1e1e;
                --card: #2a2a2a;
                --accent: #4CAF50;
                --accent-hover: #45a049;
                --text: #eaeaea;
            }
        }
        * { box-sizing: border-box; }
        body {
            font-family: system-ui, -apple-system, 'Segoe UI', Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            line-height: 1.6;
            background-color: var(--bg);
            color: var(--text);
            transition: background-color 0.3s ease, color 0.3s ease;
            text-align: center;
        }
        .container {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 20px;
        }
        textarea, input, button {
            padding: 15px;
            font-size: 18px;
            border: 2px solid #ddd;
            border-radius: 8px;
            width: 80%;
            box-sizing: border-box;
            font-family: inherit;
        }
        textarea { resize: vertical; min-height: 100px; }
        button {
            background-color: var(--accent);
            color: white;
            cursor: pointer;
            border: none;
            transition: background-color 0.3s ease;
        }
        button:hover:not(:disabled) { background-color: var(--accent-hover); }
        button:disabled { opacity: 0.6; cursor: not-allowed; }
        #response {
            padding: 20px;
            background-color: var(--card);
            border-radius: 8px;
            min-height: 150px;
            width: 90%;
            text-align: left;
            animation: fadeIn 0.5s ease;
        }
        #response_data {
            padding: 10px;
            border-radius: 8px;
            min-height: 350px;
            width: 100%;
            text-align: left;
            animation: fadeIn 0.5s ease;
            word-wrap: break-word;
            overflow-wrap: break-word;
        }
        #response_data pre {
            background: rgba(0,0,0,0.08);
            padding: 10px;
            border-radius: 6px;
            overflow-x: auto;
        }
        #response_data code {
            font-family: ui-monospace, 'Cascadia Code', Menlo, Consolas, monospace;
        }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        .loading { color: #888; font-style: italic; }
        .error { color: #c0392b; }
    </style>
</head>
<body>
    <div class="container">
        <h1>AI Assistant</h1>
        <form id="chatForm">
            <textarea id="message" placeholder="Ask me anything..." name="message"
                      rows="4" required aria-label="Enter your message"></textarea><br>
            <button type="submit" id="sendBtn" aria-label="Send message">Send</button>
        </form>
        <div id="response">
            <div id="help"></div>
            <div id="response_data" aria-label="AI response"></div><br>
        </div>
    </div>

    <script>
        const form = document.getElementById('chatForm');
        const messageInput = document.getElementById('message');
        const sendBtn = document.getElementById('sendBtn');
        const helpDiv = document.getElementById('help');
        const responseDataDiv = document.getElementById('response_data');

        form.addEventListener('submit', async function (e) {
            e.preventDefault();
            const message = messageInput.value.trim();
            if (!message) {
                alert('Please enter a message.');
                return;
            }

            sendBtn.disabled = true;
            helpDiv.innerHTML = '<div class="loading">Thinking...</div>';
            responseDataDiv.innerHTML = '';

            try {
                const response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message })
                });

                const data = await response.json().catch(() => ({}));

                if (!response.ok) {
                    throw new Error(data.error || `HTTP ${response.status}`);
                }

                helpDiv.innerHTML = '';
                responseDataDiv.innerHTML = marked.parse(data.response || '') ||
                                            'No response received';
            } catch (error) {
                helpDiv.innerHTML = '';
                responseDataDiv.innerHTML =
                    `<span class="error">Error: ${error.message}</span>`;
            } finally {
                sendBtn.disabled = false;
            }
        });
    </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments with sensible defaults."""
    parser = argparse.ArgumentParser(
        description="Run the AI chat HTTP server.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=DEFAULTS["HOST"],
                        help="Interface to bind to.")
    parser.add_argument("--port", type=int, default=DEFAULTS["PORT"],
                        help="Port to listen on.")
    parser.add_argument("--key-file", type=Path, default=DEFAULTS["API_KEY_FILE"],
                        help="File whose first line is the Hugging Face API key.")
    parser.add_argument("--api-key", default=None,
                        help="API key directly. Overrides --key-file if set.")
    parser.add_argument("--model", default=DEFAULTS["MODEL_NAME"],
                        help="Hugging Face model ID.")
    parser.add_argument("--provider", default=DEFAULTS["PROVIDER"],
                        help="Inference provider (nebius, together, etc.).")
    parser.add_argument("--temperature", type=float, default=DEFAULTS["TEMPERATURE"],
                        help="Sampling temperature.")
    parser.add_argument("--max-tokens", type=int, default=DEFAULTS["MAX_TOKENS"],
                        help="Maximum tokens to generate.")
    parser.add_argument("--top-p", type=float, default=DEFAULTS["TOP_P"],
                        help="Nucleus sampling top-p.")
    parser.add_argument("--system-context", default=DEFAULTS["SYSTEM_CONTEXT"],
                        help="Optional system prompt prepended to every request.")
    parser.add_argument("--requests-per-minute", type=int,
                        default=DEFAULTS["REQUESTS_PER_MINUTE"],
                        help="Max requests per IP per minute.")
    parser.add_argument("--ban-time", type=int, default=DEFAULTS["BAN_TIME"],
                        help="Ban duration (seconds) for rate limit violators.")
    parser.add_argument("--allowed-origins", nargs="*",
                        default=DEFAULTS["ALLOWED_ORIGINS"],
                        help="Allowed CORS origins (prefix match).")
    parser.add_argument("--max-request-size", type=int,
                        default=DEFAULTS["MAX_REQUEST_SIZE"],
                        help="Maximum request body size in bytes.")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# API key loading
# ---------------------------------------------------------------------------

def load_api_key(args: argparse.Namespace) -> str:
    """Resolve the API key from CLI args or a key file, or exit on failure."""
    if args.api_key:
        return args.api_key.strip()

    key_path: Path = args.key_file
    if not key_path.exists():
        log.error("API key file does not exist: %s", key_path)
        log.error("Provide --api-key or create the key file with your token.")
        sys.exit(1)

    key = key_path.read_text(encoding="utf-8").strip()
    if not key:
        log.error("API key file is empty: %s", key_path)
        sys.exit(1)
    return key


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple per-IP sliding-window rate limiter with temporary bans."""

    def __init__(self, requests_per_minute: int, ban_time: int):
        self.requests_per_minute = requests_per_minute
        self.ban_time = ban_time
        self.requests: Dict[str, List[float]] = defaultdict(list)
        self.banned_ips: Dict[str, float] = {}

    def check_rate_limit(self, ip: str) -> Tuple[bool, Optional[str]]:
        """Return (allowed, message). If not allowed, message explains why."""
        now = time.time()

        # Already banned?
        if ip in self.banned_ips:
            if now < self.banned_ips[ip]:
                remaining = int(self.banned_ips[ip] - now) + 1
                return False, f"Too many requests. Try again in {remaining}s."
            del self.banned_ips[ip]

        # Drop timestamps older than 60 seconds
        self.requests[ip] = [t for t in self.requests[ip] if now - t < 60]

        if len(self.requests[ip]) >= self.requests_per_minute:
            self.banned_ips[ip] = now + self.ban_time
            return False, "Too many requests. You have been temporarily banned."

        self.requests[ip].append(now)
        return True, None


# ---------------------------------------------------------------------------
# AI client wrapper
# ---------------------------------------------------------------------------

class AIClient:
    """Wrapper around the Hugging Face Inference client."""

    def __init__(self, api_key: str, config: argparse.Namespace):
        self.api_key = api_key
        self.config = config
        self._client: Optional[InferenceClient] = None

    def _ensure_client(self) -> InferenceClient:
        if self._client is None:
            self._client = InferenceClient(
                provider=self.config.provider,
                api_key=self.api_key,
            )
            log.info("AI client initialised (provider=%s, model=%s)",
                     self.config.provider, self.config.model)
        return self._client

    def get_response(self, user_message: str) -> str:
        """Send a message to the model and return the (non-streamed) reply."""
        if not user_message.strip():
            return "Please provide a valid question or prompt."

        messages: List[Dict[str, str]] = []
        if self.config.system_context:
            messages.append({"role": "system", "content": self.config.system_context})
        messages.append({"role": "user", "content": user_message})

        try:
            client = self._ensure_client()
            response = client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                top_p=self.config.top_p,
                stream=True,
            )

            full_response = ""
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    full_response += chunk.choices[0].delta.content
            return full_response or "(empty response)"

        except Exception as exc:  # noqa: BLE001
            log.exception("AI request failed")
            return f"Sorry, I encountered an error: {exc}"


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

# Content types for static file serving (kept for optional static assets)
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
}


class HTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler serving the UI and the /api/chat endpoint."""

    # Injected by the server factory
    ai_client: AIClient
    rate_limiter: RateLimiter
    config: argparse.Namespace

    server_version = "AIChatServer/1.0"

    # ----- routing ---------------------------------------------------------

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                self._serve_index()
            else:
                self._serve_static()
        except Exception as exc:  # noqa: BLE001
            log.exception("GET failed")
            self._send_error_json(500, f"Server error: {exc}")

    def do_POST(self):
        client_ip = self.client_address[0]

        allowed, message = self.rate_limiter.check_rate_limit(client_ip)
        if not allowed:
            self._send_json(429, {"error": message})
            return

        if self.path != "/api/chat":
            self._send_json(404, {"error": "Endpoint not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > self.config.max_request_size:
                self._send_json(413, {"error": "Request too large"})
                return

            content_type = self.headers.get("Content-Type", "")
            body = self.rfile.read(content_length) if content_length else b""

            user_message = ""
            if "application/json" in content_type:
                try:
                    data = json.loads(body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "Invalid JSON"})
                    return
                user_message = str(data.get("message", "")).strip()
            else:
                data = parse_qs(body.decode("utf-8"))
                user_message = unquote(data.get("message", [""])[0]).strip()

            if not user_message:
                self._send_json(400, {"error": "Message is required"})
                return

            ai_response = self.ai_client.get_response(user_message)
            self._send_json(200, {"response": ai_response})

        except Exception as exc:  # noqa: BLE001
            log.exception("POST failed")
            self._send_json(500, {"error": f"Server error: {exc}"})

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # ----- helpers ---------------------------------------------------------

    def _serve_index(self):
        body = INDEX_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self):
        """Serve files from the script directory (with path traversal guard)."""
        rel = unquote(self.path.lstrip("/"))
        if not rel or ".." in Path(rel).parts:
            self._send_error_json(403, "Forbidden")
            return

        file_path = (_SCRIPT_DIR / rel).resolve()
        try:
            file_path.relative_to(_SCRIPT_DIR)
        except ValueError:
            self._send_error_json(403, "Forbidden")
            return

        if not file_path.is_file():
            self._send_error_json(404, "File not found")
            return

        content_type = CONTENT_TYPES.get(file_path.suffix.lower(),
                                         "application/octet-stream")
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self):
        origin = self.headers.get("Origin")
        if origin and any(origin.startswith(a) for a in self.config.allowed_origins):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status: int, data: Dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str):
        self._send_json(status, {"error": message})

    def log_message(self, fmt, *args):
        # Route BaseHTTPRequestHandler logs through the logging module
        log.debug("%s - %s", self.client_address[0], fmt % args)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------

def make_handler(ai_client: AIClient, rate_limiter: RateLimiter,
                 config: argparse.Namespace):
    """Return a handler class bound to the given shared state."""

    class BoundHandler(HTTPRequestHandler):
        pass

    BoundHandler.ai_client = ai_client
    BoundHandler.rate_limiter = rate_limiter
    BoundHandler.config = config
    return BoundHandler


def run_server(config: argparse.Namespace, api_key: str):
    """Create and run the HTTP server until interrupted."""
    ai_client = AIClient(api_key, config)
    rate_limiter = RateLimiter(config.requests_per_minute, config.ban_time)
    handler = make_handler(ai_client, rate_limiter, config)

    httpd = HTTPServer((config.host, config.port), handler)
    log.info("Server listening on http://%s:%d", config.host, config.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    logging.getLogger().setLevel(args.log_level.upper())
    api_key = load_api_key(args)
    run_server(args, api_key)


if __name__ == "__main__":
    main()