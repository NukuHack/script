#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
speci_stuki.py
--------------
Turn C# method(s) into a shareable progalap.elte.hu/stuki structogram URL
(?data=<gzip+base64 of the app's own Redux state>). Every method found in
the input becomes its own structogram; they all load into the editor
together, side by side, in one URL.

Scope / honesty about limits:
  - Standard library only, no real C# compiler/parser behind this - it's a
    hand-rolled recursive-descent scanner tuned for typical "textbook
    algorithm" code (loops, ifs, switches, simple decls, simple I/O).
  - Supports: if/else if/else, while, do-while, for (desugared into an
    init statement + pre-test while + trailing increment), foreach
    (rendered as a pre-test loop over `x in collection`, with x pulled
    into the variables box), switch/case/default, break/continue as
    literal statements, local variable declarations pulled into the
    "variables" box with real C# types, and a trailing top-level `return`
    becomes the structogram's footer instead of a body statement.
  - try/catch/finally, using/lock/fixed, checked/unchecked/unsafe are
    flattened: their block bodies become ordinary sequential statements
    (the try/catch/lock/etc. semantics is lost, but no statements are
    dropped and parsing stays honest).
  - I/O sugar: Console.WriteLine(...)/Console.Write(...) become "out ...";
    a bare Console.ReadLine()/Read()/ReadKey() or `x = Console.ReadLine();`
    become "inp" / "inp x". Wrapping conversions (int.Parse(...),
    double.TryParse(...), Convert.ToInt32(...), etc.) are stripped away as
    pure C#-isms that add no algorithmic meaning - `int.Parse(Console.ReadLine())`
    collapses straight to `inp x`; a `Type.TryParse(...)` call just has its
    type-qualifier dropped (TryParse(...)) since it can't cleanly collapse
    to one value (it has an out-parameter and a bool result).
  - "Static" is handled the boring way: a `static` modifier on a method is
    just one of the modifiers the signature scanner already skips over, and
    any other static reference (Math.Sqrt(x), Console itself, a call to
    another static helper, a static field) is not treated specially at all
    - it is simply kept verbatim as ordinary statement/condition text,
    which is the same fallback every unrecognized expression gets.
  - Preprocessor directives #region/#endregion/#pragma/#nullable/#define/
    #undef are stripped before parsing (they carry no algorithmic meaning);
    #if/#else/#endif are kept so both sides of a conditional still appear.
  - Not supported: lambdas/LINQ, properties, attributes, nested local
    functions, multi-declarator statements with non-builtin types (only the
    first initializer survives). Anything genuinely unrecognized is kept as
    a literal statement line rather than crashing.

Reverse direction (--decode): a progalap.elte.hu URL can be turned back into
its content - a specifikacio URL gives back the exact specification text, a
stuki URL gives back the structogram state as JSON (the original C# is not
stored in the URL, so it cannot be restored).  Example:
    speci_stuki.py --decode 'https://progalap.elte.hu/specifikacio/v1/?data=...'

Usage: run with no arguments for the full help text. This file also builds
progalap.elte.hu/specifikacio URLs from specification text (the old
str2speci.py is merged in).
"""
from __future__ import print_function

import sys
import re
import io
import json
import gzip
import base64
import random
import zlib

try:                                   # Python 3 (kept working too)
    from urllib.parse import quote, unquote
except ImportError:                    # Python 2.7
    from urllib import quote, unquote


# --- Compact (STUKI1) form ------------------------------------------------
# base64url(raw-DEFLATE(JSON)), "STUKI1:" prefix.  This is byte-for-byte
# what compact.rs + format.rs::to_compact() produce on the Rust side.
STUKI_PREFIX = "STUKI1:"

def _b64url_encode(data):
    return (base64.b64encode(data).decode("ascii")
            .rstrip("=").replace("+", "-").replace("/", "_"))

def _b64url_decode(s):
    # tolerate standard base64 and any missing padding too
    s = s.rstrip("=").replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * ((-len(s)) % 4))

def _raw_deflate(data):                     # == miniz_oxide::deflate::compress_to_vec
    c = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return c.compress(data) + c.flush()

def _raw_inflate(data):                     # == compact::inflate
    return zlib.decompress(data, -zlib.MAX_WBITS)


def state_to_compact(state, prefix=STUKI_PREFIX):
    """The web-Stuki state as one clipboard line: STUKI1: + b64url(deflate(json))."""
    j = json.dumps(state, separators=(",", ":")).encode("utf-8")
    return prefix + _b64url_encode(_raw_deflate(j))

def compact_to_state(line, prefix=STUKI_PREFIX):
    """Read it back (chat apps love wrapping long lines -> strip whitespace)."""
    line = "".join(line.split())
    if not line.startswith(prefix):
        raise ValueError("not a %s line" % prefix)
    return json.loads(_raw_inflate(_b64url_decode(line[len(prefix):])).decode("utf-8"))


def csharp_source_to_compact(source):
    return state_to_compact(csharp_source_to_state(source))


BASE62 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def rand_id(n=8):
    return "".join(random.choice(BASE62) for _ in range(n))


# Preprocessor directives that carry no code (safe to drop before parsing).
# We deliberately do NOT include #if/#else/#elif/#endif: those gate code that
# should still be visible on both sides.
PREPROCESSOR_RE = re.compile(
    r"^[ \t]*#(?:region|endregion|pragma|nullable|define|undef)\b[^\n]*$",
    re.MULTILINE,
)


def strip_comments(src):
    """Remove // line comments, /* */ block comments, and preprocessor
    directives that carry no algorithmic meaning.  String/char aware."""
    src = PREPROCESSOR_RE.sub("", src)
    out = []
    i, n = 0, len(src)
    in_str = in_chr = False
    while i < n:
        c = src[i]
        if not in_str and not in_chr and c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if not in_str and not in_chr and c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if not in_chr and c == '"':
            in_str = not in_str
        elif not in_str and c == "'":
            in_chr = not in_chr
        out.append(c)
        i += 1
    return "".join(out)


def find_matching(s, open_idx):
    """s[open_idx] is one of ( [ { -- return index of its match, string/char aware."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    open_ch = s[open_idx]
    close_ch = pairs[open_ch]
    depth = 0
    i = open_idx
    in_str = in_chr = False
    n = len(s)
    while i < n:
        c = s[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif in_chr:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_chr = False
        else:
            if c == '"':
                in_str = True
            elif c == "'":
                in_chr = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    raise ValueError("Unbalanced %r starting at %d" % (open_ch, open_idx))


def find_stmt_end(s, i, limit):
    """Index of the ';' terminating the statement starting at i (depth/string aware)."""
    depth = 0
    in_str = in_chr = False
    j = i
    while j < limit:
        c = s[j]
        if in_str:
            if c == "\\":
                j += 2
                continue
            if c == '"':
                in_str = False
        elif in_chr:
            if c == "\\":
                j += 2
                continue
            if c == "'":
                in_chr = False
        else:
            if c == '"':
                in_str = True
            elif c == "'":
                in_chr = True
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == ";" and depth == 0:
                return j
        j += 1
    return limit


def split_top_level(s, sep):
    parts, depth, start = [], 0, 0
    in_str = in_chr = False
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif in_chr:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_chr = False
        else:
            if c == '"':
                in_str = True
            elif c == "'":
                in_chr = True
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == sep and depth == 0:
                parts.append(s[start:i])
                start = i + 1
        i += 1
    parts.append(s[start:])
    return parts


def skip_ws(s, i, limit):
    while i < limit and s[i] in " \t\r\n":
        i += 1
    return i


IDENT_RE = re.compile(r"[A-Za-z_]\w*")

METHOD_SIG_RE = re.compile(
    r"(?:public|private|protected|internal|static|virtual|override|sealed|abstract|async|unsafe|extern|new|partial|\s)*"
    r"([\w\.\<\>\[\],\s]+?)\s+(\w+)\s*\("
)


def peek_word(s, i):
    m = IDENT_RE.match(s, i)
    return m.group(0) if m else None


def match_word(s, i, word):
    return s[i:i + len(word)] == word and (
        i + len(word) >= len(s) or not (s[i + len(word)].isalnum() or s[i + len(word)] == "_")
    )


# ---------------------------------------------------------------------------
# I/O + conversion-call simplification
#   Console.WriteLine/Write        -> "out ..."
#   Console.ReadLine/Read/ReadKey  -> "inp" / "inp <target>"
#   TypeName.Parse(x) / Convert.ToXxx(x) -> x  (unwrapped, recursively)
#   TypeName.TryParse(...)         -> TryParse(...)  (qualifier dropped only)
#   ref/out/in parameter modifiers -> dropped (cosmetic)
# ---------------------------------------------------------------------------

BUILTIN_NUMERIC = r"(?:int|uint|long|ulong|short|ushort|byte|sbyte|double|float|decimal|bool|char)"
PARSE_WRAPPER_RE = re.compile(
    r"\b" + BUILTIN_NUMERIC + r"\.Parse\s*\("
    r"|\bConvert\.To(?:Int32|Int64|Int16|Byte|SByte|UInt32|UInt64|UInt16|Double|Single|Decimal|Boolean|Char|String)\s*\("
)
TRYPARSE_QUALIFIER_RE = re.compile(r"\b" + BUILTIN_NUMERIC + r"\.TryParse\b")
REF_OUT_IN_RE = re.compile(r"\b(?:ref|out|in)\s+(?=[A-Za-z_])")
CONSOLE_WRITE_RE = re.compile(r"^Console(\.Out|\.Error)?\.(?:WriteLine|Write)\s*\(")
# Console stream references inside expressions -> STDIN / STDOUT / STDERR
CONSOLE_IN_CALL_RE = re.compile(r"\bConsole\s*\.\s*(?:In\s*\.\s*)?(?:ReadLine|Read|ReadKey)\s*\([^()]*\)")
CONSOLE_IN_RE = re.compile(r"\bConsole\s*\.\s*In\b")
CONSOLE_OUT_RE = re.compile(r"\bConsole\s*\.\s*Out\b")
CONSOLE_ERR_RE = re.compile(r"\bConsole\s*\.\s*Error\b")
CONSOLE_READ_RE = re.compile(r"^Console(?:\.In)?\.(ReadLine|Read|ReadKey)\s*\(")
ASSIGN_RE = re.compile(r"^([A-Za-z_]\w*(?:\[[^\]]*\])?)\s*=\s*(.+)$", re.S)


def strip_parse_wrappers(text):
    guard = 0
    while guard < 25:
        m = PARSE_WRAPPER_RE.search(text)
        if not m:
            break
        open_paren = text.index("(", m.start())
        try:
            close_paren = find_matching(text, open_paren)
        except ValueError:
            break
        inner = text[open_paren + 1:close_paren].strip()
        text = text[:m.start()] + inner + text[close_paren + 1:]
        guard += 1
    return text


def replace_console_streams(text):
    text = CONSOLE_IN_CALL_RE.sub("STDIN", text)
    text = CONSOLE_IN_RE.sub("STDIN", text)
    text = CONSOLE_OUT_RE.sub("STDOUT", text)
    text = CONSOLE_ERR_RE.sub("STDERR", text)
    return text


def simplify_generic(text, console=True):
    """Applies to BOTH statement text and condition text.
    console=False leaves Console.* alone (simplify_stmt needs to see
    `x = Console.ReadLine()` before it turns into STDIN)."""
    text = strip_parse_wrappers(text)
    text = TRYPARSE_QUALIFIER_RE.sub("TryParse", text)
    text = REF_OUT_IN_RE.sub("", text)
    if console:
        text = replace_console_streams(text)
    return text.strip()


def _whole_call_span(text, m):
    """Given a regex match at the start of text for `Name(`, return (open, close)
    of that call's parens if the call spans to the end of (trimmed) text, else None."""
    open_paren = text.index("(", m.start())
    try:
        close_paren = find_matching(text, open_paren)
    except ValueError:
        return None
    if close_paren == len(text.rstrip()) - 1:
        return open_paren, close_paren
    return None


def simplify_stmt(text):
    """Statement-only simplification: whole-statement Console I/O -> inp/out."""
    text = text.strip()

    m = CONSOLE_WRITE_RE.match(text)
    if m:
        span = _whole_call_span(text, m)
        if span:
            inner = simplify_generic(text[span[0] + 1:span[1]].strip())
            prefix = "STDERR.out" if m.group(1) == ".Error" else "out"
            return prefix + " " + inner if inner else prefix

    m = CONSOLE_READ_RE.match(text)
    if m and _whole_call_span(text, m):
        return "inp"

    simplified = simplify_generic(text, console=False)

    m = ASSIGN_RE.match(simplified)
    if m:
        target, rhs = m.group(1), m.group(2).strip()
        m2 = CONSOLE_READ_RE.match(rhs)
        if m2 and _whole_call_span(rhs, m2):
            return "inp " + target

    return replace_console_streams(simplified).strip()


def simplify_cond(text):
    return simplify_generic(text.strip())


# ---------------------------------------------------------------------------
# Phase 1: scan C# source -> lightweight python AST (per method)
# ---------------------------------------------------------------------------

class MethodBodyParser(object):
    """Parses ONE method body (statements only - the signature has already
    been located by find_all_methods)."""

    def __init__(self):
        self.variables = []       # "name : Type" strings, in declared order
        self.pending_return = None

    def add_variable(self, name, typ):
        """Record a variable once; repeated declarations (e.g. `int i` in
        several sibling for-loops) collapse into a single entry."""
        entry = "%s : %s" % (name, typ)
        if entry not in self.variables:
            self.variables.append(entry)

    def parse_params(self, params_str):
        for p in split_top_level(params_str, ","):
            p = p.strip()
            if not p:
                continue
            p = re.sub(r"^(ref|out|in|params)\s+", "", p)
            if "=" in p:
                p = p.split("=", 1)[0].strip()
            tokens = p.rsplit(None, 1)
            if len(tokens) == 2:
                typ, name = tokens
            else:
                typ, name = "var", tokens[0]
            self.add_variable(name.strip(), typ.strip())

    def parse_statements(self, s, i, limit, top_level=False):
        items = []
        pos = i
        while True:
            pos = skip_ws(s, pos, limit)
            if pos >= limit:
                break
            new_items, pos = self.parse_one_statement(s, pos, limit, top_level)
            items.extend(new_items)
        return items

    def parse_body(self, s, pos, limit):
        pos = skip_ws(s, pos, limit)
        if pos < limit and s[pos] == "{":
            close = find_matching(s, pos)
            items = self.parse_statements(s, pos + 1, close)
            return items, close + 1
        return self.parse_one_statement(s, pos, limit, top_level=False)

    def parse_one_statement(self, s, pos, limit, top_level):
        word = peek_word(s, pos)
        if word == "if":
            node, pos2 = self.parse_if(s, pos, limit)
            return [node], pos2
        if word == "while":
            node, pos2 = self.parse_while(s, pos, limit)
            return [node], pos2
        if word == "do":
            node, pos2 = self.parse_do_while(s, pos, limit)
            return [node], pos2
        if word == "for":
            nodes_list, pos2 = self.parse_for(s, pos, limit)
            return nodes_list, pos2
        if word == "foreach":
            node, pos2 = self.parse_foreach(s, pos, limit)
            return [node], pos2
        if word == "switch":
            node, pos2 = self.parse_switch(s, pos, limit)
            return [node], pos2
        if word in ("try", "catch", "finally"):
            # Flatten try/catch/finally into one sequence of the try body, then
            # each catch body, then the finally body.  Loses the exception
            # routing, keeps every statement and keeps parsing sane.
            return self._parse_try_chain(s, pos, limit, word)
        if word in ("using", "lock", "fixed"):
            # `<kw> (expr) stmt-or-block` -> flatten to just the body.
            pos2 = pos + len(word)
            pos2 = skip_ws(s, pos2, limit)
            if pos2 < limit and s[pos2] == "(":
                close = find_matching(s, pos2)
                pos2 = skip_ws(s, close + 1, limit)
            return self.parse_body(s, pos2, limit)
        if word in ("checked", "unchecked", "unsafe"):
            # `checked { ... }` is a block; `checked(expr)` is a statement.
            pos2 = skip_ws(s, pos + len(word), limit)
            if pos2 < limit and s[pos2] == "{":
                return self.parse_body(s, pos2, limit)
            # otherwise fall through to plain-statement handling
        if s[pos] == "{":
            close = find_matching(s, pos)
            return self.parse_statements(s, pos + 1, close), close + 1
        if word in ("break", "continue"):
            semi = find_stmt_end(s, pos, limit)
            return [{"kind": "stmt", "text": s[pos:semi].strip()}], semi + 1
        if word == "return":
            semi = find_stmt_end(s, pos, limit)
            expr = simplify_generic(s[pos + 6:semi].strip()) if s[pos + 6:semi].strip() else ""
            new_pos = semi + 1
            if top_level and s[new_pos:limit].strip() == "":
                self.pending_return = expr
                return [], new_pos
            text = "return " + expr if expr else "return"
            return [{"kind": "stmt", "text": text}], new_pos
        # plain statement / declaration
        semi = find_stmt_end(s, pos, limit)
        raw = s[pos:semi].strip()
        new_pos = semi + 1
        if raw == "":
            return [], new_pos
        decl_items, is_decl = self.try_parse_declaration(raw)
        if is_decl:
            return decl_items, new_pos
        return [{"kind": "stmt", "text": simplify_stmt(raw)}], new_pos

    def _parse_try_chain(self, s, pos, limit, first_word):
        """Flatten a try/catch/finally chain into one sequential item list."""
        def parse_clause(p):
            p += len(first_word) if first_word == peek_word(s, p) else 0
            # (when called recursively for a catch/finally we pass the real word)
            p = skip_ws(s, p, limit)
            if p < limit and s[p] == "(":
                close = find_matching(s, p)
                p = skip_ws(s, close + 1, limit)
            return self.parse_body(s, p, limit)

        # First clause
        p = pos
        kw = peek_word(s, p)
        p += len(kw)
        p = skip_ws(s, p, limit)
        if p < limit and s[p] == "(":
            close = find_matching(s, p)
            p = skip_ws(s, close + 1, limit)
        items, p = self.parse_body(s, p, limit)
        all_items = list(items)

        # Trailing catch/finally clauses
        while True:
            p2 = skip_ws(s, p, limit)
            nxt = peek_word(s, p2)
            if nxt not in ("catch", "finally"):
                break
            p2 += len(nxt)
            p2 = skip_ws(s, p2, limit)
            if p2 < limit and s[p2] == "(":
                close = find_matching(s, p2)
                p2 = skip_ws(s, close + 1, limit)
            more, p = self.parse_body(s, p2, limit)
            all_items.extend(more)
        return all_items, p

    def try_parse_declaration(self, raw):
        m = re.match(
            r"^(var|bool|byte|sbyte|char|decimal|double|float|int|uint|long|ulong|short|ushort|string|object|dynamic)\s+(.+)$",
            raw,
        )
        if not m:
            m = re.match(r"^([A-Za-z_]\w*(?:<[^=;]+>)?(?:\[\s*\])?)\s+([A-Za-z_]\w*\s*(?:=.*)?)$", raw)
        if not m:
            return [], False
        typ, rest = m.group(1), m.group(2)
        items = []
        any_valid = False
        for d in split_top_level(rest, ","):
            d = d.strip()
            if not d:
                continue
            if "=" in d:
                name, expr = (p.strip() for p in d.split("=", 1))
            else:
                name, expr = d, None
            if not re.match(r"^[A-Za-z_]\w*$", name):
                return [], False
            any_valid = True
            self.add_variable(name, typ)
            if expr is not None:
                items.append({"kind": "stmt", "text": simplify_stmt("%s = %s" % (name, expr))})
        return (items, True) if any_valid else ([], False)

    def parse_if(self, s, pos, limit):
        pos += 2
        pos = skip_ws(s, pos, limit)
        close_paren = find_matching(s, pos)
        cond = simplify_cond(s[pos + 1:close_paren].strip())
        pos = skip_ws(s, close_paren + 1, limit)
        true_items, pos = self.parse_body(s, pos, limit)
        pos2 = skip_ws(s, pos, limit)
        false_items = []
        if match_word(s, pos2, "else"):
            pos3 = skip_ws(s, pos2 + 4, limit)
            if match_word(s, pos3, "if"):
                nested, pos = self.parse_if(s, pos3, limit)
                false_items = [nested]
            else:
                false_items, pos = self.parse_body(s, pos3, limit)
        node = {
            "kind": "if", "cond": cond,
            "true": {"kind": "seq", "items": true_items},
            "false": {"kind": "seq", "items": false_items},
        }
        return node, pos

    def parse_while(self, s, pos, limit):
        pos += 5
        pos = skip_ws(s, pos, limit)
        close_paren = find_matching(s, pos)
        cond = simplify_cond(s[pos + 1:close_paren].strip())
        pos = skip_ws(s, close_paren + 1, limit)
        items, pos = self.parse_body(s, pos, limit)
        return {"kind": "while", "cond": cond, "body": {"kind": "seq", "items": items}}, pos

    def parse_do_while(self, s, pos, limit):
        pos += 2
        items, pos = self.parse_body(s, pos, limit)
        pos = skip_ws(s, pos, limit)
        assert match_word(s, pos, "while"), "expected 'while' after do-block"
        pos = skip_ws(s, pos + 5, limit)
        close_paren = find_matching(s, pos)
        cond = simplify_cond(s[pos + 1:close_paren].strip())
        pos = skip_ws(s, close_paren + 1, limit)
        if pos < limit and s[pos] == ";":
            pos += 1
        return {"kind": "repeat", "cond": cond, "body": {"kind": "seq", "items": items}}, pos

    def parse_for(self, s, pos, limit):
        pos += 3
        pos = skip_ws(s, pos, limit)
        close_paren = find_matching(s, pos)
        header = s[pos + 1:close_paren]
        parts = [p.strip() for p in split_top_level(header, ";")]
        while len(parts) < 3:
            parts.append("")
        init_text, cond_text, incr_text = parts[0], parts[1], parts[2]
        pos = skip_ws(s, close_paren + 1, limit)
        body_items, pos = self.parse_body(s, pos, limit)

        result = []
        if init_text:
            decl_items, is_decl = self.try_parse_declaration(init_text)
            result.extend(decl_items if is_decl else [{"kind": "stmt", "text": simplify_stmt(init_text)}])
        loop_items = list(body_items)
        if incr_text:
            loop_items.append({"kind": "stmt", "text": simplify_stmt(incr_text)})
        result.append({
            "kind": "while", "cond": simplify_cond(cond_text) if cond_text else "true",
            "body": {"kind": "seq", "items": loop_items},
        })
        return result, pos

    def parse_foreach(self, s, pos, limit):
        pos += 7
        pos = skip_ws(s, pos, limit)
        close_paren = find_matching(s, pos)
        header_raw = s[pos + 1:close_paren].strip()
        # Pull the loop variable into the variables box, and render the
        # structogram condition as `x in collection` (nicer than `var x in …`).
        m = re.match(r"^(.+?)\s+([A-Za-z_]\w*)\s+in\s+(.+)$", header_raw, re.S)
        if m:
            self.add_variable(m.group(2), m.group(1).strip())
            header = "%s in %s" % (m.group(2), simplify_cond(m.group(3).strip()))
        else:
            header = simplify_cond(header_raw)
        pos = skip_ws(s, close_paren + 1, limit)
        items, pos = self.parse_body(s, pos, limit)
        return {"kind": "while", "cond": header, "body": {"kind": "seq", "items": items}}, pos

    def parse_switch(self, s, pos, limit):
        pos += 6
        pos = skip_ws(s, pos, limit)
        close_paren = find_matching(s, pos)
        expr = simplify_generic(s[pos + 1:close_paren].strip())
        pos = skip_ws(s, close_paren + 1, limit)
        assert s[pos] == "{", "expected '{' after switch(...)"
        close_brace = find_matching(s, pos)
        raw_cases = self.parse_switch_body(s, pos + 1, close_brace)
        cases = []
        for c in raw_cases:
            cond = "%s == %s" % (expr, c["cond"]) if c["type"] == "case" else None
            cases.append({"type": c["type"], "cond": cond, "body": {"kind": "seq", "items": c["items"]}})
        return {"kind": "switch", "cases": cases}, close_brace + 1

    def parse_switch_body(self, s, i, limit):
        cases = []
        pending = []
        current_items = []

        def flush():
            if not pending:
                return
            if any(t == "default" for t, _ in pending):
                cases.append({"type": "default", "cond": None, "items": current_items[:]})
            else:
                cond = " or ".join(c for _, c in pending)
                cases.append({"type": "case", "cond": cond, "items": current_items[:]})
            del pending[:]
            del current_items[:]

        pos = i
        while True:
            pos = skip_ws(s, pos, limit)
            if pos >= limit:
                break
            if match_word(s, pos, "case"):
                if current_items:
                    flush()
                pos = skip_ws(s, pos + 4, limit)
                colon = pos
                depth = 0
                while colon < limit and not (s[colon] == ":" and depth == 0):
                    if s[colon] in "([{":
                        depth += 1
                    elif s[colon] in ")]}":
                        depth -= 1
                    colon += 1
                cond = s[pos:colon].strip()
                pos = colon + 1
                pending.append(("case", cond))
                continue
            if match_word(s, pos, "default"):
                if current_items:
                    flush()
                pos = skip_ws(s, pos + 7, limit)
                assert s[pos] == ":", "expected ':' after default"
                pos += 1
                pending.append(("default", None))
                continue
            new_items, pos = self.parse_one_statement(s, pos, limit, top_level=False)
            current_items.extend(new_items)
        flush()
        for c in cases:
            if c["items"] and c["items"][-1]["kind"] == "stmt" and c["items"][-1]["text"].strip() == "break":
                c["items"].pop()
        return cases


# ---------------------------------------------------------------------------
# Find every method in the source (class wrapper, multiple methods, static
# modifiers -- all just fall out of scanning for "modifiers* TYPE NAME(" that
# is actually followed by a real '{' body, skipping anything that isn't).
# ---------------------------------------------------------------------------

def find_all_methods(src):
    """Returns list of (name, params_str, body_start, body_end)."""
    results = []
    pos = 0
    n = len(src)
    while True:
        m = METHOD_SIG_RE.search(src, pos)
        if not m:
            break
        name = m.group(2)
        paren_open = m.end() - 1
        try:
            paren_close = find_matching(src, paren_open)
        except ValueError:
            pos = m.end()
            continue
        after = skip_ws(src, paren_close + 1, n)
        # skip a trailing generic constraint clause, e.g. `where T : struct`
        while src[after:after + 5] == "where":
            brace_idx = src.find("{", after)
            semi_idx = src.find(";", after)
            if brace_idx == -1:
                after = n
                break
            if semi_idx != -1 and semi_idx < brace_idx:
                after = n
                break
            after = brace_idx
            break
        if after < n and src[after] == "{":
            body_open = after
            body_close = find_matching(src, body_open)
            results.append((name, src[paren_open + 1:paren_close], body_open + 1, body_close))
            pos = body_close + 1
        else:
            # interface/abstract signature (ends with ';'), property, delegate, etc. - skip it
            pos = m.end()
    return results


# ---------------------------------------------------------------------------
# Phase 2: python AST -> the app's own flat node-map format
# ---------------------------------------------------------------------------

class NodeBuilder(object):
    def __init__(self):
        self.nodes = {}

    @staticmethod
    def esc(text):
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def add_noop(self, parent):
        nid = rand_id()
        self.nodes[nid] = {"id": nid, "parentId": parent, "hidden": False, "type": "noop"}
        return nid

    def build_seq(self, parent_id, seq_ast):
        nid = rand_id()
        items = seq_ast["items"]
        child_ids = [self.add_noop(nid)] if not items else [self.build_node(nid, it) for it in items]
        self.nodes[nid] = {"id": nid, "parentId": parent_id, "hidden": False, "type": "sequence", "childrenIds": child_ids}
        return nid

    def build_node(self, parent_id, ast):
        k = ast["kind"]
        if k == "stmt":
            nid = rand_id()
            self.nodes[nid] = {"id": nid, "parentId": parent_id, "hidden": False, "type": "statement", "text": self.esc(ast["text"])}
            return nid
        if k == "if":
            nid = rand_id()
            true_id = self.build_seq(nid, ast["true"])
            false_id = self.build_seq(nid, ast["false"])
            self.nodes[nid] = {"id": nid, "parentId": parent_id, "hidden": False, "type": "if",
                                "condition": self.esc(ast["cond"]),
                                "trueBranchSequenceId": true_id, "falseBranchSequenceId": false_id}
            return nid
        if k == "while":
            nid = rand_id()
            body_id = self.build_seq(nid, ast["body"])
            self.nodes[nid] = {"id": nid, "parentId": parent_id, "hidden": False, "type": "while",
                                "condition": self.esc(ast["cond"]), "bodySequenceId": body_id}
            return nid
        if k == "repeat":
            nid = rand_id()
            body_id = self.build_seq(nid, ast["body"])
            self.nodes[nid] = {"id": nid, "parentId": parent_id, "hidden": False, "type": "repeat",
                                "condition": self.esc(ast["cond"]), "bodySequenceId": body_id}
            return nid
        if k == "switch":
            nid = rand_id()
            case_list = []
            for c in ast["cases"]:
                cid = rand_id()
                body_id = self.build_seq(nid, c["body"])
                entry = {"id": cid, "type": c["type"], "bodySequenceId": body_id}
                if c["type"] == "case":
                    entry["condition"] = self.esc(c["cond"])
                case_list.append(entry)
            self.nodes[nid] = {"id": nid, "parentId": parent_id, "hidden": False, "type": "switch", "cases": case_list}
            return nid
        raise ValueError("unknown ast kind %r" % (k,))


# ---------------------------------------------------------------------------
# Assembly: full app state (possibly many structograms) + gzip/base64/url
# ---------------------------------------------------------------------------

DEFAULT_COLORS = {
    "text": ["#ff0000", "#0000ff", "#ffc000", "#00b050", "#00b0f0", "#7030a0", "#ceaf87"],
    "textBackground": ["#ffb2b2", "#b2b2ff", "#ffecb2", "#b2e7ca", "#b2e7fa", "#d4c0e2"],
    "nodeBackground": ["#ffcccc", "#ccccff", "#fff2cc", "#ccefdc", "#b2e7fa", "#d4c0e2"],
    "structogramBackground": ["#ffffff", "#ffcccc", "#ccccff", "#fff2cc", "#ccefdc", "#ff99ff", "#008c95", "#eee4d6", "#ceaf87"],
}


def variables_to_html(vars_list):
    if not vars_list:
        return ""
    esc = NodeBuilder.esc
    first, rest = vars_list[0], vars_list[1:]
    html = esc(first)
    for v in rest:
        html += "<div>%s</div>" % esc(v)
    return html


def csharp_source_to_state(source):
    src = strip_comments(source)
    methods = find_all_methods(src)
    if not methods:
        raise ValueError("Could not locate any method signature (Type Name(...) {...}) in the input")

    builder = NodeBuilder()
    structograms = []
    for name, params_str, body_start, body_end in methods:
        mp = MethodBodyParser()
        mp.parse_params(params_str)
        body_items = mp.parse_statements(src, body_start, body_end, top_level=True)
        root_id = builder.build_seq(None, {"kind": "seq", "items": body_items})
        structograms.append({
            "name": name,
            "returnText": NodeBuilder.esc(mp.pending_return) if mp.pending_return is not None else "",
            "variables": variables_to_html(mp.variables),
            "rootId": root_id,
            "showVariables": bool(mp.variables),
            "showHeader": True,
            "showReturnText": mp.pending_return is not None,
        })

    state = {
        "id": str(random.random()),
        "hoverId": None,
        "selectedId": None,
        "nodes": builder.nodes,
        "structograms": structograms,
        "colors": DEFAULT_COLORS,
    }
    return state


def _gzip_b64_quoted(raw):
    """bytes -> gzip (mtime=0, so output is deterministic) -> base64 -> %-encoded.
    This is the `?data=` payload format used by both progalap.elte.hu apps."""
    buf = io.BytesIO()
    f = gzip.GzipFile(fileobj=buf, mode="wb", mtime=0)
    try:
        f.write(raw)
    finally:
        f.close()
    return quote(base64.b64encode(buf.getvalue()).decode("ascii"), safe="")


def state_to_url(state, base="https://progalap.elte.hu/stuki/v1/"):
    json_str = json.dumps(state, separators=(",", ":"))
    return base + "?data=" + _gzip_b64_quoted(json_str.encode("utf-8"))


def csharp_source_to_url(source):
    state = csharp_source_to_state(source)
    return state_to_url(state), state


DEMO_SOURCE = """
public class Program
{
    public static void Main(string[] args)
    {
        Console.WriteLine("Enter n:");
        int n = int.Parse(Console.ReadLine());
        Console.WriteLine(Fibonacci(n));
    }

    public static int Fibonacci(int n)
    {
        int a = 0;
        int b = 1;
        for (int i = 0; i < n; i++)
        {
            int tmp = a + b;
            a = b;
            b = tmp;
        }
        return a;
    }
}
"""


# ---------------------------------------------------------------------------
# Specification (speci) side - merged from str2speci.py
#   URL     : <base>?data=<b64(gzip(JSON {"specification":..., "tests":[]}))>
#   compact : SPECI1:<b64url(raw-deflate(spec text))>   (tests live in the text
#             as a  /*--- test --- ... ---*/  block)
# ---------------------------------------------------------------------------

SPECI_PREFIX = "SPECI1:"
SPECI_BASE = "https://progalap.elte.hu/specifikacio/v1/"
STUKI_BASE = "https://progalap.elte.hu/stuki/v1/"


def _to_text(s):
    return s.decode("utf-8") if isinstance(s, bytes) else s


def spec_to_url(spec_text, tests=None, base=SPECI_BASE):
    payload = {"specification": _to_text(spec_text),
               "tests": tests if tests is not None else []}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    raw = raw.encode("utf-8") if not isinstance(raw, bytes) else raw
    return base + "?data=" + _gzip_b64_quoted(raw)


def spec_to_compact(spec_text):
    return SPECI_PREFIX + _b64url_encode(_raw_deflate(_to_text(spec_text).encode("utf-8")))


def compact_to_spec(line):
    line = "".join(line.split())
    if not line.startswith(SPECI_PREFIX):
        raise ValueError("not a %s line" % SPECI_PREFIX)
    return _raw_inflate(_b64url_decode(line[len(SPECI_PREFIX):])).decode("utf-8")


# ---------------------------------------------------------------------------
# DECODE: URL -> original content  (the reverse of spec_to_url / state_to_url)
#   specifikacio URL -> the exact specification text (+ its tests list)
#   stuki URL        -> the structogram state (JSON).  The original C# source
#                       is NOT stored in the URL, so it cannot be recovered;
#                       the state is the closest "basic" form there is.
# ---------------------------------------------------------------------------

_DATA_PARAM_RE = re.compile(r"[?&]data=([^&#]*)")


def url_to_payload(url):
    """Decode a progalap.elte.hu `?data=` URL (or just the bare data blob)
    into the JSON object it carries.  Tolerates line-wrapped URLs, a
    missing scheme/host, url-safe base64 and missing '=' padding.
    Raises ValueError if the text is not a decodable payload."""
    url = "".join(_to_text(url).split())
    m = _DATA_PARAM_RE.search(url)
    blob = unquote(m.group(1) if m else url)
    try:
        raw = zlib.decompress(_b64url_decode(blob), 16 + zlib.MAX_WBITS)  # gzip
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError("could not decode URL data (%s: %s)" % (type(e).__name__, e))


def url_to_spec(url):
    """specifikacio URL -> (specification_text, tests_list)."""
    payload = url_to_payload(url)
    if not isinstance(payload, dict) or "specification" not in payload:
        raise ValueError("this URL carries no specification "
                         "(is it a stuki URL? then the result is a structogram state)")
    return payload["specification"], payload.get("tests") or []


def url_to_state(url):
    """stuki URL -> structogram state dict (same shape csharp_source_to_state returns)."""
    payload = url_to_payload(url)
    if not isinstance(payload, dict) or "structograms" not in payload:
        raise ValueError("this URL carries no structogram")
    return payload


def unescape(s):
    r"""Interpret literal \n, \t and \\ (for when $'...' is not available)."""
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in "nt\\":
            out.append({"n": "\n", "t": "\t", "\\": "\\"}[s[i + 1]])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

HELP = """\
speci_stuki.py - build progalap.elte.hu links (specification + structogram)

DEFAULT: specification text -> specifikacio URL
  speci_stuki.py 'Algoritmus:\\n  be: n\\n  ki: n'        (with -e, see below)
  speci_stuki.py $'Algoritmus:\\n  be: n\\n  ki: négyzetgyök(n)'
  speci_stuki.py < spec.txt            (or: echo "..." | speci_stuki.py)

--convert [LINE]   turn an exported line into a URL (type is auto-detected):
  SPECI1:...  -> specifikacio URL        STUKI1:...  -> stuki URL
  The line may be an argument or on stdin; wrapped lines and a missing
  prefix (STUKI1 assumed unless --speci is given) are tolerated.

--decode [URL]     the reverse: turn a progalap.elte.hu URL back into its content
  specifikacio URL -> the original specification text
  stuki URL        -> the structogram state as JSON (the C# source itself is
                      not stored in the URL, so it cannot be restored)
  The URL may be an argument or on stdin; wrapped lines are tolerated.
  Combine with --compact for a SPECI1:/STUKI1: line, --json for raw JSON,
  --both for text/JSON followed by the compact line.

--cs [FILE]        C# method(s) -> stuki URL (FILE, or '-'/stdin if omitted)
--dummy            C# -> stuki URL using the built-in demo (Main + Fibonacci)

Options:
  -e, --escape     interpret literal \\n \\t \\\\ in the spec string
  --compact        print a SPECI1:/STUKI1: line instead of a URL
  --both           print URL, then the compact line
  --speci          with --convert: force specification type
  --json           with --decode: print the raw decoded JSON payload
  -h, --help       this help

Notes: a SPECI1: line carries the whole spec text (including any
/*--- test ---*/ block); it is placed in the URL's "specification" field
with an empty tests list.
"""


def emit(url, compact, both, mode_compact):
    if compact:
        print(mode_compact())
    elif both:
        print(url)
        print(mode_compact())
    else:
        print(url)


def _print_text(text):
    """print() that also copes with non-ASCII text on Python 2."""
    if sys.version_info[0] == 2 and not isinstance(text, bytes):
        text = text.encode("utf-8")
    print(text)


def read_stdin():
    if sys.stdin.isatty():
        return None
    data = sys.stdin.read()
    return _to_text(data)


def main():
    args = sys.argv[1:]
    if not args and sys.stdin.isatty():
        sys.stdout.write(HELP)
        return
    flags = set(a for a in args if a in
                ("-h", "--help", "-e", "--escape", "--compact", "--both",
                 "--convert", "--speci", "--cs", "--dummy", "--decode", "--json"))
    pos = [a for a in args if a not in flags]
    if "-h" in flags or "--help" in flags:
        sys.stdout.write(HELP)
        return
    compact, both = "--compact" in flags, "--both" in flags

    joined = "".join(pos).strip()

    # --- --decode: URL -> original content -------------------------------
    if "--decode" in flags:
        url = joined or (read_stdin() or "")
        if not url.strip():
            raise SystemExit("--decode needs a progalap.elte.hu URL (argument or stdin)")
        try:
            payload = url_to_payload(url)
            if "--json" in flags:
                _print_text(json.dumps(payload, indent=2, ensure_ascii=False))
            elif isinstance(payload, dict) and "specification" in payload:
                spec, tests = url_to_spec(url)
                if tests:
                    print("note: %d test(s) in the URL are not part of the plain text "
                          "(use --json to see them)" % len(tests), file=sys.stderr)
                if not compact:
                    _print_text(spec)
                if compact or both:
                    print(spec_to_compact(spec))
            elif isinstance(payload, dict) and "structograms" in payload:
                state = url_to_state(url)
                print("decoded %d method(s): " % len(state["structograms"])
                      + ", ".join(sg["name"] for sg in state["structograms"]), file=sys.stderr)
                if not compact:
                    _print_text(json.dumps(state, indent=2, ensure_ascii=False))
                if compact or both:
                    print(state_to_compact(state))
            else:
                raise ValueError("unrecognised payload (neither specification nor structogram)")
        except ValueError as e:
            raise SystemExit("--decode: %s" % e)
        return

    # --- --convert: exported line -> URL ---------------------------------
    if "--convert" in flags or joined.startswith((SPECI_PREFIX, STUKI_PREFIX)):
        line = joined or (read_stdin() or "")
        line = "".join(line.split())
        if not line:
            raise SystemExit("--convert needs a SPECI1:/STUKI1: line (argument or stdin)")
        if line.startswith(SPECI_PREFIX) or "--speci" in flags:
            if not line.startswith(SPECI_PREFIX):
                line = SPECI_PREFIX + line
            spec = compact_to_spec(line)
            print("parsed specification (%d chars)" % len(spec), file=sys.stderr)
            print(spec_to_url(spec))
        else:
            if not line.startswith(STUKI_PREFIX):
                line = STUKI_PREFIX + line
            state = compact_to_state(line)
            print("parsed %d method(s): " % len(state["structograms"])
                  + ", ".join(sg["name"] for sg in state["structograms"]), file=sys.stderr)
            print(state_to_url(state))
        return

    # --- C# -> stuki -------------------------------------------------------
    if "--cs" in flags or "--dummy" in flags:
        if "--dummy" in flags:
            source = DEMO_SOURCE
            print("(using built-in demo: Main + Fibonacci)", file=sys.stderr)
        elif pos and pos[0] != "-":
            with io.open(pos[0], "r", encoding="utf-8") as f:
                source = f.read()
        else:
            source = read_stdin()
            if source is None:
                raise SystemExit("--cs needs a FILE or C# on stdin")
        state = csharp_source_to_state(source)
        print("parsed %d method(s): " % len(state["structograms"])
              + ", ".join(sg["name"] for sg in state["structograms"]), file=sys.stderr)
        emit(state_to_url(state), compact, both, lambda: state_to_compact(state))
        return

    # --- default: spec string -> speci URL ---------------------------------
    if pos:
        text = " ".join(pos) if len(pos) > 1 else pos[0]
        text = _to_text(text)
    else:
        text = read_stdin()
        if not text or not text.strip():
            sys.stdout.write(HELP)
            return
    if "-e" in flags or "--escape" in flags:
        text = unescape(text)
    emit(spec_to_url(text), compact, both, lambda: spec_to_compact(text))


if __name__ == "__main__":
    main()