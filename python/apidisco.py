#!/usr/bin/env python3
"""
apidisco - passive API endpoint discovery for a web page.

Static side : HTML, inline + external JS, lazy-loaded chunks (webpack/vite/next),
              source maps (original sources, JSDoc, comments), forms, GraphQL ops.
Dynamic side: optional --browser (Playwright) and --har (DevTools export) to merge
              calls that were actually observed (login flows etc.).
Active side : optional --probe asks for well-known doc paths (swagger, openapi...).
              Default mode only GETs the page and the files it references.

Only use it on sites you own or are authorized to test.
stdlib only (Playwright optional).
"""
import argparse, base64, bisect, json, re, ssl, sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (compatible; apidisco/0.2)"
PAGE_HOSTS = set()
MAX_BYTES = 25 * 1024 * 1024


# ───────────────────────── HTTP ─────────────────────────
class Http:
    def __init__(self, headers, insecure, auth_hosts):
        self.headers = {"User-Agent": UA, "Accept": "*/*"}
        self.user_headers = headers          # only sent to the pages' own hosts
        self.auth_hosts = auth_hosts
        self.ctx = ssl._create_unverified_context() if insecure else None

    def get(self, url):
        h = dict(self.headers)
        if urlparse(url).netloc in self.auth_hosts:
            h.update(self.user_headers)
        try:
            with urlopen(Request(url, headers=h), timeout=15, context=self.ctx) as r:
                body = r.read(MAX_BYTES)
                enc = r.headers.get_content_charset() or "utf-8"
                return r.status, r.headers.get("Content-Type", ""), body.decode(enc, "replace")
        except HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), ""
        except (URLError, OSError, ValueError):
            return 0, "", ""


# ───────────────────────── HTML ─────────────────────────
class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts, self.comments, self.forms = [], [], []
        self._form = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])
        elif tag == "link" and a.get("href") and (
            a.get("rel") in ("modulepreload", "preload", "prefetch") and
            (a.get("as") == "script" or ".js" in a["href"])):
            self.scripts.append(a["href"])
        elif tag == "form":
            self._form = {"action": a.get("action", ""), "method": (a.get("method") or "GET").upper(), "fields": []}
            self.forms.append(self._form)
        elif tag in ("input", "textarea", "select") and self._form is not None and a.get("name"):
            self._form["fields"].append(a["name"])

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None

    def handle_comment(self, data):
        self.comments.append(data)


# ───────────────────────── findings ─────────────────────────
class Findings:
    def __init__(self):
        self.eps = {}                        # (method, host, path) -> info
        self.comments = defaultdict(set)     # text -> {src}
        self.gql = defaultdict(set)          # op -> {src}
        self.files = set()                   # interesting original source file names
        self.docs = []                       # (url, note)
        self.auth = {"logins": [], "bearer": None, "cookies": {}, "tokens": set()}  # from --har
        self.bases = set()                   # same-origin axios/fetch baseURL literals, e.g. "/api"
        self.checked = []                    # (url, status, note) every scanned/probed resource, hit or miss
        self.schemas = {}                    # name -> {"kind","fields","used_by"} — dedup'd body/params/form shapes
        self.har_stats = None                # diagnostic: how many HAR entries matched, and why

    def add(self, method, host, path, src, kind, score, extra=()):
        e = self.eps.setdefault((method, host, path), {"src": [], "kinds": set(), "score": 0, "extra": set()})
        e["extra"].update(extra)
        if src not in e["src"] and len(e["src"]) < 6:
            e["src"].append(src)
        e["kinds"].add(kind)
        e["score"] = max(e["score"], score)


# ───────────────────────── static scanning ─────────────────────────
# Anchored on the quote + path start, so one stray quote in minified code can't desync it.
LIT_RE = re.compile(
    r'''(["'`])('''
    r'''(?:https?|wss?)://[^"'`\s\\<>]{3,300}'''
    r'''|//[A-Za-z0-9.-]+\.[A-Za-z]{2,}/[^"'`\s\\<>]{0,300}'''
    r'''|/(?!/)[^"'`\s\\<>]{1,300}'''
    r'''|(?:api|v\d{1,2}|rest|graphql|rpc|ajax|trpc|_next/data)/[^"'`\s\\<>]{0,300}'''
    r''')(?=\1)''', re.I)

CALL_RE = re.compile(r'([A-Za-z_$][\w$]*)\s*\(\s*(?:["\']([A-Za-z]+)["\']\s*,\s*)?$')
PROP_RE = re.compile(r'([A-Za-z_$][\w$]*)\s*[:=]\s*$')
METHOD_RE = re.compile(r'\bmethod\s*:\s*["\'`](\w+)["\'`]|\btype\s*:\s*["\'](GET|POST|PUT|PATCH|DELETE)["\']', re.I)
HTTP_FUNCS = {
    "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE", "del": "DELETE",
    "head": "HEAD", "options": "OPTIONS", "getjson": "GET", "sendbeacon": "POST", "useswr": "GET",
    "websocket": "WS", "eventsource": "SSE",
    "fetch": None, "ajax": None, "request": None, "axios": None, "open": None, "usefetch": None,
    "usequery": None, "http": None, "xhr": None,
}
API_HINT = re.compile(
    r'(?:^|/)(?:api|v\d{1,2}|rest|graphql|gql|rpc|trpc|ajax|auth|oauth2?|login|logout|signin|signup|register|'
    r'token|session|users?|me|admin|internal|search|upload|webhooks?|ws|socket|sockjs|health|config|'
    r'settings|actuator)(?:/|$|\?|\.json)|\.json(?:$|\?)|/_next/data|/wp-json', re.I)
STATIC_RE = re.compile(r'\.(?:png|jpe?g|gif|svg|webp|avif|ico|css|scss|less|woff2?|ttf|eot|otf|mp[34]|webm|ogg|wav|pdf|zip|gz|wasm|html?|js|mjs|map)(?:[?#]|$)', re.I)
NOISE_HOSTS = ("w3.org", "reactjs.org", "react.dev", "mozilla.org", "github.com", "npmjs.com", "fb.me",
               "schema.org", "stackoverflow.com", "fonts.googleapis.com", "fonts.gstatic.com", "gstatic.com",
               "creativecommons.org", "apache.org", "opensource.org", "mui.com", "reactrouter.com",
               "redux.js.org", "nextjs.org", "vuejs.org", "angular.io", "w3schools.com", "json-schema.org",
               "example.com")
GQL_RE = re.compile(r'\b(?:query|mutation|subscription)\s+([A-Za-z_]\w{2,})\s*[({]|operationName\s*:\s*["\'](\w+)["\']')
JS_STR_RE = re.compile(r'''["'`]((?:https?:)?[^"'`\s\\<>()]{0,200}?\.m?js)(?:\?[^"'`\s]*)?["'`]''')
WEBPACK_RE = re.compile(
    r'''["']([^"']*)["']\s*\+\s*\w+\s*\+\s*["']\.["']\s*\+\s*(\{[^{}]{8,}\})\s*\[\s*\w+\s*\]\s*\+\s*["']([^"']*\.m?js)["']''')
HASHMAP_RE = re.compile(r'''["']?([\w-]+)["']?\s*:\s*["']([0-9a-fA-F]{6,})["']''')
COMMENT_RE = re.compile(r'/\*[\s\S]*?\*/|(?<![:"\'`\\])//[^\n]*')
COMMENT_INTEREST = re.compile(
    r'\b(api|endpoint|route|todo|fixme|hack|xxx|secret|token|passw|credential|internal|admin|deprecated|'
    r'debug|swagger|openapi|staging|backend|auth|undocumented|private|legacy|http)|/[\w-]+/[\w-]+', re.I)
SOURCEMAP_RE = re.compile(r'//[#@]\s*sourceMappingURL=(\S+)\s*$')
INTERESTING_FILE = re.compile(r'(api|service|client|endpoint|route|graphql|http|fetch|request|query|mutation)', re.I)


SEG_OK = re.compile(r'^(?:/:?[A-Za-z0-9_.~%@+\-{}]+)+/?$')
QRY_OK = re.compile(r'^[A-Za-z0-9_.~%@+\-{}=&,:/\[\]]*$')
ROUTE_PROPS = {"path", "to", "href", "pathname", "route", "link", "redirect", "navigateto"}
ROUTE_FUNCS = {"navigate", "push", "replace", "redirect", "usenavigate", "redirectto"}


def path_ok(path):
    """Strict shape check: kills regex/grammar/base64 junk from minified bundles."""
    p, _, q = path.partition("?")
    return bool(SEG_OK.match(p)) and not re.fullmatch(r'(?:/\.+)+/?', p) and bool(QRY_OK.match(q))


def texty(c):
    """Is this comment natural language rather than a slice of minified code?"""
    sym = sum(ch in '{}();=<>\\[]|&!$^*' for ch in c)
    return sym <= max(2, len(c) * 0.04) and not re.search(r'=>|function|return |\)\{', c)


def normalize(p):
    p = re.sub(r'\$\{[^}]*\}', '{}', p)
    return re.sub(r'(?<=/):([A-Za-z_]\w*)', r'{\1}', p)


def idify(path):
    path = re.sub(r'/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{23,}(?=/|$)', '/{id}', path, flags=re.I)
    return re.sub(r'/\d{2,}(?=/|$)', '/{id}', path)


def host_is_noise(host):
    h = host.lower().split(":")[0]
    return any(h == n or h.endswith("." + n) for n in NOISE_HOSTS)


def split_url(p):
    """-> (host, path) ; host '' means same origin as the page."""
    if p.startswith("//"):
        p = "https:" + p
    if re.match(r'(?:https?|wss?)://', p, re.I):
        u = urlparse(p)
        return u.netloc, (u.path or "/") + (("?" + u.query[:100]) if u.query else "")
    if not p.startswith("/"):
        p = "/" + p
    return "", p


def scan(text, label, F, comments=True):
    nl = [m.start() for m in re.finditer("\n", text)]

    def loc(i):
        ln = bisect.bisect_left(nl, i)
        col = i - (nl[ln - 1] + 1 if ln else 0) + 1
        return f"{label}:{ln + 1}:{col}"

    for m in LIT_RE.finditer(text):
        q, raw = m.group(1), m.group(2)
        qs, end = m.start(), m.end(2)
        p = normalize(raw)
        host, path = split_url(p)
        if host_is_noise(host) if host else False:
            continue
        if len(path) < 3 or STATIC_RE.search(path) or not path_ok(path):
            continue

        before, after = text[max(0, qs - 80):qs], text[end + 1:end + 260]
        method, ctx, kinds, extra = "?", 0, set(), []
        c = CALL_RE.search(before)
        fn = c.group(1).lower() if c else ""
        if c and fn in ROUTE_FUNCS:
            ctx, kinds = -2, {"route"}
        elif c and fn == "open" and not c.group(2):            # window.open(url) = a link, not an API call
            ctx, kinds = -1, {"link"}
        elif c and fn in HTTP_FUNCS:
            method = HTTP_FUNCS[fn]
            if fn == "open":
                method = c.group(2).upper()
            if method is None:
                mm = METHOD_RE.search(after)
                method = (mm.group(1) or mm.group(2)).upper() if mm else ("GET" if fn == "fetch" else "?")
            ctx, kinds = 3, {"call"}
        else:
            pr = PROP_RE.search(before)
            if pr:
                name = pr.group(1).lower()
                if name in ROUTE_PROPS:
                    ctx, kinds = -2, {"route"}
                elif "path" not in name and any(name.startswith(x) or name.endswith(x)
                                                for x in ("url", "uri", "endpoint", "api", "base", "host")):
                    ctx, kinds = 2, {"named"}
                    mm = METHOD_RE.search(text[max(0, qs - 150):end + 150])
                    if mm:
                        method = (mm.group(1) or mm.group(2)).upper()
                    if not host and re.fullmatch(r'/[\w-]{1,24}', path):   # axios.create({baseURL:"/api"}) etc.
                        F.bases.add(path.rstrip("/"))
        if ctx >= 2:                                           # parameter / body / multipart field names
            pm = re.search(r'\bparams\s*:\s*\{([^{}]{0,160})\}', after)
            if pm:
                extra.append("params=" + ",".join(re.findall(r'(?:^|,)\s*([A-Za-z_$][\w$]*)\s*(?=[:,]|$)', pm.group(1))[:12]))
            bm = re.match(r'\s*,\s*\{([^{}]{1,200})\}', after)
            if bm:
                keys = re.findall(r'(?:^|,)\s*([A-Za-z_$][\w$]*)\s*(?::|(?=,|$))', bm.group(1))
                # for GET/DELETE/HEAD the 2nd arg is axios/fetch *config* (responseType, headers...), not a body
                label = "config" if method in ("GET", "DELETE", "HEAD") else "body" if method in ("POST", "PUT", "PATCH") else "fields"
                if keys and not {"method", "headers"} & set(keys):
                    extra.append(f"{label}=" + ",".join(keys[:12]))
            win = text[max(0, qs - 300):end + 300]
            if "FormData" in win:
                fl = list(dict.fromkeys(re.findall(r'\.append\(\s*["\'](\w+)["\']', win)))
                if fl:
                    extra.append("form=" + ",".join(fl[:10]))
        # string concatenation: "/api/users/" + id   |   base + "/users"
        if re.match(r'\s*\+', after):
            kinds.add("concat")
            if path.endswith(("/", "=", "&", ":", "-", "_")):
                path += "{}"
        elif re.search(r'\+\s*$', before):
            kinds.add("fragment")
        if "{}" in path:
            kinds.add("param")
        segs = path.split("?")[0].strip("/").split("/")
        if ctx == 0 and len(segs) == 1 and re.fullmatch(r'[A-Za-z0-9]+', segs[0]) and len(segs[0]) <= 14 \
                and sum(ch.isupper() for ch in segs[0]) >= 2:     # random-looking single token
            continue
        score = ctx + (2 if API_HINT.search(path) else 0) + (1 if "param" in kinds or "concat" in kinds else 0)
        if host and host not in PAGE_HOSTS and ctx < 3 and not API_HINT.search(path):
            score -= 2                                         # third-party link, not "their" API
        F.add(method, host, path, loc(qs), ",".join(sorted(kinds)) or "literal", score, extra)

    if re.search(r'graphql|gql|operationName', text, re.I):
        for m in GQL_RE.finditer(text):
            F.gql[m.group(1) or m.group(2)].add(loc(m.start()))

    if comments:
        seen = 0
        for m in COMMENT_RE.finditer(text):
            c = m.group(0)
            if c.startswith("/*!") or "@license" in c or "sourceMappingURL" in c or len(c) < 8:
                continue
            if COMMENT_INTEREST.search(c) and texty(c):
                body = re.sub(r'\s+', ' ', re.sub(r'^/\*+|\*+/$|^//', '', c)).strip()[:240]
                if len(body) > 6:
                    F.comments[body].add(loc(m.start()))
                    comment_paths(body, loc(m.start()), F)
                    seen += 1
                    if seen > 300:
                        break


def comment_paths(body, src, F):
    """Paths mentioned in comments/docs are endpoints too (often disabled or forgotten ones)."""
    for p in re.findall(r'(?<![\w.:/])(/[A-Za-z0-9_\-./{}]{3,})', body):
        p = p.rstrip(".")
        if API_HINT.search(p) and not STATIC_RE.search(p):
            F.add("?", "", normalize(p), src, "comment", 2)


def chunk_candidates(text, file_url, roots):
    """URLs of other JS files referenced from this one (lazy chunks etc.)."""
    out = []
    for m in WEBPACK_RE.finditer(text):               # webpack 4/5 hash map
        prefix, hmap, suffix = m.groups()
        for cid, h in list(HASHMAP_RE.findall(hmap))[:400]:
            out.append(f"{prefix}{cid}.{h}{suffix}")
    for m in JS_STR_RE.finditer(text):
        out.append(m.group(1))
    urls = []
    for s in out:
        if s.startswith(("http://", "https://")):
            urls.append(s)
        elif s.startswith("//"):
            urls.append("https:" + s)
        elif s.startswith("/"):
            urls += [urljoin(r, s) for r in roots]
        else:
            urls.append(urljoin(file_url, s))
            for r in roots:
                urls.append(urljoin(r + "/", s))
                if s.startswith("static/"):
                    urls.append(urljoin(r + "/_next/", s))
    return urls


# ───────────────────────── dynamic inputs ─────────────────────────
def add_observed(F, method, url, src):
    host, path = split_url(url)
    path = idify(path.split("?")[0])
    if host and host_is_noise(host):
        return
    if host in PAGE_HOSTS:          # same origin as the page itself: unify with static findings, not a new host
        host = ""
    F.add("WS" if url.startswith(("ws:", "wss:")) else method.upper(), host, path, src, "observed", 6)


LOGIN_PATH_RE = re.compile(r'(login|signin|sign-in|auth|authenticate|token|session|oauth)', re.I)
JWT_RE = re.compile(r'eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}')
SECRETY_KEY = re.compile(r'(token|jwt|access|refresh|secret|session|auth|key|bearer)', re.I)
CREDENTIAL_KEY = re.compile(r'(user(name)?|email|login|pass(word)?)', re.I)


def _hdr(headers, name):
    for h in headers or []:
        if h.get("name", "").lower() == name:
            return h.get("value", "")
    return None


def _json_body(data):
    if not data:
        return None
    try:
        return json.loads(data.get("text", "") or "")
    except ValueError:
        return None


def _token_fields(obj, prefix=""):
    """Walk a parsed JSON body and yield (key, value) for anything token/secret-shaped."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and (SECRETY_KEY.search(k) or JWT_RE.search(v)) and len(v) >= 8:
                yield prefix + k, v
            elif isinstance(v, (dict, list)):
                yield from _token_fields(v, prefix + k + ".")
    elif isinstance(obj, list):
        for v in obj:
            yield from _token_fields(v, prefix)


def load_har(path, F):
    entries = json.load(open(path, encoding="utf-8"))["log"]["entries"]
    cookie_count, bearer_count, known_tokens = defaultdict(int), defaultdict(int), {}
    rt_tally, matched, had_rt_field = defaultdict(int), 0, 0

    for e in entries:
        r, rt = e["request"], e.get("_resourceType", "")       # Chrome-only field; "" on Firefox/other exporters
        had_rt_field += "_resourceType" in e
        rt_tally[rt or "(untagged)"] += 1
        mime = (e.get("response", {}).get("content", {}).get("mimeType", "") or "").lower()
        host, path = split_url(r["url"])
        # fetch/xhr/ws tag (when present) or non-GET or json-ish mime or a /api/ path on a page host
        # — that last clause is a safety net for exporters (Firefox etc.) that mislabel mime or drop the tag
        looks_like_api = "/api/" in path.lower() and not STATIC_RE.search(path) and host in PAGE_HOSTS
        if rt in ("fetch", "xhr", "websocket", "eventsource") or r["method"] != "GET" or "json" in mime or looks_like_api:
            add_observed(F, r["method"], r["url"], "HAR")
            matched += 1

        for c in r.get("cookies", []):
            cookie_count[c["name"]] += 1
        auth_hdr = _hdr(r.get("headers"), "authorization")
        if auth_hdr and auth_hdr.lower().startswith("bearer "):
            bearer_count[auth_hdr.split(None, 1)[1][:24]] += 1

        host, path = split_url(r["url"])
        if r["method"] in ("POST", "PUT") and LOGIN_PATH_RE.search(path):
            req_json = _json_body(r.get("postData"))
            resp_text = ((e.get("response") or {}).get("content") or {}).get("text", "")
            try:
                resp_json = json.loads(resp_text) if resp_text else None
            except ValueError:
                resp_json = None
            cred_fields = sorted(k for k in (req_json or {}) if CREDENTIAL_KEY.search(k)) if isinstance(req_json, dict) else []
            found = list(_token_fields(resp_json)) if resp_json else []
            set_cookies = [sc["name"] for sc in (e.get("response", {}).get("cookies") or [])]
            if cred_fields or found or set_cookies:
                F.auth["logins"].append({
                    "method": r["method"], "host": host or urlparse(r["url"]).netloc, "path": path,
                    "request_fields": cred_fields, "response_token_fields": [k for k, _ in found],
                    "set_cookies": set_cookies,
                })
                for k, v in found:
                    known_tokens[v[:24]] = k

    if cookie_count:
        # cookies sent on *many different* requests are session/auth cookies, not one-off state
        F.auth["cookies"] = {n: c for n, c in cookie_count.items() if c >= max(3, len(entries) * 0.3)}
    if bearer_count:
        tok, n = max(bearer_count.items(), key=lambda kv: kv[1])
        F.auth["bearer"] = {"prefix": tok, "seen_on_requests": n,
                             "matches_login_response_field": known_tokens.get(tok)}
    F.auth["tokens"] = set(known_tokens.values())
    F.har_stats = {"total": len(entries), "matched": matched, "by_resource_type": dict(rt_tally),
                   "tagged_pct": round(100 * had_rt_field / len(entries), 1) if entries else 0}


def browse(url, F, headers):
    from playwright.sync_api import sync_playwright   # pip install playwright && playwright install chromium
    scripts = set()
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_context(extra_http_headers=headers).new_page()
        pg.on("request", lambda r: r.resource_type in ("fetch", "xhr", "eventsource") and
              add_observed(F, r.method, r.url, "browser"))
        pg.on("websocket", lambda ws: add_observed(F, "WS", ws.url, "browser"))
        pg.on("response", lambda r: r.request.resource_type == "script" and scripts.add(r.url))
        pg.goto(url, wait_until="networkidle")
        pg.wait_for_timeout(2000)
        b.close()
    return scripts


PROBES = ["openapi.json", "swagger.json", "swagger/v1/swagger.json", "v2/api-docs", "v3/api-docs", "api-docs",
          "api/docs", "docs", "redoc", "swagger-ui/", "swagger-ui.html", "graphql", "graphiql", "api", "api/v1",
          ".well-known/openid-configuration", "actuator", "actuator/health", "robots.txt", "sitemap.xml",
          "asset-manifest.json", "manifest.json", ".vite/manifest.json"]


def probe(origin, http, F, baseline, queue):
    for path in PROBES:
        url = urljoin(origin + "/", path)
        st, ct, body = http.get(url)
        if not st:
            F.checked.append((url, "ERR", "no response / network error")); continue
        if st != 200:
            F.checked.append((url, st, "skip")); continue
        if not body:
            F.checked.append((url, st, "skip (empty body)")); continue
        if body[:300] == baseline[:300]:
            F.checked.append((url, st, "skip (SPA fallback, same as /)")); continue
        F.checked.append((url, st, f"hit, {ct.split(';')[0] or 'unknown type'}"))
        F.docs.append((url, ct.split(";")[0]))
        if path.endswith("manifest.json"):
            queue += [urljoin(url, s) for s in re.findall(r'"([^"]+\.m?js)"', body)]
        try:
            j = json.loads(body)
        except ValueError:
            continue
        if isinstance(j, dict) and isinstance(j.get("paths"), dict):         # OpenAPI / Swagger
            for pth, ops in j["paths"].items():
                for meth in (ops if isinstance(ops, dict) else {}):
                    if meth.lower() in ("get", "post", "put", "patch", "delete", "head", "options"):
                        F.add(meth.upper(), "", normalize(pth), url, "openapi", 6)


# ───────────────────────── crawl ─────────────────────────
def discover(page, http, opts, F, extra_scripts=()):
    st, ct, html = http.get(page)
    if not st:
        print(f"[!] cannot fetch {page}", file=sys.stderr)
        return
    origin = "{0.scheme}://{0.netloc}".format(urlparse(page))
    PAGE_HOSTS.add(urlparse(page).netloc)
    pp = PageParser()
    pp.feed(html)
    scan(html, page, F)                                   # inline scripts, data-attrs, htmx...
    for c in pp.comments:
        if COMMENT_INTEREST.search(c) and texty(c) and len(c) > 8:
            F.comments[re.sub(r'\s+', ' ', c).strip()[:240]].add(page + " (html comment)")
            comment_paths(c, page + " (html comment)", F)
    for f in pp.forms:
        host, path = split_url(urljoin(page, f["action"] or "") if f["action"] else urlparse(page).path or "/")
        if urlparse(page).netloc == host:
            host = ""
        F.add(f["method"], host, path, f"{page} <form> fields={f['fields']}", "form", 4)

    queue = [urljoin(page, s) for s in pp.scripts] + list(extra_scripts)
    allowed = {urlparse(u).netloc for u in queue} | {urlparse(page).netloc}
    roots = [origin]
    if opts.probe:
        probe(origin, http, F, http.get(origin + "/")[2], queue)
        allowed |= {urlparse(u).netloc for u in queue}

    seen = set()

    def work(u):
        status, _, text = http.get(u)
        smap = None
        if status == 200 and text:
            m = SOURCEMAP_RE.search(text[-2000:])
            if m:
                ref = m.group(1)
                if ref.startswith("data:"):
                    try:
                        smap = (u + "(inline-map)", base64.b64decode(ref.split(",", 1)[1]).decode("utf-8", "replace"))
                    except Exception:
                        pass
                else:
                    mu = urljoin(u, ref)
                    ms, _, mt = http.get(mu)
                    if ms == 200:
                        smap = (mu, mt)
        return u, status, text, smap

    with ThreadPoolExecutor(max_workers=8) as pool:
        while queue and len(seen) < opts.max_files:
            batch = []
            for u in queue:
                u = u.split("#")[0]
                if u not in seen and urlparse(u).netloc in allowed and len(seen) + len(batch) < opts.max_files:
                    batch.append(u)
                    seen.add(u)
            queue = []
            for u, status, text, smap in pool.map(work, batch):
                if status != 200 or not text:
                    F.checked.append((u, status or "ERR", "skip (not fetched / empty)"))
                    continue
                note = f"scanned, {len(text) // 1024} KB" + (" +sourcemap" if smap else "")
                F.checked.append((u, status, note))
                if not opts.quiet:
                    print(f"  scanned {u} ({len(text) // 1024} KB){' +sourcemap' if smap else ''}", file=sys.stderr)
                scan(text, u, F)
                queue += chunk_candidates(text, u, roots)
                if smap:
                    process_map(*smap, F, opts.vendor)


def process_map(map_url, raw, F, vendor):
    try:
        m = json.loads(raw)
    except ValueError:
        return
    srcs, contents = m.get("sources", []), m.get("sourcesContent") or []
    for i, name in enumerate(srcs):
        clean = re.sub(r'^(webpack://[^/]*/|\.\./)+', '', name).lstrip("./")
        if "node_modules" in name and not vendor:
            continue
        if INTERESTING_FILE.search(clean):
            F.files.add(clean)
        if i < len(contents) and contents[i]:
            scan(contents[i], f"{map_url}::{clean}", F)



LOGIN_HELP = """
== Getting past login ==

Fastest and most complete: capture a HAR, not just a cookie.
  1. Open DevTools -> Network tab -> check "Preserve log".
  2. Log in normally in the browser, click around a bit (dashboard, a couple
     of pages that need auth) so the calls you care about actually fire.
  3. Right-click any request in the list -> "Save all as HAR with content".
  4. Re-run this tool with  --har that.har
This finds the login call itself, which request fields look like credentials,
which response field holds the token, and whether auth travels as a
Bearer header or a cookie -- then tells you the exact -H to replay it with.
(A HAR can contain real passwords/tokens in plaintext -- treat it like a
credential file, don't commit it, delete it when done.)

If you'd rather not touch a browser, curl can do the login itself, as long
as it's a plain POST (no JS challenge/CAPTCHA/2FA in front of it):

  # cookie-based session:
  curl -c cookies.txt -X POST https://target/api/login \
       -H 'Content-Type: application/json' \
       -d '{"username":"you","password":"..."}'
  # ...then reuse the jar on every later call:
  curl -b cookies.txt https://target/api/me

  # token-based (JWT/bearer) session:
  TOKEN=$(curl -s -X POST https://target/api/login \
       -H 'Content-Type: application/json' \
       -d '{"username":"you","password":"..."}' | python3 -c \
       'import sys,json;print(json.load(sys.stdin)["accessToken"])')
  curl -H "Authorization: Bearer $TOKEN" https://target/api/me

  # then hand either to this tool:
  apidisco.py https://target/page -H "Cookie: $(cat cookies.txt | awk '{print $6\"=\"$7}' | tr '\n' ';')"
  apidisco.py https://target/page -H "Authorization: Bearer $TOKEN"

Either way, -H only goes to hosts you pass on the command line (see Http.get),
so a leaked token never gets sent to some third-party script's CDN.
"""


def merge_bases(F):
    """axios.create({baseURL:'/api'}) means a relative call to '/common/x' is really '/api/common/x'.
    Where both forms were found (e.g. static scan sees the relative path, a HAR sees the real request),
    merge them into one entry. Where only the relative one was found, just note the likely real path."""
    if not F.bases:
        return
    for base in sorted(F.bases, key=len, reverse=True):
        for (m, h, p) in list(F.eps):
            if h or p.startswith(base + "/") or p == base:
                continue
            full = base + p
            if (m, h, full) in F.eps:
                src, dst = F.eps.pop((m, h, p)), F.eps[(m, h, full)]
                dst["src"] = list(dict.fromkeys(dst["src"] + src["src"]))[:6]
                dst["kinds"] |= src["kinds"]
                dst["extra"] |= src["extra"]
                dst["score"] = max(dst["score"], src["score"])
            elif ("?", h, full) not in F.eps:
                F.eps[(m, h, p)]["extra"].add(f"likely requested as {full} (baseURL '{base}' detected)")


SCHEMA_SUFFIX = {"body": "Body", "params": "Params", "form": "Form", "config": "Config", "fields": "Fields"}


def build_schemas(F, min_uses=2):
    """Calls that send the same set of fields (the same request shape) get a shared, named type
    instead of repeating the field list at every call site — e.g. 3 endpoints all sending
    (taskID, file) become one 'TaskFileForm', referenced as 'form=TaskFileForm' at each one."""
    sigs = {}   # (kind, fields) -> {"fields", "kind", "users": [(m,h,p), ...]}
    refs = defaultdict(list)   # (m,h,p) -> [(raw_extra_str, sig_key), ...]
    for key_mhp, e in F.eps.items():
        for item in e["extra"]:
            m_ = re.match(r'^(body|params|form|config|fields)=(.+)$', item)
            if not m_:
                continue
            kind, fields = m_.group(1), tuple(m_.group(2).split(","))
            sig_key = (kind, fields)
            sigs.setdefault(sig_key, {"fields": fields, "kind": kind, "users": []})["users"].append(key_mhp)
            refs[key_mhp].append((item, sig_key))

    names, used = {}, set()
    for sig_key, sig in sigs.items():
        if len(sig["users"]) < min_uses:
            continue
        m, h, p = sig["users"][0]
        seg = [s for s in p.split("?")[0].strip("/").split("/") if s and s != "{}"]
        base = "".join(w.capitalize() for w in re.split(r"[-_]", seg[-1])) if seg else "Root"
        name, n = base + SCHEMA_SUFFIX[sig["kind"]], 2
        while name in used:
            name, n = f"{base}{SCHEMA_SUFFIX[sig['kind']]}{n}", n + 1
        used.add(name)
        names[sig_key] = name
        F.schemas[name] = {"kind": sig["kind"], "fields": list(sig["fields"]), "used_by": sig["users"]}

    for key_mhp, items in refs.items():
        e = F.eps[key_mhp]
        for raw, sig_key in items:
            if sig_key in names:
                e["extra"].discard(raw)
                e["extra"].add(f"{sig_key[0]}={names[sig_key]}")


# ───────────────────────── report ─────────────────────────
RANK = {"high": 0, "med": 1, "low": 2, "skip": 3}


def conf(score):
    return "high" if score >= 4 else "med" if score >= 2 else "low" if score >= 0 else "skip"


def report(F, opts, page_hosts):
    specific = {(h, p) for (m, h, p) in F.eps if m != "?"}
    by_host = defaultdict(list)
    for (m, h, p), e in F.eps.items():
        if m == "?" and (h, p) in specific:
            continue
        c = conf(e["score"])
        if RANK[c] <= RANK[opts.min]:
            by_host[h or "(same origin)"].append((RANK[c], p, m, c, e))

    def host_order(h):
        return (0 if h == "(same origin)" else 1 if h in page_hosts else 2, h)

    out = []
    for h in sorted(by_host, key=host_order):
        print(f"\n== {h} ==")
        for _, p, m, c, e in sorted(by_host[h]):
            more = f" +{len(e['src']) - 1}" if len(e["src"]) > 1 else ""
            extra = ("  (" + "; ".join(sorted(e["extra"])) + ")") if e.get("extra") else ""
            print(f"[{c.upper():4}] {m:<6} {p}{extra}\n         {e['src'][0]}{more}  [{','.join(sorted(e['kinds']))}]")
            out.append({"host": h, "method": m, "path": p, "confidence": c, "kinds": sorted(e["kinds"]),
                        "sources": e["src"], "fields": sorted(e.get("extra", []))})
    if F.har_stats:
        s = F.har_stats
        print(f"\n== HAR coverage ==")
        print(f"  {s['total']} entries in the HAR, {s['matched']} treated as API calls "
              f"({s['tagged_pct']}% had Chrome's _resourceType tag; {100 - s['tagged_pct']:.1f}% relied on "
              f"mime-type/'/api/'-path fallback — if that's low and you're missing endpoints, this exporter "
              f"doesn't tag requests, which is normal for Firefox).")
        print("  by resource type: " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_resource_type"].items(), key=lambda kv: -kv[1])))
    if F.auth["logins"] or F.auth["cookies"] or F.auth["bearer"]:
        print("\n== Login / session (from HAR) ==")
        for lg in F.auth["logins"]:
            print(f"  {lg['method']} {lg['host']}{lg['path']}")
            if lg["request_fields"]:
                print(f"      sends:     {', '.join(lg['request_fields'])}")
            if lg["response_token_fields"]:
                print(f"      returns:   {', '.join(lg['response_token_fields'])}  <- likely the session token")
            if lg["set_cookies"]:
                print(f"      sets cookie(s): {', '.join(lg['set_cookies'])}")
        if F.auth["bearer"]:
            b = F.auth["bearer"]
            note = f" (matches '{b['matches_login_response_field']}' from the login response)" if b["matches_login_response_field"] else ""
            print(f"  Authorization: Bearer {b['prefix']}...  seen on {b['seen_on_requests']} requests{note}")
            print(f"      -> replay with:  -H \"Authorization: Bearer <token>\"")
        if F.auth["cookies"]:
            print(f"  Session cookie(s), sent repeatedly: {', '.join(F.auth['cookies'])}")
            print(f"      -> replay with:  -H \"Cookie: name=value; name2=value2\"")
    if F.gql:
        print("\n== GraphQL operations ==")
        for op, s in sorted(F.gql.items()):
            print(f"  {op:<32} {sorted(s)[0]}")
    if F.docs:
        print("\n== Docs / well-known paths that answered 200 ==")
        for u, t in F.docs:
            print(f"  {u}  ({t})")
    if F.files:
        print("\n== Interesting original source files (from source maps) ==")
        for f in sorted(F.files):
            print("  " + f)
    if F.schemas:
        print("\n== Request shapes (reused across calls) ==")
        for name, s in sorted(F.schemas.items()):
            print(f"  {name}  ({s['kind']}: {', '.join(s['fields'])})")
            for m, h, p in s["used_by"]:
                print(f"      {m:<6} {(h or '') + p}")
    if F.comments:
        print("\n== Comments worth reading ==")
        for c, s in sorted(F.comments.items(), key=lambda x: sorted(x[1])[0]):
            print(f"  - {c}\n      {sorted(s)[0]}")
    if F.checked:
        hits = [(u, s, n) for u, s, n in F.checked if isinstance(s, int) and s == 200 and "skip" not in n]
        skips = [(u, s, n) for u, s, n in F.checked if (u, s, n) not in hits]
        print(f"\n== Resources checked ({len(F.checked)} total: {len(hits)} scanned, {len(skips)} skipped) ==")
        if opts.verbose:
            for u, s, n in F.checked:
                print(f"  [{s}] {u}  - {n}")
        else:
            for u, s, n in hits[:15]:
                print(f"  [{s}] {u}  - {n}")
            if len(hits) > 15:
                print(f"  ... +{len(hits) - 15} more scanned (use --verbose to list all, scanned and skipped)")
            if skips:
                by_reason = defaultdict(int)
                for _, _, n in skips:
                    by_reason[n.split("(")[0].strip()] += 1
                print("  skipped: " + ", ".join(f"{n} x{c}" for n, c in sorted(by_reason.items())))
    print(f"\n{len(out)} endpoints, {len(F.comments)} comments, {len(F.gql)} graphql ops.")
    if opts.json:
        json.dump({"endpoints": out, "comments": {k: sorted(v) for k, v in F.comments.items()},
                   "graphql": {k: sorted(v) for k, v in F.gql.items()}, "source_files": sorted(F.files),
                   "docs": F.docs, "checked": F.checked,
                   "schemas": {n: {"kind": s["kind"], "fields": s["fields"],
                                   "used_by": [{"method": m, "host": h, "path": p} for m, h, p in s["used_by"]]}
                               for n, s in F.schemas.items()}},
                  open(opts.json, "w"), indent=2)
        print(f"wrote {opts.json}")
    if opts.out:
        with open(opts.out, "w") as f:
            for row in out:
                host = row["host"] if row["host"] != "(same origin)" else ""
                f.write(f"{row['method']}\t{host}{row['path']}\t{row['sources'][0]}\n")
        print(f"wrote {opts.out} ({len(out)} endpoints)")


def main():
    ap = argparse.ArgumentParser(description="Discover API endpoints referenced by a web page.")
    ap.add_argument("urls", nargs="*")
    ap.add_argument("-H", "--header", action="append", default=[], help='e.g. -H "Cookie: sid=..." (sent only to the page host)')
    ap.add_argument("--probe", action="store_true", help="also GET well-known doc paths (swagger, openapi, graphql...)")
    ap.add_argument("--browser", action="store_true", help="load the page in Playwright and record real calls")
    ap.add_argument("--har", help="merge a HAR file exported from DevTools after clicking around")
    ap.add_argument("--login-help", action="store_true", help="print how to capture a login via browser HAR or curl, then exit")
    ap.add_argument("--vendor", action="store_true", help="also analyze node_modules in source maps")
    ap.add_argument("--max-files", type=int, default=300)
    ap.add_argument("--min", choices=["high", "med", "low"], default="low", help="minimum confidence shown")
    ap.add_argument("--json", help="write full results (everything in this report) to this file")
    ap.add_argument("-o", "--out", help="write a plain METHOD/host+path/one-source-each list to this file")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every resource checked, not just a sample")
    opts = ap.parse_args()
    if opts.login_help:
        print(LOGIN_HELP)
        return
    if not opts.urls:
        ap.error("the following arguments are required: urls")

    hdrs = dict(h.split(":", 1) for h in opts.header)
    hdrs = {k.strip(): v.strip() for k, v in hdrs.items()}
    hosts = {urlparse(u).netloc for u in opts.urls}
    PAGE_HOSTS.update(hosts)    # so --browser / --har observed calls to the page's own host merge, not duplicate
    http, F = Http(hdrs, opts.insecure, hosts), Findings()

    for url in opts.urls:
        extra = browse(url, F, hdrs) if opts.browser else ()
        if not opts.quiet:
            print(f"[*] {url}", file=sys.stderr)
        discover(url, http, opts, F, extra)
    if opts.har:
        load_har(opts.har, F)
    merge_bases(F)
    build_schemas(F)
    report(F, opts, hosts)

    if not opts.har and not hdrs:
        login_eps = [p for (m, h, p) in F.eps if LOGIN_PATH_RE.search(p)]
        if login_eps:
            print(f"\n[*] found {len(login_eps)} login-shaped endpoint(s) (e.g. {login_eps[0]}) but no "
                  f"-H/--har was given, so everything behind auth is invisible here.\n"
                  f"    run with --login-help to see how to capture a session.", file=sys.stderr)


if __name__ == "__main__":
    main()
