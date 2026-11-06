# -*- coding: utf-8 -*-
# elteui.utils: pure helpers shared by canvas / tms / progenv (no terminal output in here)
#   * py2/py3 compat names, bytes helper, sizes, Budapest dates, download/multipart helpers
#   * Site: the per-website folder under  <elteui>/tmp/<site>/  holding token, cookie, log, stream, cfg
# python 2.7 / 3, stdlib only.
from __future__ import print_function
import os, sys, re, json, time, shutil, tempfile, textwrap, mimetypes, binascii, datetime, zipfile, subprocess

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

try:
    import readline
except ImportError:
    readline = None

# ------------------------------------------------------------ tmp folder
PKG_DIR = os.path.dirname(os.path.realpath(__file__))
TMP_MAX_LOG = 512 * 1024               # a log/stream file is rotated (name.old) above this size

def _usable_dir(d):
    try:
        if not os.path.isdir(d):
            os.makedirs(d, 0o700)
        return os.access(d, os.W_OK | os.X_OK)
    except OSError:
        return False

def tmp_root():
    """the shared internal folder: $ELTE_TMP, else <elteui>/tmp, else <system tmp>/elteui-<uid>"""
    uid = getattr(os, "getuid", lambda: "u")()
    for d in (os.environ.get("ELTE_TMP"), os.path.join(PKG_DIR, "tmp"),
              os.path.join(tempfile.gettempdir(), "elteui-%s" % uid)):
        if d and _usable_dir(os.path.expanduser(d)):
            return os.path.expanduser(d)
    raise OSError("no writable tmp folder for elteui")

TMP = tmp_root()


class Site(object):
    """one wrapper folder per website:  tmp/<name>/{token,cookie,log,stream,cfg}
    env    = {kind: ENV_VAR}    an env var may point a kind at another path (kept for old setups)
    legacy = {kind: old_path}   old dotfiles are moved in once, the first time the new file is missing"""
    KINDS = ("token", "cookie", "log", "stream", "cfg")

    def __init__(self, name, env=None, legacy=None):
        self.name = name
        self.dir = os.path.join(TMP, name)
        self.env = env or {}
        if not _usable_dir(self.dir):
            raise OSError("cannot create %s" % self.dir)
        for kind, old in (legacy or {}).items():
            self._migrate(kind, os.path.expanduser(old))

    def path(self, kind):
        e = self.env.get(kind)
        if e and os.environ.get(e):
            return os.path.expanduser(os.environ[e])
        return os.path.join(self.dir, kind)

    def _migrate(self, kind, old):
        new = self.path(kind)
        try:
            if os.path.isfile(old) and not os.path.exists(new) and os.path.realpath(old) != os.path.realpath(new):
                shutil.move(old, new)
                os.chmod(new, 0o600)
        except (OSError, IOError):
            pass

    def exists(self, kind):
        return os.path.exists(self.path(kind))

    def read(self, kind, default=""):
        try:
            with open(self.path(kind), "rb") as f:
                return f.read().decode("utf-8", "replace")
        except (OSError, IOError):
            return default

    def write(self, kind, data):
        """atomic, private (0600) write"""
        p = self.path(kind)
        tmp = "%s.%d.tmp" % (p, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(B(data))
        os.rename(tmp, p)

    def touch(self, kind):
        p = self.path(kind)
        if not os.path.exists(p):
            os.close(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600))
        return p

    def remove(self, kind):
        try:
            os.remove(self.path(kind))
        except OSError:
            pass

    def read_json(self, kind, default=None):
        empty = {} if default is None else default
        try:
            return json.loads(self.read(kind) or "null") or empty
        except ValueError:
            return empty

    def write_json(self, kind, obj):
        self.write(kind, json.dumps(obj))

    def open_append(self, kind):
        """binary append handle for log / stream files, with simple size rotation"""
        p = self.path(kind)
        try:
            if os.path.getsize(p) > TMP_MAX_LOG:
                os.rename(p, p + ".old")
        except OSError:
            pass
        return os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")

    def log(self, s, kind="log"):
        try:
            f = self.open_append(kind)
            try:
                f.write(B("%s %s\n" % (time.strftime("%H:%M:%S"), s)))
            finally:
                f.close()
        except Exception:
            pass

    def clear(self):
        """forget everything stored for this site (token, cookies, ...) but keep the folder"""
        for k in self.KINDS:
            self.remove(k)


# ------------------------------------------------------------ helpers

def B(s):
    return s if isinstance(s, bytes) else s.encode("utf-8")


def hsize(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


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


def hdr_get(r, name, default=""):
    try:
        return r.info().get(name, default) or default
    except Exception:
        return default


def dl_filename(r, hint="", url=""):
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
        name = hint or unquote(os.path.basename(urlparse(url).path)) or "download"
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


def multipart(items):
    bd = "----elte" + binascii.hexlify(os.urandom(12)).decode()
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
