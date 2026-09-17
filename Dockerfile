FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential git curl ca-certificates libssl-dev zlib1g-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Build the official Telegram MTProxy binary. Best-effort only: the panel also
# supports running MTProto through the telegrammessenger/proxy Docker image, so
# a compiler failure here must NOT break the whole deploy.
RUN set -eux; \
    if git clone --depth 1 https://github.com/TelegramMessenger/MTProxy.git /tmp/MTProxy \
       && make -C /tmp/MTProxy -j"$(nproc)"; then \
        install -m 0755 /tmp/MTProxy/objs/bin/mtproto-proxy /usr/local/bin/mtproto-proxy; \
    else \
        echo "MTProxy native build skipped; Docker MTProto mode still available"; \
    fi; \
    rm -rf /tmp/MTProxy

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python -m py_compile main.py telegram_proxy.py relay_vless.py shared.py pages.py brand.py telegram_bot.py

# Railway (and most PaaS) inject PORT; fall back to 8080 when running locally.
ENV PORT=8080
EXPOSE 8080
EXPOSE 443

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
