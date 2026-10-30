#!/bin/bash
# stops k3s-factory completely (litellm, llama-swap, everything in it goes
# down with the cluster). it's never enabled for boot, so this is just a
# stop - run hermes-on.sh to bring Hermes back.
set -euo pipefail

SERVICE="k3s-factory"

if ! systemctl list-unit-files "$SERVICE.service" &>/dev/null; then
    echo "$SERVICE isn't installed yet - nothing to stop" >&2
    exit 1
fi

if ! systemctl is-active --quiet "$SERVICE"; then
    echo "$SERVICE is already stopped"
else
    echo "stopping $SERVICE..."
    sudo systemctl stop "$SERVICE"
fi

echo "done - factory is off. run hermes-on.sh to bring Hermes back."
