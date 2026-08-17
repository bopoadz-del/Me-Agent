FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY common /app/common
COPY agent /app/agent
COPY VERSION /app/VERSION

# entrypoint.sh is 100644 in git (Windows checkouts do not carry the exec bit), so
# it must be made executable here or the container dies with "permission denied"
# before any Python runs. hive/Dockerfile already does this; this image did not.
RUN chmod +x /app/agent/entrypoint.sh \
    && useradd --create-home --uid 1000 agent \
    && mkdir -p /data /data/preload /data/evidence /data/logs \
    && chown -R agent:agent /data /app

ENV DATA_DIR=/data
ENV PYTHONPATH=/app
ENV PORT=8000
EXPOSE 8000

USER agent

ENTRYPOINT ["/app/agent/entrypoint.sh"]
CMD ["python3", "-m", "agent.bootstrapper"]
