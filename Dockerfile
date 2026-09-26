# Bhutan Telecom Hosting Provisioning Service
#
# Multi-stage build: wheels are built in a full image, then only the resulting
# virtualenv is copied into a slim runtime. paramiko needs build tooling for
# its crypto backend; the runtime does not.

# ---------- Stage 1: build dependencies ----------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential + libffi for paramiko/cryptography C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install into an isolated prefix so we can copy it wholesale into the runtime
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only requirements first so dependency layer is cached across code changes
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt


# ---------- Stage 2: runtime ----------
FROM python:3.13-slim AS runtime

# Unbuffered stdout/stderr so `docker logs` streams live rather than buffering
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/provisioner

# git is required by paramiko to read public keys for known_hosts handling
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash --uid 1000 provisioner

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=provisioner:provisioner app.py cli.py config.py notifier.py nic_client.py tls_config.py ./
COPY --chown=provisioner:provisioner provisioners/ ./provisioners/
COPY --chown=provisioner:provisioner templates/ ./templates/
COPY --chown=provisioner:provisioner static/ ./static/

USER provisioner

EXPOSE 8000

# Uses /api/v1/health, which touches no external server. Note this only proves
# the process is alive -- it does not verify cPanel/DA reachability.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
