#!/usr/bin/env python3
"""
Entry point for the combined pdf2epub-ai container.

Pipeline:
  1. plain pass  (pdf2epub) — extract the PDF text layer and build a clean EPUB
  2. vision pass (aifix)    — correct OCR errors against each page image using a
                              local vision model served by Ollama inside this
                              container, on the host GPU (docker run --gpus all)

Every *.pdf in the mounted /work directory becomes <name>.epub (plain) and
<name>.ai.epub (AI-corrected). Pass --no-ai to run only the plain pass (no GPU
/ no Ollama needed).
"""
import os
import sys
import json
import time
import subprocess
import urllib.request

sys.path.insert(0, "/app")
import pdf2epub          # noqa: E402
import aifix             # noqa: E402

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL      = os.environ.get("AIFIX_MODEL", "qwen3-vl:30b")
SERVE_ENV  = dict(os.environ, OLLAMA_HOST=os.environ.get("OLLAMA_HOST", "0.0.0.0:11434"))


def _get(path, timeout=5):
    with urllib.request.urlopen(OLLAMA_URL + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def start_ollama():
    print("[run] starting ollama serve ...", flush=True)
    proc = subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=SERVE_ENV)
    for _ in range(60):
        try:
            print("[run] ollama up:", _get("/api/version", 3).get("version"), flush=True)
            return proc
        except Exception:
            time.sleep(1)
    proc.terminate()
    raise SystemExit("[run] ERROR: ollama server did not come up")


def ensure_model():
    try:
        tags = _get("/api/tags").get("models", [])
        if any(MODEL in (m.get("name"), m.get("model")) for m in tags):
            print("[run] model present:", MODEL, flush=True)
            return
    except Exception:
        pass
    print("[run] pulling model (first run downloads several GB):", MODEL, flush=True)
    subprocess.run(["ollama", "pull", MODEL], check=True, env=SERVE_ENV)


def main():
    do_ai = "--no-ai" not in sys.argv[1:]

    print("[run] === plain pass: extract text -> EPUB ===", flush=True)
    pdf2epub.main()

    if not do_ai:
        print("[run] --no-ai given; skipping the vision pass.", flush=True)
        return

    proc = start_ollama()
    try:
        ensure_model()
        print("[run] === vision pass: OCR correction -> EPUB ===", flush=True)
        aifix.main()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
