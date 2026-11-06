# -*- coding: utf-8 -*-
# elteui.render: everything that draws to the terminal or turns data into text
#   * terminal: size, colors, wrapping, paging, framed screens, prompts
#   * renderers: HTML -> text (HtmlText), Markdown -> text (md_to_text), JSON -> text (json_lines)
#   * downloads with progress bar, zip listing, text/pdf viewing
# python 2.7 / 3, stdlib only. Depends on elteui.utils, never the other way round.
from __future__ import print_function
from .utils import *

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
            "TIMEOUT": "1;31", "REJECTED": "1;31", "CLOSED": "1;31", "MISSING": "31", "SIGNAL": "31",
            "PART": "1;33", "WARNING": "33", "LATE": "1;33", "NOT SUBMITTED": "33",
            "RUNNING": "1;36", "PENDING": "36", "QUEUED": "36", "WAITING": "36", "COMPILING": "36",
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


def framed(title, body_lines):
    cols, rows = term_size()
    width = max(20, cols - 1)
    rule = "-" * width
    paged([rule, fit(" " + title, width), rule] + body_lines + [rule], rows)


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


def do_download(r, dest_dir, hint="", url=""):
    name = dl_filename(r, hint, url)
    try:
        os.makedirs(dest_dir)
    except OSError:
        pass
    dest = unique_path(dest_dir, name)
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


SKIP = set(["script", "style", "head", "svg", "noscript", "template"])


PARA = set("p h1 h2 h3 h4 h5 h6 ul ol table".split())


LINE = set("li tr br div section article header footer main nav form dl dt dd aside pre".split())


INLINE = set("span button label small strong b em i code time td th option".split())


class HtmlText(HTMLParser):
    def __init__(self, base, host=None):
        HTMLParser.__init__(self)
        self.base, self.out, self.tail = base, [], ""
        self.host = host or urlparse(base).netloc
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
                if urlparse(url).netloc == self.host:
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


def show_screen(head_lines, body_lines, hints=(), menu=""):
    """clear the terminal and draw: rule, header, rule, [menu, rule], body, rule, hints (paged if tall)"""
    cols, rows = term_size()
    width = max(20, cols - 1)
    rule = "-" * width
    lines = [rule]
    for h in head_lines:
        lines.extend(wrap(h, width))
    lines.append(rule)
    if menu:
        lines += wrap(" " + menu, width) + [rule]
    for l in body_lines:
        lines.extend(wrap(l, width))
    lines += ["", rule] + [fit(h, width) for h in hints if h]
    clear_screen()
    paged(lines, rows)
