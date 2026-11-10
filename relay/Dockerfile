# Construir LOCALMENTE (nunca no servidor nomad) — ver README, secção Deploy.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py tunnel.py ./

RUN useradd --system --uid 10001 relay && mkdir -p /data && chown relay /data
USER relay
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"
CMD ["python", "relay.py"]
