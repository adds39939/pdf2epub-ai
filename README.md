# pdf2epub-ai

Convert PDFs — including image-only scans — into clean, reflowable **EPUB**s, then
fix the OCR errors with a **local vision model** running on your own GPU. No API
keys, no per-page cost, no rate limits. Everything runs in one Docker container;
Ollama runs *inside* the container and uses the **host GPU**.

It was built for messy scanned novels (garbled OCR like `Yeuitinng` → `The Yearling`,
`cotumn` → `column`, `golame` → `go lame`) but works on any text PDF.

## How it works

```
PDF ──▶ plain pass (pdf2epub.py) ──────────────▶ <name>.epub        (fast, no GPU)
    └─▶ vision pass (aifix.py + Ollama/GPU) ────▶ <name>.ai.epub     (OCR-corrected)
```

1. **Plain pass** — extracts the PDF text layer with `pdftotext`, strips running
   headers/footers and page numbers, skips garbled front matter, detects chapters,
   reflows hard-wrapped lines into paragraphs, and writes a minimal valid EPUB 2
   using only the Python standard library. Image-only scans are OCR'd first with
   `ocrmypdf`. Identical books (same text) are de-duplicated.
2. **Vision pass** — renders each page to an image with `pdftoppm` and sends the
   image **plus** the raw OCR text to a vision model (default
   [`qwen2.5vl:7b`](https://ollama.com/library/qwen2.5vl)) with a strict
   "fix OCR errors, don't paraphrase" prompt. Corrected pages are cached to
   `.aifix/` (resumable — a re-run skips finished pages), then rebuilt into
   `<name>.ai.epub`.

## Requirements

- An NVIDIA GPU with a current driver, and Docker.
- The **NVIDIA Container Toolkit**, so the container can use the GPU.

### Install the NVIDIA Container Toolkit (Linux)

From the [Ollama Docker guide](https://docs.ollama.com/docker):

```bash
# Configure the repository
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update

# Install and wire it into Docker
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Verify GPU passthrough:

```bash
docker run --rm --gpus all ollama/ollama nvidia-smi
```

### Windows

Use **Docker Desktop** with the **WSL2** backend and a current NVIDIA Windows
driver. The toolkit ships inside Docker Desktop's WSL2 distro, so `--gpus all`
works once GPU support is enabled in Docker Desktop. The same verify command
applies (run it in PowerShell).

## Build

```bash
docker build -t pdf2epub-ai .
```

## Usage

Put your PDFs in a folder and run the container with that folder mounted at
`/work`. A named `ollama` volume caches the model so it's downloaded only once.

```bash
# Linux / macOS
docker run --rm --gpus all -v "$(pwd):/work" -v ollama:/root/.ollama pdf2epub-ai
```

```powershell
# Windows PowerShell
docker run --rm --gpus all -v "${PWD}:/work" -v ollama:/root/.ollama pdf2epub-ai
```

Outputs land next to the PDFs:

- `<name>.epub` — plain extraction
- `<name>.ai.epub` — vision-corrected

> The first run downloads the vision model (several GB) into the `ollama` volume;
> later runs reuse it.

### Options

| Flag / env var      | Default                     | Purpose |
|---------------------|-----------------------------|---------|
| `--no-ai`           | (off)                       | Run only the plain pass — no GPU/Ollama needed. |
| `AIFIX_MODEL`       | `qwen2.5vl:7b`              | Ollama vision model. Try `qwen2.5vl:32b` for higher fidelity. |
| `AIFIX_DPI`         | `150`                       | Page render DPI for the vision pass. |
| `AIFIX_MAXPAGES`    | `0` (all)                   | Cap pages per book — handy for a quick test (e.g. `6`). |
| `OLLAMA_URL`        | `http://127.0.0.1:11434`    | Point at an external Ollama instead of the in-container one. |

```powershell
# Example: only the plain pass
docker run --rm -v "${PWD}:/work" pdf2epub-ai --no-ai

# Example: higher-fidelity model, test on the first 6 pages
docker run --rm --gpus all -v "${PWD}:/work" -v ollama:/root/.ollama `
  -e AIFIX_MODEL=qwen2.5vl:32b -e AIFIX_MAXPAGES=6 pdf2epub-ai
```

## Notes

- **Resumable.** Corrected pages are cached under `.aifix/<book>/<model>/`. If a
  run is interrupted, just run it again — only unfinished pages are processed.
- **Quality.** The vision model fixes OCR characters faithfully; it does not
  rewrite prose. Paragraph reconstruction is heuristic, so the odd paragraph may
  split at a stray scan blank.
- **No book files in this repo.** `.gitignore` excludes all `*.pdf`, `*.epub`,
  and the cache — only the tool ships here.

## License

MIT — see [LICENSE](LICENSE).
