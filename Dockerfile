# Runs on arm64 (Raspberry Pi) and amd64 alike — python:3.12-slim is multi-arch
# and every dependency ships a manylinux aarch64 wheel, so no compiler is needed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# uid/gid 1000 matches the default user on Raspberry Pi OS, so credentials
# mounted from the host stay readable without loosening their permissions.
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Only application code is baked in. Credentials (~/.oci, *.pem, .env, SSH keys)
# are mounted at run time so that deleting the image leaves no secrets behind.
COPY a1launcher/ ./a1launcher/

RUN mkdir -p /var/log/a1launcher /keys \
 && chown -R app:app /var/log/a1launcher /keys /app

USER app

ENTRYPOINT ["python", "-m", "a1launcher"]
CMD []
