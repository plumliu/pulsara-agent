# Backend/package validation, not a desktop image. Build from the repository root.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl binutils python3 python3-venv git \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/uv && /opt/uv/bin/pip install --no-cache-dir uv==0.10.7
ENV PATH="/opt/uv/bin:${PATH}"
WORKDIR /opt/pulsara
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --dev --no-install-project --python /usr/bin/python3 \
    && .venv/bin/python -m playwright install --with-deps chromium
# Ubuntu's default UID 1000 user keeps filesystem permission tests meaningful.
RUN chown -R ubuntu:ubuntu /opt/pulsara /root/.cache/ms-playwright \
    && mv /root/.cache/ms-playwright /opt/chromium
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/chromium CI=true
COPY --chown=ubuntu:ubuntu . .
USER ubuntu
RUN .venv/bin/python tools/prepare_ripgrep.py && uv sync --locked --dev --python /usr/bin/python3
CMD [".venv/bin/python", "-m", "pytest", "-q", "-n", "4"]
