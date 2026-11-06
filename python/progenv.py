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

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
try:
    from elteui import *               # shared rendering / download / terminal helpers
except ImportError:
    sys.stderr.write("elteui.py must be in the same folder as this script\n")
    sys.exit(1)

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


# ------------------------------------------------------------ terminal


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


# ------------------------------------------------------------ html -> text


def render(url, html):
    """returns (body, menu, user, links, has_password_field, extra)"""
    p = HtmlText(url, urlparse(BASE).netloc)
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

def scan_page(page_url, anon=False, want=None):
    """fetch a page + its scripts. returns ([(id, name)], [ids found in chunks that mention `want`]).
    anon=True scans without cookies, so a still-valid session cannot redirect /login away."""
    saved = dict(cookies)
    if anon:
        cookies.clear()
    try:
        u, code, html = http(page_url)
        cand = re.findall(r'(?:src|href)="([^"]+\.js[^"]*)"', html)
        cand += ["/_next/" + m for m in re.findall(r'static/chunks/[^"\\\s]+?\.js', html)]
        urls, seen_u = [], set()
        for s_ in cand:
            a_ = urljoin(u, s_.replace("\\u0026", "&"))
            key = a_.split("?")[0]
            if urlparse(a_).netloc == urlparse(BASE).netloc and key not in seen_u:
                seen_u.add(key)
                urls.append(a_)
        found, seen, hinted = [], set(), []
        def add(items, hint=False):
            for aid, name in items:
                if aid not in seen:
                    seen.add(aid)
                    found.append((aid, name))
                if hint and aid not in hinted:
                    hinted.append(aid)
        add(scan_actions(html), want is not None and want in html)
        for a_ in urls[:120]:
            _, c, js = http(a_)
            if c == 200:
                add(scan_actions(js), want is not None and want in js)
        return found, hinted
    finally:
        if anon:
            cookies.clear()
            cookies.update(saved)
            save_state()

# ------------------------------------------------------------ login
def discover_action():
    """find the login server-action id in the login page's scripts (without cookies)"""
    found, hinted = scan_page(LOGIN, anon=True, want="authId")
    for aid, name in found:
        if re.search(r'login|signin|sign_in|authenticate', name, re.I):
            return aid
    if len(hinted) == 1:                      # the only action in a chunk that handles "authId"
        return hinted[0]
    if len(found) == 1:
        return found[0][0]
    return ""

def extract_action(text):
    m = re.search(r'(?i)next-action["\'\s:=]+([0-9a-f]{40,64})', text) or re.search(r'\b([0-9a-f]{40,64})\b', text)
    return m.group(1).lower() if m else ""

def get_login_action(force=False):
    """cached -> auto-detected -> asked once (validated). returns '' if the user gives up."""
    if not force:
        env = os.environ.get("PROGENV_ACTION", "")
        a = extract_action(env) if env else ""
        if a:
            return a
        if state.get("action"):
            return state["action"]
    print(" (looking for the login action id in the site's scripts...)")
    try:
        a = discover_action()
    except Exception as e:
        print(" scan failed:", e)
        a = ""
    if not a:
        print("\n Could not find the login action id automatically (one-time setup).")
        print(" In Firefox: F12 > Network > log in once > click the 'login' POST > Headers >")
        print(" Request Headers > copy the value of 'Next-Action'.")
        print(" (pasting the whole 'Copy as cURL' also works)")
        while not a:
            try:
                v = input_(" Next-Action (Enter = cancel): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return ""
            if not v:
                return ""
            a = extract_action(v)
            if not a:
                print(" that does not look like an action id (40+ hex characters like 7f3a9c...)")
    state["action"] = a
    save_state()
    return a

def norm_authid(s):
    s = s.strip()
    return s.upper() if re.match(r"^[A-Za-z0-9]{6}$", s) else s      # neptun codes are upper case

def clean_err(body, n=200):
    """short, readable version of an error response (HTML pages / stack traces / JSON / plain text)"""
    body = body or ""
    m = re.findall(r'"(?:message|error|errors?|detail)"\s*:\s*"([^"]{1,200})"', body)
    if m:
        return m[0]
    if re.search(r"(?i)<(!doctype|html|head|body)\b", body):
        t = re.search(r"(?is)<title[^>]*>(.*?)</title>", body) or re.search(r"(?is)<h1[^>]*>(.*?)</h1>", body)
        txt = re.sub(r"<[^>]+>", " ", t.group(1)) if t else re.sub(r"(?is)<(script|style).*?</\1>|<[^>]+>", " ", body)
        txt = re.sub(r"\s+", " ", txt).strip()
        return "(error page) " + fit(txt, n) if txt else "(error page)"
    st = re.search(r"(?m)^(.{1,%d}?)\s*(?:\n\s*at |\n\s*Stack trace|\n#0 )" % n, body)
    if st:
        return fit(st.group(1).strip(), n) + "  (stack trace hidden)"
    txt = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", body)).strip()
    return fit(txt, n)

def server_says(body):
    return clean_err(body, 160)

def do_login():
    """asks for credentials like a browser login form. returns True on success."""
    action = get_login_action()
    if not action:
        return False
    for attempt in range(3):
        d = state.get("authId", "")
        aid = norm_authid(input_("Name (authId)%s: " % (" [%s]" % d if d else "")).strip() or d)
        pw = getpass.getpass("Password: ")
        if not aid or not pw:
            print("both are required")
            continue
        for tryno in (0, 1):
            hdr = {"Content-Type": "text/plain;charset=UTF-8", "Accept": "text/x-component",
                   "Origin": BASE, "Referer": LOGIN, "Next-Action": action}
            old = {k: cookies.get(k) for k in ("access_token", "refresh_token")}
            cookies.pop("access_token", None)
            cookies.pop("refresh_token", None)
            _, code, body = http(LOGIN, data=json.dumps([{"authId": aid, "password": pw}]), headers=hdr)
            if cookies.get("access_token"):
                state["authId"], state["action"] = aid, action
                save_state()
                print("login ok")
                return True
            for k, v in old.items():
                if v:
                    cookies[k] = v
            if (code == 404 or "Server action not found" in body[:300]) and tryno == 0:
                print("the saved login action id is no longer valid (the site was updated).")
                state["action"] = ""
                save_state()
                action = get_login_action(force=True)
                if not action:
                    return False
                continue                      # retry right away with the same name/password
            break
        print("login rejected (HTTP %s): check name and password. the action id is kept." % code)
        msg = server_says(body)
        if msg:
            print("server says: " + msg)
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
            do_download(r, DL_DIR, url=u)
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
        state["found"] = [list(x) for x in scan_page(page_url)[0]]
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
        return None, "HTTP %s %s" % (code, err or clean_err(text))
    if not isinstance(data, list):
        return None, "unexpected response: " + clean_err(text)
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
# Content-Type the browser sends for the uploaded file (pinned so it does not depend on the system's mime.types)
EXT_MIME = {".cpp": "text/x-c++src", ".cc": "text/x-c++src", ".cxx": "text/x-c++src"}
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
    a = input_(" Language value [%s] (known: cs = C#, cpp = C++, pas = Pascal): " % default).strip() or default
    return a


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
        ("file", "1_file", fname, EXT_MIME.get(os.path.splitext(fname)[1].lower()) or mimetypes.guess_type(fname)[0] or "text/plain", content),
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
        print(" upload failed (HTTP %s): %s" % (code, fit("%s" % (err if err and err != "unrecognized response" else clean_err(text, 300)), 300)))
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
        print(" error: %s" % (err if err and err != "unrecognized response" else clean_err(text, 300)))
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
HINT_TASK = [" up = upload   subs = submissions   sub <n> = details   code <n> = source",
             " opts = options   actions = server-action ids"]

def draw(p):
    head = " %s  [%s]" % (p.url, p.code)
    if p.user:
        head += "   user: " + p.user
    hints = [HINT] + (HINT_TASK if parse_task(p.url) else [])
    show_screen([head], [p.body], hints, menu=p.menu)


HELP = [
    "### Navigation",
    "  <number>                  follow that link (files go to ~/progenv-downloads)",
    "  g <path|url>              go to a page, e.g. g /hu/courses (same host)",
    "  b                         back",
    "  r                         reload this page",
    "  / <text>                  search in the page",
    "  raw                       show the page's HTML",
    "  q                         quit",
    "",
    "### Session",
    "  relogin                   log in again",
    "  logout                    forget the saved session",
    "",
    "### On a task page",
    "  up [file]                 upload a solution (asks first, then watches)",
    "  subs                      list your submissions",
    "  subs opts                 same, sending the options (see opts)",
    "  sub [n]                   full result of submission n (default: latest)",
    "  code <n> [save]           show source of submission n (save = write to ./)",
    "",
    "### Settings",
    "  opts                      edit the option list sent by  subs opts",
    "  actions                   show the server-action ids",
    "  actions bind <role> <id>  set by hand (roles: submit, list, code)",
    "  actions rescan            forget them and scan the site again",
    "",
    "### Long pages",
    "  Enter                     next screen",
    "  a                         show everything",
    "  q                         stop paging",
]

def main():
    global cookies
    load_state()
    cookies = state["cookies"]
    args = sys.argv[1:]
    if args[:1] == ["--action"] and len(args) > 1:
        a = extract_action(" ".join(args[1:]))
        if not a:
            print("that does not look like an action id (40+ hex characters)")
            return
        state["action"] = a
        save_state()
        print("login action id saved")
    elif args[:1] == ["--forget-action"]:
        state["action"] = ""
        save_state()
        print("login action id forgotten (it will be detected again)")
        return
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
    except KeyboardInterrupt:
        print("\n(cancelled)")
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
