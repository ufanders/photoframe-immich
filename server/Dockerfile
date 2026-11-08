FROM python:3.12-slim

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir . && useradd -u 1000 -m frame && mkdir -p /data && chown frame /data

USER frame
ENV CONFIG_PATH=/config/config.yaml STATE_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
