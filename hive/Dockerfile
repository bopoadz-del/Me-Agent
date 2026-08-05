FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY common /app/common
COPY hive /app/hive
COPY scripts /app/scripts
COPY VERSION /app/VERSION

RUN chmod +x /app/scripts/hive_docker_entrypoint.sh \
    && useradd --create-home --uid 1000 agent \
    && mkdir -p /data \
    && chown -R agent:agent /data /app

ENV DATA_DIR=/data
ENV PYTHONPATH=/app
ENV PORT=10000
EXPOSE 10000

USER agent

CMD ["/app/scripts/hive_docker_entrypoint.sh"]
