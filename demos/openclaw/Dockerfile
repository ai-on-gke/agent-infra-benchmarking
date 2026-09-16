FROM node:26-slim

# Install the real openclaw package from npm
RUN npm install -g openclaw@latest --allow-scripts=openclaw

# Install curl so we can make HTTP requests in the container if needed
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Install python to run our simple HTTP wrapper script
RUN apt-get update && apt-get install -y python3 && rm -rf /var/lib/apt/lists/*

COPY openclaw_actor.py /app/openclaw_actor.py

CMD ["python3", "-u", "/app/openclaw_actor.py"]
