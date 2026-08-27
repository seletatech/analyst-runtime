ARG NANOBOT_BASE_IMAGE=ghcr.io/astral-sh/uv:python3.12-bookworm-slim
FROM ${NANOBOT_BASE_IMAGE}

ARG FIRECRAWL_CLI_VERSION=1.9.8
ARG NANOBOT_VERSION=dev
# Pin gws so the musl-binary fetch URL is deterministic. Bump this when upgrading gws.
ARG GWS_CLI_VERSION=0.22.5

# Install Node.js 20 for the WhatsApp bridge
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git ffmpeg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --batch --yes --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g "firecrawl-cli@${FIRECRAWL_CLI_VERSION}" && \
    npm cache clean --force && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Install gws CLI and agent-browser with bundled Chromium
# agent-browser install downloads its own Chromium — no external browser service needed.
# System libs required by Chromium on Debian bookworm-slim:
#
# gws note: we install with --ignore-scripts to skip the postinstall binary download,
# then fetch the musl variant manually. The gnu variant (default on glibc systems)
# requires GLIBC_2.39, which Debian Bookworm (GLIBC_2.36) does not provide. The musl
# binary is statically linked and runs on any Linux regardless of glibc version.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libasound2 libatk1.0-0 libatk-bridge2.0-0 libcairo2 libcups2 libdbus-1-3 \
        libdrm2 libgbm1 libglib2.0-0 libgtk-3-0 libnspr4 libnss3 \
        libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 \
        libxext6 libxfixes3 libxkbcommon0 libxrandr2 libxshmfence1 \
        fonts-liberation xdg-utils chromium && \
    npm install -g "@googleworkspace/cli@${GWS_CLI_VERSION}" --ignore-scripts && \
    GWS_PKG_DIR="$(npm root -g | tail -1)/@googleworkspace/cli" && \
    mkdir -p "${GWS_PKG_DIR}/bin" && \
    GWS_ARCH="$(uname -m | sed 's/aarch64/aarch64/;s/x86_64/x86_64/')" && \
    curl -fsSL "https://github.com/googleworkspace/cli/releases/download/v${GWS_CLI_VERSION}/google-workspace-cli-${GWS_ARCH}-unknown-linux-musl.tar.gz" | \
        tar -xz -C "${GWS_PKG_DIR}/bin" && \
    chmod +x "${GWS_PKG_DIR}/bin/gws" && \
    echo "${GWS_CLI_VERSION}" > "${GWS_PKG_DIR}/bin/.version" && \
    "${GWS_PKG_DIR}/bin/gws" --version && \
    npm install -g agent-browser && \
    (agent-browser install || true) && \
    npm cache clean --force && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY nanobot/pyproject.toml nanobot/README.md nanobot/LICENSE ./
RUN mkdir -p nanobot bridge && touch nanobot/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf nanobot bridge

# Copy the full source and install
COPY nanobot/nanobot/ nanobot/
COPY nanobot/bridge/ bridge/
COPY workspace/ /app/workspace/
RUN uv pip install --system --no-cache .

# Build the WhatsApp bridge
WORKDIR /app/bridge
RUN npm install && npm run build
WORKDIR /app

# Create config directory
RUN mkdir -p /root/.nanobot

# Workspace volume mount point for sandbox containers
RUN mkdir -p /workspace

ENV WORKSPACE_PATH=/workspace
ENV SANDBOX_ID=default
ENV NANOBOT_VERSION=$NANOBOT_VERSION
# Chromium --no-sandbox required when running as root in Docker/ECS containers
ENV AGENT_BROWSER_CHROMIUM_FLAGS="--no-sandbox --disable-setuid-sandbox"
# On ARM64 (Apple Silicon dev), Chrome for Testing is unavailable; use system chromium
ENV AGENT_BROWSER_EXECUTABLE_PATH="/usr/bin/chromium"
# gws (Google Workspace CLI) — store OAuth credentials on EFS so they survive container restarts
ENV GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/workspace/.nanobot/oauth/gws/credentials.json

ENTRYPOINT ["nanobot"]
CMD ["sandbox"]
