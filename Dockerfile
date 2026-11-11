# Anker SOLIX watchdog: Telegram alerts and bot (no Windows power plans in a container).
# docker run -d --restart unless-stopped -v anker-data:/data --env-file .env ghcr.io/<owner>/anker-telegram-watchdog
FROM python:3.12-slim

ARG ANKER_API=https://github.com/thomluther/anker-solix-api/archive/refs/tags/v3.9.1.tar.gz

# tzdata for TIMEZONE; the rest are wheels for amd64 and arm64
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "anker-solix-api @ ${ANKER_API}" matplotlib adb-shell==0.4.4 \
    && useradd --uid 1000 --create-home app && mkdir /data && chown app:app /data

WORKDIR /app
COPY anker_ups.py ./
COPY ankerups ./ankerups

# .env (optional, variables can come from --env-file instead), database and log live in /data
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib
VOLUME /data
USER app
ENTRYPOINT ["python", "anker_ups.py"]
