"""
Free PDF Editor Web App  —  Full Edition
Features: Merge, Split, Rotate, Extract Text, Watermark, 
          Password Protect/Unlock, Compress, PDF Info,
          + EDIT TEXT (same font, same size, same color)
Backend : Flask + PyMuPDF (fitz) + pypdf + pdfplumber + reportlab
"""

import os, io, uuid, zipfile, json, base64
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template
import fitz                              # PyMuPDF  — text editing + rendering
from pypdf import PdfReader, PdfWriter   # merge / split / protect / compress
import pdfplumber                        # table-aware text extraction
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as rl_canvas

# ── App Setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024   # 50 MB

UPLOAD_FOLDER = Path("uploads");  UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER = Path("outputs");  OUTPUT_FOLDER.mkdir(exist_ok=True)


def _uid():          return uuid.uuid4().hex
def _up(file):
    p = UPLOAD_FOLDER / f"{_uid()}_{file.filename}"
    file.save(p); return p
def _out(name):      return OUTPUT_FOLDER / f"{_uid()}_{name}"
def _ok_pdf(f):      return f and "." in f.filename and f.filename.rsplit(".",1)[1].lower() == "pdf"


# ─────────────────────────────────────────────────────────────────────────────
#  FONT MAPPING  (PDF font name  →  PyMuPDF base-14 alias)
# ─────────────────────────────────────────────────────────────────────────────
def _map_font(font_name: str, bold: bool, italic: bool) -> str:
    fn = font_name.lower()
    if "+" in fn:          # strip subset prefix  e.g. "ABCDEF+Arial"
        fn = fn.split("+", 1)[1]
    fn = fn.replace("-", "").replace(" ", "")

    if any(k in fn for k in ("courier","mono","consol","typewriter")):
        return {(1,1):"cobi",(1,0):"cob",(0,1):"coit",(0,0):"cour"}[(bold,italic)]
    if any(k in fn for k in ("times","serif","roman","georgia","garamond")):
        return {(1,1):"tibi",(1,0):"tibo",(0,1):"tiit",(0,0):"tiro"}[(bold,italic)]
    if any(k in fn for k in ("symbol",)):
        return "symb"
    if any(k in fn for k in ("zapf","dingbat")):
        return "zadb"
    # Helvetica / Arial / sans-serif (default)
    return {(1,1):"fibo",(1,0):"hebo",(0,1):"heit",(0,0):"helv"}[(bold,italic)]


def _int_to_rgb(color_int: int):
    """Convert packed int color (0xRRGGBB) to (r,g,b) floats 0-1."""
    r = ((color_int >> 16) & 0xFF) / 255
    g = ((color_int >>  8) & 0xFF) / 255
    b = (  color_int       & 0xFF) / 255
    return (r, g, b)


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — static page
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ══════════════════════════════════════════════════════════════════════════════
#  TEXT EDITING  (the flagship feature)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/analyze", methods=["POST"])
def analyze():
    """
    Upload a PDF → get back:
      • base64 PNG preview of each page
      • list of text spans with font, size, color, bbox for the editor UI
    """
    file = request.files.get("file")
    if not _ok_pdf(file):
        return jsonify({"error": "Please upload a valid PDF."}), 400

    path = _up(file)
    doc  = fitz.open(str(path))
    pages_data = []

    for pno in range(len(doc)):
        page = doc[pno]
        pw, ph = page.rect.width, page.rect.height

        # ── render preview image ──────────────────────────────────────────
        mat = fitz.Matrix(1.5, 1.5)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_b64 = base64.b64encode(pix.tobytes("png")).decode()

        # ── extract text spans ────────────────────────────────────────────
        spans = []
        data  = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in data["blocks"]:
            if block["type"] != 0:       # skip images
                continue
            for line in block["lines"]:
                for sp in line["spans"]:
                    if not sp["text"].strip():
                        continue
                    bold   = bool(sp["flags"] & 16)
                    italic = bool(sp["flags"] & 2)
                    rgb    = _int_to_rgb(sp["color"])
                    spans.append({
                        "text"     : sp["text"],
                        "font_raw" : sp["font"],
                        "font_mapped": _map_font(sp["font"], bold, italic),
                        "size"     : round(sp["size"], 2),
                        "bold"     : bold,
                        "italic"   : italic,
                        "color_rgb": list(rgb),
                        "color_hex": "#{:02x}{:02x}{:02x}".format(
                                        int(rgb[0]*255),
                                        int(rgb[1]*255),
                                        int(rgb[2]*255)),
                        "bbox"     : list(sp["bbox"]),    # PDF coords (pts)
                        "origin"   : list(sp["origin"]),
                    })

        pages_data.append({
            "page_num" : pno,
            "width_pt" : pw,
            "height_pt": ph,
            "img_b64"  : img_b64,
            "img_w_px" : pix.width,
            "img_h_px" : pix.height,
            "spans"    : spans,
        })

    doc.close()
    path.unlink()
    return jsonify({"pages": pages_data})


def _sample_bg_color(bbox, pixmap, scale: float) -> tuple:
    """
    Sample the background color beneath a text span by reading pixel values
    from a pre-rendered pixmap of the page (rendered BEFORE any edits).

    Strategy: sample 6 points around the perimeter of the bbox (corners +
    top/bottom midpoints) where background is most likely visible, then
    average them.  This avoids dark ink pixels in the centre of the text.
    """
    x0, y0, x1, y1 = float(bbox.x0), float(bbox.y0), float(bbox.x1), float(bbox.y1)
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2

    sample_pts = [
        (x0, y0), (x1, y0),          # top-left, top-right
        (x0, y1), (x1, y1),          # bottom-left, bottom-right
        (mx, y0), (mx, y1),           # top-mid, bottom-mid
    ]

    samples = []
    for sx, sy in sample_pts:
        px = max(0, min(int(sx * scale), pixmap.width  - 1))
        py = max(0, min(int(sy * scale), pixmap.height - 1))
        samples.append(pixmap.pixel(px, py))   # (R, G, B) 0-255

    r = sum(s[0] for s in samples) / len(samples) / 255
    g = sum(s[1] for s in samples) / len(samples) / 255
    b = sum(s[2] for s in samples) / len(samples) / 255
    return (r, g, b)


@app.route("/apply-edits", methods=["POST"])
def apply_edits():
    """
    Receive the original PDF + a JSON list of edits, apply them, return PDF.

    Edit object:
    {
      "page"     : 0,           // 0-indexed
      "bbox"     : [x0,y0,x1,y1],   // original span bbox in PDF pts
      "old_text" : "Hello",
      "new_text" : "Hi there",
      "font"     : "helv",      // mapped font alias
      "size"     : 12.0,
      "color_rgb": [0,0,0],
      "origin"   : [x, y]       // PDF-space baseline origin
    }

    Background-preservation approach
    ─────────────────────────────────
    OLD (broken): add_redact_annot → always paints WHITE, destroying the bg.
    NEW (fixed) :
      1. Render the page to a pixmap BEFORE any edits.
      2. Sample the actual pixel color surrounding the text bbox.
      3. Paint a filled rectangle in that exact color to erase the old text.
      4. Write the new text on top with the original font / size / color.
    This preserves solid color backgrounds, gradients (approximated by the
    sampled corner average), and tinted areas perfectly.
    """
    file  = request.files.get("file")
    edits = json.loads(request.form.get("edits", "[]"))

    if not _ok_pdf(file):
        return jsonify({"error": "Please upload a valid PDF."}), 400
    if not edits:
        return jsonify({"error": "No edits provided."}), 400

    path = _up(file)
    doc  = fitz.open(str(path))

    # Pre-render every page that has at least one edit so we can sample bg colors.
    # Scale 3× gives good color accuracy without wasting memory.
    SCALE = 3.0
    mat   = fitz.Matrix(SCALE, SCALE)

    edited_pages = {e["page"] for e in edits}
    page_pixmaps = {}
    for pno in edited_pages:
        page_pixmaps[pno] = doc[pno].get_pixmap(matrix=mat, alpha=False)

    # Apply edits
    for edit in edits:
        pno      = edit["page"]
        bbox     = fitz.Rect(edit["bbox"])
        new_text = edit["new_text"]
        font     = edit.get("font", "helv")
        size     = edit.get("size", 12.0)
        color    = tuple(edit.get("color_rgb", [0, 0, 0]))
        origin   = edit.get("origin", [bbox.x0, bbox.y1])

        page = doc[pno]

        # 1️⃣  Sample the true background color from the pre-rendered pixmap
        bg_color = _sample_bg_color(bbox, page_pixmaps[pno], SCALE)

        # 2️⃣  Slightly expand bbox so ascenders / descenders are fully covered,
        #     then paint a filled rectangle in the sampled background color.
        #     No redact → no forced white → background is preserved.
        cover = fitz.Rect(
            bbox.x0 - 1,  bbox.y0 - 2,
            bbox.x1 + 2,  bbox.y1 + 2,
        )
        page.draw_rect(cover, color=None, fill=bg_color)

        # 3️⃣  Write new text at the same baseline with identical font/size/color
        page.insert_text(
            (origin[0], origin[1]),
            new_text,
            fontname=font,
            fontsize=size,
            color=color,
        )

    out = _out("edited.pdf")
    doc.save(str(out), garbage=4, deflate=True)
    doc.close()
    path.unlink()
    return send_file(out, as_attachment=True, download_name="edited.pdf")


# ══════════════════════════════════════════════════════════════════════════════
#  ALL OTHER FEATURES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/merge", methods=["POST"])
def merge_pdfs():
    files = request.files.getlist("files")
    if len(files) < 2:
        return jsonify({"error": "Upload at least 2 PDFs."}), 400
    writer = PdfWriter()
    paths  = []
    for f in files:
        if not _ok_pdf(f):
            return jsonify({"error": f"'{f.filename}' is not a PDF."}), 400
        p = _up(f); paths.append(p)
        for page in PdfReader(str(p)).pages:
            writer.add_page(page)
    out = _out("merged.pdf")
    with open(out, "wb") as fh: writer.write(fh)
    for p in paths: p.unlink()
    return send_file(out, as_attachment=True, download_name="merged.pdf")


def _parse_page_input(raw: str, total: int) -> list[int]:
    """
    Parse a flexible page string into a sorted list of 0-indexed page numbers.

    Accepts any mix of:
      • individual pages   → "1, 5, 8"
      • ranges             → "3-7"
      • combined           → "1, 3-7, 12, 20-25"

    Returns 0-indexed integers, clamped to [0, total-1], duplicates removed.
    """
    pages = set()
    for part in raw.replace(" ", "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                lo = max(1, int(bounds[0]))
                hi = min(total, int(bounds[1]))
                for p in range(lo, hi + 1):
                    pages.add(p - 1)        # convert to 0-indexed
            except ValueError:
                pass
        else:
            try:
                p = int(part)
                if 1 <= p <= total:
                    pages.add(p - 1)
            except ValueError:
                pass
    return sorted(pages)


@app.route("/split", methods=["POST"])
def split_pdf():
    """
    Two modes controlled by the 'mode' form field:

    mode = "range"  (original behaviour)
        start, end  → extract page range, one PDF per page in a ZIP.

    mode = "custom" (new)
        pages       → comma/range string e.g. "1,5,6,8,15,28"
        Produces TWO PDFs inside a ZIP:
          • selected_pages.pdf  — the pages you asked for, in order
          • remaining_pages.pdf — every other page, in original order
        If every page is selected, remaining_pages.pdf is omitted.
        If no valid pages are found, returns an error.
    """
    file = request.files.get("file")
    if not _ok_pdf(file):
        return jsonify({"error": "Upload a valid PDF."}), 400

    mode = request.form.get("mode", "range")
    path = _up(file)
    reader = PdfReader(str(path))
    total  = len(reader.pages)

    zip_path = _out("split.zip")

    # ── MODE: custom pages ────────────────────────────────────────────────
    if mode == "custom":
        raw = request.form.get("pages", "").strip()
        if not raw:
            path.unlink()
            return jsonify({"error": "Please enter page numbers to extract."}), 400

        selected = _parse_page_input(raw, total)
        if not selected:
            path.unlink()
            return jsonify({"error": f"No valid page numbers found. PDF has {total} pages."}), 400

        selected_set = set(selected)
        remaining    = [i for i in range(total) if i not in selected_set]

        def _build_pdf(page_indices):
            w = PdfWriter()
            for i in page_indices:
                w.add_page(reader.pages[i])
            buf = io.BytesIO(); w.write(buf); buf.seek(0)
            return buf.read()

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("selected_pages.pdf", _build_pdf(selected))
            if remaining:
                zf.writestr("remaining_pages.pdf", _build_pdf(remaining))

        path.unlink()

        # Return extra headers so the frontend can show a summary
        resp = send_file(zip_path, as_attachment=True, download_name="split_custom.zip")
        resp.headers["X-Selected-Count"]  = str(len(selected))
        resp.headers["X-Remaining-Count"] = str(len(remaining))
        resp.headers["X-Total-Pages"]     = str(total)
        return resp

    # ── MODE: page range (original) ────────────────────────────────────────
    start = max(1, int(request.form.get("start", 1)))
    end   = int(request.form.get("end", 0))
    end   = total if end == 0 else min(end, total)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i in range(start - 1, end):
            w = PdfWriter(); w.add_page(reader.pages[i])
            buf = io.BytesIO(); w.write(buf); buf.seek(0)
            zf.writestr(f"page_{i+1}.pdf", buf.read())

    path.unlink()
    return send_file(zip_path, as_attachment=True, download_name="split.zip")


@app.route("/rotate", methods=["POST"])
def rotate_pdf():
    file  = request.files.get("file")
    angle = int(request.form.get("angle", 90))
    if not _ok_pdf(file):
        return jsonify({"error": "Upload a valid PDF."}), 400
    if angle not in (90, 180, 270):
        return jsonify({"error": "Angle must be 90, 180 or 270."}), 400
    path = _up(file)
    reader = PdfReader(str(path)); writer = PdfWriter()
    for page in reader.pages:
        page.rotate(angle); writer.add_page(page)
    out = _out("rotated.pdf")
    with open(out, "wb") as fh: writer.write(fh)
    path.unlink()
    return send_file(out, as_attachment=True, download_name="rotated.pdf")


@app.route("/extract-text", methods=["POST"])
def extract_text():
    file = request.files.get("file")
    if not _ok_pdf(file):
        return jsonify({"error": "Upload a valid PDF."}), 400
    path = _up(file); lines = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            lines.append(f"--- Page {i} ---\n{page.extract_text() or ''}\n")
    path.unlink()
    out = _out("text.txt")
    out.write_text("\n".join(lines), encoding="utf-8")
    return send_file(out, as_attachment=True, download_name="extracted_text.txt")


@app.route("/watermark", methods=["POST"])
def watermark_pdf():
    file    = request.files.get("file")
    wm_text = request.form.get("text", "CONFIDENTIAL")
    if not _ok_pdf(file):
        return jsonify({"error": "Upload a valid PDF."}), 400
    path = _up(file)
    # Build watermark PDF in memory with reportlab
    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    c.saveState()
    c.setFont("Helvetica-Bold", 60)
    c.setFillColorRGB(0.75, 0.75, 0.75, alpha=0.4)
    c.translate(w/2, h/2); c.rotate(45)
    c.drawCentredString(0, 0, wm_text)
    c.restoreState(); c.save(); buf.seek(0)

    wm_page = PdfReader(buf).pages[0]
    reader  = PdfReader(str(path)); writer = PdfWriter()
    for page in reader.pages:
        page.merge_page(wm_page); writer.add_page(page)
    out = _out("watermarked.pdf")
    with open(out, "wb") as fh: writer.write(fh)
    path.unlink()
    return send_file(out, as_attachment=True, download_name="watermarked.pdf")


@app.route("/protect", methods=["POST"])
def protect_pdf():
    file = request.files.get("file"); pwd = request.form.get("password","")
    if not _ok_pdf(file):  return jsonify({"error": "Upload a valid PDF."}), 400
    if not pwd:            return jsonify({"error": "Provide a password."}), 400
    path = _up(file); reader = PdfReader(str(path)); writer = PdfWriter()
    for page in reader.pages: writer.add_page(page)
    writer.encrypt(pwd)
    out = _out("protected.pdf")
    with open(out, "wb") as fh: writer.write(fh)
    path.unlink()
    return send_file(out, as_attachment=True, download_name="protected.pdf")


@app.route("/unlock", methods=["POST"])
def unlock_pdf():
    file = request.files.get("file"); pwd = request.form.get("password","")
    if not _ok_pdf(file): return jsonify({"error": "Upload a valid PDF."}), 400
    path = _up(file); reader = PdfReader(str(path))
    if reader.is_encrypted:
        if not reader.decrypt(pwd):
            path.unlink()
            return jsonify({"error": "Wrong password."}), 400
    writer = PdfWriter()
    for page in reader.pages: writer.add_page(page)
    out = _out("unlocked.pdf")
    with open(out, "wb") as fh: writer.write(fh)
    path.unlink()
    return send_file(out, as_attachment=True, download_name="unlocked.pdf")


@app.route("/compress", methods=["POST"])
def compress_pdf():
    file = request.files.get("file")
    if not _ok_pdf(file): return jsonify({"error": "Upload a valid PDF."}), 400
    path = _up(file); reader = PdfReader(str(path)); writer = PdfWriter()
    for page in reader.pages:
        page.compress_content_streams(); writer.add_page(page)
    out = _out("compressed.pdf")
    with open(out, "wb") as fh: writer.write(fh)
    orig = os.path.getsize(path); comp = os.path.getsize(out)
    pct  = round((1 - comp/orig)*100, 1)
    path.unlink()
    resp = send_file(out, as_attachment=True, download_name="compressed.pdf")
    resp.headers["X-Size-Reduction"] = f"{pct}%"
    return resp


@app.route("/info", methods=["POST"])
def pdf_info():
    file = request.files.get("file")
    if not _ok_pdf(file): return jsonify({"error": "Upload a valid PDF."}), 400
    path = _up(file); reader = PdfReader(str(path)); meta = reader.metadata or {}
    info = {
        "pages"      : len(reader.pages),
        "title"      : meta.get("/Title",   "N/A"),
        "author"     : meta.get("/Author",  "N/A"),
        "subject"    : meta.get("/Subject", "N/A"),
        "creator"    : meta.get("/Creator", "N/A"),
        "encrypted"  : reader.is_encrypted,
        "file_size_kb": round(os.path.getsize(path)/1024, 1),
    }
    path.unlink()
    return jsonify(info)


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)