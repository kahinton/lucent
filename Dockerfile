# Build stage
# Pinned with SHA256 digest for supply chain security (Gemini audit finding #7).
# To update: docker pull python:3.12-slim && docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
FROM python:3.12-slim@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4 AS builder

WORKDIR /app

# Optional native tool handlers (src/lucent/services/builtin_tool_catalog.py).
# Both default to 0, so a build that sets neither flag produces exactly the
# same package set as before this gating existed. Set a flag to 1 to install
# the corresponding extra from pyproject.toml (`tools-documents`,
# `tools-images`) into the wheel built below.
ARG LUCENT_TOOLS_ENABLE_DOCUMENTS=0
ARG LUCENT_TOOLS_ENABLE_IMAGES=0

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

# Optional tool extras. The wheel is already built and installed above, so this
# only adds the third-party packages an enabled extra pulls in — it does not
# rebuild the project.
#
# Installed by *wheel path* rather than by the name ``lucent[<extra>]``: the
# wheel we just built is the authority on which extras exist, and naming it by
# path guarantees pip resolves the extras metadata from this build instead of
# asking an index about a ``lucent`` distribution. (Both forms work; this one
# fails loudly if the wheel is missing instead of silently falling back.)
# The glob is expanded into ``$WHL`` first because a bare
# ``dist/*.whl[tools-documents]`` would be parsed by the shell as a glob
# character class rather than a literal bracket suffix. `set -- dist/*.whl`
# keeps the unmatched pattern literal (dash does not nullglob), so the `-f`
# test below fails loudly instead of pip receiving an empty path.
RUN EXTRAS="" ; \
    if [ "$LUCENT_TOOLS_ENABLE_DOCUMENTS" = "1" ]; then EXTRAS="$EXTRAS,tools-documents" ; fi ; \
    if [ "$LUCENT_TOOLS_ENABLE_IMAGES" = "1" ]; then EXTRAS="$EXTRAS,tools-images" ; fi ; \
    if [ -n "$EXTRAS" ]; then \
        EXTRAS="${EXTRAS#,}" ; \
        set -- dist/*.whl ; \
        WHL="$1" ; \
        if [ ! -f "$WHL" ]; then \
            echo "ERROR: expected a built wheel at $WHL but none was found" >&2 ; \
            exit 1 ; \
        fi ; \
        echo "Installing lucent[${EXTRAS}] from ${WHL}" ; \
        pip install --no-cache-dir "${WHL}[${EXTRAS}]" ; \
    else \
        echo "No optional tool extras enabled" ; \
    fi

# Runtime stage — same pinned digest
FROM python:3.12-slim@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4

WORKDIR /app

# Re-declared because build args do not carry across stages. They are
# *informational* here: they label the image and tell a running container which
# extras this build requested. Actual availability is decided at runtime by
# importing the package (builtin_tool_catalog._package_available), so a wrong
# value here can mislead a reader but cannot make a tool work or fail.
ARG LUCENT_TOOLS_ENABLE_DOCUMENTS=0
ARG LUCENT_TOOLS_ENABLE_IMAGES=0
ENV LUCENT_TOOLS_ENABLE_DOCUMENTS=${LUCENT_TOOLS_ENABLE_DOCUMENTS} \
    LUCENT_TOOLS_ENABLE_IMAGES=${LUCENT_TOOLS_ENABLE_IMAGES}

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
