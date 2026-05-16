FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/

# Create a non-root user to run the app.
# The /data directory is created here and ownership given to appuser so it
# can write to volume-mounted paths when those paths are bind-mounted from the host.
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /data/db /data/reports /data/stocks \
    && chown -R appuser:appuser /data /app

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8765

CMD ["python", "-m", "app"]
