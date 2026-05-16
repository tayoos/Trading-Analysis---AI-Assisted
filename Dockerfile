FROM python:3.12-slim

WORKDIR /app

# Install Node.js (LTS) and the Claude Code CLI
# The CLI provides the claude binary that claude-agent-sdk calls under the hood.
# Auth credentials are mounted from the host at /home/appuser/.claude (read-only).
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @anthropic-ai/claude-code && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/
COPY entrypoint.sh /entrypoint.sh

# Non-root user with a real home directory so ~/.claude resolves correctly.
RUN groupadd -r appuser && useradd -r -g appuser -m -d /home/appuser appuser \
    && mkdir -p /data /home/appuser/.claude \
    && chown -R appuser:appuser /data /app /home/appuser \
    && chmod +x /entrypoint.sh

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/appuser

EXPOSE 8765

ENTRYPOINT ["/entrypoint.sh"]
