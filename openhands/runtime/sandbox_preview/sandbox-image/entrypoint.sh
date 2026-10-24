#!/bin/sh
set -e

TEMPLATE="${TEMPLATE:-react}"
TEMPLATE_DIR="/app/templates/${TEMPLATE}"

if [ ! -d "$TEMPLATE_DIR" ]; then
  echo "[entrypoint] Unknown template: ${TEMPLATE}, falling back to react"
  TEMPLATE="react"
  TEMPLATE_DIR="/app/templates/react"
fi

# Clean and initialize workspace from template
rm -rf /workspace/*
cp "$TEMPLATE_DIR/package.json" /workspace/
cp "$TEMPLATE_DIR/vite.config.js" /workspace/
cp "$TEMPLATE_DIR/index.html" /workspace/
cp -r "$TEMPLATE_DIR/src" /workspace/src
ln -s "$TEMPLATE_DIR/node_modules" /workspace/node_modules

cd /workspace
exec npx vite --host 0.0.0.0 --port 3000
