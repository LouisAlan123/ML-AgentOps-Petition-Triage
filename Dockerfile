FROM python:3.13-slim
ARG INSTALL_ST=0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app PYTHONWARNINGS=ignore
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$INSTALL_ST" = "1" ]; then pip install --no-cache-dir sentence-transformers; fi
RUN useradd -m -u 1000 app && mkdir -p /data/index && chown -R app /data
COPY --chown=app . .
USER app
EXPOSE 8000 8080 9100
