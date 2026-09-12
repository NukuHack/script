"""
docx_to_html.py

Convert a .docx file's body into a self-contained HTML string.

Improvements over the basic version:
  - Preserves tables, including merged cells (colspan via gridSpan,
    rowspan via vMerge continue/restart).
  - Preserves paragraph indentation (list-style and block-quote style
    left/right indents) and alignment (left/right/center/justify).
  - Renders numbered/bulleted lists as real <ol>/<ul> markup, reading
    numbering.xml to tell bullets apart from numbered formats and to
    respect nesting level.
  - Renders inline images (w:drawing / a:blip) as <img> tags with the
    image data embedded directly as a base64 data URI, so the output
    HTML is a single portable file with no external assets.
  - Preserves more run formatting: bold, italic, underline, strikethrough,
    highlight, font color, and font size.
  - Renders hyperlinks as real <a href="..."> tags.
  - Renders line breaks (w:br) as <br>.

Usage:
    python3 docx_to_html.py input.docx > output.html
"""

import base64
import mimetypes
import os
import sys
import zipfile
from lxml import etree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

HIGHLIGHT_COLORS = {
    "yellow": "#FFFF00", "green": "#00FF00", "cyan": "#00FFFF",
    "magenta": "#FF00FF", "blue": "#0000FF", "red": "#FF0000",
    "darkBlue": "#000080", "darkCyan": "#008080", "darkGreen": "#008000",
    "darkMagenta": "#800080", "darkRed": "#800000", "darkYellow": "#808000",
    "darkGray": "#808080", "lightGray": "#C0C0C0", "black": "#000000",
}

JC_MAP = {"left": "left", "right": "right", "center": "center", "both": "justify"}

# Word list formats that should render as an ordered list; everything else
# (bullet, none, etc.) falls back to an unordered list.
ORDERED_FORMATS = {
    "decimal": "decimal", "decimalZero": "decimal",
    "lowerLetter": "lower-alpha", "upperLetter": "upper-alpha",
    "lowerRoman": "lower-roman", "upperRoman": "upper-roman",
}


def esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def twips_to_px(val):
    try:
        return round(int(val) / 15)  # 1440 twips = 96px
    except (TypeError, ValueError):
        return 0


def emu_to_px(val):
    try:
        return round(int(val) / 9525)  # 914400 EMU = 96px
    except (TypeError, ValueError):
        return None


class DocxConverter:
    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        self.path = path
        self.document = etree.fromstring(self.zip.read("word/document.xml"))
        self.rels = self._load_rels("word/_rels/document.xml.rels")
        self.numbering = self._load_numbering()
        self.style_num_pr = self._load_style_num_pr()
        # Tracks, per numId, the running counter for each list level so
        # that numbered lists restart correctly and continue across
        # consecutive <li> paragraphs at the same level.
        self._num_counters = {}

    # ---------- relationships & media ----------

    def _load_rels(self, rels_path):
        rels = {}
        if rels_path in self.zip.namelist():
            root = etree.fromstring(self.zip.read(rels_path))
            for rel in root.findall(f"{PKG_REL}Relationship"):
                rels[rel.get("Id")] = rel.get("Target")
        return rels

    def _image_data_uri(self, r_id):
        target = self.rels.get(r_id)
        if not target:
            return None
        path = "word/" + target.lstrip("/") if not target.startswith("word/") else target
        path = path.replace("word/word/", "word/")
        if path not in self.zip.namelist():
            # Target may already be relative like "media/img1.png"
            path = "word/" + target
        if path not in self.zip.namelist():
            return None
        data = self.zip.read(path)
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"

    # ---------- numbering.xml ----------

    def _load_numbering(self):
        """Returns {numId: {ilvl: numFmt}}."""
        result = {}
        if "word/numbering.xml" not in self.zip.namelist():
            return result
        root = etree.fromstring(self.zip.read("word/numbering.xml"))
        abstract = {}  # abstractNumId -> {ilvl: numFmt}
        for an in root.findall(f"{W}abstractNum"):
            an_id = an.get(f"{W}abstractNumId")
            levels = {}
            for lvl in an.findall(f"{W}lvl"):
                ilvl = lvl.get(f"{W}ilvl")
                fmt_el = lvl.find(f"{W}numFmt")
                fmt = fmt_el.get(f"{W}val") if fmt_el is not None else "decimal"
                levels[ilvl] = fmt
            abstract[an_id] = levels
        for num in root.findall(f"{W}num"):
            num_id = num.get(f"{W}numId")
            abs_ref = num.find(f"{W}abstractNumId")
            if abs_ref is not None:
                result[num_id] = abstract.get(abs_ref.get(f"{W}val"), {})
        return result

    def _load_style_num_pr(self):
        """Returns {styleId: (numId, ilvl_or_None)} for paragraph styles that
        carry list numbering (e.g. Word's built-in "List Bullet"/"List
        Number" styles), resolving basedOn inheritance."""
        if "word/styles.xml" not in self.zip.namelist():
            return {}
        root = etree.fromstring(self.zip.read("word/styles.xml"))
        raw = {}       # styleId -> (numId, ilvl_or_None)
        based_on = {}  # styleId -> parent styleId
        for style in root.findall(f"{W}style"):
            style_id = style.get(f"{W}styleId")
            based = style.find(f"{W}basedOn")
            if based is not None:
                based_on[style_id] = based.get(f"{W}val")
            ppr = style.find(f"{W}pPr")
            if ppr is None:
                continue
            num_pr = ppr.find(f"{W}numPr")
            if num_pr is None:
                continue
            num_id_el = num_pr.find(f"{W}numId")
            ilvl_el = num_pr.find(f"{W}ilvl")
            if num_id_el is not None:
                raw[style_id] = (num_id_el.get(f"{W}val"),
                                  ilvl_el.get(f"{W}val") if ilvl_el is not None else None)

        def resolve(style_id, seen=None):
            if style_id in raw:
                return raw[style_id]
            seen = seen or set()
            parent = based_on.get(style_id)
            if parent and parent not in seen:
                return resolve(parent, seen | {style_id})
            return None

        return {sid: resolve(sid) for sid in set(raw) | set(based_on)}

    def _list_format(self, num_id, ilvl):
        levels = self.numbering.get(num_id, {})
        return levels.get(ilvl, "bullet")

    # ---------- run-level formatting ----------

    def _run_to_html(self, run):
        rpr = run.find(f"{W}rPr")
        styles = []
        if rpr is not None:
            hl = rpr.find(f"{W}highlight")
            if hl is not None:
                color = HIGHLIGHT_COLORS.get(hl.get(f"{W}val"))
                if color:
                    styles.append(f"background-color:{color}")
            col = rpr.find(f"{W}color")
            if col is not None and col.get(f"{W}val") not in (None, "auto"):
                styles.append(f"color:#{col.get(f'{W}val')}")
            sz = rpr.find(f"{W}sz")
            if sz is not None and sz.get(f"{W}val"):
                try:
                    styles.append(f"font-size:{int(sz.get(f'{W}val')) / 2}pt")
                except ValueError:
                    pass
            if rpr.find(f"{W}b") is not None and rpr.find(f"{W}b").get(f"{W}val") != "false":
                styles.append("font-weight:bold")
            if rpr.find(f"{W}i") is not None and rpr.find(f"{W}i").get(f"{W}val") != "false":
                styles.append("font-style:italic")
            u = rpr.find(f"{W}u")
            if u is not None and u.get(f"{W}val") not in (None, "none"):
                styles.append("text-decoration:underline")
            strike = rpr.find(f"{W}strike")
            if strike is not None and strike.get(f"{W}val") != "false":
                existing = "text-decoration:underline" in styles
                if existing:
                    styles = [s if s != "text-decoration:underline" else
                              "text-decoration:underline line-through" for s in styles]
                else:
                    styles.append("text-decoration:line-through")

        parts = []
        for child in run:
            tag = etree.QName(child).localname
            if tag == "t":
                parts.append(esc(child.text or ""))
            elif tag == "br" or tag == "cr":
                parts.append("<br>")
            elif tag == "tab":
                parts.append("&#9;")
            elif tag == "drawing":
                img = self._drawing_to_html(child)
                if img:
                    parts.append(img)

        text = "".join(parts)
        if not text:
            return ""
        if styles:
            return f'<span style="{";".join(styles)}">{text}</span>'
        return text

    def _drawing_to_html(self, drawing):
        blip = drawing.find(f".//{A}blip")
        if blip is None:
            return ""
        r_id = blip.get(f"{R}embed")
        if not r_id:
            return ""
        src = self._image_data_uri(r_id)
        if not src:
            return ""
        extent = drawing.find(f".//{WP}extent")
        dims = ""
        if extent is not None:
            w_px = emu_to_px(extent.get("cx"))
            h_px = emu_to_px(extent.get("cy"))
            if w_px and h_px:
                dims = f' width="{w_px}" height="{h_px}"'
        return f'<img src="{src}"{dims} style="max-width:100%">'

    # ---------- paragraph-level formatting ----------

    def _paragraph_style(self, ppr):
        styles = []
        if ppr is None:
            return styles
        jc = ppr.find(f"{W}jc")
        if jc is not None and jc.get(f"{W}val") in JC_MAP:
            styles.append(f"text-align:{JC_MAP[jc.get(f'{W}val')]}")
        ind = ppr.find(f"{W}ind")
        if ind is not None:
            left = ind.get(f"{W}left") or ind.get(f"{W}start")
            right = ind.get(f"{W}right") or ind.get(f"{W}end")
            if left:
                styles.append(f"margin-left:{twips_to_px(left)}px")
            if right:
                styles.append(f"margin-right:{twips_to_px(right)}px")
        return styles

    def _paragraph_list_info(self, ppr):
        if ppr is None:
            return None
        num_id = None
        ilvl = "0"

        num_pr = ppr.find(f"{W}numPr")
        if num_pr is not None:
            ilvl_el = num_pr.find(f"{W}ilvl")
            num_id_el = num_pr.find(f"{W}numId")
            if num_id_el is not None:
                num_id = num_id_el.get(f"{W}val")
                ilvl = ilvl_el.get(f"{W}val") if ilvl_el is not None else "0"

        if num_id is None:
            # Fall back to numbering inherited from the paragraph style
            # (this is how Word's built-in "List Bullet"/"List Number"
            # styles attach numbering).
            style_el = ppr.find(f"{W}pStyle")
            if style_el is not None:
                resolved = self.style_num_pr.get(style_el.get(f"{W}val"))
                if resolved:
                    num_id, style_ilvl = resolved
                    if style_ilvl is not None:
                        ilvl = style_ilvl

        if num_id is None or num_id == "0":
            return None
        fmt = self._list_format(num_id, ilvl)
        is_ordered = fmt in ORDERED_FORMATS
        return {"num_id": num_id, "ilvl": int(ilvl), "ordered": is_ordered,
                "list_style": ORDERED_FORMATS.get(fmt, "disc")}

    def _paragraph_inner_html(self, p):
        parts = []
        for child in p:
            tag = etree.QName(child).localname
            if tag == "r":
                parts.append(self._run_to_html(child))
            elif tag == "hyperlink":
                r_id = child.get(f"{R}id")
                href = self.rels.get(r_id, "#")
                inner = "".join(self._run_to_html(r) for r in child.findall(f"{W}r"))
                parts.append(f'<a href="{esc(href)}">{inner}</a>')
        return "".join(parts)

    def _paragraph_to_html(self, p):
        ppr = p.find(f"{W}pPr")
        styles = self._paragraph_style(ppr)
        inner = self._paragraph_inner_html(p)
        style_attr = f' style="{";".join(styles)}"' if styles else ""
        return f"<p{style_attr}>{inner}</p>"

    # ---------- list rendering (stateful across sibling paragraphs) ----------

    def _render_body(self, body):
        out = []
        list_stack = []  # list of (num_id, ilvl, ordered)

        def close_lists_to(level):
            while len(list_stack) > level:
                _, _, ordered = list_stack.pop()
                out.append("</ol>" if ordered else "</ul>")

        for el in body:
            tag = etree.QName(el).localname
            if tag == "p":
                ppr = el.find(f"{W}pPr")
                info = self._paragraph_list_info(ppr)
                if info is None:
                    close_lists_to(0)
                    out.append(self._paragraph_to_html(el))
                    continue

                target_level = info["ilvl"] + 1
                # Close deeper/mismatched lists, open new ones as needed.
                while len(list_stack) > target_level:
                    _, _, ordered = list_stack.pop()
                    out.append("</ol>" if ordered else "</ul>")
                if len(list_stack) == target_level and list_stack and (
                        list_stack[-1][0] != info["num_id"]):
                    # Same depth but a different list -> restart it.
                    _, _, ordered = list_stack.pop()
                    out.append("</ol>" if ordered else "</ul>")
                while len(list_stack) < target_level:
                    ordered = info["ordered"] if len(list_stack) == target_level - 1 else True
                    style = f' style="list-style-type:{info["list_style"]}"' if ordered else ""
                    tag_name = "ol" if ordered else "ul"
                    out.append(f"<{tag_name}{style}>")
                    list_stack.append((info["num_id"], info["ilvl"], ordered))

                inner = self._paragraph_inner_html(el)
                out.append(f"<li>{inner}</li>")
            elif tag == "tbl":
                close_lists_to(0)
                out.append(self._table_to_html(el))

        close_lists_to(0)
        return out

    # ---------- table rendering (handles gridSpan / vMerge) ----------

    def _table_to_html(self, tbl):
        rows_xml = tbl.findall(f"{W}tr")
        # grid[row_idx][col_idx] = cell-descriptor dict, or None for a slot
        # consumed by a colspan/rowspan from elsewhere.
        grid = []
        # open_spans[col_idx] = descriptor dict of the cell currently
        # growing downward via vMerge. OOXML gives every merged-into cell
        # its own explicit <w:tc> (marked vMerge="continue") at the same
        # grid column, so we never need to guess column positions.
        open_spans = {}

        for row in rows_xml:
            row_cells = []
            col_idx = 0

            def place(idx, descriptor):
                while len(row_cells) <= idx:
                    row_cells.append(None)
                row_cells[idx] = descriptor

            for tc in row.findall(f"{W}tc"):
                tcpr = tc.find(f"{W}tcPr")
                span = 1
                vmerge_val = None
                if tcpr is not None:
                    gs = tcpr.find(f"{W}gridSpan")
                    if gs is not None:
                        span = int(gs.get(f"{W}val", "1"))
                    vm = tcpr.find(f"{W}vMerge")
                    if vm is not None:
                        vmerge_val = vm.get(f"{W}val") or "continue"

                if vmerge_val == "continue" and col_idx in open_spans:
                    open_spans[col_idx]["rowspan"] += 1
                    place(col_idx, {"skip": True})
                    col_idx += span
                    continue

                content = "".join(self._paragraph_to_html(p) for p in tc.findall(f"{W}p"))
                descriptor = {"skip": False, "colspan": span, "rowspan": 1,
                              "content": content}
                place(col_idx, descriptor)
                if vmerge_val == "restart":
                    open_spans[col_idx] = descriptor
                elif col_idx in open_spans:
                    del open_spans[col_idx]
                col_idx += span

            grid.append(row_cells)

        html = ["<table border=\"1\" style=\"border-collapse:collapse\">"]
        for row_cells in grid:
            html.append("<tr>")
            for cell in row_cells:
                if cell is None or cell.get("skip"):
                    continue
                attrs = ""
                if cell["colspan"] > 1:
                    attrs += f' colspan="{cell["colspan"]}"'
                if cell["rowspan"] > 1:
                    attrs += f' rowspan="{cell["rowspan"]}"'
                html.append(f"<td{attrs}>{cell['content']}</td>")
            html.append("</tr>")
        html.append("</table>")
        return "".join(html)

    # ---------- top-level ----------

    def _document_title(self, fallback):
        """Best-effort title: docProps/core.xml's dc:title, else the first
        heading paragraph's text, else the given fallback (e.g. filename)."""
        if "docProps/core.xml" in self.zip.namelist():
            core = etree.fromstring(self.zip.read("docProps/core.xml"))
            dc_title = core.find("{http://purl.org/dc/elements/1.1/}title")
            if dc_title is not None and dc_title.text and dc_title.text.strip():
                return dc_title.text.strip()

        body = self.document.find(f"{W}body")
        for p in body.findall(f"{W}p"):
            ppr = p.find(f"{W}pPr")
            style_el = ppr.find(f"{W}pStyle") if ppr is not None else None
            if style_el is not None and style_el.get(f"{W}val", "").startswith("Heading"):
                text = "".join(t.text or "" for t in p.iter(f"{W}t")).strip()
                if text:
                    return text

        return fallback

    def convert(self):
        body = self.document.find(f"{W}body")
        pieces = self._render_body(body)
        content = "\n".join(pieces)
        title = esc(self._document_title(fallback=os.path.splitext(os.path.basename(self.path))[0]))
        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  html, body {{
    margin: 0;
    padding: 0;
    background: #d9d9d9;
    font-family: "Calibri", "Segoe UI", Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.4;
  }}
  /* Outer wrapper centers the page horizontally and lets the whole
     document scroll normally when content runs past one page. */
  .page-wrapper {{
    display: flex;
    justify-content: center;
    padding: 32px 16px;
    box-sizing: border-box;
    min-height: 100vh;
  }}
  /* A4 page: 210mm wide (~794px @96dpi) with 1in (~96px) margins,
     growing downward to fit however much content there is. */
  .page {{
    width: 794px;
    max-width: 100%;
    min-height: 1123px;
    background: #ffffff;
    box-shadow: 0 0 12px rgba(0, 0, 0, 0.25);
    padding: 96px;
    box-sizing: border-box;
    overflow-wrap: break-word;
  }}
  .page p {{
    margin: 0 0 8pt 0;
  }}
  .page table {{
    width: 100%;
    margin-bottom: 8pt;
  }}
  .page td {{
    padding: 4px 8px;
    vertical-align: top;
  }}
  .page ul, .page ol {{
    margin: 0 0 8pt 0;
    padding-left: 24px;
  }}
  .page img {{
    max-width: 100%;
  }}
</style>
</head>
<body>
  <div class="page-wrapper">
    <div class="page">
{content}
    </div>
  </div>
</body>
</html>"""


def docx_to_html(path):
    return DocxConverter(path).convert()


if __name__ == "__main__":
    print(docx_to_html(sys.argv[1]))
