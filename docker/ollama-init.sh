#!/bin/sh
# Ollama service entrypoint for the demo (docker-compose.yml): start the server,
# pull the models Mochi needs, create the 8k-context variant, then keep serving.
# The healthcheck waits for qwen2.5:7b-8k to exist, so downstream services only
# start once the models are actually ready.
set -e

ollama serve &
SERVER_PID=$!

until ollama list >/dev/null 2>&1; do
  sleep 2
done

ollama pull qwen2.5:7b
ollama pull nomic-embed-text
ollama create qwen2.5:7b-8k -f /modelfiles/Modelfile.qwen2.5-7b-8k

wait "$SERVER_PID"
