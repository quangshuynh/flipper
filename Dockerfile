# Production image for one hosted Flipper instance. See docs/operations/deployment.md.
#
# The image contains application code and pinned dependencies only. The database, attachments,
# eBay credentials, backups, and every secret live on the mounted persistent disk or in the host's
# environment settings; .dockerignore allow-lists the build context so none can be copied in.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# openssh-sftp-server and /root/.ssh let the host's SSH (Render's `scp -s`) transfer a verified
# backup onto the disk at cutover. The image runs no SSH server of its own.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-sftp-server \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0700 /root/.ssh \
    && groupadd --system --gid 10001 flipper \
    && useradd --system --uid 10001 --gid flipper --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin flipper

COPY requirements.txt ./
RUN pip install --no-compile -r requirements.txt

COPY . .
RUN chmod 0755 deploy/entrypoint.sh deploy/flipper-cli \
    && ln -s /app/deploy/flipper-cli /usr/local/bin/flipper

# Fail closed: hosted security is the default, so the container refuses to start without a
# password hash, session secret, and https public origin. The data directory must be on a mounted
# persistent disk (checked by the entrypoint) and eBay seller tokens use the file backend there.
ENV FLIPPER_WEB_SECURITY_MODE=hosted \
    FLIPPER_DATA_DIR=/var/data/flipper \
    FLIPPER_CREDENTIAL_BACKEND=file \
    PORT=10000

EXPOSE 10000

# The entrypoint starts as root only to take ownership of the mounted disk, then drops to the
# unprivileged flipper user before running the command below.
ENTRYPOINT ["/app/deploy/entrypoint.sh"]

# One Uvicorn process (SQLite has one writer; login throttling is per-process). Forwarded headers
# are not trusted: security decisions use FLIPPER_PUBLIC_ORIGIN, never proxy-supplied values.
CMD ["sh", "-c", "exec uvicorn web.app:app --host 0.0.0.0 --port \"$PORT\" --no-proxy-headers --no-server-header --timeout-graceful-shutdown 20"]
