# recast-server: the web app + paced Sonarr/Radarr automation, with jellyfin-ffmpeg (QSV/VAAPI/NVENC ready).
# Published as ghcr.io/awpsec/recast — see compose.yaml.
FROM python:3.13-slim-trixie

ARG FFMPEG_PKG=jellyfin-ffmpeg8
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg tzdata; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg; \
    printf 'Types: deb\nURIs: https://repo.jellyfin.org/debian\nSuites: trixie\nComponents: main\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/jellyfin.gpg\n' \
        "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/jellyfin.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends "$FFMPEG_PKG"; \
    ln -s /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg; \
    ln -s /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe; \
    apt-get purge -y --auto-remove curl gnupg; \
    # a plain file, not a symlink, so compose's /etc/localtime mount (the host's zone) replaces it in place
    rm -f /etc/localtime; cp /usr/share/zoneinfo/Etc/UTC /etc/localtime; \
    rm -rf /var/lib/apt/lists/*; \
    ffmpeg -hide_banner -version | head -1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/src /root/.cache

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /config /scratch

ENV RECAST_HOME=/config \
    RECAST_SCRATCH=/scratch \
    RECAST_HOST=0.0.0.0 \
    RECAST_PORT=8484 \
    PUID=1000 \
    PGID=1000 \
    UMASK=002 \
    PYTHONUNBUFFERED=1
VOLUME ["/config", "/scratch"]
EXPOSE 8484
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('RECAST_PORT', '8484'), timeout=4)" || exit 1
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
