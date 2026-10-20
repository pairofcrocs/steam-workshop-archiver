# syntax=docker/dockerfile:1
FROM ubuntu:24.04

ARG TARGETARCH
ARG APP_VERSION=dev
ARG DEPOTDOWNLOADER_VERSION=3.4.0
ARG DEPOTDOWNLOADER_SHA256_AMD64=a999dec66b4850fc961bd50366696d23c2d0fad7b18790e6a5647b2f19097a53
ARG DEPOTDOWNLOADER_SHA256_ARM64=d9fb612ccebc1db8eeea3b4045d2221ec70431381393ce908fb72f01d4f9c812

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
    APP_VERSION=${APP_VERSION} \
    META_DIR=/meta \
    DOWNLOADS_DIR=/downloads \
    STEAMCMD_PATH=/meta/steamcmd/steamcmd.sh \
    DEPOTDOWNLOADER_PATH=/opt/depotdownloader/DepotDownloader \
    PATH=/opt/venv/bin:$PATH

RUN set -eux; \
    if [ "$TARGETARCH" = "amd64" ]; then dpkg --add-architecture i386; fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3 python3-venv ca-certificates curl unzip gosu tini tzdata; \
    if [ "$TARGETARCH" = "amd64" ]; then apt-get install -y --no-install-recommends lib32gcc-s1; fi; \
    rm -rf /var/lib/apt/lists/*

# SteamCMD is x86-only. It's copied into /meta/steamcmd on first start so its
# self-updates and cached Steam logins survive container recreation.
RUN set -eux; \
    if [ "$TARGETARCH" = "amd64" ]; then \
        mkdir -p /opt/steamcmd; \
        curl -fsSL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz | tar -xzf - -C /opt/steamcmd; \
        /opt/steamcmd/steamcmd.sh +quit || true; \
        test -x /opt/steamcmd/linux32/steamcmd; \
    fi

RUN set -eux; \
    case "$TARGETARCH" in \
        amd64) dd=linux-x64;   sum="$DEPOTDOWNLOADER_SHA256_AMD64" ;; \
        arm64) dd=linux-arm64; sum="$DEPOTDOWNLOADER_SHA256_ARM64" ;; \
        *) echo "unsupported arch $TARGETARCH"; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/dd.zip \
        "https://github.com/SteamRE/DepotDownloader/releases/download/DepotDownloader_${DEPOTDOWNLOADER_VERSION}/DepotDownloader-${dd}.zip"; \
    echo "$sum  /tmp/dd.zip" | sha256sum -c -; \
    mkdir -p /opt/depotdownloader; \
    unzip -q /tmp/dd.zip -d /opt/depotdownloader; \
    rm /tmp/dd.zip; \
    chmod +x /opt/depotdownloader/DepotDownloader

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv && pip install --no-cache-dir -r /tmp/requirements.txt

RUN useradd --system --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY app/ ./app/
COPY docker/entrypoint.sh docker/swa-steam-login /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/swa-steam-login

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
