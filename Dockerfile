FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/

# Persistent data lives here — mount a volume in prod
RUN mkdir -p /data/db /data/reports /data/stocks

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8765

CMD ["python", "-m", "app"]
