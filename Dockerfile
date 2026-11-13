FROM node:24-bookworm-slim AS bridge

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DICEBOT_STATE_PATH=/data/state.json

WORKDIR /app

COPY requirements.txt .
ARG PIP_INDEX_URL
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=bridge /usr/local/bin/node /usr/local/bin/node
COPY --from=bridge /app/node_modules ./node_modules

# Dedicated unprivileged runtime user
RUN groupadd --gid 10001 dicebot \
    && useradd --uid 10001 --gid dicebot --no-create-home dicebot \
    && mkdir -p /data \
    && chown dicebot:dicebot /data

COPY --chown=dicebot:dicebot dicebot.py worker.py voice-client.mjs package.json ./

USER dicebot

VOLUME ["/data"]

CMD ["node", "voice-client.mjs"]
