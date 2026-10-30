FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DICEBOT_STATE_PATH=/data/state.json

WORKDIR /app

COPY requirements.txt .
ARG PIP_INDEX_URL
RUN pip install --no-cache-dir -r requirements.txt

# Dedicated unprivileged runtime user
RUN groupadd --gid 10001 dicebot \
    && useradd --uid 10001 --gid dicebot --no-create-home dicebot \
    && mkdir -p /data \
    && chown dicebot:dicebot /data

COPY --chown=dicebot:dicebot dicebot.py .

USER dicebot

VOLUME ["/data"]

CMD ["python", "-u", "dicebot.py"]
