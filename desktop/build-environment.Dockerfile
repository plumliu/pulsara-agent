# syntax=docker/dockerfile:1
FROM ghcr.io/jdx/mise:2026.10.15@sha256:0593528c637b0dae8a0acca66b5d49266cf262a811f06530d7273d3cc002f3a0 AS mise
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS tools
COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential bison flex pkg-config binutils patchelf libopenblas0 \
    ca-certificates curl git xz-utils libyaml-0-2 libcrypt1 \
    && rm -rf /var/lib/apt/lists/*
ENV MISE_DATA_DIR=/opt/mise MISE_YES=1 MISE_STATE_DIR=/root/.local/state/mise MISE_CACHE_DIR=/root/.cache/mise UV_PYTHON_INSTALL_DIR=/opt/python
WORKDIR /workspace
COPY mise.toml .python-version ./
RUN mise trust mise.toml && mise install && mise exec -- uv python install

FROM tools AS browser-version
COPY uv.lock ./
RUN mise exec -- uv run --no-project python -c "import tomllib; print(next(p['version'] for p in tomllib.load(open('uv.lock', 'rb'))['package'] if p['name'] == 'playwright'))" > /tmp/pulsara-playwright-version

FROM tools AS environment
# Playwright owns its Linux package list. Its version comes from the same lock
# as the bundled runtime, rather than a second hand-maintained dependency list.
# BuildKit compares this exact SDK version value, so unrelated lockfile changes
# do not reinstall tools or the browser's operating-system dependencies.
COPY --from=browser-version /tmp/pulsara-playwright-version /tmp/pulsara-playwright-version
RUN version=$(cat /tmp/pulsara-playwright-version) \
    && mise exec -- uvx --from "playwright==$version" playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*
# initdb deliberately rejects root; build and relocation verification use the
# same unprivileged owner. Native resource caches remain outside application data.
RUN useradd --create-home --uid 1001 builder && chown -R builder:builder /workspace /opt/mise /opt/python
ENV HOME=/home/builder MISE_STATE_DIR=/home/builder/.local/state/mise MISE_CACHE_DIR=/home/builder/.cache/mise MISE_TRUSTED_CONFIG_PATHS=/workspace
USER builder
RUN mise trust mise.toml

FROM environment AS build
ARG TARGETARCH
COPY --chown=builder:builder . .
# BuildKit owns cache storage and per-architecture isolation. mise owns which
# resource tasks are stale; its state is disposable alongside these resources.
RUN --mount=type=cache,id=pulsara-uv-${TARGETARCH},target=/home/builder/.cache/uv,sharing=locked,uid=1001,gid=1001 \
    --mount=type=cache,id=pulsara-npm-${TARGETARCH},target=/home/builder/.npm,sharing=locked,uid=1001,gid=1001 \
    --mount=type=cache,id=pulsara-frontend-modules-${TARGETARCH},target=/workspace/frontend/node_modules,sharing=locked,uid=1001,gid=1001 \
    --mount=type=cache,id=pulsara-shell-modules-${TARGETARCH},target=/workspace/desktop/node_modules,sharing=locked,uid=1001,gid=1001 \
    --mount=type=cache,id=pulsara-electron-${TARGETARCH},target=/home/builder/.cache/electron,sharing=locked,uid=1001,gid=1001 \
    --mount=type=cache,id=pulsara-electron-builder-${TARGETARCH},target=/home/builder/.cache/electron-builder,sharing=locked,uid=1001,gid=1001 \
    --mount=type=cache,id=pulsara-native-${TARGETARCH},target=/workspace/desktop/resources,sharing=locked,uid=1001,gid=1001 \
    --mount=type=cache,id=pulsara-build-${TARGETARCH},target=/workspace/desktop/build,sharing=locked,uid=1001,gid=1001 \
    --mount=type=cache,id=pulsara-mise-${TARGETARCH},target=/home/builder/.local/state/mise,sharing=locked,uid=1001,gid=1001 \
    --mount=type=cache,id=pulsara-mise-cache-${TARGETARCH},target=/home/builder/.cache/mise,sharing=locked,uid=1001,gid=1001 \
    mise run --jobs 1 desktop:package

FROM scratch AS installer
COPY --from=build /workspace/desktop/release/*.deb /
