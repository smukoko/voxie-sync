FROM python:3.10-slim

WORKDIR /app

# Without this, print() sits in a buffer and `docker compose logs` shows nothing
# until the buffer flushes -- the gateway's entire progress output is prints.
ENV PYTHONUNBUFFERED=1

# Install basic dependencies (unzip is needed for the Sync Packs)
RUN apt-get update && apt-get install -y unzip && rm -rf /var/lib/apt/lists/*

# Gateway deps only. requirements.txt additionally carries the engine's ML
# stack, which this container never imports -- see requirements-gateway.txt.
COPY requirements-gateway.txt .
RUN pip install --no-cache-dir -r requirements-gateway.txt

COPY . .
RUN chmod +x start.sh

EXPOSE 3000

CMD ["./start.sh"]
