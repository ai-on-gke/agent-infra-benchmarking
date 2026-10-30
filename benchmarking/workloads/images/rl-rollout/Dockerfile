FROM python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy==2.4.6

WORKDIR /app
COPY rl_actor.py /app/rl_actor.py
COPY train_rl.py /app/train_rl.py

EXPOSE 80
CMD ["python3", "-u", "/app/rl_actor.py"]
