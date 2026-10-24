#!/bin/bash
# Start both Python Action Execution Server and Node.js Sandbox Preview Service

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
SANDBOX_DIR="$PROJECT_ROOT/openhands/runtime/sandbox_preview"

# Default ports
PYTHON_PORT=${PYTHON_PORT:-35123}
SANDBOX_API_PORT=${SANDBOX_API_PORT:-4000}
PREVIEW_PORT=${PREVIEW_PORT:-4001}

# Environment
LOCAL_RUNTIME_MODE=${LOCAL_RUNTIME_MODE:-1}
WORKSPACE_DIR=${WORKSPACE_DIR:-"$PROJECT_ROOT/workspace"}

# Kill processes occupying required ports
kill_port() {
    local port=$1
    local pids=$(lsof -ti :$port 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "Port $port is in use (PID: $pids), killing..."
        echo "$pids" | xargs kill -9 2>/dev/null || true
        sleep 1
    fi
}

kill_port $SANDBOX_API_PORT
kill_port $PREVIEW_PORT
kill_port $PYTHON_PORT

echo "======================================"
echo "Starting Qwen Chat Coder Services"
echo "======================================"
echo "Python API Port:     $PYTHON_PORT"
echo "Sandbox API Port:    $SANDBOX_API_PORT"
echo "Preview Port:        $PREVIEW_PORT (Vite dev server)"
echo "Workspace:           $WORKSPACE_DIR"
echo "======================================"

# Function to cleanup background processes on exit
cleanup() {
    echo ""
    echo "Shutting down services..."
    if [ ! -z "$NODE_PID" ]; then
        kill $NODE_PID 2>/dev/null || true
        echo "Node.js service stopped"
    fi
    if [ ! -z "$PYTHON_PID" ]; then
        kill $PYTHON_PID 2>/dev/null || true
        echo "Python service stopped"
    fi
    exit 0
}

trap cleanup SIGINT SIGTERM

# Start Node.js Sandbox Preview Service
echo ""
echo "[1/2] Starting Sandbox Preview Service (Node.js)..."
cd "$SANDBOX_DIR"

# Check if node_modules exists
if [ ! -d "node_modules" ]; then
    echo "Installing Node.js dependencies..."
    npm install
fi

export PORT=$SANDBOX_API_PORT
export PREVIEW_HOST_PORT=$PREVIEW_PORT
export SANDBOX_MODE=host
export WORKSPACE_DIR="$WORKSPACE_DIR"

node server.js &
NODE_PID=$!
echo "Sandbox Preview Service started (PID: $NODE_PID)"

# Wait for Node.js service to be ready
echo "Waiting for sandbox service to be ready..."
for i in {1..10}; do
    if curl -s http://localhost:$SANDBOX_API_PORT/health > /dev/null 2>&1; then
        echo "Sandbox service ready!"
        break
    fi
    sleep 1
done

# Start Python Action Execution Server
echo ""
echo "[2/2] Starting Action Execution Server (Python)..."
cd "$PROJECT_ROOT"

export LOCAL_RUNTIME_MODE=$LOCAL_RUNTIME_MODE
export SANDBOX_SERVICE_URL="http://localhost:$SANDBOX_API_PORT"
export PREVIEW_HOST_PORT=$PREVIEW_PORT

# Get current user info
CURRENT_USER=$(whoami)
CURRENT_UID=$(id -u)

python openhands/action_execution_server.py \
    --no-enable-browser \
    --working-dir "$WORKSPACE_DIR" \
    --username "$CURRENT_USER" \
    --user-id "$CURRENT_UID" \
    $PYTHON_PORT &
PYTHON_PID=$!
echo "Action Execution Server started (PID: $PYTHON_PID)"

# Wait for Python service to be ready
echo "Waiting for Python service to be ready..."
for i in {1..10}; do
    if curl -s http://localhost:$PYTHON_PORT/alive > /dev/null 2>&1; then
        echo "Python service ready!"
        break
    fi
    sleep 1
done

echo ""
echo "======================================"
echo "All services started successfully!"
echo "======================================"
echo ""
echo "Endpoints:"
echo "  Python API:    http://localhost:$PYTHON_PORT"
echo "  Sandbox Proxy: http://localhost:$PYTHON_PORT/sandbox/*"
echo "  Sandbox API:   http://localhost:$SANDBOX_API_PORT"
echo "  Preview URL:   http://localhost:$PREVIEW_PORT"
echo ""
echo "Press Ctrl+C to stop all services"
echo ""

# Wait for both processes
wait