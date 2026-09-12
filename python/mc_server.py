#!/usr/bin/env python3
"""
All-in-one, SINGLE-FILE launcher for connecting a Mineflayer bot to a
Minecraft server via ViaProxy (protocol translation), even when mineflayer
itself doesn't support the server's exact version yet.

Improvements applied:
  - Optimized visualizer: colors are precomputed once per chunk scan (no more
    per-pixel regex matching), giving a large render speedup.
  - Map is now player-relative: the world rotates under the player, so the
    player marker always points "up" on screen. The marker is drawn as a
    fixed overlay in the browser (cheap, and always visible).
  - Live coordinates + facing shown in a HUD box in the top-right corner,
    polled independently of the map image so it updates quickly even if the
    map itself renders more slowly.
  - Added Q/E turning (smooth continuous turn while held), alongside WASD +
    space.
  - Windows-friendly DNS: added fallback nslookup parsing for guaranteed SRV
    resolution.
  - Process safety: registered Python 'atexit' hooks to prevent leftover
    Java/Node processes.
  - Added CLI color support and extra local bot commands (.help, .status).
"""

import atexit
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.request

# ANSI Colors for Terminal Output
CLR_RESET = "\033[0m"
CLR_GREEN = "\033[92m"
CLR_YELLOW = "\033[93m"
CLR_RED = "\033[91m"
CLR_CYAN = "\033[96m"

INDEX_JS_SOURCE = r"""#!/usr/bin/env node
// Console Minecraft bot using Mineflayer
'use strict';
const mineflayer = require('mineflayer');
const readline = require('readline');
const visualizer = require('./visualizer');

function parseArgs(argv) {
  let [a, b, c, d] = argv;
  let host, port, username, version;

  if (a && a.includes(':')) {
    const idx = a.lastIndexOf(':');
    host = a.slice(0, idx);
    port = parseInt(a.slice(idx + 1), 10);
    username = b;
    version = c;
  } else {
    host = a;
    port = parseInt(b, 10);
    username = c;
    version = d;
  }

  host = host || 'localhost';
  if (!Number.isFinite(port)) port = 25565;
  username = username || 'ConsoleBot';
  return { host, port, username, version };
}

const { host: initialHost, port: initialPort, username, version } = parseArgs(process.argv.slice(2));

let host = initialHost;
let port = initialPort;
let lastHost = initialHost;
let lastPort = initialPort;

const isTTY = process.stdout.isTTY && process.stdin.isTTY;
const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: isTTY,
  prompt: '',
});

function printLine(text) {
  if (isTTY) {
    readline.cursorTo(process.stdout, 0);
    readline.clearScreenDown(process.stdout);
  }
  console.log(text);
  if (isTTY) rl.prompt(true);
}

printLine(`[launcher] Connecting to ${host}:${port} as "${username}"${version ? ` (version ${version})` : ''}...`);

let ready = false;
let bot;
let intentionalDisconnect = false;

function connect() {
  lastHost = host;
  lastPort = port;
  intentionalDisconnect = false;
  const opts = { host, port, username };
  if (version) opts.version = version;

  bot = mineflayer.createBot(opts);
  bot.loadPlugin(visualizer({ httpPort: 8095 }));

  bot.on('login', () => printLine(`[connected] Logged in as ${bot.username}`));

  bot.on('spawn', () => {
    ready = true;
    printLine('[ready] Bot has spawned. Type a message to chat, /command for server commands, or .help for local bot options.');
  });

  bot.on('kicked', (reason) => {
    ready = false;
    const text = typeof reason === 'string' ? reason : JSON.stringify(reason);
    printLine(`[kicked] ${text}`);
    if (/online mode/i.test(text) && /authentic/i.test(text)) {
      printLine('[hint] This server requires a real Microsoft account (online mode).');
      printLine('[hint] Run:  python launch.py --setup   (to log in once)');
      printLine('[hint] Then reconnect with --online added to your normal command.');
    }
  });

  bot.on('error', (err) => printLine(`[error] ${err.message}`));

  bot.on('end', () => {
    ready = false;
    if (intentionalDisconnect) {
      printLine('[disconnected] Left the server. Use .join <target> or .rejoin to reconnect.');
    } else {
      printLine('[disconnected] Connection closed.');
    }
  });

  bot.on('chat', (username, message) => printLine(`<${username}> ${message}`));
  bot.on('whisper', (username, message) => printLine(`[whisper] <${username}> ${message}`));

  bot.on('message', (jsonMsg, position) => {
    if (position === 'chat') return;
    const text = jsonMsg.toString();
    if (text.trim()) printLine(`[server] ${text}`);
  });
}

connect();

function parseJoinTarget(raw) {
  let str = raw.trim().replace(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//, '').split(/[\/?#]/)[0];
  if (!str) return null;

  let h, p;
  if (str.includes(':')) {
    const idx = str.lastIndexOf(':');
    h = str.slice(0, idx);
    p = parseInt(str.slice(idx + 1), 10);
    if (!Number.isFinite(p)) return null;
  } else {
    h = str;
    p = null;
  }
  return h ? { host: h, port: p } : null;
}

function disconnectCurrent() {
  intentionalDisconnect = true;
  ready = false;
  if (bot) {
    try { bot.quit(); } catch (e) {}
  }
}

function handleLocalCommand(text) {
  const parts = text.slice(1).split(/\s+/);
  const cmd = parts[0].toLowerCase();
  const arg = parts.slice(1).join(' ');

  switch (cmd) {
    case 'help':
      printLine('[local] Available commands:');
      printLine('        .help          - Show this help message');
      printLine('        .leave         - Disconnect from current server');
      printLine('        .join <target> - Connect to a new server (e.g. .join play.example.com)');
      printLine('        .rejoin        - Reconnect to previous server');
      printLine('        .cls / .clear  - Clear console screen');
      return true;

    case 'cls':
    case 'clear':
      console.clear();
      if (isTTY) rl.prompt(true);
      return true;

    case 'leave':
      if (!bot || !ready) {
        printLine('[local] Not currently connected.');
        return true;
      }
      printLine(`[local] Leaving ${host}:${port}...`);
      disconnectCurrent();
      return true;

    case 'join': {
      if (!arg) {
        printLine('[local] Usage: .join <ip|host|url>[:port]');
        return true;
      }
      const target = parseJoinTarget(arg);
      if (!target) {
        printLine(`[local] Could not parse target: "${arg}"`);
        return true;
      }
      host = target.host;
      port = target.port !== null ? target.port : port;
      printLine(`[local] Joining ${host}:${port}...`);
      disconnectCurrent();
      setTimeout(connect, 300);
      return true;
    }

    case 'rejoin':
      host = lastHost;
      port = lastPort;
      printLine(`[local] Rejoining ${host}:${port}...`);
      disconnectCurrent();
      setTimeout(connect, 300);
      return true;

    default:
      printLine(`[local] Unknown command: .${cmd}. Type .help for a list of commands.`);
      return true;
  }
}

const ILLEGAL_CHARS = /[\u00a7\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;

rl.on('line', (line) => {
  const text = line.trim();
  if (!text) return;

  if (text.startsWith('.')) {
    handleLocalCommand(text);
    return;
  }

  if (!bot || !ready) {
    printLine('[not ready] Not connected — message not sent. Use .join <target> or .rejoin.');
    return;
  }

  if (text.startsWith('/')) {
    bot.chat(text);
    return;
  }

  const clean = text.replace(ILLEGAL_CHARS, '');
  if (!clean) return;
  bot.chat(clean);
});

rl.on('close', () => {
  if (bot) bot.quit();
  process.exit(0);
});

process.on('SIGINT', () => rl.close());
"""

PACKAGE_JSON_SOURCE = r"""{
  "name": "mc-bot",
  "version": "1.0.0",
  "description": "Tiny console Minecraft bot using Mineflayer",
  "main": "index.js",
  "type": "commonjs",
  "scripts": {
    "start": "node index.js"
  },
  "dependencies": {
    "mineflayer": "^4.37.1",
    "pngjs": "^7.0.0",
    "vec3": "^0.1.8"
  }
}
"""

VISUALIZER_JS_SOURCE = r"""// visualizer.js — fast, player-relative top-down world visualizer
'use strict';
const { PNG } = require('pngjs');
const http = require('http');
const fs = require('fs');
const path = require('path');
const { exec } = require('child_process');
const Vec3 = require('vec3').Vec3;

function openInBrowser(url) {
  const plat = process.platform;
  const cmd = plat === 'darwin' ? `open "${url}"`
    : plat === 'win32' ? `start "" "${url}"`
    : `xdg-open "${url}"`;
  exec(cmd, (err) => {
    if (err) console.log(`[visualizer] Open browser manually: ${url}`);
  });
}

const PALETTE = [
  [/water/, [64, 105, 224]],
  [/lava/, [216, 82, 20]],
  [/(grass_block|grass$)/, [86, 156, 61]],
  [/leaves/, [55, 110, 45]],
  [/log|wood|planks/, [128, 92, 56]],
  [/sand/, [219, 207, 143]],
  [/snow|ice/, [235, 240, 245]],
  [/(stone|cobble|deepslate|andesite|diorite|granite)/, [130, 130, 130]],
  [/dirt|path/, [110, 80, 55]],
  [/ore/, [200, 190, 90]],
  [/(air)$/, [10, 10, 15]],
];
const DEFAULT_COLOR = [90, 90, 100];
const VOID_COLOR = [8, 8, 12];
const UNKNOWN_COLOR = [18, 18, 22];

function colorFor(blockName) {
  for (const [re, col] of PALETTE) if (re.test(blockName)) return col;
  return DEFAULT_COLOR;
}

let sharedServer = null;
let currentBot = null;

module.exports = function visualizerPlugin(opts = {}) {
  const httpPort = opts.httpPort || 8095;
  const outFile = opts.outFile || path.join(__dirname, 'world.png');
  const VIEW_RADIUS = opts.viewRadius || 96;
  // Minimum time between successive re-renders. Lowered from the previous
  // 200ms so the map (and the HUD, which is decoupled from it anyway) feels
  // noticeably snappier without saturating the CPU.
  const RENDER_INTERVAL_MS = opts.renderIntervalMs || 100;
  const TURN_SPEED = opts.turnSpeedRadPerSec || Math.PI * 0.9; // ~162°/s

  return function inject(bot) {
    currentBot = bot;
    // key -> Uint8Array(16*16*3), colors precomputed once at scan time so
    // rendering never has to run a regex per pixel again.
    const columns = new Map();
    let dirty = false;

    function keyOf(cx, cz) { return cx + ',' + cz; }

    // Reusable Vec3 instance to eliminate GC overhead during chunk scans.
    const scanVec = new Vec3(0, 0, 0);

    function scanColumn(chunkX, chunkZ) {
      const column = bot.world.getColumn(chunkX, chunkZ);
      if (!column) return;

      const colors = new Uint8Array(16 * 16 * 3);
      const yMin = (bot.game && bot.game.minY) || 0;
      const yMax = ((bot.game && bot.game.height) || 256) + yMin;

      for (let x = 0; x < 16; x++) {
        for (let z = 0; z < 16; z++) {
          let rgb = null;
          for (let y = yMax - 1; y >= yMin; y--) {
            scanVec.set(x, y, z);
            const block = column.getBlock(scanVec);
            if (block && block.name !== 'air' && block.name !== 'cave_air' && block.name !== 'void_air') {
              rgb = colorFor(block.name);
              break;
            }
          }
          const idx = (x * 16 + z) * 3;
          const c = rgb || VOID_COLOR;
          colors[idx] = c[0]; colors[idx + 1] = c[1]; colors[idx + 2] = c[2];
        }
      }

      columns.set(keyOf(chunkX, chunkZ), colors);
      dirty = true;
    }

    // Writes the RGB for world block coords (bx, bz) into `out` (length-3
    // array). Returns false if that chunk hasn't been scanned yet.
    function colorAt(bx, bz, out) {
      const cx = Math.floor(bx / 16), cz = Math.floor(bz / 16);
      const grid = columns.get(keyOf(cx, cz));
      if (!grid) return false;
      const lx = ((bx % 16) + 16) % 16, lz = ((bz % 16) + 16) % 16;
      const idx = (lx * 16 + lz) * 3;
      out[0] = grid[idx]; out[1] = grid[idx + 1]; out[2] = grid[idx + 2];
      return true;
    }

    let writing = false;
    let renderQueued = false;
    const tmp = [0, 0, 0];

    // The map is rendered player-relative and rotated so the direction the
    // player is facing always points to the top of the image. That means
    // the player marker itself never needs to rotate — it's drawn as a
    // fixed "up" arrow overlay in the browser instead of being baked into
    // the PNG, which is both cheaper and guarantees it's always visible.
    function render() {
      if (!dirty || !bot.entity) return;
      if (writing) { renderQueued = true; return; }
      writing = true;
      dirty = false;

      const centerX = bot.entity.position.x;
      const centerZ = bot.entity.position.z;
      const yaw = bot.entity.yaw || 0;

      // Forward vector (world space) for the direction the player faces.
      const fx = -Math.sin(yaw), fz = -Math.cos(yaw);

      const size = VIEW_RADIUS * 2 + 1;
      const half = VIEW_RADIUS;
      const png = new PNG({ width: size, height: size });

      // For every screen pixel, rotate back into world space to find which
      // block color to sample. This keeps "forward" pointing at the top of
      // the image regardless of which way the player is actually facing.
      for (let iv = 0; iv < size; iv++) {
        const dv = iv - half; // screen-down offset from player
        const rowBase = iv * size;
        for (let iu = 0; iu < size; iu++) {
          const du = iu - half; // screen-right offset from player
          const wx = fz * du - fx * dv;
          const wz = -fx * du - fz * dv;
          const bx = Math.floor(centerX + wx);
          const bz = Math.floor(centerZ + wz);
          const idx = (rowBase + iu) << 2;
          if (colorAt(bx, bz, tmp)) {
            png.data[idx] = tmp[0]; png.data[idx + 1] = tmp[1]; png.data[idx + 2] = tmp[2]; png.data[idx + 3] = 255;
          } else {
            png.data[idx] = UNKNOWN_COLOR[0]; png.data[idx + 1] = UNKNOWN_COLOR[1]; png.data[idx + 2] = UNKNOWN_COLOR[2]; png.data[idx + 3] = 255;
          }
        }
      }

      const tmpFile = outFile + '.tmp';
      png.pack().pipe(fs.createWriteStream(tmpFile)).on('finish', () => {
        fs.rename(tmpFile, outFile, () => {
          writing = false;
          if (renderQueued) { renderQueued = false; dirty = true; render(); }
        });
      });
    }

    let lastRenderAt = 0;
    function requestRender() {
      dirty = true;
      const now = Date.now();
      if (now - lastRenderAt < RENDER_INTERVAL_MS) return;
      lastRenderAt = now;
      render();
    }

    bot.on('chunkColumnLoad', (point) => {
      scanColumn(Math.floor(point.x / 16), Math.floor(point.z / 16));
      requestRender();
    });

    // physicsTick fires ~20/s and covers both movement and turning (since
    // the map rotates with yaw, a turn-in-place needs a re-render too).
    // requestRender() throttles this down to RENDER_INTERVAL_MS internally.
    bot.on('physicsTick', () => requestRender());

    // --- Q/E smooth turning -------------------------------------------------
    let turnDir = 0; // -1 = turning left (Q), 1 = turning right (E), 0 = none
    let lastTurnTickAt = Date.now();
    const turnInterval = setInterval(() => {
      const now = Date.now();
      const dt = (now - lastTurnTickAt) / 1000;
      lastTurnTickAt = now;
      if (turnDir !== 0 && bot.entity) {
        const newYaw = bot.entity.yaw + turnDir * TURN_SPEED * dt;
        try { bot.look(newYaw, bot.entity.pitch, true); } catch (e) {}
      }
    }, 50);
    bot.once('end', () => clearInterval(turnInterval));

    const KEY_TO_CONTROL = { w: 'forward', s: 'back', a: 'left', d: 'right', ' ': 'jump' };
    function handleControl(key, pressed) {
      if (key === 'q' || key === 'e') {
        const dir = key === 'q' ? -1 : 1;
        if (pressed) turnDir = dir;
        else if (turnDir === dir) turnDir = 0;
        return;
      }
      const control = KEY_TO_CONTROL[key];
      if (!control) return;
      try { currentBot.setControlState(control, !!pressed); } catch (e) {}
    }

    bot.on('death', () => {
      for (const c of Object.values(KEY_TO_CONTROL)) bot.setControlState(c, false);
      turnDir = 0;
    });

    bot.once('spawn', () => {
      if (sharedServer) return;
      const server = http.createServer((req, res) => {
        const urlPath = req.url.split('?')[0];

        if (urlPath === '/world.png') {
          if (!fs.existsSync(outFile)) { res.writeHead(404); res.end('no chunks loaded yet'); return; }
          res.writeHead(200, { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' });
          fs.createReadStream(outFile).pipe(res);
          return;
        }

        // Lightweight JSON endpoint so the HUD (coords/facing) can be
        // polled quickly and independently of the (heavier) map image.
        if (urlPath === '/state') {
          const e = currentBot.entity;
          const state = e ? {
            x: +e.position.x.toFixed(2),
            y: +e.position.y.toFixed(2),
            z: +e.position.z.toFixed(2),
            yaw: e.yaw,
            health: currentBot.health != null ? +currentBot.health.toFixed(1) : null,
            food: currentBot.food != null ? currentBot.food : null,
          } : null;
          res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
          res.end(JSON.stringify(state));
          return;
        }

        if (urlPath === '/control' && req.method === 'POST') {
          let body = '';
          req.on('data', (chunk) => { body += chunk; });
          req.on('end', () => {
            try {
              const { key, pressed } = JSON.parse(body);
              handleControl(String(key).toLowerCase(), pressed);
              res.writeHead(200, { 'Content-Type': 'application/json' });
              res.end('{"ok":true}');
            } catch (e) {
              res.writeHead(400, { 'Content-Type': 'application/json' });
              res.end('{"ok":false}');
            }
          });
          return;
        }

        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(`<!doctype html><html><body style="margin:0;background:#111;overflow:hidden">
<div id="wrap" style="position:relative;width:100%;line-height:0">
  <img id="w" src="/world.png" style="image-rendering:pixelated;width:100%;display:block">
  <!-- Fixed "always faces up" player marker. The map rotates underneath
       this instead of the marker rotating on the map, so it never has to
       move and can never fail to render. -->
  <svg id="marker" width="28" height="28" viewBox="0 0 28 28"
       style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);pointer-events:none;filter:drop-shadow(0 0 2px #000)">
    <polygon points="14,2 24,24 14,18 4,24" fill="#e62828" stroke="#fff" stroke-width="1"/>
  </svg>
</div>
<div id="hud" style="position:fixed;top:8px;right:8px;color:#eee;background:rgba(0,0,0,.55);
     padding:6px 10px;font:12px/1.5 monospace;border-radius:6px;white-space:pre;pointer-events:none">--</div>
<div style="position:fixed;bottom:8px;left:8px;color:#888;font:12px monospace">WASD move · SPACE jump · Q/E turn (click page first)</div>
<script>
const img = document.getElementById('w');
const hud = document.getElementById('hud');

// Map image is the heavier fetch — refresh at a moderate rate.
setInterval(() => { img.src = '/world.png?' + Date.now(); }, 250);

// HUD (coords/facing/health) is cheap JSON — refresh quickly so movement
// and jumping are visibly reflected right away even between map frames.
function dirName(yaw) {
  const deg = ((-yaw * 180 / Math.PI) % 360 + 360) % 360;
  const dirs = ['S','SW','W','NW','N','NE','E','SE'];
  return dirs[Math.round(deg / 45) % 8];
}
async function pollState() {
  try {
    const res = await fetch('/state', { cache: 'no-store' });
    const s = await res.json();
    if (s) {
      hud.textContent = 'X ' + s.x + '\\nY ' + s.y + '\\nZ ' + s.z +
        '\\nFacing ' + dirName(s.yaw) +
        (s.health != null ? '\\nHP ' + s.health : '') +
        (s.food != null ? '  Food ' + s.food : '');
    } else {
      hud.textContent = 'not spawned';
    }
  } catch (e) {}
}
setInterval(pollState, 150);
pollState();

const CONTROL_KEYS = ['w', 'a', 's', 'd', ' ', 'q', 'e'];
const held = new Set();
function sendControl(key, pressed) {
  fetch('/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key, pressed }) }).catch(() => {});
}
window.addEventListener('keydown', (e) => {
  const k = e.key.toLowerCase();
  if (!CONTROL_KEYS.includes(k) || held.has(k)) return;
  e.preventDefault(); held.add(k); sendControl(k, true);
});
window.addEventListener('keyup', (e) => {
  const k = e.key.toLowerCase();
  if (!CONTROL_KEYS.includes(k)) return;
  e.preventDefault(); held.delete(k); sendControl(k, false);
});
window.addEventListener('blur', () => {
  for (const k of held) sendControl(k, false);
  held.clear();
});
</script></body></html>`);
      });

      server.listen(httpPort, () => {
        sharedServer = server;
        const url = `http://localhost:${httpPort}`;
        console.log(`[visualizer] live view: ${url}`);
        if (opts.autoOpen !== false) openInBrowser(url);
      });
    });
  };
};
"""

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VIAPROXY_DIR = None
BOT_DIR = None
BIND_HOST = "127.0.0.1"
PREFERRED_BIND_PORT = 25599
GITHUB_API_LATEST = "https://api.github.com/repos/ViaVersion/ViaProxy/releases/latest"

processes = []


def _write_varint(value):
    value &= 0xFFFFFFFF
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        if value != 0:
            out += bytes([byte | 0x80])
        else:
            out += bytes([byte])
            break
    return out


def _read_varint(sock_file):
    value = 0
    position = 0
    while True:
        byte = sock_file.read(1)
        if not byte:
            raise EOFError("Connection closed while reading VarInt")
        byte = byte[0]
        value |= (byte & 0x7F) << position
        if not (byte & 0x80):
            break
        position += 7
        if position >= 32:
            raise ValueError("VarInt too big")
    return value


def _build_handshake(ip, port, protocol_version=-1):
    packet = b"\x00" + _write_varint(protocol_version)
    packet += _write_varint(len(ip.encode())) + ip.encode()
    packet += struct.pack(">H", port) + _write_varint(1)
    return _write_varint(len(packet)) + packet


def _build_status_request():
    return _write_varint(1) + b"\x00"


def _normalize_host_input(raw):
    s = raw.strip()
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", s)
    return s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()


def _is_ip_address(s):
    try:
        import ipaddress
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _dns_read_name(data, offset):
    labels = []
    while True:
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if (length & 0xC0) == 0xC0:
            pointer = struct.unpack(">H", data[offset:offset + 2])[0] & 0x3FFF
            sub, _ = _dns_read_name(data, pointer)
            labels.append(sub)
            offset += 2
            break
        offset += 1
        labels.append(data[offset:offset + length].decode("ascii", errors="ignore"))
        offset += length
    return ".".join(labels), offset


def _dns_skip_name(data, offset):
    while True:
        length = data[offset]
        if length == 0:
            return offset + 1
        if (length & 0xC0) == 0xC0:
            return offset + 2
        offset += 1 + length


def _system_resolvers():
    resolvers = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("nameserver") and len(line.split()) >= 2:
                    resolvers.append(line.split()[1])
    except Exception:
        pass
    resolvers += ["1.1.1.1", "8.8.8.8"]
    return resolvers


def _query_srv_record(name, timeout=2.5):
    import random
    tid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split("."))
    query = header + qname + b"\x00" + struct.pack(">HH", 33, 1)

    for server in _system_resolvers():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(query, (server, 53))
                data, _ = s.recvfrom(1024)

            resp_id, flags, qdcount, ancount, _, _ = struct.unpack(">HHHHHH", data[:12])
            if resp_id != tid or ancount == 0:
                continue
            offset = 12
            for _ in range(qdcount):
                offset = _dns_skip_name(data, offset) + 4

            results = []
            for _ in range(ancount):
                offset = _dns_skip_name(data, offset)
                rtype, _, _, rdlength = struct.unpack(">HHIH", data[offset:offset + 10])
                offset += 10
                if rtype == 33:
                    priority, weight, port = struct.unpack(">HHH", data[offset:offset + 6])
                    target, _ = _dns_read_name(data, offset + 6)
                    results.append((priority, weight, port, target.rstrip(".")))
                offset += rdlength
            if results:
                results.sort(key=lambda r: (r[0], -r[1]))
                return results
        except Exception:
            continue
    return None


def _query_srv_nslookup(hostname):
    """Fallback Windows/Linux system nslookup command parser."""
    try:
        out = subprocess.check_output(
            ["nslookup", "-type=SRV", f"_minecraft._tcp.{hostname}"],
            stderr=subprocess.DEVNULL, timeout=3, text=True
        )
        port_m = re.search(r"port\s*=\s*(\d+)", out, re.I)
        target_m = re.search(r"(?:svr hostname|target)\s*=\s*(\S+)", out, re.I)
        if port_m and target_m:
            return target_m.group(1).rstrip("."), int(port_m.group(1))
    except Exception:
        pass
    return None


def resolve_minecraft_srv(hostname):
    try:
        results = _query_srv_record(f"_minecraft._tcp.{hostname}")
        if results:
            return results[0][3], results[0][2]
    except Exception:
        pass
    return _query_srv_nslookup(hostname)


def query_server_status(ip, port, protocol_version=-1, timeout=5, handshake_host=None):
    hs_host = handshake_host if handshake_host is not None else ip
    sock = socket.create_connection((ip, port), timeout=timeout)
    try:
        sock.sendall(_build_handshake(hs_host, port, protocol_version))
        sock.sendall(_build_status_request())

        sock_file = sock.makefile("rb")
        _total_len = _read_varint(sock_file)
        _packet_id = _read_varint(sock_file)
        json_len = _read_varint(sock_file)

        data = b""
        while len(data) < json_len:
            chunk = sock_file.read(json_len - len(data))
            if not chunk:
                break
            data += chunk

        if not data:
            raise ValueError("No response from server")
        return json.loads(data.decode("utf-8"))
    finally:
        sock.close()


def extract_motd(info):
    desc = info.get("description")
    motd = ""
    if isinstance(desc, str):
        motd = desc
    elif isinstance(desc, dict):
        motd = desc.get("text", "")
        for part in desc.get("extra", []):
            if isinstance(part, dict):
                motd += part.get("text", "")
    return re.sub(r"§[0-9a-fk-or]", "", motd) if motd else None


def print_server_status(info, show_json=False):
    print(f"{CLR_GREEN}✅ MINECRAFT SERVER DETECTED!{CLR_RESET}")
    motd = extract_motd(info)
    print(f"📝 MOTD: {motd or '(could not extract)'}")

    players = info.get("players", {})
    online, max_p = players.get("online"), players.get("max")
    if online is not None and max_p is not None:
        print(f"👤 Players: {CLR_CYAN}{online} / {max_p}{CLR_RESET}")

    sample = players.get("sample") or []
    if sample:
        print(f"   Sample: {', '.join(p.get('name', '?') for p in sample)}")

    version = info.get("version", {})
    if isinstance(version, dict):
        vname, vproto = version.get("name"), version.get("protocol")
        if vname:
            print(f"📦 Version: {vname} (protocol {vproto})")

    if show_json:
        print("\nFull JSON response:")
        print(json.dumps(info, indent=2, ensure_ascii=False))


def _pop_protocol_flag(args):
    protocol = None
    if "--protocol" in args:
        idx = args.index("--protocol")
        try:
            protocol = int(args[idx + 1])
        except (IndexError, ValueError):
            print(f"{CLR_RED}❌ --protocol needs a numeric value{CLR_RESET}")
            sys.exit(1)
        args = args[:idx] + args[idx + 2:]
    return args, protocol


def run_status_only(argv):
    args, protocol = _pop_protocol_flag(argv)
    show_json = "--json" in args
    args = [a for a in args if a != "--json"]

    if len(args) < 1:
        print("Usage: python launch.py --status <IP[:PORT]> [PORT] [--json] [--protocol N]")
        sys.exit(1)

    ip = _normalize_host_input(args[0])
    port, explicit_port = 25565, False

    if ":" in ip:
        ip, port_str = ip.rsplit(":", 1)
        try:
            port = int(port_str)
            explicit_port = True
        except ValueError:
            print(f"{CLR_RED}❌ Invalid port in '{args[0]}'{CLR_RESET}")
            sys.exit(1)
    elif len(args) > 1:
        port = int(args[1])
        explicit_port = True

    handshake_host = ip
    if not explicit_port and not _is_ip_address(ip):
        srv = resolve_minecraft_srv(ip)
        if srv:
            target, srv_port = srv
            print(f"[launcher] SRV record found: {ip} -> {target}:{srv_port}")
            ip, port = target, srv_port

    try:
        info = query_server_status(
            ip, port,
            protocol_version=protocol if protocol is not None else -1,
            handshake_host=handshake_host
        )
    except Exception as e:
        print(f"{CLR_RED}❌ Could not connect / not a valid Minecraft server: {e}{CLR_RESET}")
        sys.exit(1)

    print_server_status(info, show_json=show_json)


def log(msg):
    print(f"[launcher] {msg}", flush=True)


def _detect_bot_dir():
    sub = os.path.join(SCRIPT_DIR, "bot")
    os.makedirs(sub, exist_ok=True)

    index_path = os.path.join(sub, "index.js")
    pkg_path = os.path.join(sub, "package.json")
    viz_path = os.path.join(sub, "visualizer.js")

    def write_if_changed(path, text):
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                if f.read() == text:
                    return False
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return True

    changed = False
    changed |= write_if_changed(index_path, INDEX_JS_SOURCE)
    changed |= write_if_changed(pkg_path, PACKAGE_JSON_SOURCE)
    changed |= write_if_changed(viz_path, VISUALIZER_JS_SOURCE)
    if changed:
        log("Updated bot source files.")
        node_modules = os.path.join(sub, "node_modules")
        if os.path.isdir(node_modules):
            shutil.rmtree(node_modules, ignore_errors=True)

    return sub


def parse_target(argv):
    args = argv[:]
    online = "--online" in args
    args = [a for a in args if a != "--online"]

    if not args:
        print(__doc__)
        sys.exit(1)

    a = _normalize_host_input(args.pop(0))
    explicit_port = False
    if ":" in a:
        idx = a.rindex(":")
        host, port_str = a[:idx], a[idx + 1:]
        try:
            port = int(port_str)
            explicit_port = True
        except ValueError:
            log(f"Invalid port in '{a}'")
            sys.exit(1)
    else:
        host = a
        if args and args[0].isdigit():
            port = int(args.pop(0))
            explicit_port = True
        else:
            port = 25565

    handshake_host = host
    if not explicit_port and not _is_ip_address(host):
        srv = resolve_minecraft_srv(host)
        if srv:
            target, srv_port = srv
            log(f"SRV record found: {host} -> {target}:{srv_port}")
            host, port = target, srv_port

    client_version = args.pop(0) if args else "1.21.11"
    return host, port, client_version, online, handshake_host


def find_free_port(preferred):
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((BIND_HOST, port))
                return port
            except OSError:
                continue
    log("ERROR: Could not find a free local port.")
    sys.exit(1)


def find_java():
    java = shutil.which("java")
    if not java:
        log("ERROR: Java 17+ is required but not found on PATH.")
        sys.exit(1)
    return java


def find_npm_node():
    npm, node = shutil.which("npm"), shutil.which("node")
    if not npm or not node:
        log("ERROR: Node.js/npm is required but not found on PATH.")
        sys.exit(1)
    return npm, node


def find_existing_jar():
    if not os.path.isdir(VIAPROXY_DIR):
        return None
    for name in os.listdir(VIAPROXY_DIR):
        if name.lower().startswith("viaproxy") and name.endswith(".jar") and "java8" not in name.lower():
            return os.path.join(VIAPROXY_DIR, name)
    return None


def _fetch_via_api():
    req = urllib.request.Request(
        GITHUB_API_LATEST,
        headers={"User-Agent": "mc-launcher", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    for a in data.get("assets", []):
        name = a.get("name", "")
        if name.lower().endswith(".jar") and "java8" not in name.lower():
            return name, a["browser_download_url"], a.get("size", 0)
    return None


def _fetch_via_redirect_and_scrape():
    latest_url = "https://github.com/ViaVersion/ViaProxy/releases/latest"
    req = urllib.request.Request(latest_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        final_url = resp.geturl()
        html = resp.read().decode("utf-8", errors="ignore")

    tag_match = re.search(r"/releases/tag/([^\"/?#]+)", final_url)
    if not tag_match:
        return None
    tag = tag_match.group(1)

    jar_links = re.findall(r'href="([^"]+ViaProxy-[^"]+\.jar)"', html)
    jar_links = [l for l in jar_links if "java8" not in l.lower()]
    if jar_links:
        href = jar_links[0]
        if href.startswith("/"):
            href = "https://github.com" + href
        name = href.rsplit("/", 1)[-1]
        return name, href, 0

    version = tag.lstrip("v")
    name = f"ViaProxy-{version}.jar"
    url = f"https://github.com/ViaVersion/ViaProxy/releases/download/{tag}/{name}"
    return name, url, 0


def download_latest_viaproxy():
    existing = find_existing_jar()
    if existing:
        log(f"Using existing ViaProxy jar: {os.path.basename(existing)}")
        return existing

    os.makedirs(VIAPROXY_DIR, exist_ok=True)
    log("Fetching latest ViaProxy release info from GitHub...")

    result = None
    for attempt in range(3):
        try:
            result = _fetch_via_api()
            if result:
                break
        except Exception as e:
            log(f"GitHub API attempt {attempt + 1} failed ({e}), retrying...")
            time.sleep(2)

    if not result:
        log("GitHub API unavailable (likely rate-limited), falling back to page scrape...")
        try:
            result = _fetch_via_redirect_and_scrape()
        except Exception as e:
            log(f"ERROR: Fallback also failed: {e}")
            sys.exit(1)

    if not result:
        log("ERROR: Could not determine the latest ViaProxy download URL.")
        sys.exit(1)

    name, url, size = result
    dest = os.path.join(VIAPROXY_DIR, name)
    size_str = f" ({size / 1_000_000:.1f} MB)" if size else ""
    log(f"Downloading {name}{size_str}...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    log("Download complete.")
    return dest


def start_viaproxy(jar_path, java_bin, target_host, target_port, bind_port, online_mode):
    cmd = [
        java_bin, "-jar", jar_path, "cli",
        "--bind-address", f"{BIND_HOST}:{bind_port}",
        "--target-address", f"{target_host}:{target_port}",
        "--target-version", "Auto Detect (1.7+ servers)",
    ]
    if online_mode:
        cmd += ["--auth-method", "ACCOUNT", "--minecraft-account-index", "0"]

    log("Starting ViaProxy...")
    proc = subprocess.Popen(
        cmd, cwd=VIAPROXY_DIR,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, bufsize=1,
    )
    processes.append(proc)

    ready_pattern = re.compile(r"Binding proxy server to", re.IGNORECASE)
    start_time = time.time()

    while True:
        line = proc.stdout.readline()
        if line:
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line).rstrip()
            print(f"  [viaproxy] {clean}")
            if ready_pattern.search(clean):
                break
        if proc.poll() is not None or (time.time() - start_time > 60):
            log("ERROR: ViaProxy failed to start.")
            terminate_all()
            sys.exit(1)

    import threading
    def pump():
        for line in proc.stdout:
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line).rstrip()
            print(f"  [viaproxy] {clean}")
    threading.Thread(target=pump, daemon=True).start()

    return proc


def has_saved_account():
    """Best-effort check for whether `--setup` has been run and an account
    was actually saved. ViaProxy persists added accounts (including the auth
    token) in saves.json in its working directory. Rather than assume its
    exact schema, look for content that only shows up once a real account
    has been added (a username/token/microsoft entry), since the file may
    exist with baseline config even before any account is added.
    """
    for name in ("saves.json", "accounts.json", "account.json"):
        path = os.path.join(VIAPROXY_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            return True
        lowered = text.lower()
        if any(marker in lowered for marker in ("accesstoken", "access_token", "microsoft", "\"username\"")):
            return True
    return False


def setup_account(jar_path, java_bin):
    """Runs ViaProxy's interactive CLI console so the user can add a Microsoft
    account (device code login) for servers that require online mode. Fully
    interactive: stdin/stdout are inherited so the user can type commands and
    follow the device-code login flow directly.
    """
    log("Starting ViaProxy in interactive mode so you can add a Microsoft account.")
    log("Once it's running, type:  account add")
    log("Follow the printed URL + code to log in with your Microsoft account.")
    log("Then type:  account list   (to confirm it was added, should be index 0)")
    log("Type 'stop' or press Ctrl+C when done.\n")
    cmd = [java_bin, "-jar", jar_path, "cli"]
    proc = subprocess.Popen(cmd, cwd=VIAPROXY_DIR)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    log("Account setup session ended.")


def npm_install_if_needed(npm_bin):
    if os.path.isdir(os.path.join(BOT_DIR, "node_modules")):
        return
    log("Running npm install for bot...")
    if subprocess.run([npm_bin, "install"], cwd=BOT_DIR).returncode != 0:
        log("ERROR: npm install failed.")
        terminate_all()
        sys.exit(1)


def start_bot(node_bin, client_version, bind_port):
    cmd = [node_bin, "index.js", f"{BIND_HOST}:{bind_port}", "ConsoleBot", client_version]
    log("Starting Mineflayer bot...")
    proc = subprocess.Popen(cmd, cwd=BOT_DIR)
    processes.append(proc)
    return proc


def terminate_all():
    log("Shutting down processes...")
    for p in processes:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    time.sleep(1)
    for p in processes:
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


atexit.register(terminate_all)


def main():
    global BOT_DIR, VIAPROXY_DIR

    if "--status" in sys.argv[1:]:
        run_status_only([a for a in sys.argv[1:] if a != "--status"])
        return

    BOT_DIR = _detect_bot_dir()
    VIAPROXY_DIR = os.path.join(BOT_DIR, "viaproxy")

    if "--setup-account" in sys.argv[1:] or "--setup" in sys.argv[1:]:
        java_bin = find_java()
        jar_path = download_latest_viaproxy()
        setup_account(jar_path, java_bin)
        return

    remaining_argv, status_protocol = _pop_protocol_flag(sys.argv[1:])
    host, port, client_version, online_mode, handshake_host = parse_target(remaining_argv)

    if online_mode and not has_saved_account():
        log("No Microsoft account found yet.")
        log("Online-mode servers need a saved account to authenticate as.")
        log("Run this first, then try again:  python launch.py --setup")
        sys.exit(1)

    log(f"Checking server status at {host}:{port}...")
    try:
        info = query_server_status(
            host, port,
            protocol_version=status_protocol if status_protocol is not None else -1,
            handshake_host=handshake_host
        )
        print_server_status(info)
    except Exception as e:
        log(f"{CLR_YELLOW}⚠️ Status ping failed ({e}). Attempting connection anyway...{CLR_RESET}")

    bind_port = find_free_port(PREFERRED_BIND_PORT)
    java_bin = find_java()
    npm_bin, node_bin = find_npm_node()
    jar_path = download_latest_viaproxy()

    viaproxy_proc = start_viaproxy(jar_path, java_bin, host, port, bind_port, online_mode)
    npm_install_if_needed(npm_bin)
    bot_proc = start_bot(node_bin, client_version, bind_port)

    log(f"{CLR_GREEN}Everything is running. Press Ctrl+C to stop.{CLR_RESET}")

    try:
        while True:
            if viaproxy_proc.poll() is not None or bot_proc.poll() is not None:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
