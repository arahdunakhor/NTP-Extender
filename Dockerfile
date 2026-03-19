FROM python:3.12-slim

LABEL org.opencontainers.image.title="NTP Extender" \
      org.opencontainers.image.description="Local NTP relay/server with web UI" \
      org.opencontainers.image.vendor="arahdunakhor"

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

VOLUME ["/data"]

EXPOSE 12312/tcp
EXPOSE 123/udp

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "12312"]
