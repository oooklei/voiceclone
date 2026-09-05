# Voice Clone Service - dev/prod container image
# Based on the care_tts Dockerfile template (python:3.12-slim + uvicorn).
#
# Runtime notes:
# - sherpa-onnx / onnxruntime need libgomp1 (OpenMP) at runtime; soundfile
#   needs libsndfile1; pydub needs ffmpeg for audio slicing.
# - Models are NEVER baked into the image: models/ is a bind-mounted volume
#   (see docker-compose.yml) and populated by scripts/download_models.*.
# - The service runs as a non-root user (uid 1000).

FROM python:3.12-slim

# apt runtime libraries (kept minimal, no build toolchain)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first so rebuilds reuse the layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code + static management SPA + model download scripts
COPY app/ ./app/
COPY static/ ./static/
COPY scripts/ ./scripts/

# Non-root runtime user; data/ and models/ are volumes mounted at runtime
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data /app/models \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8021

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8021"]