#!/usr/bin/env python
# -*- coding: utf-8 -*-
# tms: terminal client for https://tms.inf.elte.hu  (python 2.7 / 3, stdlib only)
# Shares the token file with tms.sh (~/.tms_token), asks for login only when needed.
from __future__ import print_function
import os, sys, re, json, time, base64, getpass, textwrap
import mimetypes, binascii, datetime, zipfile, subprocess
from collections import OrderedDict

try:
    import readline
except ImportError:
    readline = None

try:                                   # py2
    import urllib2 as ur
    from urlparse import urlparse
    from urllib import unquote
    input_ = raw_input
    string_types = basestring
except ImportError:                    # py3
    import urllib.request as ur
    from urllib.parse import urlparse, unquote
    input_ = input
    string_types = str

BASE = os.environ.get("TMS_BASE", "https://tms.inf.elte.hu/api").rstrip("/")
TOKEN_FILE = os.environ.get("TMS_TOKEN_FILE") or os.path.expanduser("~/.tms_token")
CFG_FILE = os.path.expanduser("~/.tms_client.json")
LOG_FILE = os.path.expanduser("~/.tms_last.log")
DL_DIR = os.path.expanduser(os.environ.get("TMS_DL", "~/tms-downloads"))
UA = "Mozilla/5.0 (X11; Linux x86_64) tms-cli"
WATCH_SECS = 120

CFG = {"user": "", "sem": None}
TOKEN = {"v": ""}
S = {"me": None, "sems": None, "cur": None, "hist": []}

def B(s):
    return s if isinstance(s, bytes) else s.encode("utf-8")

# ------------------------------------------------------------ terminal
COLOR = sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb"

def term_size():
    try:
        import shutil
        s = shutil.get_terminal_size((80, 24))
        return s.columns, s.lines
    except Exception:
        pass
    try:
        r, c = os.popen("stty size 2>/dev/null").read().split()
        return int(c), int(r)
    except Exception:
        pass
    try:
        return int(os.environ.get("COLUMNS", 80)), int(os.environ.get("LINES", 24))
    except Exception:
        return 80, 24

def w(s):
    if not isinstance(s, bytes):
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        s = s.encode(enc, "replace")
    out = getattr(sys.stdout, "buffer", sys.stdout)
    out.write(s)
    out.flush()

def col(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if COLOR else s

WORD_COL = {"SUCCESS": "1;32", "PASS": "1;32", "PASSED": "1;32", "OK": "32", "ACCEPTED": "1;32",
            "VERIFIED": "32", "OPEN": "1;32",
            "FAILED": "1;31", "FAILURE": "1;31", "ERROR": "1;31", "WRONG": "1;31", "CRASH": "1;31",
            "TIMEOUT": "1;31", "REJECTED": "1;31", "CLOSED": "1;31", "MISSING": "31",
            "PART": "1;33", "WARNING": "33", "LATE": "1;33", "NOT SUBMITTED": "33",
            "RUNNING": "1;36", "PENDING": "36", "QUEUED": "36", "WAITING": "36",
            "EVALUATING": "36", "UPCOMING": "36", "SUBMITTED": "36"}
WORD_RE = re.compile(r"\b(NOT SUBMITTED|" + "|".join(sorted(
    [k for k in WORD_COL if " " not in k], key=len, reverse=True)) + r")\b")

def colorize(line):
    if re.match(r"^#{1,3} ", line):
        return col("1;33", line)
    line = re.sub(r"\[(\d+)\]", lambda m: col("1;36", m.group(0)), line)
    return WORD_RE.sub(lambda m: col(WORD_COL[m.group(1)], m.group(1)), line)

def clear_screen():
    if COLOR:
        w("\033[H\033[2J\033[3J")

def erase_prev_line():
    if COLOR:
        w("\033[1A\033[2K\r")

def wrap(text, width):
    out = []
    for ln in text.split("\n"):
        if len(ln) <= width:
            out.append(ln)
            continue
        s = ln.lstrip()
        sub = " " * (len(ln) - len(s) + (2 if s.startswith(("* ", "- ")) else 0))
        out.extend(textwrap.wrap(ln, width, subsequent_indent=sub) or [""])
    return out

def fit(s, width):
    s = "%s" % s
    return s if len(s) <= width else s[:max(0, width - 3)] + "..."

def paged(lines, rows, color=True):
    f = colorize if color else (lambda s: s)
    step = max(5, rows - 2)
    n, i = len(lines), 0
    while i < n:
        w("\n".join(f(l) for l in lines[i:i + step]) + "\n")
        i += step
        if i < n and sys.stdout.isatty():
            try:
                a = input_("-- %d%%  Enter: more | a: all | q: stop -- " % (100 * i // n)).strip().lower()
            except (EOFError, KeyboardInterrupt):
                w("\n")
                break
            erase_prev_line()
            if a == "q":
                break
            if a == "a":
                w("\n".join(f(l) for l in lines[i:]) + "\n")
                break

def hsize(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0

def yes(prompt):
    try:
        return input_(prompt).strip().lower() in ("y", "yes", "i", "igen")
    except (EOFError, KeyboardInterrupt):
        return False

def ask_path(prompt):
    if not readline:
        return input_(prompt).strip()
    import glob
    def comp(text, i):
        m = [x + ("/" if os.path.isdir(x) else "")
             for x in glob.glob(os.path.expanduser(text) + "*")]
        return m[i] if i < len(m) else None
    old, olddel = readline.get_completer(), readline.get_completer_delims()
    readline.set_completer(comp)
    readline.set_completer_delims(" \t\n")
    readline.parse_and_bind("tab: complete")
    try:
        return input_(prompt).strip()
    finally:
        readline.set_completer(old)
        readline.set_completer_delims(olddel)

# ------------------------------------------------------------ dates (Europe/Budapest without tz database)
def utcnow():
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=time.time())

def bud_offset(utc):
    y = utc.year
    def last_sun(month):
        d = datetime.datetime(y, month, 31, 1)
        return d - datetime.timedelta(days=(d.weekday() + 1) % 7)
    return 2 if last_sun(3) <= utc < last_sun(10) else 1

def parse_dt(s):
    """returns (utc, local_budapest) naive datetimes, or None"""
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?$",
                 "%s" % (s or ""))
    if not m:
        return None
    y, mo, d, h, mi = [int(x) for x in m.groups()[:5]]
    se = int(m.group(6) or 0)
    dt = datetime.datetime(y, mo, d, h, mi, se)
    tz = m.group(7)
    if tz is None:                                   # naive: assume it is already Budapest time
        utc = dt - datetime.timedelta(hours=bud_offset(dt - datetime.timedelta(hours=1)))
        return utc, dt
    off = 0
    if tz != "Z":
        t = tz.replace(":", "")
        off = (1 if t[0] == "+" else -1) * (int(t[1:3]) * 60 + int(t[3:5]))
    utc = dt - datetime.timedelta(minutes=off)
    return utc, utc + datetime.timedelta(hours=bud_offset(utc))

def fmt_dt(s, short=False):
    p = parse_dt(s)
    if not p:
        return "%s" % (s or "-")
    return p[1].strftime("%m-%d %H:%M" if short else "%Y-%m-%d %H:%M")

def rel(s):
    p = parse_dt(s)
    if not p:
        return ""
    secs = int((p[0] - utcnow()).total_seconds())
    a = abs(secs)
    if a < 3600:
        t = "%dm" % (a // 60)
    elif a < 86400:
        t = "%dh %dm" % (a // 3600, a % 3600 // 60)
    else:
        t = "%dd %dh" % (a // 86400, a % 86400 // 3600)
    return ("in " + t) if secs >= 0 else (t + " ago")

# ------------------------------------------------------------ config / token / login
def load_cfg():
    try:
        with open(CFG_FILE) as f:
            CFG.update(json.load(f))
    except Exception:
        pass

def save_cfg():
    try:
        fd = os.open(CFG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(CFG, f)
    except Exception:
        pass

def jwt_exp(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p.encode()).decode()).get("exp")
    except Exception:
        return None

def save_token(t):
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t)

class ApiError(Exception):
    pass

def err_text(code, text):
    try:
        o = json.loads(text)
        if isinstance(o, dict):
            for k in ("message", "error", "detail", "name"):
                if isinstance(o.get(k), string_types) and o[k]:
                    return "HTTP %s: %s" % (code, o[k])
    except ValueError:
        pass
    return "HTTP %s: %s" % (code, text.strip()[:300])

def login():
    """ask for credentials (like a login form) and cache the token. returns token."""
    u, p = os.environ.get("TMS_USER"), os.environ.get("TMS_PASS")
    for attempt in range(3):
        if not (u and p):
            if not sys.stdin.isatty():
                raise ApiError("no TMS_USER/TMS_PASS set and no terminal to ask on")
            d = CFG.get("user", "")
            u = input_("TMS username (Neptun)%s: " % (" [%s]" % d if d else "")).strip() or d
            p = getpass.getpass("TMS password: ")
        if not u or not p:
            print("both are required")
            u = p = None
            continue
        req = ur.Request(BASE + "/common/auth/ldap-login", data=B(json.dumps({"username": u, "password": p})))
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", UA)
        try:
            r = ur.urlopen(req, timeout=30)
        except ur.HTTPError as e:
            r = e
        except Exception as e:
            raise ApiError("login request failed: %s" % e)
        text = r.read().decode("utf-8", "replace")
        tok = ""
        try:
            tok = json.loads(text).get("accessToken") or ""
        except Exception:
            m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', text)
            tok = m.group(1) if m else ""
        if tok:
            TOKEN["v"] = tok
            save_token(tok)
            CFG["user"] = u
            save_cfg()
            print("login ok")
            return tok
        print("login failed: %s" % err_text(getattr(r, "code", 0), text))
        if os.environ.get("TMS_USER"):
            break
        u = p = None
    raise ApiError("not logged in")

def ensure_token():
    if not TOKEN["v"]:
        try:
            with open(TOKEN_FILE) as f:
                TOKEN["v"] = f.read().strip()
        except Exception:
            TOKEN["v"] = ""
    exp = jwt_exp(TOKEN["v"]) if TOKEN["v"] else None
    if not TOKEN["v"] or (exp and exp < time.time() + 20):
        login()
    return TOKEN["v"]

# ------------------------------------------------------------ api
def log(s):
    try:
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as f:
            f.write(B("%s %s\n" % (time.strftime("%H:%M:%S"), s)))
    except Exception:
        pass

def api_request(method, path, body=None, ctype=None):
    """returns (code, open_response); one automatic re-login on 401"""
    for attempt in (0, 1):
        tok = ensure_token()
        req = ur.Request(BASE + path)
        req.get_method = lambda m=method: m
        req.add_header("Authorization", "Bearer " + tok)
        req.add_header("Accept", "application/json, */*")
        req.add_header("User-Agent", UA)
        if body is not None:
            req.data = B(body)
            req.add_header("Content-Type", ctype or "application/json")
        elif method in ("POST", "PUT", "PATCH"):
            req.data = b""
            req.add_header("Content-Length", "0")
        try:
            r = ur.urlopen(req, timeout=60)
        except ur.HTTPError as e:
            r = e
        except Exception as e:
            raise ApiError("network error: %s" % e)
        code = getattr(r, "code", 200)
        if code == 401 and attempt == 0:
            print("(token expired, logging in again)")
            try:
                r.close()
            except Exception:
                pass
            TOKEN["v"] = ""
            try:
                os.remove(TOKEN_FILE)
            except OSError:
                pass
            continue
        return code, r
    raise ApiError("not authorised")

def api(method, path, body=None, ctype=None):
    """returns parsed json (OrderedDict/list/str/None). raises ApiError on http errors."""
    code, r = api_request(method, path, body, ctype)
    text = r.read().decode("utf-8", "replace")
    log("%s %s -> %s %s" % (method, path, code, text[:4000].replace("\n", "\\n")))
    if code >= 400:
        raise ApiError(err_text(code, text))
    if not text.strip():
        return None
    try:
        return json.loads(text, object_pairs_hook=OrderedDict)
    except ValueError:
        return text

# ------------------------------------------------------------ downloads
def hdr_get(r, name, default=""):
    try:
        return r.info().get(name, default) or default
    except Exception:
        return default

def dl_filename(r, hint):
    cd = hdr_get(r, "Content-Disposition")
    ct = hdr_get(r, "Content-Type").split(";")[0].strip().lower()
    name = ""
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.I)
    if m:
        name = unquote(m.group(1).strip().strip('"'))
    if not name:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        if m:
            name = m.group(1).strip()
    if not name:
        name = hint or "download"
    try:
        name = name.encode("latin-1").decode("utf-8")
    except Exception:
        pass
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .") or "download"
    if "." not in name:
        name += mimetypes.guess_extension(ct) or ""
    return name

def unique_path(d, name):
    base, ext = os.path.splitext(name)
    p, i = os.path.join(d, name), 1
    while os.path.exists(p):
        p = os.path.join(d, "%s (%d)%s" % (base, i, ext))
        i += 1
    return p

def sniff(path):
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        data = f.read(65536)
    if data.startswith(b"%PDF"):
        return "PDF document"
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "Office document" if ext in (".docx", ".xlsx", ".pptx", ".odt") else "ZIP archive"
    if data[:2] == b"\x1f\x8b":
        return "gzip archive"
    if data and b"\x00" not in data:
        try:
            data.decode("utf-8")
            return "text file"
        except UnicodeDecodeError:
            return "text file (not UTF-8)"
    return "text file" if not data else "binary file"

def which(cmd):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, cmd)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None

def progress(got, total, t0, width):
    el = max(time.time() - t0, 0.001)
    speed = "%s/s" % hsize(got / el)
    if total:
        rest = " %3d%%  %s / %s  %s" % (got * 100.0 / total, hsize(got), hsize(total), speed)
        bw = max(10, width - len(rest) - 4)
        k = int(bw * got / float(total))
        bar = "[" + "#" * k + "-" * (bw - k) + "]"
    else:
        rest, bar = "  %s  %s" % (hsize(got), speed), ""
    w("\r\033[2K " + (bar + rest)[:width - 1])

def do_download(r, hint=""):
    name = dl_filename(r, hint)
    try:
        os.makedirs(DL_DIR)
    except OSError:
        pass
    dest = unique_path(DL_DIR, name)
    cl = hdr_get(r, "Content-Length")
    total = int(cl) if cl.isdigit() else 0
    width = max(30, min(term_size()[0] - 1, 100))
    tty = sys.stdout.isatty()
    w("\n Downloading %s ...\n" % name)
    part, got, t0, last = dest + ".part", 0, time.time(), 0
    try:
        with open(part, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if tty and time.time() - last > 0.1:
                    last = time.time()
                    progress(got, total, t0, width)
        os.rename(part, dest)
    except KeyboardInterrupt:
        try:
            os.remove(part)
        except OSError:
            pass
        w("\n download cancelled\n")
        return
    if tty:
        progress(got, total or got, t0, width)
        w("\n")
    kind = sniff(dest)
    cols = term_size()[0]
    rule = "-" * max(20, cols - 1)
    lines = [rule, " Downloaded  " + name, " Saved to    " + dest,
             " Size        %s   (%s, %.1fs)" % (hsize(got), kind, time.time() - t0), rule]
    opts = []
    if kind == "ZIP archive":
        try:
            infos = zipfile.ZipFile(dest).infolist()
            lines.append(" Contents (%d entries):" % len(infos))
            for i in infos[:40]:
                lines.append("   %9s  %s" % (hsize(i.file_size) if not i.filename.endswith("/") else "", i.filename))
            if len(infos) > 40:
                lines.append("   ... and %d more" % (len(infos) - 40))
            lines.append(rule)
            opts.append(("x", "extract"))
        except Exception as e:
            lines.append(" (cannot read archive: %s)" % e)
    elif kind == "PDF document":
        if which("pdftotext"):
            opts.append(("v", "view as text"))
        else:
            lines += [" no 'pdftotext' on this server: copy the file to your computer to read it,",
                      " e.g.  scp <this-server>:%s ." % dest.replace(" ", "\\ "), rule]
    elif kind.startswith("text"):
        opts.append(("v", "view"))
    for l in lines:
        w(colorize(fit(l, cols - 1)) + "\n")
    if opts and sys.stdout.isatty():
        try:
            a = input_(" [%s]  Enter = continue: " % "  ".join("%s = %s" % o for o in opts)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if a == "x" and ("x", "extract") in opts:
            out = os.path.splitext(dest)[0]
            try:
                zipfile.ZipFile(dest).extractall(out)
                print(" extracted to " + out)
            except Exception as e:
                print(" extract failed:", e)
        elif a == "v" and ("v", "view") in opts:
            with open(dest, "rb") as f:
                txt = f.read().decode("utf-8", "replace").replace("\t", "    ")
            clear_screen()
            paged(wrap(txt, max(20, cols - 1)), term_size()[1], color=False)
        elif a == "v":
            try:
                txt = subprocess.check_output(["pdftotext", "-layout", dest, "-"]).decode("utf-8", "replace")
                clear_screen()
                paged(txt.split("\n"), term_size()[1], color=False)
            except Exception as e:
                print(" pdftotext failed:", e)

def download(path, hint=""):
    code, r = api_request("GET", path)
    if code >= 400:
        raise ApiError(err_text(code, r.read().decode("utf-8", "replace")))
    ct = hdr_get(r, "Content-Type").split(";")[0].strip().lower()
    if ct == "application/json" and "attachment" not in hdr_get(r, "Content-Disposition").lower():
        text = r.read().decode("utf-8", "replace")
        raise ApiError("server returned JSON instead of a file: %s" % fit(text, 200))
    do_download(r, hint)

def multipart(items):
    bd = "----tms" + binascii.hexlify(os.urandom(12)).decode()
    out = []
    for it in items:
        if it[0] == "field":
            out.append(B('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n' % (bd, it[1])))
            out.append(B(it[2]))
        else:
            fn = it[2].replace('"', "%22")
            out.append(B('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                         'Content-Type: %s\r\n\r\n' % (bd, it[1], fn, it[3])))
            out.append(it[4])
        out.append(b"\r\n")
    out.append(B("--%s--\r\n" % bd))
    return b"".join(out), "multipart/form-data; boundary=" + bd

# ------------------------------------------------------------ generic json rendering
PRIO = ["id", "name", "title", "status", "translatedStatus", "grade", "points", "maxPoints", "score",
        "taskID", "uploadTime", "date", "startDate", "endDate", "startTime", "type", "text", "message"]

def is_scalar(v):
    return not isinstance(v, (dict, list))

def fmtval(v):
    if v is None:
        return "-"
    if v is True:
        return "yes"
    if v is False:
        return "no"
    if isinstance(v, string_types):
        if re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d", v):
            return fmt_dt(v)
        return v.replace("\r", "").replace("\n", " ")
    return "%s" % v

def table_lines(rows, width, indent=0):
    keys, count = [], {}
    for r in rows:
        if isinstance(r, dict):
            for k, v in r.items():
                if is_scalar(v):
                    count[k] = count.get(k, 0) + 1
                    if k not in keys:
                        keys.append(k)
    keys = [k for k in keys if count[k] >= max(1, len(rows) // 2)]
    keys.sort(key=lambda k: PRIO.index(k) if k in PRIO else 99)
    if not keys:
        return None
    cells = [[fmtval(r.get(k)) if isinstance(r, dict) else "" for k in keys] for r in rows]
    wd = [min(32, max([len(k)] + [len(c[i]) for c in cells])) for i, k in enumerate(keys)]
    avail = width - indent - 6
    while len(keys) > 1 and sum(wd) + 2 * (len(keys) - 1) > avail:
        keys.pop()
        wd.pop()
        cells = [c[:len(keys)] for c in cells]
    pad = " " * indent
    out = [pad + " #   " + "  ".join("%-*s" % (wd[i], k) for i, k in enumerate(keys))]
    for n, c in enumerate(cells, 1):
        out.append(pad + "%3d  " % n + "  ".join("%-*s" % (wd[i], fit(x, wd[i])) for i, x in enumerate(c)))
    return out

def json_lines(obj, width, indent=0):
    pad = " " * indent
    out = []
    if isinstance(obj, dict):
        if not obj:
            return [pad + "(empty)"]
        kw = min(26, max(len(k) for k in obj))
        for k, v in obj.items():
            if is_scalar(v):
                text = fmtval(v)
                first = True
                for part in textwrap.wrap(text, max(20, width - indent - kw - 3)) or [""]:
                    out.append(pad + ("%-*s  %s" % (kw, k, part) if first else " " * (kw + 2 + indent) + part))
                    first = False
            elif isinstance(v, list) and not v:
                out.append(pad + "%-*s  -" % (kw, k))
            elif isinstance(v, list) and all(is_scalar(x) for x in v):
                out.append(pad + "%-*s  %s" % (kw, k, fit(", ".join(fmtval(x) for x in v), width - indent - kw - 3)))
            else:
                out.append(pad + "%s:" % k)
                out.extend(json_lines(v, width, indent + 2))
    elif isinstance(obj, list):
        if not obj:
            return [pad + "(empty list)"]
        if all(is_scalar(x) for x in obj):
            return [pad + "* " + fmtval(x) for x in obj]
        t = table_lines(obj, width, indent) if all(isinstance(x, dict) for x in obj) else None
        if t:
            return t
        for n, x in enumerate(obj, 1):
            out.append(pad + "--- item %d" % n)
            out.extend(json_lines(x, width, indent + 2))
    else:
        out.append(pad + fmtval(obj))
    return out

# ------------------------------------------------------------ markdown-ish -> text
ENT = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}

def md_to_text(s):
    if not s:
        return ""
    s = s.replace("\r", "")
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|h\d|tr)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    for k, v in ENT.items():
        s = s.replace(k, v)
    out, fence = [], False
    for ln in s.split("\n"):
        if ln.strip().startswith("```"):
            fence = not fence
            continue
        if fence:
            out.append("    " + ln)
            continue
        ln = re.sub(r"!\[([^\]]*)\]\([^)]*\)", lambda m: "[image: %s]" % m.group(1) if m.group(1) else "[image]", ln)
        ln = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: "%s (%s)" % (m.group(1), m.group(2)), ln)
        ln = re.sub(r"\*\*(.+?)\*\*", r"\1", ln)
        ln = re.sub(r"__(.+?)__", r"\1", ln)
        ln = re.sub(r"`([^`]+)`", r"\1", ln)
        out.append(ln.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()

# ------------------------------------------------------------ screens
class Screen(object):
    def __init__(self, title, kind="json"):
        self.title, self.kind = title, kind
        self.lines, self.items, self.data, self.ctx = [], [], None, {}
    def add(self, *ls):
        self.lines.extend(ls)
    def item(self, *spec):
        self.items.append(spec)
        return "[%d]" % len(self.items)

def need_me():
    if S["me"] is None:
        try:
            S["me"] = api("GET", "/common/user-settings") or {}
        except ApiError:
            S["me"] = {}
    return S["me"]

def semesters():
    if S["sems"] is None:
        S["sems"] = api("GET", "/common/semesters") or []
    return S["sems"]

def cur_sem():
    sems = semesters()
    ids = [s.get("id") for s in sems]
    if CFG.get("sem") in ids:
        return [s for s in sems if s.get("id") == CFG["sem"]][0]
    for s in sems:
        if s.get("actual"):
            return s
    return sems[-1] if sems else None

def sem_label(s):
    return "%s%s" % (s.get("name", "?"), "  (active)" if s.get("actual") else "") if s else "-"

def task_state(t):
    now = utcnow()
    av, soft, hard = parse_dt(t.get("available")), parse_dt(t.get("softDeadline")), parse_dt(t.get("hardDeadline"))
    if av and now < av[0]:
        return "UPCOMING", "from " + fmt_dt(t.get("available"), True)
    if hard and now > hard[0]:
        return "CLOSED", "closed " + fmt_dt(t.get("hardDeadline"), True)
    due = t.get("hardDeadline") or t.get("softDeadline")
    txt = ("due " + fmt_dt(due, True) + " " + rel(due)) if due else "no deadline"
    if soft and now > soft[0]:
        return "LATE", txt
    return "OPEN", txt

def sub_status(sub):
    if not sub:
        return "NOT SUBMITTED"
    return ("%s" % (sub.get("translatedStatus") or sub.get("status") or "?")).upper()

def grade_text(sub):
    g = sub.get("grade") if sub else None
    return "" if g is None else "grade %s" % g

def scr_home():
    me = need_me()
    sem = cur_sem()
    s = Screen("Home", "home")
    s.add("## %s" % sem_label(sem) + "      (sem = change semester)")
    if not sem:
        s.add("no semesters found")
        return s
    groups = api("GET", "/student/groups?semesterID=%s" % sem["id"]) or []
    s.data = groups
    if not groups:
        s.add("", "no groups in this semester")
    for g in groups:
        c = g.get("course") or {}
        when = " ".join("%s" % x for x in (g.get("day"), g.get("startTime")) if x)
        room = g.get("roomNumber") or ""
        ins = ", ".join(g.get("instructorNames") or [])
        n = s.item("screen", scr_group, g.get("id"))
        s.add("", "%s %s  -  group %s" % (n, c.get("name", "?"), g.get("number", "?")))
        meta = "   ".join(x for x in (when, ("room " + room) if room else "", ("tutor: " + ins) if ins else "",
                                      "EXAM GROUP" if g.get("isExamGroup") else "") if x)
        if meta:
            s.add("      " + meta)
    return s

def scr_semesters():
    s = Screen("Semesters", "list")
    s.add("## Semesters", "")
    for x in semesters():
        n = s.item("call", set_sem, x.get("id"))
        s.add(" %s %-12s %s .. %s%s" % (n, x.get("name"), x.get("startDate"), x.get("endDate"),
                                       "   active" if x.get("actual") else ""))
    return s

def set_sem(sid):
    CFG["sem"] = sid
    save_cfg()
    return scr_home, ()

def scr_group(gid):
    g = api("GET", "/student/groups/%s?expand=course,notes" % gid) or {}
    tasks = api("GET", "/student/tasks?groupID=%s&expand=submission" % gid) or []
    c = g.get("course") or {}
    s = Screen("Group", "group")
    s.data = {"group": g, "tasks": tasks}
    s.ctx["gid"] = gid
    s.add("## %s - group %s" % (c.get("name", "?"), g.get("number", "?")))
    info = []
    if g.get("instructorNames"):
        info.append("Tutors:    " + ", ".join(g["instructorNames"]))
    if c.get("lecturerNames"):
        info.append("Lecturers: " + ", ".join(c["lecturerNames"]))
    when = " ".join("%s" % x for x in (g.get("day"), g.get("startTime"), g.get("roomNumber")) if x)
    if when:
        info.append("Time/room:  " + when)
    s.add(*info)
    notes = g.get("notes")
    if notes:
        s.add("", "Notes:")
        s.add(*(["  " + l for l in (md_to_text(notes).split("\n") if isinstance(notes, string_types)
                                    else json_lines(notes, 78))]))
    flat = []
    for grp in tasks:
        flat.extend(grp if isinstance(grp, list) else [grp])
    namew = min(40, max([len(t.get("name", "")) for t in flat] + [10]))
    cat = None
    for t in flat:
        c2 = t.get("translatedCategory") or t.get("category") or ""
        if c2 != cat:
            cat = c2
            s.add("", "### %s" % (c2 or "Tasks"))
        st, when = task_state(t)
        sub = t.get("submission")
        n = s.item("screen", scr_task, t.get("id"))
        sst = sub_status(sub)
        s.add(" %s %-*s  %-8s %-26s  %s %s" % (n, namew, fit(t.get("name", "?"), namew), st, when,
                                              sst, grade_text(sub)))
    if not flat:
        s.add("", "no tasks")
    return s

def sub_lines(sub, width):
    out = []
    rows = [("Status", sub_status(sub)), ("Grade", fmtval(sub.get("grade"))),
            ("File", fmtval(sub.get("name"))), ("Uploaded", fmt_dt(sub.get("uploadTime"))),
            ("Graded", fmt_dt(sub.get("lastGradedAt")) if sub.get("lastGradedAt") else "-"),
            ("Grader", fmtval(sub.get("graderName"))), ("Verified", fmtval(sub.get("verified")))]
    for k, v in rows:
        out.append("  %-9s %s" % (k, v))
    if sub.get("notes"):
        out.append("  Notes")
        out.extend("    " + l for l in wrap(md_to_text(sub["notes"]), width - 4))
    if sub.get("errorMsg"):
        out.append("  ERROR")
        out.extend("    " + l for l in wrap("%s" % sub["errorMsg"], width - 4))
    return out

def scr_task(tid):
    t = api("GET", "/student/tasks/%s?expand=submission,taskFiles,submission.codeCheckerResult,"
                   "submission.codeCheckerResult.codeCheckerReports" % tid) or {}
    width = max(20, term_size()[0] - 1)
    s = Screen("Task", "task")
    s.data = t
    s.ctx.update({"task": t, "tid": tid})
    st, when = task_state(t)
    sub = t.get("submission")
    s.add("## %s" % t.get("name", "?"))
    s.add("%s   %s   %s" % (st, when, ("by " + t["creatorName"]) if t.get("creatorName") else ""))
    facts = []
    if t.get("available"):
        facts.append("available %s" % fmt_dt(t["available"]))
    if t.get("softDeadline"):
        facts.append("soft deadline %s" % fmt_dt(t["softDeadline"]))
    if t.get("hardDeadline"):
        facts.append("hard deadline %s (%s)" % (fmt_dt(t["hardDeadline"]), rel(t["hardDeadline"])))
    s.add("   ".join(facts))
    flags = []
    if t.get("autoTest"):
        flags.append("auto-test")
    if t.get("staticCodeAnalysis"):
        flags.append("static analysis")
    if t.get("entryPasswordProtected"):
        flags.append("entry password")
    if t.get("appType"):
        flags.append("type %s" % t["appType"])
    if t.get("submissionLimit"):
        flags.append("limit %s submissions" % t["submissionLimit"])
    if flags:
        s.add("Info: " + ", ".join(flags))
    s.add("", "### My submission")
    if sub:
        s.ctx["subid"] = sub.get("id")
        s.add(*sub_lines(sub, width))
        cc = sub.get("codeCheckerResult")
        if cc:
            reps = cc.get("codeCheckerReports") if isinstance(cc, dict) else None
            s.add("  Code checker: %s" % ("%d report(s)" % len(reps) if isinstance(reps, list) else "result present"))
            for r in (reps or [])[:8]:
                s.add("    * " + fit("  ".join("%s=%s" % (k, fmtval(v)) for k, v in r.items() if is_scalar(v)), width - 6))
        n1 = s.item("dl", "/student/submissions/%s/download" % sub.get("id"), "submission-%s-%s" % (sub.get("id"), sub.get("name") or "file"))
        n2 = s.item("dl", "/student/submissions/%s/download-report" % sub.get("id"), "report-%s" % sub.get("id"))
        n3 = s.item("screen", scr_json, "Auto-tester results #%s" % sub.get("id"),
                    "/student/submissions/%s/auto-tester-results" % sub.get("id"), None)
        s.add("  %s download my submission   %s download report   %s auto-tester results" % (n1, n2, n3))
    else:
        s.add("  NOT SUBMITTED yet   (up <file> to upload)")
    files = t.get("taskFiles") or []
    if files:
        s.add("", "### Task files")
        for f in files:
            if not isinstance(f, dict):
                continue
            fname = f.get("name") or f.get("fileName") or f.get("filename") or "file-%s" % f.get("id")
            n = s.item("dl", "/student/task-files/%s/download" % f.get("id"), fname)
            size = f.get("size") or f.get("fileSize")
            s.add("  %s %s%s" % (n, fname, ("   (%s)" % hsize(size)) if isinstance(size, (int, float)) else ""))
    if t.get("description"):
        s.add("", "### Description")
        s.add(*wrap(md_to_text(t["description"]), width))
    return s

def scr_subs():
    subs = api("GET", "/student/submissions") or []
    s = Screen("My submissions", "list")
    s.data = subs
    s.add("## My submissions (%d)" % len(subs), "")
    for x in subs:
        n = s.item("screen", scr_sub, x.get("id"))
        s.add(" %s #%s  %-28s %s  %s %s" % (n, x.get("id"), fit(x.get("name") or x.get("taskName") or "", 28),
                                          fmt_dt(x.get("uploadTime")), sub_status(x), grade_text(x)))
    return s

def scr_sub(subid):
    subs = api("GET", "/student/submissions") or []
    x = [y for y in subs if y.get("id") == subid]
    if not x:
        raise ApiError("submission %s not found" % subid)
    x = x[0]
    s = Screen("Submission", "sub")
    s.data = x
    s.ctx["subid"] = subid
    s.add("## Submission #%s" % subid)
    s.add(*sub_lines(x, max(20, term_size()[0] - 1)))
    n1 = s.item("dl", "/student/submissions/%s/download" % subid, "submission-%s-%s" % (subid, x.get("name") or "file"))
    n2 = s.item("dl", "/student/submissions/%s/download-report" % subid, "report-%s" % subid)
    n3 = s.item("screen", scr_json, "Auto-tester results #%s" % subid,
                "/student/submissions/%s/auto-tester-results" % subid, None)
    if x.get("taskID") is not None:
        n4 = s.item("screen", scr_task, x.get("taskID"))
        s.add("", "  %s download submission   %s download report   %s auto-tester results   %s open task" % (n1, n2, n3, n4))
    else:
        s.add("", "  %s download submission   %s download report   %s auto-tester results" % (n1, n2, n3))
    return s

def scr_json(title, path, drill=None):
    data = api("GET", path)
    return scr_static(title, data, drill)

def scr_static(title, data, drill=None):
    s = Screen(title, "json")
    s.data = data
    s.add("## " + title, "")
    width = max(20, term_size()[0] - 1)
    s.add(*json_lines(data, width))
    if drill and isinstance(data, list):
        s.add("", " open one:  type the row number")
        for x in data:
            if isinstance(x, dict) and x.get("id") is not None:
                s.item("screen", scr_json, "%s #%s" % (title, x["id"]), drill.format(id=x["id"]), None)
            else:
                s.item("noop")
    return s

# ------------------------------------------------------------ upload + auto-test watch
def cmd_upload(cur, arg):
    t = cur.ctx.get("task")
    if not t:
        print(" open a task first")
        return
    path = arg.strip() or ask_path(" File to upload (Tab completes): ")
    path = os.path.expanduser(path.strip().strip("'\""))
    if not path or not os.path.isfile(path):
        print(" not a file: %s" % path)
        return
    size = os.path.getsize(path)
    st, when = task_state(t)
    cols = term_size()[0]
    rule = "-" * max(20, cols - 1)
    print("\n" + rule)
    print(" Upload  %s  (%s)" % (os.path.basename(path), hsize(size)))
    print(" Task    %s" % t.get("name"))
    print(" State   %s   %s" % (st, when))
    if st in ("CLOSED", "UPCOMING"):
        print(" WARNING the task is %s; the server will probably reject this" % st)
    if st == "LATE":
        print(" WARNING the soft deadline has passed")
    if t.get("submission"):
        print(" NOTE    you already have a submission (%s); this uploads another one" % sub_status(t["submission"]))
    if t.get("submissionLimit"):
        print(" NOTE    submission limit: %s" % t["submissionLimit"])
    print(rule)
    if not yes(" Upload now? [y/N] "):
        print(" cancelled")
        return
    with open(path, "rb") as f:
        content = f.read()
    fname = os.path.basename(path)
    body, ctype = multipart([("field", "taskID", "%s" % t.get("id")),
                             ("file", "file", fname, mimetypes.guess_type(fname)[0] or "application/octet-stream", content)])
    try:
        sub = api("POST", "/student/submissions/upload", body, ctype)
    except ApiError as e:
        print(" upload failed:", e)
        return
    print(" uploaded.")
    width = max(20, cols - 1)
    if isinstance(sub, dict):
        print("\n".join(colorize(l) for l in ["", " Result"] + sub_lines(sub, width)))
        subid = sub.get("id")
    else:
        subid = None
        print(" (unexpected response: %s)" % fit("%s" % sub, 200))
    if subid is not None and (t.get("autoTest") or t.get("staticCodeAnalysis")):
        watch(t, subid)
    print(" raw responses are logged in %s" % LOG_FILE)

def watch(t, subid):
    cols = term_size()[0]
    width = max(20, cols - 1)
    rule = "-" * width
    w("\n" + rule + "\n" + colorize(fit(" LIVE  waiting for auto-test results of #%s   (Ctrl-C stops watching)" % subid, width)) + "\n" + rule + "\n")
    t0, last, res = time.time(), None, None
    try:
        while time.time() - t0 < WATCH_SECS:
            msg = ""
            try:
                code, r = api_request("GET", "/student/submissions/%s/auto-tester-results" % subid)
                text = r.read().decode("utf-8", "replace")
                log("auto-tester %s -> %s %s" % (subid, code, text[:4000].replace("\n", "\\n")))
                if code == 200 and text.strip() not in ("", "null", "[]", "{}"):
                    res = json.loads(text, object_pairs_hook=OrderedDict)
                    break
                msg = "no results yet (HTTP %s)" % code
            except ApiError as e:
                msg = "%s" % e
            except ValueError:
                msg = "unreadable response"
            try:
                tt = api("GET", "/student/tasks/%s?expand=submission" % t.get("id"))
                sub = tt.get("submission") or {}
                msg += "   status %s" % sub_status(sub)
                if sub.get("errorMsg"):
                    msg += "   ERROR %s" % fit(sub["errorMsg"], 60)
            except ApiError:
                pass
            if msg != last:
                last = msg
                w(colorize("%7s  %s" % ("+%.0fs" % (time.time() - t0), msg)) + "\n")
            time.sleep(3)
    except KeyboardInterrupt:
        w("\n (stopped watching)\n")
    if res is not None:
        w(colorize("%7s  results are in" % ("+%.0fs" % (time.time() - t0))) + "\n" + rule + "\n")
        lines = json_lines(res, width) + [rule]
        paged(lines, term_size()[1])
    else:
        w(" (no auto-test results yet; try  at  later)\n")

# ------------------------------------------------------------ main
HINT = " <n> open   b back   r reload   h home   / search   raw   ? help   q quit"
HINTS = {
    "home": " sem = semester   subs = my submissions   notif   att = attendance   quiz   me",
    "group": " pick a task by number",
    "task": " up <file> = upload   at = auto-test results   get = my submission   report   (files: type their number)",
    "sub": " at = auto-test results   get = download   report",
}

HELP = """ <number>      open the item with that number (files/submissions are downloaded to ~/tms-downloads)
 b back   r reload   h home   / <text> search in screen   raw  raw JSON of this screen
 sem           choose semester          subs   all my submissions
 notif         notifications            att    attendance (this semester)
 quiz          quiz instances (read-only; pick one for its results)         me   your settings
 on a task:    up [file]  upload (asks first, then watches auto-test)    at  auto-test results
               get  download my submission     report  download my report
 api [METHOD] <path> [json]   any API call (anything but GET asks first)
 token         check the cached token         relogin   logout   q quit
 (long screens: Enter = next screen, a = show all, q = stop)""".split("\n")

def draw(sc):
    cols, rows = term_size()
    width = max(20, cols - 1)
    rule = "-" * width
    me = S["me"] or {}
    head = " TMS  |  %s" % sc.title
    who = " ".join(x for x in (me.get("name"), ("(%s)" % me["userCode"]) if me.get("userCode") else "") if x)
    if who:
        head += "  |  " + who
    lines = [rule, fit(head, width), rule]
    for l in sc.lines:
        lines.extend(wrap(l, width))
    lines += ["", rule, fit(HINT, width)]
    if HINTS.get(sc.kind):
        lines.append(fit(HINTS[sc.kind], width))
    clear_screen()
    paged(lines, rows)

def framed(title, body_lines):
    cols, rows = term_size()
    width = max(20, cols - 1)
    rule = "-" * width
    paged([rule, fit(" " + title, width), rule] + body_lines + [rule], rows)

def go(func, args=(), push=True):
    sc = func(*args)
    if push and S["cur"] is not None:
        S["hist"].append(S["cur"][1:])
    S["cur"] = (sc,) + (func, args)
    draw(sc)

def run_item(item, cur):
    kind = item[0]
    if kind == "screen":
        go(item[1], item[2:])
    elif kind == "dl":
        download(item[1], item[2] if len(item) > 2 else "")
    elif kind == "call":
        func, args = item[1](*item[2:])
        go(func, args)
    # noop: nothing

def cmd_api(arg):
    toks = arg.split(None, 1)
    method = "GET"
    if toks and toks[0].upper() in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        method = toks[0].upper()
        toks = toks[1].split(None, 1) if len(toks) > 1 else []
    if not toks:
        print(" usage: api [METHOD] <path> [json-body]")
        return
    parts = toks
    path = parts[0] if parts[0].startswith("/") else "/" + parts[0]
    body = parts[1] if len(parts) > 1 else None
    if body:
        try:
            json.loads(body)
        except ValueError:
            print(" body is not valid JSON")
            return
    if method != "GET":
        print(" %s %s%s" % (method, path, ("\n body: " + body) if body else ""))
        print(" This can change data on the server.")
        if not yes(" Send? [y/N] "):
            return
    code, r = api_request(method, path, body)
    ct = hdr_get(r, "Content-Type").split(";")[0].strip().lower()
    if code < 400 and ct and "json" not in ct and not ct.startswith("text/"):
        do_download(r, "download")
        return
    text = r.read().decode("utf-8", "replace")
    log("api %s %s -> %s %s" % (method, path, code, text[:4000].replace("\n", "\\n")))
    if code >= 400:
        print(" " + err_text(code, text))
        return
    try:
        data = json.loads(text, object_pairs_hook=OrderedDict) if text.strip() else None
    except ValueError:
        data = text
    go(scr_static, ("%s %s  [%s]" % (method, path, code), data))

def main():
    load_cfg()
    try:
        go(scr_home)
    except Exception as e:
        print("error:", e)
    while True:
        try:
            cmd = input_("\ntms> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not cmd:
            continue
        verb, _, arg = cmd.partition(" ")
        cur = S["cur"][0] if S["cur"] else None
        try:
            if cmd == "q":
                break
            elif cmd == "?":
                framed("help", HELP)
            elif cmd.isdigit():
                n = int(cmd)
                if cur and 1 <= n <= len(cur.items):
                    run_item(cur.items[n - 1], cur)
                else:
                    print("no such item")
            elif cmd in ("h", "home"):
                go(scr_home)
            elif cmd == "b":
                if S["hist"]:
                    func, args = S["hist"].pop()
                    go(func, args, push=False)
                else:
                    print("nothing to go back to")
            elif cmd == "r":
                if S["cur"]:
                    S["me"] = None
                    S["sems"] = None
                    go(S["cur"][1], S["cur"][2], push=False)
            elif cmd.startswith("/") and cur:
                q = cmd[1:].strip().lower()
                hits = [l for l in cur.lines if q and q in l.lower()]
                framed("search: %s  (%d matches)" % (q, len(hits)), hits)
            elif cmd == "raw" and cur:
                clear_screen()
                paged(json.dumps(cur.data, indent=2, ensure_ascii=False).split("\n"), term_size()[1], color=False)
            elif cmd == "sem":
                go(scr_semesters)
            elif cmd == "subs":
                go(scr_subs)
            elif cmd in ("notif", "notifications"):
                go(scr_json, ("Notifications", "/common/notifications", None))
            elif cmd in ("att", "attendance"):
                sem = cur_sem()
                go(scr_json, ("Attendance - %s" % sem_label(sem), "/student/attendance?semesterID=%s" % sem["id"], None))
            elif cmd == "quiz":
                sem = cur_sem()
                go(scr_json, ("Quizzes - %s" % sem_label(sem), "/student/quiz-test-instances?semesterID=%s" % sem["id"],
                              "/student/quiz-test-instances/{id}/results"))
            elif cmd == "me":
                go(scr_json, ("My settings", "/common/user-settings", None))
            elif verb == "api":
                cmd_api(arg)
            elif cmd == "token":
                ensure_token()
                code, r = api_request("GET", "/common/user-settings")
                exp = jwt_exp(TOKEN["v"])
                print(" token file: %s" % TOKEN_FILE)
                print(" token: %s%s" % ("VALID" if code == 200 else "HTTP %s" % code,
                                        (", expires %s" % datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")) if exp else ""))
            elif cmd == "relogin":
                TOKEN["v"] = ""
                login()
            elif cmd == "logout":
                TOKEN["v"] = ""
                try:
                    os.remove(TOKEN_FILE)
                except OSError:
                    pass
                print("token removed")
            elif verb in ("up", "upload", "submit"):
                cmd_upload(cur, arg) if cur else print(" open a task first")
            elif verb in ("at", "get", "report"):
                subid = cur.ctx.get("subid") if cur else None
                if subid is None:
                    print(" open a task or submission that has a submission first")
                elif verb == "at":
                    go(scr_json, ("Auto-tester results #%s" % subid, "/student/submissions/%s/auto-tester-results" % subid, None))
                elif verb == "get":
                    download("/student/submissions/%s/download" % subid, "submission-%s" % subid)
                else:
                    download("/student/submissions/%s/download-report" % subid, "report-%s" % subid)
            else:
                print("unknown command, '?' for help")
        except KeyboardInterrupt:
            print("\n(interrupted)")
        except Exception as e:
            print("error:", e)

if __name__ == "__main__":
    main()
