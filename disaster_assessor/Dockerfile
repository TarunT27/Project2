# Rapid Disaster Assessment platform.
#   CPU, demo + exports:        docker build -t dra .
#   With PyTorch/Ultralytics:   docker build --build-arg FULL=1 -t dra .
# For NVIDIA GPUs build FROM a CUDA-enabled PyTorch image instead and run with --gpus all.
FROM python:3.12-slim

ARG FULL=0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DRA_DATA_DIR=/data \
    DRA_MODELS_DIR=/models \
    HOST=0.0.0.0 \
    PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-demo.txt requirements.txt ./
RUN pip install -r requirements-demo.txt && if [ "$FULL" = "1" ]; then pip install -r requirements.txt; fi

COPY . .
RUN useradd --create-home --uid 10001 dra && mkdir -p /data /models && chown -R dra /data /models
USER dra
VOLUME ["/data", "/models"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"
CMD ["python", "server.py"]
