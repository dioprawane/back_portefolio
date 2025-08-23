FROM python:3.11-slim

# système (optionnel mais utile)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render fournit $PORT. On ne l’expose pas explicitement, mais c’est ok.
CMD ["sh", "-c", "uvicorn github_analytics:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
