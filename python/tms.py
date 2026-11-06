#!/usr/bin/env python
# -*- coding: utf-8 -*-
# tms: terminal client for https://tms.inf.elte.hu  (python 2.7 / 3, stdlib only)
# Keeps its token in elteui/tmp/tms/token (override: TMS_TOKEN_FILE, e.g. to share it with tms.sh).
from __future__ import print_function
import os, sys, re, json, time, base64, getpass, textwrap
import mimetypes, binascii, datetime, zipfile, subprocess
from collections import OrderedDict

try:
    import readline
except ImportError:
    readline = None

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
try:
    from elteui import *               # shared rendering / download / terminal helpers
except ImportError:
    sys.stderr.write("the elteui/ folder must be next to this script\n")
    sys.exit(1)

BASE = os.environ.get("TMS_BASE", "https://tms.inf.elte.hu/api").rstrip("/")
# internal files live in  elteui/tmp/tms/{token,cfg,log}  (old ~/.tms_* files are moved in once;
# tms.sh can follow with TMS_TOKEN_FILE=<that token path>)
SITE = Site("tms", env={"token": "TMS_TOKEN_FILE"},
            legacy={"token": "~/.tms_token", "cfg": "~/.tms_client.json", "log": "~/.tms_last.log"})
TOKEN_FILE = SITE.path("token")
LOG_FILE = SITE.path("log")
DL_DIR = os.path.expanduser(os.environ.get("TMS_DL", "~/tms-downloads"))
UA = "Mozilla/5.0 (X11; Linux x86_64) tms-cli"
WATCH_SECS = 120

CFG = {"user": "", "sem": None}
TOKEN = {"v": ""}
S = {"me": None, "sems": None, "cur": None, "hist": []}


# ------------------------------------------------------------ terminal


# ------------------------------------------------------------ dates (Europe/Budapest without tz database)


# ------------------------------------------------------------ config / token / login
def load_cfg():
    CFG.update(SITE.read_json("cfg"))

def save_cfg():
    try:
        SITE.write_json("cfg", CFG)
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
    SITE.write("token", t)

class ApiError(Exception):
    pass

def err_text(code, text):
    # PHP/Yii debug pages: show only the message, the full trace stays in the log file
    m = re.search(r"with message '([^']*)'", text)
    if m and "Stack trace" in text:
        return "HTTP %s: server error (%s)  [full trace in %s]" % (code, fit(m.group(1), 120), LOG_FILE)
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
            TOKEN["v"] = SITE.read("token").strip()
        except Exception:
            TOKEN["v"] = ""
    exp = jwt_exp(TOKEN["v"]) if TOKEN["v"] else None
    if not TOKEN["v"] or (exp and exp < time.time() + 20):
        login()
    return TOKEN["v"]

# ------------------------------------------------------------ api
log = SITE.log

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


def download(path, hint=""):
    code, r = api_request("GET", path)
    if code >= 400:
        raise ApiError(err_text(code, r.read().decode("utf-8", "replace")))
    ct = hdr_get(r, "Content-Type").split(";")[0].strip().lower()
    if ct == "application/json" and "attachment" not in hdr_get(r, "Content-Disposition").lower():
        text = r.read().decode("utf-8", "replace")
        raise ApiError("server returned JSON instead of a file: %s" % fit(text, 200))
    do_download(r, DL_DIR, hint)


# ------------------------------------------------------------ generic json rendering


# ------------------------------------------------------------ markdown-ish -> text


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
    if t.get("entryPasswordProtected"):
        s.add("      entry password: " + ("UNLOCKED" if t.get("entryPasswordUnlocked") else "LOCKED - type  pw  to unlock"))
    if t.get("isIpAddressAllowed") is False:
        s.add("      WARNING this IP address is not allowed for this task (the web UI blocks unlock/upload here)")
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

# ------------------------------------------------------------ entry password
# The web UI's exact password request is not documented. Capture it once in the browser
# (DevTools -> Network -> submit the password -> right click the request -> Copy as cURL),
# then describe it here with the same syntax as the `api` command:
#     pwcfg POST /student/tasks/{id}/<what-you-saw> {"password":"{pw}"}
# {id} = task id, {pw} = the password (JSON-escaped). It is saved in ~/.tms_client.json
# (without any password). Env var TMS_PW_REQ overrides it.
# found in the web app's own JS:  POST /student/tasks/{id}/unlock  {"password": ...}
DEFAULT_PW_REQ = 'POST /student/tasks/{id}/unlock {"password":"{pw}"}'

def pw_spec():
    return os.environ.get("TMS_PW_REQ") or CFG.get("pw_req") or DEFAULT_PW_REQ

def parse_spec(spec):
    toks = spec.split(None, 2)
    if len(toks) < 2 or toks[0].upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return None
    return toks[0].upper(), toks[1], (toks[2] if len(toks) > 2 else None)

def cmd_pwcfg(arg):
    if not arg.strip():
        print(" current: %s%s" % (pw_spec(), "" if CFG.get("pw_req") else "   (built-in default)"))
        print(" usage:   pwcfg POST /student/tasks/{id}/<path> {\"password\":\"{pw}\"}")
        print("          (copy method/path/body from the browser's Network tab; {id} and {pw} are filled in)")
        return
    if arg.strip() == "clear":
        CFG.pop("pw_req", None)
        save_cfg()
        print(" cleared")
        return
    if not parse_spec(arg):
        print(" format: METHOD /path [json-body]")
        return
    CFG["pw_req"] = arg.strip()
    save_cfg()
    print(" saved")

def cmd_pw(cur, arg):
    t = cur.ctx.get("task") if cur else None
    if not t:
        print(" open a task first")
        return
    spec = parse_spec(pw_spec())
    if not spec:
        print(" bad password request config. Run  pwcfg clear  to use the built-in default.")
        return
    if t.get("entryPasswordUnlocked"):
        print(" already unlocked")
        return
    if t.get("entryPasswordProtected") is False:
        print(" this task has no entry password")
        return
    if t.get("isIpAddressAllowed") is False:
        print(" WARNING the server reports this IP as not allowed for the task; the web UI would not offer unlock here.")
        if not yes(" Try anyway? [y/N] "):
            return
    method, path, body = spec
    pw = arg if arg else getpass.getpass(" Entry password: ")
    if not pw:
        return
    path = path.replace("{id}", "%s" % t.get("id"))
    shown = None
    if body:
        body = body.replace("{id}", "%s" % t.get("id"))
        shown = body.replace("{pw}", "***")
        body = body.replace("{pw}", json.dumps(pw)[1:-1])
        if "{pw}" not in pw_spec():
            print(" WARNING the saved request has no {pw} placeholder (a literal password is stored in the config?)")
    print(" %s %s%s" % (method, path, ("   body: " + shown) if shown else ""))
    code, r = api_request(method, path, body)
    text = r.read().decode("utf-8", "replace")
    log("pw %s %s -> %s %s" % (method, path, code, text[:2000].replace("\n", "\\n")))
    if code >= 400:
        print(" rejected: " + err_text(code, text))
        return
    print(" accepted (HTTP %s)%s" % (code, (": " + fit(text.strip(), 150)) if text.strip() else ""))
    S["me"] = None
    go(S["cur"][1], S["cur"][2], push=False)

def cmd_hunt(arg):
    """headless replacement for browser DevTools: crawl the site's JS bundles (including lazy
    chunks) and print code that mentions the entry/exit password or the task API paths.
    hunt            -> default: entryPassword / exitPassword / unlock + /student/tasks calls
    hunt <regex>    -> your own case-insensitive regex"""
    site = os.environ.get("TMS_SITE") or re.sub(r"/api$", "", BASE) + "/"
    pat_s = arg.strip() or r"entry.?password|exit.?password|password.?unlock|unlock.?password|/unlock|/enter|student/tasks"
    try:
        pat = re.compile(r"[^\n]{0,110}(?:%s)[^\n]{0,160}" % pat_s, re.I)
    except re.error as e:
        print(" bad regex: %s" % e)
        return
    def get(u):
        rq = ur.Request(u)
        rq.add_header("User-Agent", UA)
        return ur.urlopen(rq, timeout=60).read().decode("utf-8", "replace")
    try:
        html = get(site)
    except Exception as e:
        print(" cannot fetch %s: %s" % (site, e))
        return
    todo = [urljoin(site, x) for x in re.findall(r'(?:src|href)=["\']([^"\']+\.js[^"\']*)["\']', html)]
    seen, shown = set(), 0
    print(" crawling %s ..." % site)
    while todo and len(seen) < 150:
        src = todo.pop(0)
        if src in seen:
            continue
        seen.add(src)
        try:
            js = get(src)
        except Exception as e:
            print(" skip %s (%s)" % (src.rsplit("/", 1)[-1], e))
            continue
        # lazy chunks: import("./X.js"), "assets/X.js", "X.js" (resolved against this file)
        for ch in re.findall(r'["\'`]((?:\.{0,2}/)?[\w./-]+\.js)["\'`]', js):
            u = urljoin(src, ch)
            if u.startswith(site) and u not in seen and u not in todo:
                todo.append(u)
        hits = []
        for m in pat.finditer(js):
            h = m.group(0).strip()
            if h not in hits:
                hits.append(h)
        if hits:
            print("\n== %s  (%d hit%s)" % (src.rsplit("/", 1)[-1], len(hits), "" if len(hits) == 1 else "s"))
            for h in hits[:25]:
                print("   " + h)
                shown += 1
    print("\n %d file(s) scanned, %d hit(s) shown." % (len(seen), shown))
    print(" Look for a call like  .post(`/student/tasks/${id}/...`,{...})  near entryPassword.")
    print(" Narrow down with e.g.  hunt entryPassword   or   hunt unlock")

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

HELP = [
    "### Navigation",
    "  <number>                    open that item (downloads go to ~/tms-downloads)",
    "  b                           back",
    "  r                           reload this screen",
    "  h  /  home                  home screen (your groups)",
    "  / <text>                    search the current screen",
    "  raw                         raw JSON of the current screen",
    "  q                           quit",
    "",
    "### Browse",
    "  sem                         choose semester",
    "  subs                        all my submissions",
    "  notif                       notifications",
    "  att                         attendance (this semester)",
    "  quiz                        quiz instances (read-only; pick one for results)",
    "  me                          your settings",
    "",
    "### On a task or submission",
    "  up [file]                   upload (asks first, then watches the auto-test)",
    "  at                          auto-tester results",
    "  get                         download my submission",
    "  report                      download my report",
    "",
    "### Entry password",
    "  pw [password]               unlock the task (prompts if no password given)",
    "  pwcfg [spec]                show/change the unlock request (clear = reset)",
    "  hunt [regex]                scan the site's JS for API paths",
    "",
    "### Advanced",
    "  api [METHOD] <path> [json]  any API call (non-GET asks first)",
    "  token                       check the cached token",
    "  relogin                     log in again",
    "  logout                      remove the cached token",
    "",
    "### Long screens",
    "  Enter                       next screen",
    "  a                           show everything",
    "  q                           stop paging",
]

def draw(sc):
    me = S["me"] or {}
    head = " TMS  |  %s" % sc.title
    who = " ".join(x for x in (me.get("name"), ("(%s)" % me["userCode"]) if me.get("userCode") else "") if x)
    if who:
        head += "  |  " + who
    show_screen([head], sc.lines, [HINT, HINTS.get(sc.kind)])


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
        do_download(r, DL_DIR, "download")
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
            elif verb == "hunt":
                cmd_hunt(arg)
            elif verb == "pwcfg":
                cmd_pwcfg(arg)
            elif verb in ("pw", "password"):
                cmd_pw(cur, arg)
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
