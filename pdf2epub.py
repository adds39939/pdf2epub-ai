#!/usr/bin/env python3
"""
PDF -> clean plain-text EPUB.

Extracts the PDF text layer with pdftotext (OCRs first only if there is no
text layer), strips page numbers / running headers / garbled front matter,
detects chapters, re-flows hard-wrapped lines into paragraphs, and writes a
minimal valid EPUB 2 using only the standard library.
"""
import os, re, sys, glob, html, uuid, zipfile, subprocess
from collections import Counter

WORK = "/work"
MIN_CHARS_PER_PAGE = 100   # below this a page-set is treated as image-only -> OCR


# --------------------------------------------------------------------------- #
# Shelling out to the poppler / ocr tools
# --------------------------------------------------------------------------- #
def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

def pdf_pages(path):
    m = re.search(r'^Pages:\s+(\d+)', run(["pdfinfo", path]).stdout, re.M)
    return int(m.group(1)) if m else 0

def pdftotext(path):
    return run(["pdftotext", "-enc", "UTF-8", path, "-"]).stdout

def maybe_ocr(path, pages):
    """Return a path to a text-bearing PDF, running OCR only if needed."""
    raw = pdftotext(path)
    chars = len(re.sub(r'\s', '', raw))
    if pages and chars / pages >= MIN_CHARS_PER_PAGE:
        return path, raw                      # already has a real text layer
    print("    no text layer -> running OCR (this is slow)...")
    ocr_path = path[:-4] + ".ocr.pdf"
    run(["ocrmypdf", "-l", "eng", "--skip-text", path, ocr_path])
    return ocr_path, pdftotext(ocr_path)


# --------------------------------------------------------------------------- #
# Text cleaning / structuring
# --------------------------------------------------------------------------- #
CHAP_RE   = re.compile(r'^(prologue|epilogue|chapter\s+[\w-]+)\.?$', re.I)
NUM_RE    = re.compile(r'^\d{1,3}$')
TERM_RE   = re.compile(r'[.!?”’"\')]$')          # sentence-ending punctuation
PROSE_HINTS = (' the ', ' and ', ' was ', ' her ', ' his ', ' that ', ' with ')


def page_line_lists(raw):
    pages = raw.replace('\r\n', '\n').replace('\r', '\n').split('\f')
    out = []
    for pg in pages:
        ls = pg.split('\n')
        while ls and not ls[0].strip():
            ls.pop(0)
        while ls and not ls[-1].strip():
            ls.pop()
        out.append(ls)
    return out


LEGAL = ('all rights reserved', 'no part of this publication', 'isbn',
         'printed by', 'copyright ©', 'published by', 'first published',
         'this book is sold subject', 'library of congress', 'scholastic inc')


def detect_noise(page_lines):
    """Lines recurring at the top/bottom of pages = running headers/footers
    (caught even when a header spans several lines, e.g. 'THE' / 'YEARLING')."""
    n = len(page_lines)
    cut = max(3, int(n * 0.15))
    counts = Counter()
    for ls in page_lines:
        ne = [l.strip() for l in ls if l.strip()]
        for l in ne[:3] + ne[-3:]:
            counts[l] += 1
    return {t for t, c in counts.items()
            if c >= cut and len(t) <= 30 and not t.isdigit()
            and not TERM_RE.search(t)}


def is_legal(ls):
    t = ' '.join(ls).lower()
    return any(k in t for k in LEGAL)


def content_start(page_lines):
    """First real-content page: at or after the first chapter heading, skipping
    title pages and copyright / legal pages."""
    chap_pages = [i for i, ls in enumerate(page_lines)
                  if any(CHAP_RE.match(l.strip()) for l in ls)]
    lower = min(chap_pages) if chap_pages else 0
    for i in range(lower, len(page_lines)):
        ls = page_lines[i]
        txt = ' ' + ' '.join(ls).lower() + ' '
        if len(txt) > 500 and sum(h in txt for h in PROSE_HINTS) >= 3 \
                and not is_legal(ls):
            return i
    return lower


def reflow(lines):
    """Join hard-wrapped lines into paragraphs (blank line or short terminal
    line ends a paragraph; trailing hyphens are de-hyphenated)."""
    widths = sorted(len(l) for l in lines if l.strip())
    if not widths:
        return []
    full   = widths[min(len(widths) - 1, int(len(widths) * 0.8))]
    thresh = max(20, full * 0.65)

    paras, buf = [], ""

    def flush():
        nonlocal buf
        t = re.sub(r'\s{2,}', ' ', buf).strip()
        if t:
            paras.append(t)
        buf = ""

    for raw in lines:
        s = raw.strip()
        if not s:
            flush()
            continue
        if re.search(r'[A-Za-z]-$', buf):       # word split across the wrap
            buf = buf[:-1] + s
        else:
            buf = (buf + " " + s) if buf else s
        if len(s) < thresh and TERM_RE.search(s):
            flush()
    flush()
    return paras


def _strip_page(ls, noise):
    """Remove running heads/feet and surrounding blanks from one page."""
    body = [l for l in ls if l.strip() not in noise]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return body


def build_chapters(raw):
    page_lines = page_line_lists(raw)
    noise = detect_noise(page_lines)
    start = content_start(page_lines)

    chapters, cur_title, cur_lines = [], None, []

    def new_chapter(title):
        nonlocal cur_title, cur_lines
        if cur_lines or cur_title:
            chapters.append((cur_title, cur_lines))
        cur_title, cur_lines = title, []

    for ls in page_lines[start:]:
        body = _strip_page(ls, noise)
        if not body:
            continue
        if NUM_RE.match(body[0].strip()):                  # numeric chapter at top
            new_chapter(body[0].strip())
            body.pop(0)
        if body and NUM_RE.match(body[-1].strip()):        # trailing page number
            body.pop()
        for line in body:
            if CHAP_RE.match(line.strip()):
                new_chapter(line.strip().title())
            else:
                cur_lines.append(line)
    new_chapter(None)

    # Safety net: if numeric "chapters" exploded (page numbers misread as
    # headings), rebuild ignoring numeric headings entirely.
    real = [c for c in chapters if c[1]]
    if len(real) > max(60, len(page_lines) / 3):
        return _build_no_numeric(page_lines, noise, start)

    out = []
    for i, (title, lines) in enumerate(real):
        paras = _strip_dup_heading(title, reflow(lines))
        if not paras:
            continue
        if not title:
            title = ("Chapter %d" % (i + 1)) if len(real) > 1 else "Text"
        out.append((title, paras))
    return out


def _strip_dup_heading(title, paras):
    """Drop a chapter heading that leaked into the first body paragraph
    (e.g. title 'Chapter 1' + body 'Chapter 1 As the auditorium...')."""
    if not title or not paras:
        return paras
    m = re.match(re.escape(title) + r'\b[\s.:—–-]*', paras[0], re.I)
    if not m or m.end() == 0:
        return paras
    rest = paras[0][m.end():].lstrip()
    return ([rest] + paras[1:]) if rest else paras[1:]


def _build_no_numeric(page_lines, noise, start):
    chapters, cur_title, cur_lines = [], None, []

    def new_chapter(title):
        nonlocal cur_title, cur_lines
        if cur_lines or cur_title:
            chapters.append((cur_title, cur_lines))
        cur_title, cur_lines = title, []

    for ls in page_lines[start:]:
        body = _strip_page(ls, noise)
        if not body:
            continue
        if NUM_RE.match(body[-1].strip()):
            body.pop()
        for line in body:
            if CHAP_RE.match(line.strip()):
                new_chapter(line.strip().title())
            else:
                cur_lines.append(line)
    new_chapter(None)

    out = []
    real = [c for c in chapters if c[1]]
    for i, (title, lines) in enumerate(real):
        paras = _strip_dup_heading(title, reflow(lines))
        if not paras:
            continue
        out.append((title or (("Chapter %d" % (i + 1)) if len(real) > 1 else "Text"),
                    paras))
    return out


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
def meta_from_name(stem):
    if ' -- ' in stem:
        parts = [p.strip() for p in stem.split(' -- ')]
        title  = re.sub(r'\s*\(Point Horror\)\s*', '', parts[0]).strip()
        author = parts[1] if len(parts) > 1 else 'Unknown'
        if ',' in author:
            last, first = [x.strip() for x in author.split(',', 1)]
            author = (first.replace('_', '.').strip() + ' ' + last).strip()
    elif ' - ' in stem:
        parts  = [p.strip() for p in stem.split(' - ')]
        author = parts[0]
        if ',' in author:
            bits = [b.strip() for b in author.split(',')]
            author = (bits[1] + ' ' + bits[0]) if len(bits) >= 2 else author
        title = re.sub(r'\s*\(\d{4}\)\s*', '', parts[1]).strip() if len(parts) > 1 else stem
    else:
        title, author = stem, 'Unknown'
    return title or stem, author or 'Unknown'


# --------------------------------------------------------------------------- #
# EPUB writing (stdlib only)
# --------------------------------------------------------------------------- #
def esc(s):
    return html.escape(s, quote=True)

CHAP_XHTML = ('<?xml version="1.0" encoding="utf-8"?>\n'
              '<!DOCTYPE html>\n'
              '<html xmlns="http://www.w3.org/1999/xhtml">\n'
              '<head><meta charset="utf-8"/><title>{title}</title></head>\n'
              '<body>\n<h2>{title}</h2>\n{body}\n</body>\n</html>\n')


def write_epub(out_path, title, author, chapters):
    book_id = "urn:uuid:" + str(uuid.uuid4())
    manifest, spine, navpoints, files = [], [], [], {}

    for i, (ctitle, paras) in enumerate(chapters, 1):
        fname = "OEBPS/ch%03d.xhtml" % i
        body  = "\n".join("<p>%s</p>" % esc(p) for p in paras)
        files[fname] = CHAP_XHTML.format(title=esc(ctitle), body=body)
        manifest.append('<item id="ch%03d" href="ch%03d.xhtml" '
                        'media-type="application/xhtml+xml"/>' % (i, i))
        spine.append('<itemref idref="ch%03d"/>' % i)
        navpoints.append(
            '<navPoint id="np%03d" playOrder="%d"><navLabel><text>%s</text>'
            '</navLabel><content src="ch%03d.xhtml"/></navPoint>'
            % (i, i, esc(ctitle), i))

    files["OEBPS/content.opf"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
        'unique-identifier="bookid">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">\n'
        '<dc:title>%s</dc:title>\n<dc:creator opf:role="aut">%s</dc:creator>\n'
        '<dc:language>en</dc:language>\n'
        '<dc:identifier id="bookid">%s</dc:identifier>\n</metadata>\n'
        '<manifest>\n<item id="ncx" href="toc.ncx" '
        'media-type="application/x-dtbncx+xml"/>\n%s\n</manifest>\n'
        '<spine toc="ncx">\n%s\n</spine>\n</package>\n'
        % (esc(title), esc(author), book_id,
           "\n".join(manifest), "\n".join(spine)))

    files["OEBPS/toc.ncx"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        '<head><meta name="dtb:uid" content="%s"/></head>\n'
        '<docTitle><text>%s</text></docTitle>\n<navMap>\n%s\n</navMap>\n</ncx>\n'
        % (book_id, esc(title), "\n".join(navpoints)))

    files["META-INF/container.xml"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles>\n</container>\n')

    with zipfile.ZipFile(out_path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        for name, data in files.items():
            z.writestr(name, data, zipfile.ZIP_DEFLATED)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    pdfs = sorted(glob.glob(os.path.join(WORK, "*.pdf")))
    pdfs = [p for p in pdfs if not p.endswith(".ocr.pdf")]
    seen = {}
    for path in pdfs:
        stem = os.path.splitext(os.path.basename(path))[0]
        print("=" * 70)
        print("PDF:", os.path.basename(path))
        pages = pdf_pages(path)
        _, raw = maybe_ocr(path, pages)

        sig = len(raw)
        if sig in seen:
            print("    duplicate text of '%s' -> skipped" % seen[sig])
            continue
        seen[sig] = stem

        chapters = build_chapters(raw)
        if not chapters:
            print("    !! no text recovered -> skipped")
            continue

        title, author = meta_from_name(stem)
        out_path = os.path.join(WORK, stem + ".epub")
        write_epub(out_path, title, author, chapters)

        npara = sum(len(p) for _, p in chapters)
        print("    title : %s" % title)
        print("    author: %s" % author)
        print("    chapters: %d   paragraphs: %d" % (len(chapters), npara))
        print("    first chapter '%s' preview:" % chapters[0][0])
        preview = (" ".join(chapters[0][1]))[:400]
        print("      " + preview + ("..." if len(preview) == 400 else ""))
        print("    -> %s.epub" % stem)


if __name__ == "__main__":
    main()
