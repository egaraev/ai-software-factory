#!/bin/bash
# stops aibox's current k3s (the Hermes platform) completely and disables
# it so it doesn't come back on a reboot while k3s-factory is the active
# cluster. stopping k3s takes every pod down with it -- litellm, llama-swap,
# comfyui, hermes-agent, honcho, the lot.
#
# run factory-on.sh after this. run hermes-on.sh later to switch back.
set -euo pipefail

SERVICE="k3s"

if ! systemctl list-unit-files "$SERVICE.service" &>/dev/null; then
    echo "no $SERVICE.service on this box - nothing to stop" >&2
    exit 1
fi

if ! systemctl is-active --quiet "$SERVICE"; then
    echo "$SERVICE is already stopped"
else
    echo "stopping $SERVICE (every Hermes pod goes down with it)..."
    sudo systemctl stop "$SERVICE"
fi

echo "disabling $SERVICE so it stays off across a reboot until hermes-on.sh re-enables it..."
sudo systemctl disable "$SERVICE"

echo "done - Hermes is off."
