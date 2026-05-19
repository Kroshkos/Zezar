#!/bin/bash
set -e

PYTHON_PORT=${PYTHON_PORT:-35123}
SANDBOX_API_PORT=${SANDBOX_API_PORT:-4000}
PREVIEW_HOST_PORT=${PREVIEW_HOST_PORT:-4001}
SANDBOX_MODE=${SANDBOX_MODE:-host}
WORKSPACE_DIR=${WORKSPACE_DIR:-/workspace}

mkdir -p "$WORKSPACE_DIR"

cleanup() {
    echo "Shutting down services..."
    kill "$NODE_PID" 2>/dev/null || true
    kill "$PYTHON_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

# Start Sandbox Preview Service
export PORT=$SANDBOX_API_PORT
export PREVIEW_HOST_PORT SANDBOX_MODE WORKSPACE_DIR

cd /app/openhands/runtime/sandbox_preview
node server.js &
NODE_PID=$!

# Wait for sandbox service to be ready
for i in $(seq 1 10); do
    if curl -s "http://127.0.0.1:${SANDBOX_API_PORT}/health" > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Start Python Action Execution Server
cd /app
export SANDBOX_SERVICE_URL="http://127.0.0.1:${SANDBOX_API_PORT}"

sudo python openhands/action_execution_server.py \
    --no-enable-browser \
    --working-dir "$WORKSPACE_DIR" \
    --username "$(whoami)" \
    --user-id "$(id -u)" \
    "$PYTHON_PORT" &
PYTHON_PID=$!

wait -n
cleanup
