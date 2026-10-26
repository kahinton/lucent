# Ops console image — the standalone local organization-administration tool.
#
# Kept separate from the server image on purpose: the console is a *client* of
# the hyperadmin API and shares no code path with the server at runtime. It needs
# FastAPI, Jinja2, httpx and uvicorn and nothing else — no database driver, no
# LLM SDKs, no Docker socket.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Only the console package and the shared static assets (logo + Tailwind build)
# are copied. Mounting Lucent's source here would be a footgun: it would let the
# console process import server internals and quietly bypass the API it exists to
# exercise.
COPY ops_console/ /app/ops_console/
COPY src/lucent/web/static/ /app/src/lucent/web/static/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        "fastapi>=0.141.1,<1.0" \
        "uvicorn>=0.52.3" \
        "jinja2>=3.1.6" \
        "httpx>=0.28.1" \
        "python-multipart>=0.0.32,<1.0"

# Never run as root: the console has no reason to write anything.
RUN useradd --create-home --uid 10001 ops \
    && chown -R ops:ops /app
USER ops

EXPOSE 8767

# Bind to all interfaces *inside the container* so the published port works; the
# compose file publishes it to 127.0.0.1 only, which is the actual boundary.
ENV LUCENT_OPS_HOST=0.0.0.0 \
    LUCENT_OPS_PORT=8767 \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "ops_console.app"]
