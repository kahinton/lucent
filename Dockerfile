# Build stage
# Pinned with SHA256 digest for supply chain security (Gemini audit finding #7).
# To update: docker pull python:3.12-slim && docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
FROM python:3.12-slim@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4 AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ src/
# The daemon is a top-level package, outside src/. Without this the wheel has
# no daemon module and `python -m daemon.daemon` cannot run in this image.
COPY daemon/ daemon/

# Keep pip patched before building and installing project wheels.
RUN python -m pip install --no-cache-dir --upgrade pip

# Install the package
RUN pip install --no-cache-dir build && \
    python -m build --wheel && \
    pip install --no-cache-dir dist/*.whl

# Runtime stage — same pinned digest
FROM python:3.12-slim@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4

WORKDIR /app

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
# Copy the installed console scripts. Globbing `lucent*` rather than naming each
# one keeps a newly added entry point (lucent-migrate, lucent-bootstrap, …) from
# silently missing from the image — `lucent` and `lucent-daemon` are the same
# wheel's scripts, so they all belong together.
COPY --from=builder /usr/local/bin/lucent* /usr/local/bin/

# Built-in agent/skill/sandbox-template definitions. The server looks for these
# at /app/.github (builtin_definitions.py) and the daemon resolves its cognitive
# agent definition from the same place. Without them the image silently ships
# with no built-in skills — it only worked in dev because Compose bind-mounts
# the repo's .github into the container.
COPY .github/ /app/.github/

# Create non-root user
RUN groupadd --gid 1000 lucent && \
    useradd --uid 1000 --gid lucent --create-home --shell /bin/bash lucent && \
    # The local file-storage provider writes here (LUCENT_FILE_STORAGE_PATH).
    # /var/lib is root-owned, so uid 1000 cannot create it — without this the
    # documented local fallback fails with PermissionError on the first upload.
    mkdir -p /var/lib/lucent/files && \
    chown -R lucent:lucent /var/lib/lucent
USER lucent

# Default port
EXPOSE 8766

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8766/api/health')" || exit 1

# Run the server
CMD ["lucent"]
