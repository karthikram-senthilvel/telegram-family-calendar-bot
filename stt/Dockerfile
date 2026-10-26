FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models \
    STT_MODEL=nemo-parakeet-tdt-0.6b-v2 \
    STT_QUANTIZATION=int8

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download the model at build time so the running container never goes online.
RUN python -c "import os, onnx_asr; onnx_asr.load_model(os.environ['STT_MODEL'], quantization=os.environ['STT_QUANTIZATION'] or None)"
ENV HF_HUB_OFFLINE=1

COPY server.py .
RUN useradd --uid 1000 --create-home stt && chown -R stt /models
USER stt

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"
CMD ["python", "server.py"]
