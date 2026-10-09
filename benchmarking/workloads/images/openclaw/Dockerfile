FROM node:26.10.0-slim@sha256:ec7758ee051e457b468b32bde57b0879010b325bb9862718e9615225ce4aaae1

# Install the real openclaw package from npm
RUN npm install -g openclaw@2026.9.7 --allow-scripts=openclaw

# Install curl so we can make HTTP requests in the container if needed
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Install python to run our simple HTTP wrapper script
RUN apt-get update && apt-get install -y python3 && rm -rf /var/lib/apt/lists/*

COPY openclaw_actor.py /app/openclaw_actor.py

CMD ["python3", "-u", "/app/openclaw_actor.py"]
