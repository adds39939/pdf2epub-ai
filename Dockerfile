# pdf2epub-ai — PDF -> clean EPUB with local vision OCR correction.
#
# Base image provides the Ollama server + NVIDIA CUDA runtime. We add the PDF
# toolchain (poppler/ocrmypdf/tesseract) and a small Python orchestrator. The
# container runs Ollama internally and uses the HOST GPU via `docker run --gpus all`
# (see https://docs.ollama.com/docker and the README for the NVIDIA Container Toolkit).
FROM ollama/ollama:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
        ocrmypdf \
        tesseract-ocr \
        python3 \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY pdf2epub.py aifix.py run.py /app/

# Ollama listens in-container; aifix talks to it on localhost.
ENV OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_URL=http://127.0.0.1:11434 \
    AIFIX_MODEL=qwen3-vl:30b \
    AIFIX_DPI=150

WORKDIR /work
ENTRYPOINT ["python3", "/app/run.py"]
