#!/usr/bin/env python3
"""
Vision-grounded OCR-correction pass.

For each PDF page: render the page to an image (pdftoppm) and pull the raw OCR
text (pdftotext), then ask a local vision model (via Ollama on the host GPU) to
emit the corrected page text, using the image as ground truth. Corrected pages
are cached to disk (resumable), then fed into pdf2epub's chapter/EPUB builder.

Runs entirely against a local Ollama server — no API key, no rate limits.

Env knobs:
  OLLAMA_URL     default http://host.docker.internal:11434
  AIFIX_MODEL    default qwen3-vl:30b
  AIFIX_DPI      default 150
  AIFIX_MAXPAGES default 0 (0 = all pages; set e.g. 6 for a quick test)
"""
import os, re, glob, json, time, base64, hashlib, subprocess, urllib.request
import pdf2epub as P

WORK   = "/work"
OLLAMA = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
MODEL  = os.environ.get("AIFIX_MODEL", "qwen3-vl:30b")
DPI    = os.environ.get("AIFIX_DPI", "150")
MAXP   = int(os.environ.get("AIFIX_MAXPAGES", "0"))
CACHE  = os.path.join(WORK, ".aifix")

PROMPT_HEAD = """You are given a scanned page from a printed book: the page image and the raw OCR text extracted from it. The OCR text has recognition errors AND broken line/paragraph structure.

Using the IMAGE as the source of truth, output the corrected, properly-formatted text of the page.

Fix OCR errors:
- Wrong letters (e.g. "cotumn" -> "column"), split or merged words (e.g. "golame" -> "go lame"), garbled punctuation and quotation marks, mis-recognized characters.
- Preserve the author's exact wording. Do NOT paraphrase, summarize, translate, modernize, or rewrite — correct recognition errors only.

Fix line and paragraph breaks (important):
- Reflow the text into proper paragraphs. Join lines that were hard-wrapped mid-paragraph into one continuous line; remove the line breaks WITHIN a paragraph.
- Use the page image to decide where paragraphs truly begin — a new paragraph is shown by an indented first line or extra vertical spacing. Start a new paragraph only at those real boundaries.
- In dialogue, each new speaker's turn is normally its own paragraph.
- Output each paragraph as a single line with no internal breaks, and separate paragraphs with exactly one blank line.
- Remove stray breaks or blank lines that wrongly split one paragraph; merge a paragraph the OCR split across a column or page break.
- Re-join words hyphenated across a line break (e.g. "exam-" / "ple" -> "example").

Other:
- Keep chapter headings / titles / section markers (e.g. "Chapter 5", "Prologue") on their own line.
- Omit running page headers/footers (a repeated book title) and standalone page numbers.
- Output ONLY the page's text. No commentary, notes, labels, or markup.
- If the page has no readable body text (cover, blank page, illustration only), output nothing.

Raw OCR text:
<<<
"""
PROMPT_TAIL = "\n>>>\n"
PROMPT_HASH = hashlib.sha1((PROMPT_HEAD + PROMPT_TAIL).encode()).hexdigest()[:8]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def run_bytes(cmd):
    return subprocess.run(cmd, capture_output=True)


def page_count(path):
    m = re.search(r'^Pages:\s+(\d+)', run(["pdfinfo", path]).stdout, re.M)
    return int(m.group(1)) if m else 0


def full_text_len(path):
    return len(run(["pdftotext", "-enc", "UTF-8", path, "-"]).stdout)


def page_text(path, n):
    return run(["pdftotext", "-enc", "UTF-8", "-f", str(n), "-l", str(n),
                path, "-"]).stdout


def page_image_b64(path, n):
    prefix = "/tmp/aifix_page"
    png = prefix + ".png"
    if os.path.exists(png):
        os.remove(png)
    run_bytes(["pdftoppm", "-f", str(n), "-l", str(n), "-r", DPI,
               "-png", "-singlefile", path, prefix])
    if not os.path.exists(png):
        raise RuntimeError("pdftoppm produced no image for page %d" % n)
    with open(png, "rb") as f:
        return base64.b64encode(f.read()).decode()


def ollama_generate(prompt, image_b64, retries=5):
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 6000},
    }).encode()
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(
                OLLAMA + "/api/generate", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read().decode()).get("response", "")
        except Exception as e:
            last = e
            time.sleep(2 * (a + 1))
    raise last


def correct_page(path, stem, n):
    cdir = os.path.join(CACHE, stem, MODEL.replace(":", "_").replace("/", "_"), PROMPT_HASH)
    os.makedirs(cdir, exist_ok=True)
    cfile = os.path.join(cdir, "p%04d.txt" % n)
    if os.path.exists(cfile):
        with open(cfile, encoding="utf-8") as f:
            return f.read()
    raw = page_text(path, n)
    img = page_image_b64(path, n)
    out = ollama_generate(PROMPT_HEAD + raw + PROMPT_TAIL, img).strip()
    tmp = cfile + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(out)
    os.replace(tmp, cfile)
    return out


def main():
    pdfs = [p for p in sorted(glob.glob(os.path.join(WORK, "*.pdf")))
            if not p.endswith(".ocr.pdf")]
    seen = {}
    for path in pdfs:
        base = os.path.basename(path)
        stem = os.path.splitext(base)[0]
        sig = full_text_len(path)
        if sig in seen:
            print("== %s\n   duplicate text of '%s' -> skipped" % (base, seen[sig]))
            continue
        seen[sig] = stem

        pages = page_count(path)
        if MAXP:
            pages = min(pages, MAXP)
        print("== %s  (%d pages, model=%s, dpi=%s)" % (base, pages, MODEL, DPI),
              flush=True)

        corrected = []
        t0 = time.time()
        for n in range(1, pages + 1):
            corrected.append(correct_page(path, stem, n))
            if n % 5 == 0 or n == pages:
                rate = (time.time() - t0) / n
                print("   page %d/%d  (%.1fs/page)" % (n, pages, rate), flush=True)

        chapters = P.build_chapters("\f".join(corrected))
        if not chapters:
            print("   !! no text recovered -> skipped")
            continue
        title, author = P.meta_from_name(stem)
        out = os.path.join(WORK, stem + ".ai.epub")
        P.write_epub(out, title, author, chapters)
        npara = sum(len(p) for _, p in chapters)
        print("   -> %s.ai.epub  (%d chapters, %d paragraphs)"
              % (stem, len(chapters), npara), flush=True)


if __name__ == "__main__":
    main()
