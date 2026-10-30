#!/bin/bash
# starts k3s-factory, installing it the first time this runs, and brings
# up its own litellm + llama-swap inside it (same images/configs as
# Hermes's, reapplied unchanged from the aibox-platform repo -- separate
# cluster, same host, same GGUF files on disk, nothing duplicated there).
#
# refuses if Hermes's k3s is still up: the two are not meant to run at the
# same time (same physical GPU, same NVIDIA_VISIBLE_DEVICES=all trick that
# llama-swap relies on -- see k8s/p1/llama-swap-deployment.yaml). run
# hermes-off.sh first.
set -euo pipefail

HERMES_SERVICE="k3s"
SERVICE="k3s-factory"
DATA_DIR="/data/k3s-factory"
KUBECONFIG_PATH="/etc/rancher/k3s-factory/k3s.yaml"
REPO="$HOME/repos/aibox-platform"
export KUBECONFIG="$KUBECONFIG_PATH"

if systemctl is-active --quiet "$HERMES_SERVICE" 2>/dev/null; then
    echo "$HERMES_SERVICE (Hermes) is still running - stop it first (hermes-off.sh)" >&2
    exit 1
fi

if systemctl list-unit-files "$SERVICE.service" &>/dev/null; then
    echo "$SERVICE already installed, starting it..."
    sudo systemctl start "$SERVICE"
else
    echo "$SERVICE not installed yet - installing it now (one-time)..."

    # match the factory's k3s version to whatever Hermes runs, so the two
    # don't drift apart over time. falls back to the installer's own
    # latest-stable pick if k3s isn't found for some reason.
    HERMES_VERSION=$(k3s --version 2>/dev/null | head -1 | awk '{print $3}') || true

    sudo mkdir -p "$DATA_DIR" "$(dirname "$KUBECONFIG_PATH")"

    # official k3s installer: INSTALL_K3S_NAME names the systemd unit
    # "k3s-factory" instead of the default "k3s" so it can't collide with
    # Hermes's own unit. --data-dir and --write-kubeconfig keep every file
    # (containerd state, certs, kubeconfig) fully separate from Hermes too.
    curl -sfL https://get.k3s.io | \
        INSTALL_K3S_NAME=factory \
        ${HERMES_VERSION:+INSTALL_K3S_VERSION=$HERMES_VERSION} \
        sh -s - server \
        --data-dir "$DATA_DIR" \
        --write-kubeconfig "$KUBECONFIG_PATH" \
        --write-kubeconfig-mode 644

    # the installer enables+starts the service on its own - undo the
    # enable immediately so a reboot always comes back up as Hermes
    sudo systemctl disable "$SERVICE"
fi

echo "waiting for the node to come Ready..."
for i in $(seq 1 60); do
    kubectl get nodes 2>/dev/null | grep -q " Ready" && break
    sleep 2
done
kubectl get nodes

echo "applying the nvidia RuntimeClass (llama-swap needs it to see the GPU)..."
kubectl apply -f "$REPO/k8s/p0/nvidia-runtimeclass.yaml"

echo "creating namespace ai-models..."
kubectl create namespace ai-models --dry-run=client -o yaml | kubectl apply -f -

if kubectl get secret litellm-secret -n ai-models &>/dev/null; then
    echo "litellm-secret already exists, leaving it as-is"
else
    # a fresh key for THIS litellm instance -- not shared with Hermes's,
    # since this is its own separate copy, not the same running pod.
    LITELLM_KEY=$(openssl rand -hex 32)
    kubectl create secret generic litellm-secret \
        --namespace ai-models \
        --from-literal=master_key="$LITELLM_KEY"
    echo "generated a new litellm master key - save it somewhere, it won't be shown again:"
    echo "  $LITELLM_KEY"
fi

if kubectl get secret openrouter-secret -n ai-models &>/dev/null; then
    echo "openrouter-secret already exists, leaving it as-is"
else
    # same real OpenRouter key Hermes uses, read from the same .env file
    # on disk -- it's an external cloud credential, not cluster-specific,
    # so there's no reason to issue a second one.
    OPENROUTER_KEY=$(grep '^OPENROUTER_API_KEY=' "$REPO/.env" | head -1 | cut -d= -f2- | tr -d '"')
    kubectl create secret generic openrouter-secret \
        --namespace ai-models \
        --from-literal=OPENROUTER_API_KEY="$OPENROUTER_KEY"
fi

echo "applying llama-swap and litellm (unchanged from Hermes's own manifests)..."
kubectl apply -f "$REPO/k8s/p1/llama-swap-configmap.yaml"
kubectl apply -f "$REPO/k8s/p1/llama-swap-deployment.yaml"
kubectl apply -f "$REPO/k8s/p1/litellm.yaml"

echo
echo "done - k3s-factory is up with its own litellm + llama-swap."
echo "kubeconfig: $KUBECONFIG_PATH"
echo "check: KUBECONFIG=$KUBECONFIG_PATH kubectl get pods -n ai-models"
echo
echo "known no-ops in this copy (harmless, just won't do anything useful yet):"
echo "  - the media-gen busy-flag redis check (no ComfyUI/Redis in this cluster)"
echo "  - Langfuse tracing (LANGFUSE_HOST points at Hermes's instance, unreachable from here)"
echo "these only affect routing niceties and tracing, not chat completions themselves."
