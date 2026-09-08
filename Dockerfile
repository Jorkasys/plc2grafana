# Образ веб-приложения. Нужен только для docker-режима: локально
# приложение запускается напрямую через `python run.py`.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/
COPY db/ ./db/
COPY poller/src/ ./poller/src/
COPY tools/ ./tools/
COPY run.py ./

RUN mkdir -p /app/config /app/runtime \
    && useradd --system --uid 10001 --create-home plc \
    && chown -R plc:plc /app
USER plc

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

CMD ["python", "run.py", "--host", "0.0.0.0", "--no-browser"]
