FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# Chromium + its system libraries must be installed as root.
RUN pip install ".[postgres,browser]" \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 onion \
 && mkdir -p /data \
 && chown onion:onion /data

USER onion
ENV OPENONION_DATA_DIR=/data
EXPOSE 8000

CMD ["openonion", "serve", "--host", "0.0.0.0", "--port", "8000"]
