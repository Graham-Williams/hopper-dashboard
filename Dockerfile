# hopper-dashboard — two gunicorn processes (read :8080, ingest :8081) in one
# container, with a pinned + checksum-verified rclone for the in-container
# destination probes. PID 1 starts as root only to stage the 0600 host
# rclone.conf, then setpriv's to the `dashboard` user (see entrypoint.sh) —
# so there is deliberately no USER directive here.
#
# Base image pinned by digest (supply chain). Refresh with:
#   docker buildx imagetools inspect python:3.12-slim   → Digest: sha256:…
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# --- rclone (pinned; SHA256 from https://downloads.rclone.org/<ver>/SHA256SUMS)
# (named RCLONE_REL, not RCLONE_VERSION: rclone reads RCLONE_* env vars as flags, so
#  RCLONE_VERSION="v1.75.1" would be parsed as --version and crash the build.)
ARG RCLONE_REL=v1.75.1
ARG RCLONE_SHA256_AMD64=982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab
ARG RCLONE_SHA256_ARM64=03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sum="$RCLONE_SHA256_AMD64" ;; \
      arm64) sum="$RCLONE_SHA256_ARM64" ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    zip="rclone-${RCLONE_REL}-linux-${arch}.zip"; \
    curl -fsSLo "/tmp/$zip" "https://downloads.rclone.org/${RCLONE_REL}/${zip}"; \
    echo "${sum}  /tmp/$zip" | sha256sum -c -; \
    unzip -q "/tmp/$zip" -d /tmp/rclone; \
    install -m 0755 "/tmp/rclone/rclone-${RCLONE_REL}-linux-${arch}/rclone" /usr/local/bin/rclone; \
    rm -rf /tmp/rclone "/tmp/$zip"; \
    apt-get purge -y --auto-remove curl unzip; \
    rm -rf /var/lib/apt/lists/*; \
    rclone version; \
    command -v setpriv   # entrypoint.sh depends on it (util-linux)

RUN useradd --create-home --uid 10001 dashboard
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard/ ./dashboard/
COPY entrypoint.sh ./entrypoint.sh
RUN chmod 0755 entrypoint.sh \
    && mkdir -p /app/data && mkdir -p -m 0700 /tmp/rclone \
    && chown -R dashboard:dashboard /app /tmp/rclone

ENV DASHBOARD_DATA=/app/data \
    JOBS_FILE=/app/jobs.yml \
    RCLONE_CONFIG=/tmp/rclone/rclone.conf \
    PYTHONUNBUFFERED=1

# No USER: entrypoint.sh drops to `dashboard` itself (see header comment).
EXPOSE 8080 8081

HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=30s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=5); u.urlopen('http://127.0.0.1:8081/healthz', timeout=5)"

ENTRYPOINT ["./entrypoint.sh"]
