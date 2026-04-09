FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system --gid 10001 operator \
    && adduser --system --uid 10001 --ingroup operator operator \
    && pip install --no-cache-dir kopf kubernetes pyyaml

COPY src/operator.py /app/operator.py

USER 10001:10001

ENTRYPOINT ["kopf"]
CMD ["run", "--standalone=false", "--all-namespaces", "/app/operator.py"]
