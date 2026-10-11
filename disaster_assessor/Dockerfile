FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DISASTER_AUTH_MODE=oidc DISASTER_DB_PATH=/app/data/workspace.sqlite3 \
    DISASTER_MODEL_DIR=/app/models
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 app && useradd -u 10001 -g app -m app
COPY requirements-demo.txt requirements.txt ./
ARG INSTALL_REAL_MODEL=false
RUN pip install --no-cache-dir -r requirements-demo.txt \
    && if [ "$INSTALL_REAL_MODEL" = "true" ]; then pip install --no-cache-dir -r requirements.txt; fi
COPY --chown=app:app . .
RUN mkdir -p /app/data /app/models && chown app:app /app/data /app/models
USER app
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.headless=true"]
