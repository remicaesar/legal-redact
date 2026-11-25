FROM python:3.12-slim-bookworm

# Build-time only (ARG, not ENV): apt has no terminal to ask questions on, and
# without this debconf prints five lines of harmless-looking failures.
ARG DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LEGAL_ANALYZER_STOCK_DIR=/app/samples/documents
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-tur tesseract-ocr-eng poppler-utils \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
COPY docker/requirements.txt /tmp/docker-requirements.txt
# Installing as root is intended here: this is the image's system Python, and the
# app runs as the unprivileged user created below.
RUN pip install --no-cache-dir --root-user-action=ignore \
    -r /tmp/requirements.txt -r /tmp/docker-requirements.txt
COPY . /app
RUN useradd --uid 10001 --create-home workstation \
    && mkdir -p /state /app/data \
    && ln -s /state/legal_documents.db /app/db/legal_documents.db \
    && chown -R workstation:workstation /state /app/data
USER workstation
ENTRYPOINT ["python", "docker/bootstrap.py"]
