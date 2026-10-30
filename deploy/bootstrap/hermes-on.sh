#!/bin/bash
# brings Hermes's k3s back up and re-enables it as the boot default.
# refuses if k3s-factory is still running - the two share the same GPU(s)
# and llama-swap's GPU pinning assumes only one process holds them at a
# time (see k8s/p1/llama-swap-deployment.yaml's NVIDIA_VISIBLE_DEVICES
# comment). run factory-off.sh first.
set -euo pipefail

SERVICE="k3s"
FACTORY_SERVICE="k3s-factory"

if systemctl is-active --quiet "$FACTORY_SERVICE" 2>/dev/null; then
    echo "$FACTORY_SERVICE is still running - stop it first (factory-off.sh)" >&2
    exit 1
fi

echo "enabling $SERVICE for boot..."
sudo systemctl enable "$SERVICE"

echo "starting $SERVICE..."
sudo systemctl start "$SERVICE"

echo "done - Hermes is back and will start on next boot."
