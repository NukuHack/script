#!/usr/bin/env python
# -*- coding: utf-8 -*-
# progenv: terminal browser + submitter for https://progenv.inf.elte.hu
# python 2.7 / 3, stdlib only. Run it; it asks for login when needed.
from __future__ import print_function
import os, sys, re, json, time, base64, getpass, textwrap, threading
import mimetypes, binascii, datetime, zipfile, subprocess

try:
    import readline
except ImportError:
    readline = None

try:                                   # py2
    import urllib2 as ur
    from urlparse import urljoin, urlparse
    from urllib import unquote
    from HTMLParser import HTMLParser
    from htmlentitydefs import name2codepoint
    import Queue as queue
    input_ = raw_input
    unichr_ = unichr
    string_types = basestring
except ImportError:                    # py3
    import urllib.request as ur
    from urllib.parse import urljoin, urlparse, unquote
    from html.parser import HTMLParser
    from html.entities import name2codepoint
    import queue
    input_ = input
    unichr_ = chr
    string_types = str

BASE = os.environ.get("PROGENV_BASE", "https://progenv.inf.elte.hu").rstrip("/")
START = BASE + "/hu/"
LOGIN = BASE + "/hu/login"
STATE_FILE = os.path.expanduser("~/.progenv.json")
STREAM_LOG = os.path.expanduser("~/.progenv_stream.log")
STREAM_URL = BASE + "/api/stream?target=/submissions/stream"
DL_DIR = os.path.expanduser(os.environ.get("PROGENV_DL", "~/progenv-downloads"))
MAX_WATCH = 180                         # seconds to follow a run
UA = "Mozilla/5.0 (X11; Linux x86_64) progenv-cli"
DEFAULT_OPTS = ["COMPILE_CHECKER", "COMPILE_SOLUTION", "GENERATE_TEST_OUTS"]

# lines matching these (regex, whole stripped line) are hidden from pages
DROP = [r"^Toggle Sidebar$", r"^Made with", r"^S\.D\.G\.?$"]

state = {"cookies": {}, "authId": "", "action": "", "roles": {}, "names": {},
         "found": [], "opts": list(DEFAULT_OPTS)}
cookies = None
NOASK = set()                           # roles the user declined to provide this session

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
    """write text safely, whatever the terminal encoding is"""
    if not isinstance(s, bytes):
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        s = s.encode(enc, "replace")
    out = getattr(sys.stdout, "buffer", sys.stdout)
    out.write(s)
    out.flush()

def col(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if COLOR else s

WORD_RE = re.compile(r"\b(SUCCESS|PASS|OK|FAILED|FAILURE|ERROR|WRONG|CRASH|TIMEOUT|SIGNAL|PART|"
                     r"WARNING|RUNNING|PENDING|QUEUED|COMPILING|EVALUATING)\b")
WORD_COL = {"SUCCESS": "1;32", "PASS": "1;32", "OK": "32",
            "FAILED": "1;31", "FAILURE": "1;31", "ERROR": "1;31", "WRONG": "1;31",
            "CRASH": "1;31", "TIMEOUT": "1;31", "SIGNAL": "31",
            "PART": "1;33", "WARNING": "33",
            "RUNNING": "1;36", "PENDING": "36", "QUEUED": "36",
            "COMPILING": "36", "EVALUATING": "36"}

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
        sub = " " * (len(ln) - len(s) + (2 if s.startswith("* ") else 0))
        out.extend(textwrap.wrap(ln, width, subsequent_indent=sub) or [""])
    return out

def fit(s, width):
    return s if len(s) <= width else s[:max(0, width - 3)] + "..."

def paged(lines, rows, color=True):
    """print lines; if taller than the screen, stop after each screenful"""
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

def ask_path(prompt):
    """input() with tab-completion of file names"""
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

# ------------------------------------------------------------ state file
def load_state():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except Exception:
        pass

def save_state():
    fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)

# ------------------------------------------------------------ jwt
def jwt_exp(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p.encode()).decode()).get("exp", 0)
    except Exception:
        return 0

def token_ok(name, margin=20):
    t = cookies.get(name)
    return bool(t) and jwt_exp(t) > time.time() + margin

# ------------------------------------------------------------ http
class NoRedirect(ur.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

opener = ur.build_opener(NoRedirect)

def absorb(headers):
    try:
        vals = headers.get_all("Set-Cookie") or []
    except AttributeError:
        vals = headers.getheaders("Set-Cookie")
    for v in vals:
        parts = [x.strip() for x in v.split(";")]
        if "=" not in parts[0]:
            continue
        k, val = parts[0].split("=", 1)
        dead = (val == "" or any(p.lower() == "max-age=0" for p in parts[1:]))
        if dead:
            cookies.pop(k, None)
        else:
            cookies[k] = val
    save_state()

def http_open(url, data=None, headers=None, max_redir=8):
    """returns (final_url, status, open_response). follows redirects, keeps cookies."""
    is_post = data is not None
    for _ in range(max_redir):
        req = ur.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Accept-Language", "hu,en;q=0.8")
        if cookies:
            req.add_header("Cookie", "; ".join("%s=%s" % kv for kv in cookies.items()))
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if is_post:
            req.data = B(data)
        try:
            r = opener.open(req, timeout=25)
        except ur.HTTPError as e:
            r = e
        code = r.code if hasattr(r, "code") else 200
        absorb(r.info())
        if code in (301, 302, 303, 307, 308):
            loc = r.info().get("Location")
            if loc:
                try:
                    r.close()
                except Exception:
                    pass
                url = urljoin(url, loc)
                if code in (301, 302, 303):
                    is_post, data = False, None
                continue
        return url, code, r
    return url, 0, None

def http(url, data=None, headers=None):
    """returns (final_url, status, body_text)"""
    u, code, r = http_open(url, data, headers)
    if r is None:
        return u, 0, ""
    return u, code, r.read().decode("utf-8", "replace")

def hdr_get(r, name, default=""):
    try:
        return r.info().get(name, default) or default
    except Exception:
        return default

# ------------------------------------------------------------ html -> text
SKIP = set(["script", "style", "head", "svg", "noscript", "template"])
PARA = set("p h1 h2 h3 h4 h5 h6 ul ol table".split())
LINE = set("li tr br div section article header footer main nav form dl dt dd aside pre".split())
INLINE = set("span button label small strong b em i code time td th option".split())

class T(HTMLParser):
    def __init__(self, base):
        HTMLParser.__init__(self)
        self.base, self.out, self.tail = base, [], ""
        self.links, self.astack = [], []
        self.skip = self.inlink = 0
        self.has_password = False
        self.selects, self.hidden, self._opt = [], {}, None

    def put(self, s):
        if s:
            self.out.append(s)
            self.tail = (self.tail + s)[-3:]

    def ensure(self, n):
        if not self.out:
            return
        have = len(self.tail) - len(self.tail.rstrip("\n"))
        if have < n:
            self.put("\n" * (n - have))

    def spacer(self):
        if self.tail and self.tail[-1] not in " \n":
            self.put(" ")

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("type") == "password":
            self.has_password = True
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "select":
            self.selects.append({"name": a.get("name") or "", "options": []})
        elif tag == "option" and self.selects:
            self._opt = [a.get("value"), ""]
        elif tag == "input" and a.get("type") == "hidden" and a.get("name") \
                and not a["name"].startswith("$"):
            self.hidden[a["name"]] = a.get("value") or ""
        if tag == "a":
            h = a.get("href") or ""
            ok = False
            if h and not h.startswith(("#", "javascript", "mailto")):
                url = urljoin(self.base, h)
                if urlparse(url).netloc == urlparse(BASE).netloc:
                    self.links.append(url)
                    self.spacer()
                    self.put("[%d] " % len(self.links))
                    self.inlink += 1
                    ok = True
            self.astack.append(ok)
            return
        if self.inlink:
            self.spacer()
            return
        if tag in PARA:
            self.ensure(2)
        elif tag in LINE:
            self.ensure(1)
        elif tag in INLINE:
            self.spacer()
        if tag in ("h1", "h2", "h3"):
            self.put("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.put("  * ")
        elif tag in ("td", "th"):
            self.put("| ")
        elif tag == "input" and a.get("type") not in ("hidden", "password", "checkbox", "radio"):
            self.put("[input %s=%s] " % (a.get("name", ""), a.get("value", "")))

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "option" and self._opt is not None:
            v, l = self._opt
            l = l.strip()
            self.selects[-1]["options"].append((v if v is not None else l, l))
            self._opt = None
        if tag == "a":
            if self.astack and self.astack.pop():
                self.inlink -= 1
                self.spacer()
            return
        if self.inlink:
            return
        if tag in PARA:
            self.ensure(2)
        elif tag in LINE:
            self.ensure(1)
        elif tag in INLINE:
            self.spacer()

    def handle_data(self, d):
        if self.skip:
            return
        if self._opt is not None:
            self._opt[1] += d
        d = re.sub(r"\s+", " ", d)
        if not d.strip():
            self.spacer()
            return
        if not self.tail or self.tail[-1] in " \n":
            d = d.lstrip()
        self.put(d)

    def handle_entityref(self, name):          # py2 only
        try:
            self.handle_data(unichr_(name2codepoint[name]))
        except KeyError:
            self.handle_data("&%s;" % name)

    def handle_charref(self, name):
        try:
            n = int(name[1:], 16) if name[0] in "xX" else int(name)
            self.handle_data(unichr_(n))
        except Exception:
            pass

def render(url, html):
    """returns (body, menu, user, links, has_password_field, extra)"""
    p = T(url)
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    lines = [l.rstrip() for l in "".join(p.out).split("\n")]
    lines = [l for l in lines if not any(re.search(r, l.strip()) for r in DROP)]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    menu = user = ""
    m = re.search(r"(?m)^#{1,3} ", text)
    if m and m.start() > 0:
        chrome, rest = text[:m.start()], text[m.start():]
        if chrome.count("\n") < 15:
            menu = "   ".join("[%s] %s" % (n, t.strip())
                              for n, t in re.findall(r"\[(\d+)\] ([^\[\n]+)", chrome))
            ul = [l.strip(" *|") for l in chrome.split("\n")
                  if l.strip() and not re.search(r"\[\d+\]", l)]
            user = " ".join(x for x in ul if len(x) > 1)
            text = rest
    extra = {"selects": p.selects, "hidden": p.hidden}
    return text, menu, user, p.links, p.has_password, extra

# ------------------------------------------------------------ server-action discovery
def scan_actions(text):
    """(id, name) pairs of Next.js server actions found in js/html text"""
    res = []
    for m in re.finditer(r'createServerReference\)?\(\s*["\']([0-9a-f]{40,})["\']([^)]{0,200})\)', text):
        names = re.findall(r'["\']([A-Za-z_$][\w$]*)["\']', m.group(2))
        res.append((m.group(1), names[-1] if names else ""))
    for m in re.finditer(r'\$ACTION_ID_([0-9a-f]{40,})', text):
        res.append((m.group(1), ""))
    return res

def scan_page(page_url, say=None):
    """fetch a page + its scripts, return [(id, name)] of all server actions"""
    u, code, html = http(page_url)
    cand = re.findall(r'(?:src|href)="([^"]+\.js[^"]*)"', html)
    cand += ["/_next/" + m for m in re.findall(r'static/chunks/[^"\\\s]+?\.js', html)]
    urls, seen_u = [], set()
    for s in cand:
        a = urljoin(u, s.replace("\\u0026", "&"))
        key = a.split("?")[0]
        if urlparse(a).netloc == urlparse(BASE).netloc and key not in seen_u:
            seen_u.add(key)
            urls.append(a)
    found, seen = [], set()
    def add(items):
        for aid, name in items:
            if aid not in seen:
                seen.add(aid)
                found.append((aid, name))
    add(scan_actions(html))
    for a in urls[:80]:
        _, c, js = http(a)
        if c == 200:
            add(scan_actions(js))
    return found

# ------------------------------------------------------------ login
def discover_action():
    cands = scan_page(LOGIN)
    for aid, name in cands:
        if re.search(r'login|signin|sign_in|authenticate', name, re.I):
            return aid
    return cands[0][0] if len(cands) == 1 else ""

def do_login():
    """asks for credentials like a browser login form. returns True on success."""
    for attempt in range(3):
        d = state.get("authId", "")
        aid = input_("Name (authId)%s: " % (" [%s]" % d if d else "")).strip() or d
        pw = getpass.getpass("Password: ")
        if not aid or not pw:
            print("both are required")
            continue
        action = state.get("action") or discover_action()
        if not action:
            print("(could not auto-detect the login action id)")
            action = input_("Next-Action header value (dev tools, login request) or empty to try without: ").strip()
        hdr = {"Content-Type": "text/plain;charset=UTF-8",
               "Accept": "text/x-component",
               "Origin": BASE, "Referer": LOGIN}
        if action:
            hdr["Next-Action"] = action
        old = cookies.get("access_token")
        cookies.pop("access_token", None)
        cookies.pop("refresh_token", None)
        _, code, body = http(LOGIN, data=json.dumps([{"authId": aid, "password": pw}]), headers=hdr)
        if cookies.get("access_token"):
            state["authId"], state["action"] = aid, action
            save_state()
            print("login ok")
            return True
        if old:
            cookies["access_token"] = old
        print("login failed (HTTP %s). wrong password, or the action id is stale." % code)
        state["action"] = ""
        save_state()
    return False

def logged_in_page(url, html):
    haspw = render(url, html)[4]
    return not (haspw or urlparse(url).path.rstrip("/").endswith("/login"))

def ensure_session():
    if token_ok("access_token"):
        return True
    if cookies.get("refresh_token") and token_ok("refresh_token"):
        u, c, h = http(START)
        if token_ok("access_token") and logged_in_page(u, h):
            return True
    return do_login()

# ------------------------------------------------------------ downloads
def dl_filename(url, r):
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
        name = unquote(os.path.basename(urlparse(url).path)) or "download"
    try:                                  # py3 decodes headers as latin-1; undo it for utf-8 names
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

def progress(name, got, total, t0, width):
    el = max(time.time() - t0, 0.001)
    speed = "%s/s" % hsize(got / el)
    if total:
        pct = got * 100.0 / total
        rest = " %3d%%  %s / %s  %s" % (pct, hsize(got), hsize(total), speed)
        bw = max(10, width - len(rest) - 4)
        k = int(bw * got / float(total))
        bar = "[" + "#" * k + "-" * (bw - k) + "]"
    else:
        rest, bar = "  %s  %s" % (hsize(got), speed), ""
    w("\r\033[2K " + (bar + rest)[:width - 1])

def do_download(url, r):
    name = dl_filename(url, r)
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
                    progress(name, got, total, t0, width)
        os.rename(part, dest)
    except KeyboardInterrupt:
        try:
            os.remove(part)
        except OSError:
            pass
        w("\n download cancelled\n")
        return
    if tty:
        progress(name, got, total or got, t0, width)
        w("\n")
    secs = time.time() - t0
    kind = sniff(dest)
    cols = term_size()[0]
    rule = "-" * max(20, cols - 1)
    lines = [rule,
             " Downloaded  " + name,
             " Saved to    " + dest,
             " Size        %s   (%s, %.1fs)" % (hsize(got), kind, secs),
             rule]
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

def classify(r):
    cd = hdr_get(r, "Content-Disposition").lower()
    ct = hdr_get(r, "Content-Type").split(";")[0].strip().lower()
    if "attachment" in cd:
        return "download"
    if ct in ("", "text/html", "application/xhtml+xml", "text/x-component"):
        return "page"
    if ct == "application/json" or ct.endswith("+json"):
        return "json"
    return "download"

# ------------------------------------------------------------ pages
class Page(object):
    pass

def fetch(url):
    """returns a Page, or None if the url was a file (which is downloaded)"""
    if not ensure_session():
        raise RuntimeError("not logged in")
    for attempt in (0, 1):
        u, code, r = http_open(url)
        if r is None:
            raise RuntimeError("too many redirects")
        kind = classify(r)
        if kind == "download":
            do_download(u, r)
            return None
        text = r.read().decode("utf-8", "replace")
        if kind == "json" or "login" in url or logged_in_page(u, text):
            break
        cookies.pop("access_token", None)
        if not ensure_session():
            raise RuntimeError("not logged in")
    p = Page()
    p.url, p.code, p.html = u, code, text
    if kind == "json":
        try:
            p.body = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            p.body = text
        p.menu = p.user = ""
        p.links, p.selects, p.hidden = [], [], {}
    else:
        p.body, p.menu, p.user, p.links, _, extra = render(u, text)
        p.selects, p.hidden = extra["selects"], extra["hidden"]
    return p

# ------------------------------------------------------------ flight (react server component) responses
def flight_parse(raw):
    rows, i, n = {}, 0, len(raw)
    while i < n:
        j = raw.find(b":", i)
        if j < 0:
            break
        rid = raw[i:j].decode("ascii", "replace").strip()
        i = j + 1
        if raw[i:i + 1] == b"T":                       # text chunk: T<hexlen>,<bytes>
            k = raw.find(b",", i)
            try:
                ln = int(raw[i + 1:k], 16)
            except ValueError:
                break
            rows[rid] = raw[k + 1:k + 1 + ln].decode("utf-8", "replace")
            i = k + 1 + ln
            if raw[i:i + 1] == b"\n":
                i += 1
        else:
            k = raw.find(b"\n", i)
            if k < 0:
                k = n
            line = raw[i:k].decode("utf-8", "replace")
            try:
                rows[rid] = json.loads(line)
            except ValueError:
                rows[rid] = line
            i = k + 1
    return rows

def flight_resolve(v, rows, depth=0):
    if depth > 8:
        return v
    if isinstance(v, string_types):
        if v == "$undefined":
            return None
        if re.match(r"^\$[0-9a-f]+$", v) and v[1:] in rows:
            return flight_resolve(rows[v[1:]], rows, depth + 1)
        return v
    if isinstance(v, list):
        return [flight_resolve(x, rows, depth + 1) for x in v]
    if isinstance(v, dict):
        return dict((k, flight_resolve(x, rows, depth + 1)) for k, x in v.items())
    return v

def flight_data(text):
    """returns (data, error) from a server-action response"""
    rows = flight_parse(B(text))
    def order(k):
        return int(k, 16) if re.match(r"^[0-9a-f]+$", k) else 10 ** 9
    for k in sorted(rows, key=order):
        v = rows[k]
        if isinstance(v, dict) and ("data" in v or "error" in v):
            v = flight_resolve(v, rows)
            return v.get("data"), v.get("error")
    return None, "unrecognized response"

# ------------------------------------------------------------ server actions
BAD = re.compile(r"delet|remov|updat|edit|cancel|rerun|restart|reset|toggle|publish|grade|invit|"
                 r"join|leav|log(in|out)|password|mail|profile|member|role|avatar|image|photo|logo",
                 re.I)
R_CODE = re.compile(r"^(get|fetch|load|download|read)\w*(code|source)", re.I)
R_LIST = re.compile(r"^(get|fetch|load|list|refresh|find|read)\w*submission", re.I)
R_SUBMIT = re.compile(r"submit|upload|(create|add|send|post)\w*(submission|solution)", re.I)
ROLE_DESC = {
    "submit": "the POST that uploads your file (multipart body)",
    "list": 'the POST with body ["/courses/.../submissions"]',
    "code": 'the POST with body ["/courses/.../codes?download=true"]',
}

def assign_roles(found, force=False):
    cands = {"code": [], "list": [], "submit": []}
    for aid, name in found:
        if not name or BAD.search(name):
            continue
        if R_CODE.search(name):
            cands["code"].append(aid)
        elif R_LIST.search(name):
            cands["list"].append(aid)
        elif R_SUBMIT.search(name):
            cands["submit"].append(aid)
    for role, ids in cands.items():
        if ids and (force or not state["roles"].get(role)):
            state["roles"][role] = ids[0]
    for aid, name in found:
        if name:
            state["names"][aid] = name
    save_state()

def get_action(role, page_url, force=False):
    if state["roles"].get(role) and not force:
        return state["roles"][role]
    if role in NOASK:
        return ""
    if force or not state["found"]:
        print(" (scanning the site's scripts for server actions, one time...)")
        state["found"] = [list(x) for x in scan_page(page_url)]
        save_state()
    assign_roles([tuple(x) for x in state["found"]], force=force)
    if state["roles"].get(role):
        return state["roles"][role]
    print("\n Could not find the '%s' action automatically." % role)
    print(" Firefox: F12 > Network > click %s > Headers > 'Next-Action'." % ROLE_DESC.get(role, role))
    v = input_(" Next-Action value (Enter = skip): ").strip()
    if re.match(r"^[0-9a-f]{20,}$", v):
        state["roles"][role] = v
        save_state()
        return v
    NOASK.add(role)
    return ""

def call_action(role, page_url, args=None, raw=None, ctype=None):
    """POST a server action. returns (status, text)"""
    for attempt in (0, 1):
        if not ensure_session():
            raise RuntimeError("not logged in")
        aid = get_action(role, page_url, force=(attempt == 1))
        if not aid:
            raise RuntimeError("no server action id for '%s'" % role)
        hdr = {"Next-Action": aid, "Accept": "text/x-component", "Origin": BASE,
               "Referer": page_url, "Content-Type": ctype or "text/plain;charset=UTF-8"}
        body = raw if raw is not None else B(json.dumps(args))
        u, code, text = http(page_url, data=body, headers=hdr)
        if urlparse(u).path.rstrip("/").endswith("/login"):
            cookies.pop("access_token", None)
            continue
        if code == 404 or "Server action not found" in text[:300]:
            state["roles"].pop(role, None)
            save_state()
            continue
        return code, text
    raise RuntimeError("server action '%s' not found or session lost" % role)

def multipart(items):
    """items: ('field', name, value) | ('file', name, filename, ctype, bytes)"""
    bd = "----progenv" + binascii.hexlify(os.urandom(12)).decode()
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

TASK_RE = re.compile(r"^(https?://[^/]+)((?:/[a-z]{2})?)(/courses/\d+/groups/\d+/topics/\d+/tasks/\d+)")

def parse_task(url):
    m = TASK_RE.match(url or "")
    return (m.group(1) + m.group(2) + m.group(3), m.group(3)) if m else None

def fetch_subs(page_url, task, opts=None):
    try:
        args = [task + "/submissions"] + ([opts] if opts else [])
        code, text = call_action("list", page_url, args)
    except Exception as e:
        return None, str(e)
    data, err = flight_data(text)
    if code != 200 or err:
        return None, "HTTP %s %s" % (code, err or text[:200])
    if not isinstance(data, list):
        return None, "unexpected response: " + text[:200]
    return data, None

# ------------------------------------------------------------ submission rendering
DONE = set(["SUCCESS", "FAILED", "FAILURE", "ERROR", "COMPILE_ERROR", "TIMEOUT",
            "CANCELLED", "CANCELED", "FINISHED", "COMPLETED", "DONE"])
PENDING = set(["PENDING", "QUEUED", "WAITING", "RUNNING", "IN_PROGRESS", "COMPILING",
               "EVALUATING", "CREATED", "STARTED", "PROCESSING", "SCHEDULED", "SUBMITTED", "TESTING"])

def is_done(s):
    st = ("%s" % s.get("status", "")).upper()
    if st in DONE:
        return True
    if st in PENDING:
        return False
    return s.get("resultObject") is not None

def fmt_date(s):
    s = "%s" % (s or "")
    if s.startswith("$D"):
        s = s[2:]
    m = re.match(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)", s)
    return "%s %s" % m.groups() if m else s

def secs_between(a, b):
    try:
        f = lambda s: datetime.datetime.strptime(re.sub(r"^\$D", "", "%s" % s)[:19], "%Y-%m-%dT%H:%M:%S")
        return int((f(b) - f(a)).total_seconds())
    except Exception:
        return None

def clean_out(s):
    s = s.replace("/work/submission/app/", "")
    return re.sub(r"\s*\[/work/[^\]]*\]", "", s)

def test_row(t, done):
    st = t.get("stat") or {}
    sub = t.get("subtest") or []
    pts = sum(x.get("points", 0) for x in sub)
    mx = sum(x.get("maxPoints", 0) for x in sub)
    status = st.get("status")
    note = ""
    if t.get("code") is None:
        res = "..."
    elif t.get("code") == 0 and not status:
        if mx and pts >= mx:
            res = "PASS"
        elif pts > 0:
            res = "PART"
        else:
            res = "WRONG" if done else "..."
            note = "ran fine (exit %s), no points" % st.get("exitcode", "0")
    else:
        res = {"SG": "CRASH", "RE": "CRASH", "TO": "TIMEOUT", "XX": "ERROR"}.get(status, "FAILED")
        note = st.get("message") or ""
        if st.get("exitsig"):
            note += (" " if note else "") + "(signal %s)" % st["exitsig"]
        if st.get("exitcode") not in (None, "0"):
            note += (" " if note else "") + "exit %s" % st["exitcode"]
    try:
        tm = "%.3fs" % float(st.get("time"))
    except (TypeError, ValueError):
        tm = "-"
    try:
        mem = "%.1f MB" % (float(st.get("max-rss")) / 1024.0)
    except (TypeError, ValueError):
        mem = "-"
    return res, tm, mem, pts, mx, note

def render_sub(s, width):
    rule = "-" * width
    ro = s.get("resultObject") or {}
    done = is_done(s)
    took = secs_between(s.get("createdAt"), s.get("updatedAt"))
    lines = [rule,
             " Submission #%s    %s    %s    %s / %s points" % (
                 s.get("submissionNo"), s.get("status"), s.get("environment", ""),
                 s.get("points", 0), s.get("maxPoints", 0)),
             " created %s UTC%s" % (fmt_date(s.get("createdAt")),
                                   ("   took %ss" % took) if took is not None else ""),
             rule]
    comp = ro.get("compilation")
    if comp:
        out = clean_out(comp.get("out") or "")
        err = (comp.get("error") or "").strip()
        if comp.get("code") not in (0, None):
            lines.append(" Compilation: ERROR (exit code %s)" % comp.get("code"))
        else:
            nw = len(re.findall(r"warning", out))
            lines.append(" Compilation: OK" + ((" (%d warning%s)" % (nw, "" if nw == 1 else "s")) if nw else ""))
        body = [l for l in (out + ("\n" + err if err else "")).strip().split("\n") if l.strip()]
        for l in body[:30]:
            lines += wrap("   " + l, width)
        if len(body) > 30:
            lines.append("   ... (%d more lines)" % (len(body) - 30))
        lines.append(rule)
    if s.get("resultMessage"):
        lines += wrap(" Message: %s" % s["resultMessage"], width) + [rule]
    tests = ro.get("tests") or []
    if tests:
        lines.append("  #   result      time      memory     points   note")
        cnt = {}
        for i, t in enumerate(tests, 1):
            res, tm, mem, pts, mx, note = test_row(t, done)
            cnt[res] = cnt.get(res, 0) + 1
            row = " %3d   %-9s %8s  %9s  %7s   " % (i, res, tm, mem, "%s/%s" % (pts, mx))
            lines.append(row + fit(note, max(0, width - len(row))))
        lines.append(rule)
        order = ["PASS", "PART", "WRONG", "CRASH", "TIMEOUT", "ERROR", "FAILED", "..."]
        lines.append(" Tests: %d   %s" % (len(tests), "   ".join(
            "%s %d" % (k, cnt[k]) for k in order if k in cnt)))
    elif not comp:
        lines.append(" (no detailed result data)")
        for l in wrap(json.dumps(ro)[:600], width):
            lines.append(" " + l)
    lines.append(" Total: %s / %s points" % (s.get("points", 0), s.get("maxPoints", 0)))
    lines.append(rule)
    return lines

def render_list(subs, width):
    rule = "-" * width
    lines = [rule, " Submissions (%d)" % len(subs), rule,
             "  #    status     env    points   created (UTC)        took"]
    for s in subs:
        took = secs_between(s.get("createdAt"), s.get("updatedAt"))
        lines.append(" %3s   %-9s  %-5s %8s   %s   %s" % (
            s.get("submissionNo"), s.get("status"), s.get("environment", ""),
            "%s/%s" % (s.get("points", 0), s.get("maxPoints", 0)),
            fmt_date(s.get("createdAt")), ("%ss" % took) if took is not None else ""))
    lines += [rule, " sub <n> = details     code <n> = source"]
    return lines

def pick_target(subs, target_no):
    if target_no is not None:
        for s in subs:
            if s.get("submissionNo") == target_no:
                return s
        return None
    return max(subs, key=lambda s: s.get("submissionNo") or 0) if subs else None

# ------------------------------------------------------------ live stream
def find_submission(obj, depth=0):
    if depth > 3:
        return None
    if isinstance(obj, dict):
        if "status" in obj and ("resultObject" in obj or "submissionNo" in obj):
            return obj
        for v in obj.values():
            r = find_submission(v, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj[:20]:
            r = find_submission(v, depth + 1)
            if r is not None:
                return r
    return None

def sub_line(s):
    ro = s.get("resultObject") or {}
    tests = ro.get("tests") or []
    total = (ro.get("task") or {}).get("numberOfTests") or len(tests)
    parts = ["#%s" % s.get("submissionNo", "?"), "%s" % s.get("status")]
    if total:
        k = int(20.0 * len(tests) / total)
        parts.append("[" + "#" * k + "." * (20 - k) + "] %d/%d tests" % (len(tests), total))
    if ro.get("compilation") and not tests:
        c = ro["compilation"]
        parts.append("compiled OK" if c.get("code") in (0, None) else "COMPILE ERROR")
    parts.append("points %s/%s" % (s.get("points", 0), s.get("maxPoints", 0)))
    return "  ".join(parts)

def flat(obj, prefix=""):
    out = []
    if isinstance(obj, dict):
        for k in sorted(obj, key=lambda x: "%s" % x):
            out.extend(flat(obj[k], prefix + ("%s" % k) + "."))
    elif isinstance(obj, list):
        if len(obj) <= 3 and not any(isinstance(x, (dict, list)) for x in obj):
            for i, x in enumerate(obj):
                out.append(("%s%d" % (prefix, i), x))
        else:
            out.append((prefix.rstrip("."), "[%d items]" % len(obj)))
    else:
        out.append((prefix.rstrip("."), obj))
    return out

def short(v, n=60):
    s = v if isinstance(v, string_types) else json.dumps(v)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n - 3] + "..."

def fmt_generic(ev, obj, data):
    low = ("%s %s" % (ev, obj.get("type", "") if isinstance(obj, dict) else "")).lower()
    if re.search(r"ping|heartbeat|keep-?alive", low):
        return []
    tag = ev if ev and ev != "message" else ""
    if isinstance(obj, dict):
        main = None
        for k in ("message", "msg", "text", "output", "log", "line", "stdout", "stderr"):
            if isinstance(obj.get(k), string_types) and obj[k].strip():
                main = (k, obj[k])
                break
        rest = ["%s=%s" % (k, short(v, 30)) for k, v in flat(obj) if not main or k != main[0]]
        if main:
            lines = [l for l in main[1].rstrip().split("\n")]
            head = ("[%s] " % tag if tag else "")
            res = [head + lines[0]] + ["    " + l for l in lines[1:]]
            if rest:
                res.append("    (" + " ".join(rest[:6]) + ")")
            return res
        return [("[%s] " % tag if tag else "") + "  ".join(rest)]
    text = data.strip()
    if not text:
        return [("[%s]" % tag)] if tag else []
    lines = text.split("\n")
    return [("[%s] " % tag if tag else "") + lines[0]] + ["    " + l for l in lines[1:]]

def stream_reader(q, stop):
    try:
        req = ur.Request(STREAM_URL)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "text/event-stream")
        req.add_header("Cache-Control", "no-cache")
        if cookies:
            req.add_header("Cookie", "; ".join("%s=%s" % kv for kv in cookies.items()))
        r = opener.open(req, timeout=120)
        q.put(("connected", getattr(r, "code", 200)))
        ev, data = None, []
        while not stop.is_set():
            line = r.readline()
            if not line:
                break
            line = line.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if data or ev:
                    q.put(("event", ev or "message", "\n".join(data)))
                ev, data = None, []
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                d = line[5:]
                data.append(d[1:] if d.startswith(" ") else d)
        q.put(("end",))
    except Exception as e:
        q.put(("error", "%s" % e))

def open_log():
    fd = os.open(STREAM_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "ab")

def watch(page_url, task, target_no, label):
    cols = term_size()[0]
    width = max(20, cols - 1)
    rule = "-" * width
    q, stop = queue.Queue(), threading.Event()
    th = threading.Thread(target=stream_reader, args=(q, stop))
    th.daemon = True
    th.start()
    logf = open_log()
    def log(s):
        logf.write(B("%s %s\n" % (time.strftime("%H:%M:%S"), s)))
        logf.flush()
    log("=== run %s (%s) ===" % (label, datetime.datetime.now()))
    t0 = time.time()
    st = {"last": None}
    w("\n" + colorize(rule) + "\n")
    w(colorize(fit(" LIVE  %s     (Ctrl-C stops watching, the run continues on the server)" % label, width)) + "\n")
    w(rule + "\n")
    def say(msg):
        if msg == st["last"]:
            return
        st["last"] = msg
        pre = "%7s  " % ("+%.1fs" % (time.time() - t0))
        for i, ln in enumerate(wrap(msg, max(20, width - len(pre)))):
            w(colorize((pre if i == 0 else " " * len(pre)) + ln) + "\n")
    done_sub, last_poll, dead = None, 0, False
    try:
        while time.time() - t0 < MAX_WATCH and not done_sub:
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                item = None
            if item:
                if item[0] == "connected":
                    say("stream connected")
                elif item[0] == "event":
                    _, ev, data = item
                    log("event=%s data=%s" % (ev, data.replace("\n", "\\n")))
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        obj = None
                    sub = find_submission(obj) if obj is not None else None
                    if sub is not None:
                        if target_no is None or sub.get("submissionNo") == target_no:
                            say(sub_line(sub))
                            if is_done(sub):
                                done_sub = sub
                    else:
                        for ln in fmt_generic(ev, obj, data):
                            say(ln)
                elif item[0] == "error":
                    log("error %s" % item[1])
                    say("stream error: %s  (falling back to polling)" % item[1])
                elif item[0] == "end":
                    log("end")
                    say("stream closed by the server (polling for the result)")
            if not done_sub and time.time() - last_poll >= 4:
                last_poll = time.time()
                subs, err = fetch_subs(page_url, task)
                if subs:
                    tgt = pick_target(subs, target_no)
                    if tgt:
                        say(sub_line(tgt))
                        if is_done(tgt):
                            done_sub = tgt
                elif err:
                    say("poll: %s" % fit(err, 80))
    except KeyboardInterrupt:
        w("\n (stopped watching; use  subs  later)\n")
    stop.set()
    logf.close()
    if done_sub:
        subs, err = fetch_subs(page_url, task)           # fresh, complete version
        tgt = pick_target(subs, done_sub.get("submissionNo")) if subs else None
        return tgt or done_sub
    return None

# ------------------------------------------------------------ commands
EXT_LANG = {".cs": "cs", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".pas": "pas", ".pp": "pas"}
EXT_LABEL = {".cs": "c#", ".cpp": "c++", ".cc": "c++", ".cxx": "c++", ".pas": "pascal", ".pp": "pascal"}

def pick_language(cur, fname):
    ext = os.path.splitext(fname)[1].lower()
    sel = None
    for s in getattr(cur, "selects", []):
        if "lang" in s["name"].lower():
            sel = s
            break
    if sel is None:
        for s in getattr(cur, "selects", []):
            if len([o for o in s["options"] if o[0]]) >= 2:
                sel = s
                break
    opts = [(v, l) for v, l in (sel["options"] if sel else []) if v]
    default = EXT_LANG.get(ext, "")
    if opts:
        for v, l in opts:
            if v.lower() == EXT_LANG.get(ext) or l.lower() == EXT_LABEL.get(ext):
                default = v
        shown = ", ".join("%s = %s" % (v, l) if v != l else v for v, l in opts)
        while True:
            a = input_(" Language (%s) [%s]: " % (shown, default)).strip() or default
            for v, l in opts:
                if a.lower() in (v.lower(), l.lower()):
                    return v
            print(" not one of the options")
    a = input_(" Language value [%s] (known: cs = C#): " % default).strip() or default
    return a

def yes(prompt):
    try:
        return input_(prompt).strip().lower() in ("y", "yes", "i", "igen")
    except (EOFError, KeyboardInterrupt):
        return False

def cmd_upload(cur, arg):
    pt = parse_task(cur.url)
    page_url, task = pt
    path = arg.strip() or ask_path(" File to upload (Tab completes): ")
    path = os.path.expanduser(path.strip().strip("'\""))
    if not path or not os.path.isfile(path):
        print(" not a file: %s" % path)
        return
    size = os.path.getsize(path)
    if size > 1024 * 1024 and not yes(" %s is large. continue? [y/N] " % hsize(size)):
        return
    with open(path, "rb") as f:
        content = f.read()
    fname = os.path.basename(path)
    lang = pick_language(cur, fname)
    if not lang:
        print(" no language chosen")
        return
    mode = (getattr(cur, "hidden", {}) or {}).get("mode") or "task"
    try:
        aid = get_action("submit", page_url)
    except Exception as e:
        print(" error:", e)
        return
    if not aid:
        return
    cols = term_size()[0]
    rule = "-" * max(20, cols - 1)
    print("\n" + rule)
    print(" Upload   %s  (%s)" % (fname, hsize(size)))
    print(" Language %s     mode %s" % (lang, mode))
    print(" Task     %s" % page_url)
    print(" Action   %s" % (state["names"].get(aid) or aid[:12] + "..."))
    print(rule)
    if not yes(" Upload as a NEW submission? [y/N] "):
        print(" cancelled")
        return
    before, _ = fetch_subs(page_url, task)
    before_max = max([s.get("submissionNo") or 0 for s in before]) if before else 0
    body, ctype = multipart([
        ("field", "1_language", lang),
        ("file", "1_file", fname, mimetypes.guess_type(fname)[0] or "text/plain", content),
        ("field", "1_mode", mode),
        ("field", "0", json.dumps(["$K1", task + "/submissions"])),
    ])
    try:
        code, text = call_action("submit", page_url, raw=body, ctype=ctype)
    except Exception as e:
        print(" upload failed:", e)
        return
    data, err = flight_data(text)
    if code != 200 or (err and err != "unrecognized response"):
        print(" upload failed (HTTP %s): %s" % (code, fit("%s" % (err or text), 300)))
        return
    print(" uploaded.")
    target = None
    for _ in range(6):
        after, _e = fetch_subs(page_url, task)
        if after:
            new = [s.get("submissionNo") for s in after if (s.get("submissionNo") or 0) > before_max]
            if new:
                target = max(new)
                break
        elif before is None:
            break
        time.sleep(1.5)
    final = watch(page_url, task, target, "%s  ->  submission #%s" % (fname, target if target else "?"))
    if final:
        lines = render_sub(final, max(20, term_size()[0] - 1))
        w("\n")
        paged(lines, term_size()[1])
    else:
        print("\n (no final result yet. try:  subs  /  sub <n>)")
    print(" raw stream log: %s" % STREAM_LOG)

def cmd_subs(cur, arg):
    page_url, task = parse_task(cur.url)
    opts = None
    if arg.strip() == "opts":
        opts = state.get("opts") or []
        print(" This sends exactly what the browser sent, including the options:")
        print("   %s" % ", ".join(opts))
        print(" (I do not know what they do on the server, so I ask first.)")
        if not yes(" Send? [y/N] "):
            return
    subs, err = fetch_subs(page_url, task, opts)
    if subs is None:
        print(" error:", err)
        return
    paged(render_list(subs, max(20, term_size()[0] - 1)), term_size()[1])

def cmd_sub(cur, arg):
    page_url, task = parse_task(cur.url)
    subs, err = fetch_subs(page_url, task)
    if subs is None:
        print(" error:", err)
        return
    n = int(arg) if arg.strip().isdigit() else None
    s = pick_target(subs, n)
    if not s:
        print(" no such submission")
        return
    paged(render_sub(s, max(20, term_size()[0] - 1)), term_size()[1])

def cmd_code(cur, arg):
    page_url, task = parse_task(cur.url)
    parts = arg.split()
    if not parts or not parts[0].isdigit():
        print(" usage: code <n> [save]")
        return
    n = int(parts[0])
    try:
        code, text = call_action("code", page_url, [task + "/submissions/%d/codes?download=true" % n])
    except Exception as e:
        print(" error:", e)
        return
    data, err = flight_data(text)
    if not isinstance(data, dict):
        print(" error: %s" % fit("%s" % (err or text), 300))
        return
    if data.get("kind") == "single-file" and isinstance(data.get("content"), string_types):
        fname, src = data.get("fileName") or "code.txt", data["content"]
        cols, rows = term_size()
        width = max(20, cols - 1)
        rule = "-" * width
        body = src.replace("\t", "    ").split("\n")
        lines = [rule, " Submission #%d   %s   (%d lines)" % (n, fname, len(body)), rule]
        for i, l in enumerate(body, 1):
            lines += wrap("%4d  %s" % (i, l), width) if len(l) + 6 > width else ["%4d  %s" % (i, l)]
        lines.append(rule)
        clear_screen()
        paged(lines, rows, color=False)
        if len(parts) > 1 and parts[1] == "save":
            dest = unique_path(os.getcwd(), os.path.basename(fname))
            with open(dest, "wb") as f:
                f.write(B(src))
            print(" saved to %s" % dest)
    else:
        print(" unknown code format, keys: %s" % ", ".join(sorted("%s" % k for k in data)))

def cmd_opts():
    opts = state.setdefault("opts", list(DEFAULT_OPTS))
    while True:
        print("\n options sent by  'subs opts':")
        for i, o in enumerate(opts, 1):
            print("   %d. %s" % (i, o))
        if not opts:
            print("   (none)")
        a = input_(" add <NAME> | rm <n> | clear | reset | Enter = done: ").strip()
        if not a:
            break
        v, _, rest = a.partition(" ")
        rest = rest.strip()
        if v == "add" and rest:
            o = re.sub(r"[^A-Za-z0-9_]", "_", rest).upper()
            if o not in opts:
                opts.append(o)
        elif v == "rm" and rest.isdigit() and 1 <= int(rest) <= len(opts):
            opts.pop(int(rest) - 1)
        elif v == "clear":
            del opts[:]
        elif v == "reset":
            opts[:] = DEFAULT_OPTS
        else:
            print(" ?")
    save_state()

def cmd_actions(arg):
    parts = arg.split()
    if parts[:1] == ["bind"] and len(parts) == 3:
        state["roles"][parts[1]] = parts[2]
        NOASK.discard(parts[1])
        save_state()
        print(" bound %s -> %s" % (parts[1], parts[2][:12] + "..."))
        return
    print("\n roles:")
    for r in ("submit", "list", "code"):
        aid = state["roles"].get(r)
        print("   %-7s %s" % (r, ("%s  %s" % (state["names"].get(aid, "?"), aid[:12] + "...")) if aid else "(not set)"))
    print(" discovered actions: %d" % len(state["found"]))
    for aid, name in state["found"][:30]:
        print("   %s  %s" % (aid[:12] + "...", name or "(unnamed)"))
    print(" set by hand:  actions bind <submit|list|code> <id>      rescan:  actions rescan")
    if parts[:1] == ["rescan"]:
        state["found"], state["roles"] = [], {}
        NOASK.clear()
        save_state()
        print(" cleared; will rescan on next use")

# ------------------------------------------------------------ screen
HINT = " <n> open link   g <path>   b back   r reload   / search   ? help   q quit"
HINT_TASK = " up = upload file   subs = submissions   sub <n> = details   code <n> = source   opts   actions"

def draw(p):
    cols, rows = term_size()
    width = max(20, cols - 1)
    rule = "-" * width
    head = " %s  [%s]" % (p.url, p.code)
    if p.user:
        head += "   user: " + p.user
    lines = [rule] + wrap(head, width) + [rule]
    if p.menu:
        lines += wrap(" " + p.menu, width) + [rule]
    lines += wrap(p.body, width)
    lines += ["", rule, fit(HINT, width)]
    if parse_task(p.url):
        lines.append(fit(HINT_TASK, width))
    clear_screen()
    paged(lines, rows)

def framed(title, body_lines):
    cols, rows = term_size()
    width = max(20, cols - 1)
    rule = "-" * width
    paged([rule, fit(" " + title, width), rule] + body_lines + [rule], rows)

HELP = """ <number>      follow link [number]  (files like PDF / zip are downloaded to ~/progenv-downloads)
 g <path|url>  go to e.g. /hu/courses (same host only)
 b back   r reload   / <text> search in page   raw  show html
 relogin       log in again      logout  forget session      q quit
 on a task page:
   up [file]     upload a solution (asks language, confirms, then shows the live run)
   subs          list your submissions        subs opts   same, sending the options (see opts)
   sub [n]       full result of submission n (default: latest)
   code <n> [save]   show the source of submission n (save = write it to the current dir)
   opts          edit the option list      actions   show / set the server-action ids
 (long pages: Enter = next screen, a = show all, q = stop)""".split("\n")

def main():
    global cookies
    load_state()
    cookies = state["cookies"]
    S = {"cur": None, "hist": []}

    def navigate(url, push=True):
        p = fetch(url)
        if p is None:                         # it was a file download
            return
        if push and S["cur"]:
            S["hist"].append(S["cur"].url)
        S["cur"] = p
        draw(p)

    try:
        navigate(START)
    except Exception as e:
        print("error:", e)
    while True:
        try:
            cmd = input_("\nprogenv> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not cmd:
            continue
        cur = S["cur"]
        verb, _, arg = cmd.partition(" ")
        try:
            if cmd == "q":
                break
            elif cmd == "?":
                framed("help", HELP)
            elif cmd.isdigit():
                n = int(cmd)
                if cur and 1 <= n <= len(cur.links):
                    navigate(cur.links[n - 1])
                else:
                    print("no such link")
            elif verb == "g" and arg:
                navigate(urljoin(cur.url if cur else START, arg.strip()))
            elif cmd == "b":
                if S["hist"]:
                    navigate(S["hist"].pop(), push=False)
                else:
                    print("nothing to go back to")
            elif cmd == "r":
                navigate(cur.url if cur else START, push=False)
            elif cmd.startswith("/") and cur:
                q = cmd[1:].strip().lower()
                hits = [l for l in cur.body.split("\n") if q and q in l.lower()]
                framed("search: %s  (%d matches)" % (q, len(hits)), hits)
            elif cmd == "raw" and cur:
                clear_screen()
                paged(cur.html.split("\n"), term_size()[1], color=False)
            elif cmd == "relogin":
                cookies.clear()
                if do_login():
                    navigate(cur.url if cur else START, push=False)
            elif cmd == "logout":
                cookies.clear()
                save_state()
                print("session forgotten")
            elif verb == "opts":
                cmd_opts()
            elif verb == "actions":
                cmd_actions(arg)
            elif verb in ("up", "u", "submit", "subs", "sub", "code"):
                if not cur or not parse_task(cur.url):
                    print(" open a task page first (.../tasks/<n>)")
                elif verb in ("up", "u", "submit"):
                    cmd_upload(cur, arg)
                elif verb == "subs":
                    cmd_subs(cur, arg)
                elif verb == "sub":
                    cmd_sub(cur, arg)
                else:
                    cmd_code(cur, arg)
            else:
                print("unknown command, '?' for help")
        except KeyboardInterrupt:
            print("\n(interrupted)")
        except Exception as e:
            print("error:", e)

if __name__ == "__main__":
    main()
