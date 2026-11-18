FROM python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e
# turns.py is the recorder's entrypoint, which runs the server and sends an
# actor its turns.
COPY mock_llm.py recording.py turns.py /app/
# The recordings a benchmark case can replay, each <name>/cassette.jsonl.
COPY recordings/ /recordings/
CMD ["python3", "-u", "/app/mock_llm.py"]
