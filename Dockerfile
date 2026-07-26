FROM python:3.12-slim-bookworm

ARG UID=1000
ARG GID=1000

# Tini gives us PID 1 / signal handling without depending on compose's `init:`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini clang ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid "${GID}" app \
 && useradd  --uid "${UID}" --gid "${GID}" --home-dir /app/config --shell /usr/sbin/nologin app

WORKDIR /app

# Install deps as root so site-packages stay read-only for the app user.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# get en_core_web_sm for spacy, which is a kokoro dependency
RUN python -m spacy download en_core_web_sm

COPY . /app

# Pre-create bind-mount targets with correct ownership; if the host directory
# doesn't exist, Docker would otherwise create it as root.
RUN mkdir -p /app/downloads /app/audiobooks /app/config \
 && chown -R app:app /app/downloads /app/audiobooks /app/config

USER app
ENV HOME=/app/config \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "tui.py"]