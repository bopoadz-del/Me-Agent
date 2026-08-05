FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY common /app/common
COPY agent /app/agent
COPY VERSION /app/VERSION

RUN useradd --create-home --uid 1000 agent \
    && mkdir -p /data /data/preload /data/evidence /data/logs \
    && chown -R agent:agent /data /app

ENV DATA_DIR=/data
ENV PYTHONPATH=/app
ENV PORT=8000
EXPOSE 8000

USER agent

COPY agent/entrypoint.sh /app/agent/entrypoint.sh
ENTRYPOINT ["/app/agent/entrypoint.sh"]
CMD ["python3", "-m", "agent.bootstrapper"]
