FROM nousresearch/hermes-agent:v0.23.1@sha256:276d44685c65c0cb2a25f190b63c65c8906f9dc76486fb1c05ae71b5826bda08

COPY hermes_actor.py /app/hermes_actor.py

# The shim starts the image's own entrypoint for the gateway, as a child.
ENTRYPOINT []
CMD ["python3", "-u", "/app/hermes_actor.py"]
