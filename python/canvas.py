#!/usr/bin/env python
# -*- coding: utf-8 -*-
# canvas: terminal client for https://canvas.elte.hu  (python 2.7 / 3, stdlib only)
# Logs in through the ELTE IdP (Neptun login, SAML) and keeps the session cookies in
# elteui/tmp/canvas/cookie, so the login is only asked for when the session has expired.
# Screens are built from the Canvas JSON API (/api/v1, session-cookie auth); any other
# page can be shown as text with  g <path>.
from __future__ import print_function
import os, sys, re, json, time, getpass, mimetypes
from collections import OrderedDict

try:
    import cookielib
    from urllib import urlencode
except ImportError:
    import http.cookiejar as cookielib
    from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
try:
    from elteui import *               # shared rendering / download / terminal helpers
except ImportError:
    sys.stderr.write("the elteui/ folder must be next to this script\n")
    sys.exit(1)

BASE = os.environ.get("CANVAS_BASE", "https://canvas.elte.hu").rstrip("/")
START = BASE + "/belepes/"
HOST = urlparse(BASE).netloc
# internal files live in  elteui/tmp/canvas/{cookie,cfg,log}  (old ~/.canvas_* files are moved in once)
SITE = Site("canvas", env={"cookie": "CANVAS_COOKIES"},
            legacy={"cookie": "~/.canvas_cookies", "cfg": "~/.canvas_client.json", "log": "~/.canvas_last.log"})
DL_DIR = os.path.expanduser(os.environ.get("CANVAS_DL", "~/canvas-downloads"))
UA = "Mozilla/5.0 (X11; Linux x86_64) canvas-cli"

CFG = {"user": ""}
S = {"me": None, "cur": None, "hist": []}


class ApiError(Exception):
    pass

class LoginError(ApiError):
    pass


# print(..., e) crashes (UnicodeEncodeError/UnicodeDecodeError) the moment an exception's text
# has a non-ASCII character -- a Hungarian IdP error page, an accented file name, etc. -- because
# print() str()s its arguments with the ascii codec when stdout isn't a utf-8 tty (redirected to
# a file/pipe, some remote/AFS terminals, python 2 generally). w() (elteui.render) already encodes
# safely for the screen, so every print() in this file is routed through it instead.
_print = print
def print(*args, **kw):
    try:
        _print(*args, **kw)
    except (UnicodeEncodeError, UnicodeDecodeError):
        sep, end = kw.get("sep", " "), kw.get("end", "\n")
        parts = []
        for a in args:
            try:
                parts.append(u"%s" % a)
            except Exception:
                parts.append(a.decode("utf-8", "replace") if isinstance(a, bytes) else repr(a))
        w(sep.join(parts) + end)


# ------------------------------------------------------------ config / cookies / log  (all in SITE)
def load_cfg():
    CFG.update(SITE.read_json("cfg"))

def save_cfg():
    try:
        SITE.write_json("cfg", CFG)
    except Exception:
        pass

log = SITE.log

JAR = cookielib.LWPCookieJar(SITE.path("cookie"))
OPENER = ur.build_opener(ur.HTTPCookieProcessor(JAR))

def load_jar():
    try:
        JAR.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        pass

def save_jar():
    try:
        SITE.touch("cookie")
        JAR.save(ignore_discard=True, ignore_expires=True)
    except Exception:
        pass

def csrf():
    for c in JAR:
        if c.name == "_csrf_token" and HOST.split(":")[0] in (c.domain or ""):
            return unquote(c.value)
    return ""


# ------------------------------------------------------------ http
def open_url(url, data=None, headers=None, method=None, timeout=60):
    """one request, redirects followed, cookies kept. returns the open response (also for 4xx/5xx)"""
    req = ur.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Language", "en,hu;q=0.8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.data = B(data)
    if method:
        req.get_method = lambda m=method: m
    try:
        r = OPENER.open(req, timeout=timeout)
    except ur.HTTPError as e:
        r = e
    except Exception as e:
        raise ApiError("network error: %s" % e)
    save_jar()
    return r

def body_of(r):
    return r.read().decode("utf-8", "replace")

def form_body(pairs):
    return B(urlencode([(B(k), B(v)) for k, v in pairs]))


# ------------------------------------------------------------ html pages (login flow)
class Page(HTMLParser):
    """collects forms, links, meta refresh and scripts of a page"""
    def __init__(self):
        HTMLParser.__init__(self)
        self.forms, self.links, self.scripts, self.refresh = [], [], [], None
        self._form = self._a = None
        self._script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._form = {"action": a.get("action") or "", "method": (a.get("method") or "get").lower(), "fields": []}
        elif tag in ("input", "button", "select", "textarea") and self._form is not None and a.get("name"):
            self._form["fields"].append((a["name"], a.get("value") or "", (a.get("type") or "text").lower()))
        elif tag == "a":
            self._a = [a.get("href") or "", ""]
        elif tag == "meta" and (a.get("http-equiv") or "").lower() == "refresh":
            m = re.search(r"url\s*=\s*['\"]?([^'\"]+)", a.get("content") or "", re.I)
            if m:
                self.refresh = m.group(1).strip()
        elif tag == "script":
            self._script = True
            self.scripts.append("")

    def handle_endtag(self, tag):
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None
        elif tag == "a" and self._a is not None:
            self.links.append((self._a[0], self._a[1].strip()))
            self._a = None
        elif tag == "script":
            self._script = False

    def handle_data(self, d):
        if self._script and self.scripts:
            self.scripts[-1] += d
        if self._a is not None:
            self._a[1] += d

def snap(r):
    """(final_url, html, parsed Page) of a response"""
    url, html, pg = r.geturl(), body_of(r), Page()
    try:
        pg.feed(html)
        pg.close()
    except Exception:
        pass
    return url, html, pg

def host_of(url):
    return urlparse(url).netloc

SKIP_TYPES = ("submit", "button", "image", "reset", "file", "checkbox", "radio")
TEXT_TYPES = ("text", "password", "email", "tel", "search", "number")

def submit_form(url, form, extra=None):
    pairs = OrderedDict()
    for n, v, t in form["fields"]:
        if t not in SKIP_TYPES:
            pairs[n] = v
    for k, v in (extra or {}).items():
        pairs[k] = v
    action = urljoin(url, form["action"]) if form["action"] else url
    hd = {"Referer": url}
    if form["method"] == "post":
        hd["Origin"] = "%s://%s" % (urlparse(url).scheme, host_of(url))
        hd["Content-Type"] = "application/x-www-form-urlencoded"
        return open_url(action, form_body(list(pairs.items())), hd)
    q = urlencode([(B(k), B(v)) for k, v in pairs.items()])
    return open_url(action + ("&" if "?" in action else "?") + q, headers=hd)

def auto_step(url, pg):
    """the page redirects/submits itself (SAML post binding, meta refresh, js redirect): do what a browser would"""
    for f in pg.forms:
        if f["fields"] and not any(t in TEXT_TYPES for _, _, t in f["fields"]):
            return submit_form(url, f)
    target = pg.refresh
    if not target:
        for sc in pg.scripts:
            m = re.search(r"location(?:\.href|\.replace\()?\s*=?\s*\(?\s*[\"']([^\"']+)[\"']", sc)
            if m:
                target = m.group(1).replace("\\/", "/")
                break
    if target:
        return open_url(urljoin(url, target), headers={"Referer": url})
    return None

def page_text(url, html):
    p = HtmlText(url)
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    return [l.strip() for l in "".join(p.out).split("\n") if l.strip()]

def login_error_msg(url, html):
    lines = page_text(url, html)
    hits = [l for l in lines if re.search(r"(?i)error|invalid|incorrect|wrong|failed|denied|hib|helytelen|sikertelen|nem sikerült", l)]
    return fit(" | ".join((hits or ["wrong username or password?"])[:3]), 200)


# ------------------------------------------------------------ login (ELTE IdP / Neptun, SAML)
def find_login_link(pg):
    best = None
    for href, text in pg.links:
        if not href or href.startswith(("#", "javascript", "mailto")):
            continue
        if "saml" in href.lower():
            return href
        if best is None and (re.search(r"/login(/|\?|$)", href) and "canvas" not in href.lower()
                             or re.search(r"(?i)log ?in|belép|bejelentkez", text)):
            best = href
    return best

def find_login_form(pg):
    for f in pg.forms:
        if any(n == "username_neptun" for n, _, _ in f["fields"]):
            return f
    return None

def reach_login_form():
    """canvas /belepes/ -> 'log in' -> IdP authpage. returns (page_url, form)"""
    r = open_url(START)
    url, html, pg = snap(r)
    link = find_login_link(pg)
    r = open_url(urljoin(url, link) if link else BASE + "/login/saml", headers={"Referer": url})
    url, html, pg = snap(r)
    for _ in range(6):
        f = find_login_form(pg)
        if f:
            return url, f
        nxt = auto_step(url, pg) if host_of(url) != HOST else None
        if nxt is None:
            break
        url, html, pg = snap(nxt)
    if host_of(url) != HOST and "LoginType" not in url:        # IdP wants the login type in the query string
        url2 = url + ("&" if "?" in url else "?") + "LoginType=neptun&sp_language=en"
        url, html, pg = snap(open_url(url2, headers={"Referer": url}))
        f = find_login_form(pg)
        if f:
            return url, f
    raise LoginError("could not find the Neptun login form (stopped at %s)" % fit(url, 100))

def follow(r):
    """follow the IdP's chain (resume.php redirector page, SAMLResponse auto-post) until we are back on canvas"""
    for _ in range(14):
        url, html, pg = snap(r)
        if host_of(url) == HOST:
            return url, html
        if any(t == "password" for f in pg.forms for _, _, t in f["fields"]):
            raise LoginError(login_error_msg(url, html))
        nxt = auto_step(url, pg)
        if nxt is None:
            raise LoginError("login flow stopped at %s: %s" % (fit(url, 80), fit(" ".join(page_text(url, html)[:3]), 100)))
        r = nxt
    raise LoginError("too many redirects during login")

def credentials():
    u, p = os.environ.get("CANVAS_USER"), os.environ.get("CANVAS_PASS")
    if u and p:
        return u.upper(), p, True
    if not sys.stdin.isatty():
        raise LoginError("no CANVAS_USER/CANVAS_PASS set and no terminal to ask on")
    d = CFG.get("user", "").upper()
    u = (input_("Neptun code%s: " % (" [%s]" % d if d else "")).strip() or d).upper()
    p = getpass.getpass("Neptun password: ")
    return u, p, False

def do_login():
    for attempt in range(3):
        u, p, from_env = credentials()
        if not u or not p:
            print("both are required")
            continue
        JAR.clear()
        try:
            url, form = reach_login_form()
            r = submit_form(url, form, {"username_neptun": u, "password_neptun": p, "source": "neptun"})
            follow(r)
            me = whoami()
            if not me:
                raise LoginError("the IdP accepted the login but canvas gave no session")
        except LoginError as e:
            print("login failed: %s" % e)
            if from_env:
                break
            continue
        CFG["user"] = u
        save_cfg()
        S["me"] = me
        print("login ok: %s" % (me.get("name") or u))
        return True
    raise LoginError("not logged in")

def whoami():
    try:
        d = api("/api/v1/users/self/profile", retry=False)
        return d if isinstance(d, dict) else None
    except ApiError:
        return None


# ------------------------------------------------------------ api
def err_text(code, text):
    try:
        o = json.loads(text)
        if isinstance(o, dict):
            if o.get("errors"):
                e = o["errors"]
                if isinstance(e, list):
                    return "HTTP %s: %s" % (code, "; ".join(x.get("message", "%s" % x) if isinstance(x, dict) else "%s" % x for x in e))
                return "HTTP %s: %s" % (code, e)
            for k in ("message", "error", "status"):
                if isinstance(o.get(k), string_types) and o[k]:
                    return "HTTP %s: %s" % (code, o[k])
    except ValueError:
        pass
    return "HTTP %s: %s" % (code, text.strip()[:300])

def api(path, method="GET", fields=None, paged=True, retry=True, headers=None, raw=None):
    """json call (relative to BASE). lists are followed over all pages. one automatic re-login on 401.
    fields = [(name, value)] form fields for POST/PUT. raw = (body, content-type)."""
    url = path if path.startswith("http") else BASE + path
    out, pages = None, 0
    while url and pages < 30:
        pages += 1
        hd = {"Accept": "application/json"}
        data = None
        if method != "GET":
            hd["X-CSRF-Token"] = csrf()
            if raw:
                data, hd["Content-Type"] = raw
            else:
                data = form_body(fields or [])
                hd["Content-Type"] = "application/x-www-form-urlencoded"
        hd.update(headers or {})
        r = open_url(url, data, hd, method if method != "GET" else None)
        code = r.getcode()
        text = body_of(r)
        log("%s %s -> %s %s" % (method, url, code, text[:3000].replace("\n", "\\n")))
        if code == 401 or text.lstrip()[:1] == "<":            # html login page, or 401
            if retry and whoami() is None:                      # really logged out: log in once more
                print("(session expired, logging in again)")
                do_login()
                return api(path, method, fields, paged, False, headers, raw)
            if code == 401 and text.lstrip()[:1] != "<":
                raise ApiError("no access to this item (HTTP 401)")
            raise ApiError("not logged in")
        if code >= 400:
            raise ApiError(err_text(code, text))
        text = re.sub(r"^\s*while\(1\);", "", text)
        try:
            data = json.loads(text, object_pairs_hook=OrderedDict) if text.strip() else None
        except ValueError:
            raise ApiError("unreadable response: %s" % fit(text, 200))
        if not (paged and isinstance(data, list)):
            return data
        out = (out or []) + data
        m = re.search(r'<([^>]+)>;\s*rel="next"', r.info().get("Link", "") or "")
        url = m.group(1) if m else None
    return out if out is not None else []


# ------------------------------------------------------------ downloads
def download_url(url, hint=""):
    r = open_url(url)
    code = r.getcode()
    if code >= 400:
        raise ApiError(err_text(code, body_of(r)))
    ct = hdr_get(r, "Content-Type").split(";")[0].strip().lower()
    if ct == "text/html" and "attachment" not in hdr_get(r, "Content-Disposition").lower():
        raise ApiError("got a web page instead of a file (no permission, or the session expired)")
    do_download(r, DL_DIR, hint, url)

def download_link(url, fid):
    """a /files/<id> page link carries the course and verifier, so fetch <link>/download first"""
    p = urlparse(url)
    path = p.path.rstrip("/")
    if not path.endswith("/download"):
        path += "/download"
    dl = "%s://%s%s%s" % (p.scheme or "https", p.netloc or HOST, path, ("?" + p.query) if p.query else "")
    try:
        download_url(dl)
    except ApiError as e:
        log("link download failed (%s), trying the API" % e)
        download_file(fid)

def download_file(fid):
    f = api("/api/v1/files/%s" % fid) or {}
    if not f.get("url"):
        raise ApiError("file %s has no download url (locked?)" % fid)
    download_url(f["url"], f.get("display_name") or f.get("filename") or "")


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

def add_html(s, html, base):
    """render html into screen s; same-site links become numbered items"""
    p = HtmlText(base)
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        pass
    text = re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip()
    off = len(s.items)
    for u in p.links:
        s.item("link", u)
    text = re.sub(r"\[(\d+)\] ", lambda m: "[%d] " % (int(m.group(1)) + off), text)
    s.add(*[l.rstrip() for l in text.split("\n")])

def route(url):
    """a canvas url -> (screen func, args), or does a download and returns None"""
    path = urlparse(url).path
    m = re.search(r"/courses/(\d+)/assignments/(\d+)", path)
    if m:
        return scr_assignment, (m.group(1), m.group(2))
    m = re.search(r"/courses/(\d+)/quizzes/(\d+)", path)
    if m:
        return scr_quiz, (m.group(1), m.group(2))
    m = re.search(r"/files/(\d+)", path)
    if m:
        download_link(url, m.group(1))
        return None
    m = re.search(r"/courses/(\d+)/pages/([^/]+)", path)
    if m:
        return scr_page, (m.group(1), m.group(2))
    m = re.search(r"^/courses/(\d+)/?$", path)
    if m:
        return scr_course, (m.group(1),)
    return scr_web, (url,)

def me_name():
    me = S["me"] or {}
    lid = me.get("login_id") or ""
    return " ".join(x for x in (me.get("name"), ("(%s)" % lid) if lid and lid not in (me.get("name") or "") else "") if x)

def sub_status(a):
    types = a.get("submission_types") or []
    sub = a.get("submission") or {}
    if not types or types == ["none"] or types == ["not_graded"]:
        return "-"
    ws = sub.get("workflow_state")
    if sub.get("excused"):
        return "EXCUSED"
    if sub.get("missing"):
        return "MISSING"
    st = {"unsubmitted": "NOT SUBMITTED", "submitted": "SUBMITTED", "graded": "GRADED",
          "pending_review": "PENDING"}.get(ws, (ws or "NOT SUBMITTED").upper())
    return st + (" LATE" if sub.get("late") else "")

def grade_text(a):
    sub = a.get("submission") or {}
    if sub.get("score") is None and not sub.get("grade"):
        return ("(%s pts)" % fmtval(a.get("points_possible"))) if a.get("points_possible") else ""
    return "grade %s/%s" % (fmtval(sub.get("score")), fmtval(a.get("points_possible")))

def when_text(a):
    now = utcnow()
    unl, lock, due = parse_dt(a.get("unlock_at")), parse_dt(a.get("lock_at")), parse_dt(a.get("due_at"))
    if unl and now < unl[0]:
        return "UPCOMING", "from " + fmt_dt(a["unlock_at"], True)
    if lock and now > lock[0]:
        return "CLOSED", "closed " + fmt_dt(a["lock_at"], True)
    if due:
        return "OPEN", "due %s %s" % (fmt_dt(a["due_at"], True), rel(a["due_at"]))
    return "OPEN", "no due date"

def scr_home(old=False):
    q = "enrollment_state=%s&include[]=term&include[]=total_scores&include[]=teachers&per_page=100" % ("completed" if old else "active")
    courses = [c for c in (api("/api/v1/courses?" + q) or []) if isinstance(c, dict) and c.get("name")]
    s = Screen("Home", "home")
    s.data = courses
    s.add("## %s courses      (all = past courses)" % ("Past" if old else "Active"))
    if not courses:
        s.add("", "no courses")
    for c in courses:
        n = s.item("screen", scr_course, c.get("id"))
        enr = (c.get("enrollments") or [{}])[0]
        gr = enr.get("computed_current_score")
        extra = "   ".join(x for x in (
            (c.get("term") or {}).get("name") or "",
            ("grade %s%%" % gr) if gr is not None else "",
            ("tutor: " + ", ".join(t.get("display_name", "") for t in c["teachers"][:2])) if c.get("teachers") else "") if x)
        s.add("", "%s %s  -  %s" % (n, c.get("course_code") or "", c.get("name")))
        if extra:
            s.add("      " + extra)
    if not old:
        try:
            todo = api("/api/v1/users/self/todo?per_page=20", paged=False) or []
        except ApiError:
            todo = []
        if todo:
            s.add("", "### To do")
            for t in todo[:12]:
                a = t.get("assignment") or {}
                if not a.get("id"):
                    continue
                n = s.item("screen", scr_assignment, t.get("course_id") or a.get("course_id"), a.get("id"))
                s.add(" %s %-40s %s" % (n, fit(a.get("name", "?"), 40), "due " + fmt_dt(a["due_at"], True) + " " + rel(a["due_at"]) if a.get("due_at") else ""))
    return s

def scr_course(cid):
    c = api("/api/v1/courses/%s?include[]=syllabus_body&include[]=term&include[]=teachers&include[]=total_scores" % cid) or {}
    s = Screen("Course", "course")
    s.data = c
    s.ctx["cid"] = cid
    s.add("## %s  -  %s" % (c.get("course_code") or "", c.get("name") or "?"))
    if c.get("teachers"):
        s.add("Teachers: " + ", ".join(t.get("display_name", "") for t in c["teachers"]))
    if (c.get("term") or {}).get("name"):
        s.add("Term:     " + c["term"]["name"])
    enr = (c.get("enrollments") or [{}])[0]
    if enr.get("computed_current_score") is not None:
        s.add("Grade:    %s%%  %s" % (enr["computed_current_score"], enr.get("computed_current_grade") or ""))
    s.add("")
    for label, func in (("assignments", scr_assignments), ("quizzes", scr_quizzes), ("announcements", scr_announcements),
                        ("modules", scr_modules), ("files", scr_files), ("pages", scr_pages)):
        s.add(" %s %s" % (s.item("screen", func, cid), label))
    if c.get("syllabus_body"):
        s.add(" %s syllabus" % s.item("screen", scr_html, "Syllabus", c["syllabus_body"], BASE + "/courses/%s/assignments/syllabus" % cid, cid))
    return s

def scr_assignments(cid):
    lst = api("/api/v1/courses/%s/assignments?include[]=submission&order_by=due_at&per_page=100" % cid) or []
    s = Screen("Assignments", "list")
    s.data = lst
    s.ctx["cid"] = cid
    s.add("## Assignments (%d)" % len(lst), "")
    namew = min(40, max([len(a.get("name", "")) for a in lst] + [10]))
    for a in lst:
        st, when = when_text(a)
        n = s.item("screen", scr_assignment, cid, a.get("id"))
        s.add(" %s %-*s  %-8s %-26s  %s %s" % (n, namew, fit(a.get("name", "?"), namew), st, when, sub_status(a), grade_text(a)))
    return s

def scr_assignment(cid, aid):
    a = api("/api/v1/courses/%s/assignments/%s" % (cid, aid)) or {}
    sub = None
    try:
        sub = api("/api/v1/courses/%s/assignments/%s/submissions/self?include[]=submission_comments" % (cid, aid))
    except ApiError:
        pass
    a["submission"] = sub or {}
    width = max(20, term_size()[0] - 1)
    s = Screen("Assignment", "task")
    s.data = a
    s.ctx.update({"cid": cid, "assignment": a})
    st, when = when_text(a)
    s.add("## %s" % a.get("name", "?"))
    s.add("%s   %s" % (st, when))
    facts = []
    if a.get("unlock_at"):
        facts.append("available %s" % fmt_dt(a["unlock_at"]))
    if a.get("due_at"):
        facts.append("due %s" % fmt_dt(a["due_at"]))
    if a.get("lock_at"):
        facts.append("locks %s" % fmt_dt(a["lock_at"]))
    if facts:
        s.add("   ".join(facts))
    info = ["%s points" % fmtval(a.get("points_possible")), "types: " + ", ".join(a.get("submission_types") or ["-"])]
    if a.get("allowed_extensions"):
        info.append("extensions: " + ", ".join(a["allowed_extensions"]))
    if a.get("allowed_attempts") and a["allowed_attempts"] > 0:
        info.append("attempts: %s" % a["allowed_attempts"])
    s.add("Info: " + ", ".join(info))
    if a.get("locked_for_user"):
        s.add("      LOCKED: %s" % md_to_text(a.get("lock_explanation") or ""))
    s.add("", "### My submission")
    if sub and sub.get("workflow_state") not in (None, "unsubmitted"):
        s.add("  %-10s %s" % ("Status", sub_status(a)), "  %-10s %s" % ("Grade", grade_text(a) or "-"),
              "  %-10s %s" % ("Submitted", fmt_dt(sub.get("submitted_at"))), "  %-10s %s" % ("Attempt", fmtval(sub.get("attempt"))))
        if sub.get("body"):
            s.add("  Text:")
            s.add(*["    " + l for l in wrap(md_to_text(sub["body"]), width - 4)])
        for f in sub.get("attachments") or []:
            n = s.item("dl", f.get("url"), f.get("display_name") or "")
            s.add("  %s %s   (%s)" % (n, f.get("display_name"), hsize(f.get("size") or 0)))
        for cm in sub.get("submission_comments") or []:
            s.add("", "  Comment  %s  %s" % (fmt_dt(cm.get("created_at")), cm.get("author_name", "?")))
            s.add(*["    " + l for l in wrap(md_to_text(cm.get("comment") or ""), width - 4)])
    else:
        s.add("  NOT SUBMITTED yet" + ("   (up <file> to upload)" if "online_upload" in (a.get("submission_types") or []) else ""))
    if a.get("description"):
        s.add("", "### Description")
        add_html(s, a["description"], BASE + "/courses/%s/assignments/%s" % (cid, aid))
    return s

def scr_announcements(cid):
    lst = api("/api/v1/announcements?context_codes[]=course_%s&per_page=50" % cid) or []
    s = Screen("Announcements", "list")
    s.data = lst
    s.ctx["cid"] = cid
    s.add("## Announcements (%d)" % len(lst), "")
    for x in lst:
        n = s.item("screen", scr_html, x.get("title") or "?", x.get("message") or "", BASE + "/courses/%s/" % cid, cid,
                   ["%s  %s" % (fmt_dt(x.get("posted_at")), x.get("user_name") or "")])
        s.add(" %s %s  %s" % (n, fmt_dt(x.get("posted_at"), True), x.get("title")))
    return s

def scr_html(title, html, base, cid=None, meta=()):
    s = Screen(title, "text")
    s.data = html
    s.ctx["cid"] = cid
    s.add("## " + title)
    s.add(*meta)
    s.add("")
    add_html(s, html, base)
    return s

def scr_modules(cid):
    mods = api("/api/v1/courses/%s/modules?include[]=items&per_page=100" % cid) or []
    s = Screen("Modules", "list")
    s.data = mods
    s.ctx["cid"] = cid
    s.add("## Modules (%d)" % len(mods))
    for m in mods:
        s.add("", "### %s%s" % (m.get("name"), "   (locked)" if m.get("state") == "locked" else ""))
        items = m.get("items")
        if items is None and m.get("items_url"):
            items = api(m["items_url"] + ("&" if "?" in m["items_url"] else "?") + "per_page=100") or []
        for it in items or []:
            pad = "  " * (it.get("indent") or 0)
            t = it.get("type")
            if t == "SubHeader":
                s.add("   " + pad + "-- %s --" % it.get("title"))
            elif t == "File":
                s.add("   %s%s %s  (file)" % (pad, s.item("file", it.get("content_id")), it.get("title")))
            elif t == "Page":
                s.add("   %s%s %s  (page)" % (pad, s.item("screen", scr_page, cid, it.get("page_url")), it.get("title")))
            elif t == "Assignment":
                s.add("   %s%s %s" % (pad, s.item("screen", scr_assignment, cid, it.get("content_id")), it.get("title")))
            elif t == "ExternalUrl":
                s.add("   %s%s %s  -> %s" % (pad, s.item("noop"), it.get("title"), it.get("external_url")))
            else:
                s.add("   %s%s %s  (%s)" % (pad, s.item("link", it.get("html_url") or ""), it.get("title"), (t or "?").lower()))
    return s

def scr_page(cid, url):
    p = api("/api/v1/courses/%s/pages/%s" % (cid, url)) or {}
    return scr_html(p.get("title") or url, p.get("body") or "", BASE + "/courses/%s/pages/%s" % (cid, url), cid,
                    ["updated " + fmt_dt(p.get("updated_at"))])

def scr_pages(cid):
    lst = api("/api/v1/courses/%s/pages?sort=title&per_page=100" % cid) or []
    s = Screen("Pages", "list")
    s.data = lst
    s.ctx["cid"] = cid
    s.add("## Pages (%d)" % len(lst), "")
    for p in lst:
        s.add(" %s %s" % (s.item("screen", scr_page, cid, p.get("url")), p.get("title")))
    return s

def scr_files(cid):
    files = api("/api/v1/courses/%s/files?sort=name&per_page=100" % cid) or []
    try:
        folders = dict((f.get("id"), f.get("full_name") or "") for f in api("/api/v1/courses/%s/folders?per_page=100" % cid) or [])
    except ApiError:
        folders = {}
    s = Screen("Files", "list")
    s.data = files
    s.ctx["cid"] = cid
    s.add("## Files (%d)" % len(files), "")
    files.sort(key=lambda f: (folders.get(f.get("folder_id"), ""), f.get("display_name", "").lower()))
    cur = None
    for f in files:
        fo = re.sub(r"^course files/?", "", folders.get(f.get("folder_id"), ""))
        if fo != cur:
            cur = fo
            s.add("### /" + fo)
        s.add(" %s %-44s %9s  %s" % (s.item("dl", f.get("url"), f.get("display_name") or ""), fit(f.get("display_name", "?"), 44),
                                     hsize(f.get("size") or 0), fmt_dt(f.get("updated_at"), True)))
    return s

def scr_inbox():
    lst = api("/api/v1/conversations?per_page=40", paged=False) or []
    s = Screen("Inbox", "list")
    s.data = lst
    s.add("## Inbox (%d)" % len(lst), "")
    for c in lst:
        who = ", ".join(p.get("name", "") for p in (c.get("participants") or [])[:3])
        s.add(" %s %s %-30s %s" % (s.item("screen", scr_conv, c.get("id")), fmt_dt(c.get("last_message_at"), True),
                                   fit(c.get("subject") or "(no subject)", 30), fit(who, 30)))
    return s

def scr_conv(cid):
    c = api("/api/v1/conversations/%s" % cid) or {}
    names = dict((p.get("id"), p.get("name")) for p in c.get("participants") or [])
    s = Screen("Message", "text")
    s.data = c
    s.add("## %s" % (c.get("subject") or "(no subject)"))
    for m in reversed(c.get("messages") or []):
        s.add("", "### %s  %s" % (names.get(m.get("author_id"), "?"), fmt_dt(m.get("created_at"))))
        s.add(*md_to_text(m.get("body") or "").split("\n"))
    return s

def scr_web(url):
    r = open_url(url)
    code = r.getcode()
    ct = hdr_get(r, "Content-Type").split(";")[0].strip().lower()
    if code < 400 and ct and ct != "text/html" and ("json" not in ct):
        do_download(r, DL_DIR, "download", url)
        return None
    final, html = r.geturl(), body_of(r)
    if code >= 400:
        raise ApiError("HTTP %s for %s" % (code, final))
    m = re.search(r"(?is)<title>(.*?)</title>", html)
    s = Screen(re.sub(r"\s+", " ", m.group(1)).strip() if m else final, "text")
    s.data = html
    s.add("## " + s.title, final, "")
    add_html(s, html, final)
    return s

def scr_static(title, data):
    s = Screen(title, "json")
    s.data = data
    s.add("## " + title, "")
    s.add(*json_lines(data, max(20, term_size()[0] - 1)))
    return s

def scr_json(title, path):
    return scr_static(title, api(path))


# ------------------------------------------------------------ upload
def upload_file_get_id(preflight_path, path):
    """steps 1+2 of canvas's 3-step upload dance: ask canvas where to put the file, then send
    it there, and hand back the resulting file id. shared by assignment submissions and quiz
    file-upload questions -- only the preflight path and what you do with the id differ."""
    fname = os.path.basename(path)
    size = os.path.getsize(path)
    ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    pre = api(preflight_path, "POST", [("name", fname), ("size", "%d" % size), ("content_type", ctype)])
    if not isinstance(pre, dict) or not pre.get("upload_url"):
        raise ApiError("unexpected preflight response: %s" % fit("%s" % pre, 200))
    with open(path, "rb") as f:
        content = f.read()
    items = [("field", k, v) for k, v in (pre.get("upload_params") or {}).items()]
    items.append(("file", pre.get("file_param") or "file", fname, ctype, content))
    body, ct = multipart(items)
    r = open_url(pre["upload_url"], body, {"Content-Type": ct}, timeout=300)
    text = body_of(r)
    log("upload -> %s %s" % (r.getcode(), text[:1500]))
    if r.getcode() >= 400:
        raise ApiError("upload failed: " + err_text(r.getcode(), text))
    try:
        fid = json.loads(re.sub(r"^\s*while\(1\);", "", text)).get("id")
    except Exception:
        fid = None
    if not fid:
        raise ApiError("upload step did not return a file id: %s" % fit(text, 200))
    return fid


def cmd_upload(cur, arg):
    """up <file[,file2,...]>  -- online_upload submission, one or several files at once"""
    a = cur.ctx.get("assignment") if cur else None
    cid = cur.ctx.get("cid") if cur else None
    if not a:
        print(" open an assignment first")
        return
    raw = arg.strip() or ask_path(" File to upload (Tab completes; comma-separate for several): ")
    paths = [os.path.expanduser(p.strip().strip("'\"")) for p in raw.split(",")]
    paths = [p for p in paths if p]
    bad = [p for p in paths if not os.path.isfile(p)]
    if not paths or bad:
        print(" not a file: %s" % (bad[0] if bad else raw))
        return
    desc = ", ".join(os.path.basename(p) for p in paths)
    st, when = when_text(a)
    rule = "-" * max(20, term_size()[0] - 1)
    print("\n" + rule)
    print(" Upload  %s  (%s)" % (desc, hsize(sum(os.path.getsize(p) for p in paths))))
    print(" Task    %s" % a.get("name"))
    print(" State   %s   %s" % (st, when))
    if "online_upload" not in (a.get("submission_types") or []):
        print(" WARNING this assignment does not accept file uploads (%s)" % ", ".join(a.get("submission_types") or []))
    ext = [e.lower() for e in a.get("allowed_extensions") or []]
    if ext:
        unmatched = [os.path.basename(p) for p in paths if os.path.splitext(p)[1].lstrip(".").lower() not in ext]
        if unmatched:
            print(" WARNING allowed extensions: %s  (not matched: %s)" % (", ".join(ext), ", ".join(unmatched)))
    if st in ("CLOSED", "UPCOMING"):
        print(" WARNING the assignment is %s; the server will probably reject this" % st)
    if (a.get("submission") or {}).get("workflow_state") not in (None, "unsubmitted"):
        print(" NOTE    you already have a submission; this creates a new attempt")
    print(rule)
    if not yes(" Upload now? [y/N] "):
        print(" cancelled")
        return
    base = "/api/v1/courses/%s/assignments/%s/submissions" % (cid, a.get("id"))
    try:
        fids = [upload_file_get_id(base + "/self/files", p) for p in paths]
    except ApiError as e:
        print(" %s" % e)
        return
    fields = [("submission[submission_type]", "online_upload")] + [("submission[file_ids][]", "%s" % f) for f in fids]
    sub = api(base, "POST", fields)
    print(" uploaded.")
    if isinstance(sub, dict):
        print("  status %s, attempt %s, submitted %s" % (sub.get("workflow_state"), sub.get("attempt"), fmt_dt(sub.get("submitted_at"))))
    go(scr_assignment, (cid, a.get("id")), push=False)


def cmd_submit_text(cur, arg):
    """uptext [text]  -- online_text_entry submission; with no text, opens a multi-line prompt"""
    a = cur.ctx.get("assignment") if cur else None
    cid = cur.ctx.get("cid") if cur else None
    if not a:
        print(" open an assignment first")
        return
    if "online_text_entry" not in (a.get("submission_types") or []):
        print(" WARNING this assignment does not list online_text_entry (%s) -- trying anyway" % ", ".join(a.get("submission_types") or []))
    body = arg.strip()
    if not body:
        print(" Type the submission text below, end with a single '.' on its own line:")
        lines = []
        while True:
            try:
                ln = input_()
            except (EOFError, KeyboardInterrupt):
                break
            if ln.strip() == ".":
                break
            lines.append(ln)
        body = "\n".join(lines)
    if not body.strip():
        print(" empty, cancelled")
        return
    st, when = when_text(a)
    print(" Task    %s" % a.get("name"))
    print(" State   %s   %s" % (st, when))
    if not yes(" Submit this text (%d chars) now? [y/N] " % len(body)):
        print(" cancelled")
        return
    base = "/api/v1/courses/%s/assignments/%s/submissions" % (cid, a.get("id"))
    sub = api(base, "POST", [("submission[submission_type]", "online_text_entry"), ("submission[body]", body)])
    print(" submitted.")
    if isinstance(sub, dict):
        print("  status %s, attempt %s" % (sub.get("workflow_state"), sub.get("attempt")))
    go(scr_assignment, (cid, a.get("id")), push=False)


def cmd_submit_url(cur, arg):
    """upurl <url>  -- online_url submission"""
    a = cur.ctx.get("assignment") if cur else None
    cid = cur.ctx.get("cid") if cur else None
    if not a:
        print(" open an assignment first")
        return
    url = arg.strip() or input_(" URL to submit: ").strip()
    if not url:
        print(" cancelled")
        return
    if "online_url" not in (a.get("submission_types") or []):
        print(" WARNING this assignment does not list online_url (%s) -- trying anyway" % ", ".join(a.get("submission_types") or []))
    if not yes(" Submit %s now? [y/N] " % url):
        print(" cancelled")
        return
    base = "/api/v1/courses/%s/assignments/%s/submissions" % (cid, a.get("id"))
    sub = api(base, "POST", [("submission[submission_type]", "online_url"), ("submission[url]", url)])
    print(" submitted.")
    if isinstance(sub, dict):
        print("  status %s, attempt %s" % (sub.get("workflow_state"), sub.get("attempt")))
    go(scr_assignment, (cid, a.get("id")), push=False)


# ------------------------------------------------------------ quizzes (classic quiz API)
def quiz_status(q):
    if q.get("locked_for_user"):
        return "LOCKED"
    return (q.get("quiz_type") or "quiz").replace("_", " ")

def scr_quizzes(cid):
    lst = api("/api/v1/courses/%s/quizzes?per_page=100" % cid) or []
    s = Screen("Quizzes", "list")
    s.data = lst
    s.ctx["cid"] = cid
    s.add("## Quizzes (%d)" % len(lst), "")
    namew = min(40, max([len(q.get("title", "")) for q in lst] + [10]))
    for q in lst:
        n = s.item("screen", scr_quiz, cid, q.get("id"))
        due = ("due " + fmt_dt(q["due_at"], True) + " " + rel(q["due_at"])) if q.get("due_at") else "no due date"
        s.add(" %s %-*s  %-16s %-26s  %s pts" % (n, namew, fit(q.get("title", "?"), namew),
              quiz_status(q), due, fmtval(q.get("points_possible"))))
    return s

def scr_quiz(cid, qid):
    q = api("/api/v1/courses/%s/quizzes/%s" % (cid, qid)) or {}
    subs = []
    try:
        subs = (api("/api/v1/courses/%s/quizzes/%s/submissions" % (cid, qid)) or {}).get("quiz_submissions") or []
    except ApiError:
        pass
    s = Screen("Quiz", "quiz")
    s.data = q
    s.ctx.update({"cid": cid, "quiz": q})
    s.add("## %s" % q.get("title", "?"))
    facts = []
    if q.get("unlock_at"):
        facts.append("available %s" % fmt_dt(q["unlock_at"]))
    if q.get("due_at"):
        facts.append("due %s" % fmt_dt(q["due_at"]))
    if q.get("lock_at"):
        facts.append("locks %s" % fmt_dt(q["lock_at"]))
    if facts:
        s.add("   ".join(facts))
    allowed = q.get("allowed_attempts")
    info = ["%s pts" % fmtval(q.get("points_possible")), "%s question(s)" % fmtval(q.get("question_count")),
            "type: %s" % (q.get("quiz_type") or "-"),
            "attempts: unlimited" if (allowed is None or allowed < 0) else "attempts: %s" % allowed]
    if q.get("time_limit"):
        info.append("time limit %s min" % q["time_limit"])
    s.add("Info: " + ", ".join(info))
    if q.get("locked_for_user"):
        s.add("      LOCKED: %s" % md_to_text(q.get("lock_explanation") or ""))
    if q.get("access_code"):
        s.add("      NOTE access code required (not supported here)")
    s.add("", "### Your attempts")
    if subs:
        for qs in subs:
            s.add("  attempt %-4s %-10s score %s/%s" % (fmtval(qs.get("attempt")), qs.get("workflow_state") or "-",
                  fmtval(qs.get("score")), fmtval(qs.get("quiz_points_possible") or q.get("points_possible"))))
    else:
        s.add("  no attempts yet")
    if not q.get("locked_for_user"):
        s.add("", "  take     start/resume this quiz")
    if q.get("description"):
        s.add("", "### Description")
        add_html(s, q["description"], BASE + "/courses/%s/quizzes/%s" % (cid, qid))
    return s

def quiz_start_or_resume(cid, qid):
    try:
        subs = (api("/api/v1/courses/%s/quizzes/%s/submissions" % (cid, qid), "POST", []) or {}).get("quiz_submissions") or []
        if subs:
            return subs[0]
    except ApiError:
        pass
    # starting failed (often because one is already open) -- look for an untaken attempt to resume
    lst = api("/api/v1/courses/%s/quizzes/%s/submissions" % (cid, qid)) or {}
    for qs in (lst.get("quiz_submissions") or []):
        if qs.get("workflow_state") == "untaken":
            return qs
    raise ApiError("could not start or resume this quiz (already completed, attempts used up, or needs an access code)")

def quiz_answer_prompt(qq):
    """show one question, read an answer. returns (value, label) for the api call, or (None, None)
    to skip. file_upload_question support is UNVERIFIED -- never exercised against a live Canvas,
    the endpoint shape is a best guess from Canvas's public API docs; it may simply not work."""
    qt = qq.get("question_type")
    text = (md_to_text(qq.get("question_text") or "") or "(no question text)").strip()
    print("\n " + text.replace("\n", "\n "))
    answers = qq.get("answers") or []
    if qt in ("multiple_choice_question", "true_false_question"):
        for i, a in enumerate(answers, 1):
            print("   [%d] %s" % (i, md_to_text(a.get("text") or a.get("html") or "")))
        ans = input_(" answer (number, Enter to skip): ").strip()
        if not ans:
            return None, None
        try:
            a = answers[int(ans) - 1]
            return a["id"], a.get("text")
        except (ValueError, IndexError, KeyError):
            print(" not a valid choice, skipping")
            return None, None
    if qt == "multiple_answers_question":
        for i, a in enumerate(answers, 1):
            print("   [%d] %s" % (i, md_to_text(a.get("text") or a.get("html") or "")))
        ans = input_(" answers (comma-separated numbers, Enter to skip): ").strip()
        if not ans:
            return None, None
        ids = []
        for part in ans.split(","):
            try:
                ids.append(answers[int(part.strip()) - 1]["id"])
            except (ValueError, IndexError):
                pass
        return (ids or None), ("%d selected" % len(ids))
    if qt in ("essay_question", "short_answer_question"):
        ans = input_(" answer (Enter to skip): ")
        return (ans or None), ans
    if qt == "numerical_question":
        ans = input_(" numeric answer (Enter to skip): ").strip()
        if not ans:
            return None, None
        try:
            return float(ans), ans
        except ValueError:
            print(" not a number, skipping")
            return None, None
    if qt == "file_upload_question":
        p = ask_path(" [UNVERIFIED] file for this question (Tab completes, Enter to skip): ").strip()
        if not p:
            return None, None
        p = os.path.expanduser(p.strip().strip("'\""))
        if not os.path.isfile(p):
            print(" not a file: %s" % p)
            return None, None
        return ("__FILE__", p), os.path.basename(p)
    print(" (question type '%s' isn't supported here, skipping)" % qt)
    return None, None

def cmd_take_quiz(cur, arg):
    q = cur.ctx.get("quiz") if cur else None
    cid = cur.ctx.get("cid") if cur else None
    if not q:
        print(" open a quiz first")
        return
    qid = q.get("id")
    if q.get("locked_for_user"):
        print(" LOCKED: %s" % md_to_text(q.get("lock_explanation") or ""))
        return
    rule = "-" * max(20, term_size()[0] - 1)
    print("\n" + rule)
    print(" Take quiz   %s" % q.get("title"))
    print(" %s pts, %s question(s)%s" % (fmtval(q.get("points_possible")), fmtval(q.get("question_count")),
          (", time limit %s min" % q["time_limit"]) if q.get("time_limit") else ""))
    if q.get("access_code"):
        print(" WARNING this quiz needs an access code, which this client has no prompt for")
    print(rule)
    if not yes(" Start / resume now? [y/N] "):
        print(" cancelled")
        return
    try:
        qs = quiz_start_or_resume(cid, qid)
    except ApiError as e:
        print(" %s" % e)
        return
    qsid, attempt, token = qs.get("id"), qs.get("attempt"), qs.get("validation_token")
    try:
        questions = (api("/api/v1/quiz_submissions/%s/questions" % qsid) or {}).get("quiz_submission_questions") or []
    except ApiError as e:
        print(" could not load questions: %s" % e)
        return
    print(" %d question(s). Enter skips a question for now; answered ones are saved as you go." % len(questions))
    for i, qq in enumerate(questions, 1):
        print("\n--- question %d/%d ---" % (i, len(questions)))
        val, shown = quiz_answer_prompt(qq)
        if val is None:
            continue
        fields = [("attempt", "%s" % attempt), ("validation_token", token), ("quiz_questions[][id]", "%s" % qq.get("id"))]
        if isinstance(val, tuple) and val[0] == "__FILE__":
            try:
                fid = upload_file_get_id(
                    "/api/v1/courses/%s/quizzes/%s/submissions/%s/questions/%s/files" % (cid, qid, qsid, qq.get("id")), val[1])
            except ApiError as e:
                print("  upload failed, skipping this question: %s" % e)
                continue
            fields.append(("quiz_questions[][answer]", "%s" % fid))
        elif isinstance(val, list):
            fields.extend(("quiz_questions[][answer][]", "%s" % v) for v in val)
        else:
            fields.append(("quiz_questions[][answer]", "%s" % val))
        try:
            api("/api/v1/quiz_submissions/%s/questions" % qsid, "POST", fields)
            print("  saved.")
        except ApiError as e:
            print("  not saved: %s" % e)
    if yes("\n Submit / finish the quiz now? [y/N] "):
        try:
            api("/api/v1/courses/%s/quizzes/%s/submissions/%s/complete" % (cid, qid, qsid), "POST",
                [("attempt", "%s" % attempt), ("validation_token", token)])
            print(" quiz submitted.")
        except ApiError as e:
            print(" could not finish: %s" % e)
    else:
        print(" left as a draft -- run 'take' again later to continue (if the server allows resuming).")
    go(scr_quiz, (cid, qid), push=False)


# ------------------------------------------------------------ main
HINT = " <n> open   b back   r reload   h home   / search   raw   ? help   q quit"
HINTS = {
    "home": " all = past courses   inbox   me   g <path> = any canvas page as text",
    "course": " as = assignments   qz = quizzes   an = announcements   mod = modules   files   pages",
    "task": " up <file[,file2,..]>   uptext [..]   upurl <url>   (files / links: type their number)",
    "quiz": " take  = start/resume this quiz",
}

HELP = [
    "### Navigation",
    "  <number>                    open that item (downloads go to ~/canvas-downloads)",
    "  b                           back",
    "  r                           reload this screen",
    "  h  /  home                  home screen (your courses + to-do)",
    "  / <text>                    search the current screen",
    "  raw                         raw JSON/HTML of the current screen",
    "  q                           quit",
    "",
    "### Browse",
    "  all                         past courses",
    "  inbox                       messages",
    "  as / qz / an / mod / files / pages   inside a course: assignments, quizzes, announcements, modules, files, pages",
    "  me                          your profile",
    "  g <path-or-url>             show any canvas page as text, e.g.  g /courses/123/grades",
    "",
    "### On an assignment",
    "  up [file[,file2,...]]       upload one or more files (online_upload; asks first)",
    "  uptext [text]               submit text (online_text_entry); no text opens a multi-line prompt",
    "  upurl [url]                 submit a URL (online_url)",
    "",
    "### On a quiz",
    "  take                        start or resume taking the quiz, question by question",
    "                              (file_upload questions are unverified -- never tested live)",
    "",
    "### Advanced",
    "  api <path>                  GET any API path, e.g.  api /api/v1/users/self/upcoming_events",
    "  relogin                     log in again",
    "  logout                      forget the session cookies",
    "",
    "### Long screens",
    "  Enter next screen   a show everything   q stop paging",
]

def draw(sc):
    head = " CANVAS  |  %s" % sc.title
    if me_name():
        head += "  |  " + me_name()
    show_screen([head], sc.lines, [HINT, HINTS.get(sc.kind)])

def go(func, args=(), push=True):
    sc = func(*args)
    if sc is None:
        return
    if push and S["cur"] is not None:
        S["hist"].append(S["cur"][1:])
    S["cur"] = (sc, func, args)
    draw(sc)

def run_item(item):
    kind = item[0]
    if kind == "screen":
        go(item[1], item[2:])
    elif kind == "dl":
        if not item[1]:
            raise ApiError("no download url")
        download_url(item[1], item[2] if len(item) > 2 else "")
    elif kind == "file":
        download_file(item[1])
    elif kind == "link":
        if not item[1]:
            raise ApiError("no link")
        res = route(item[1])
        if res:
            go(res[0], res[1])

def ensure_session():
    if whoami() is None:
        do_login()
    else:
        S["me"] = whoami()

def main():
    load_cfg()
    load_jar()
    try:
        ensure_session()
        go(scr_home)
    except KeyboardInterrupt:
        print("\n(cancelled)")
    except Exception as e:
        print("error:", e)
    while True:
        try:
            cmd = input_("\ncanvas> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not cmd:
            continue
        verb, _, arg = cmd.partition(" ")
        cur = S["cur"][0] if S["cur"] else None
        cid = cur.ctx.get("cid") if cur else None
        try:
            if cmd == "q":
                break
            elif cmd == "?":
                framed("help", HELP)
            elif cmd.isdigit():
                n = int(cmd)
                if cur and 1 <= n <= len(cur.items):
                    run_item(cur.items[n - 1])
                else:
                    print("no such item")
            elif cmd in ("h", "home"):
                go(scr_home)
            elif cmd == "all":
                go(scr_home, (True,))
            elif cmd == "b":
                if S["hist"]:
                    func, args = S["hist"].pop()
                    go(func, args, push=False)
                else:
                    print("nothing to go back to")
            elif cmd == "r":
                if S["cur"]:
                    go(S["cur"][1], S["cur"][2], push=False)
            elif cmd.startswith("/") and cur:
                q = cmd[1:].strip().lower()
                hits = [l for l in cur.lines if q and q in l.lower()]
                framed("search: %s  (%d matches)" % (q, len(hits)), hits)
            elif cmd == "raw" and cur:
                clear_screen()
                data = cur.data
                lines = data.split("\n") if isinstance(data, string_types) else json.dumps(data, indent=2, ensure_ascii=False).split("\n")
                paged(lines, term_size()[1], color=False)
            elif cmd == "inbox":
                go(scr_inbox)
            elif cmd == "me":
                go(scr_json, ("My profile", "/api/v1/users/self/profile"))
            elif verb == "g" and arg:
                go(scr_web, (urljoin(BASE + "/", arg.strip()),))
            elif verb == "api" and arg:
                p = arg.strip()
                go(scr_json, ("GET " + p, p if p.startswith("/") else "/" + p))
            elif cmd in ("as", "an", "mod", "files", "pages", "qz", "quiz", "quizzes") and cid:
                go({"as": scr_assignments, "an": scr_announcements, "mod": scr_modules, "files": scr_files,
                    "pages": scr_pages, "qz": scr_quizzes, "quiz": scr_quizzes, "quizzes": scr_quizzes}[cmd], (cid,))
            elif verb in ("up", "upload", "submit"):
                cmd_upload(cur, arg)
            elif verb == "uptext":
                cmd_submit_text(cur, arg)
            elif verb == "upurl":
                cmd_submit_url(cur, arg)
            elif verb == "take":
                cmd_take_quiz(cur, arg)
            elif cmd == "relogin":
                do_login()
                go(scr_home)
            elif cmd == "logout":
                JAR.clear()
                save_jar()
                S["me"] = None
                print("session cookies removed")
            else:
                print("unknown command, '?' for help")
        except KeyboardInterrupt:
            print("\n(interrupted)")
        except Exception as e:
            print("error:", e)

if __name__ == "__main__":
    main()
