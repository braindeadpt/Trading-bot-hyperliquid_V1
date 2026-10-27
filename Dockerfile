# Hyperliquid paper bot — Linux (Oracle A1 arm64 / amd64) packaging.
# One image, one container per pm2 process (see deploy/linux/compose.yaml).
# Secrets never enter the image: env via /etc/hyperliquid/bot.env (env_file),
# AWS creds via read-only ~/.aws mount.
FROM python:3.12-slim

ENV PYTHONIOENCODING=utf-8     PYTHONUTF8=1     PYTHONDONTWRITEBYTECODE=1     PIP_NO_CACHE_DIR=1

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin bot

WORKDIR /opt/hyperliquid
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY config/ ./config/
COPY audit_all.py ./
COPY deploy/linux/ ./deploy/linux/

RUN mkdir -p /data /logs && chown -R bot:bot /data /logs /opt/hyperliquid
USER bot

# No default ENTRYPOINT — each compose service sets its command.
CMD ["python", "-u", "-X", "utf8", "main.py", "--config", "config/settings.yaml", "--mode", "paper"]
