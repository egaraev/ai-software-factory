import asyncio
import os
import sys

import aiohttp
import requests
from dotenv import load_dotenv

load_dotenv()

from kubernetes import client, config

import jira_client

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm.ai-models.svc.cluster.local:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus.observe.svc.cluster.local:9090")
LOKI_URL = os.environ.get("LOKI_URL", "http://loki.observe.svc.cluster.local:3100")

SYSTEM_PROMPT = """You are an SRE diagnosing a Kubernetes alert on a small homelab
cluster. You're given the alert details, recent metrics, recent pod events, and
recent log lines. Explain what's actually wrong in plain terms, point at the most
likely root cause, and suggest the next troubleshooting or fix step a human should
take. You cannot run any commands yourself -- this is read-only diagnosis, not a
fix. Be concise -- a few short paragraphs, not an essay."""


# ticket_key is like "KAN-91" -- an ops ticket alert_webhook.py created. pulls
# metrics/events/logs for the namespace+pod named in it, asks fx-verify to
# diagnose, posts the result as a jira comment, and leaves the ticket at Needs
# Info for a human to actually act on -- this agent never changes the cluster
async def diagnose_ticket(ticket_key):
    jira_client.transition_to(ticket_key, "In Progress")
    ticket = jira_client.get_ticket(ticket_key)
    alert = _parse_alert(ticket["description"])

    if alert["namespace"] and alert["pod"]:
        metrics = _query_metrics(alert["namespace"], alert["pod"])
        events = _recent_events(alert["namespace"], alert["pod"])
        logs = _recent_logs(alert["namespace"], alert["pod"])
    else:
        metrics = events = logs = "no namespace/pod on this ticket -- alert didn't carry pod-level labels"

    diagnosis = await _chat(_build_prompt(ticket, alert, metrics, events, logs))
    jira_client.add_comment(ticket_key, f"**SRE agent diagnosis (fx-verify):**\n\n{diagnosis}")
    jira_client.transition_to(ticket_key, "Needs Info")
    print(f"posted diagnosis on {ticket_key}")


# ticket descriptions from alert_webhook.py are plain "Key: value" lines (see
# that file's _create_ticket) -- this just splits them back out
def _parse_alert(description):
    fields = {}
    for line in description.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip()
    return {
        "alertname": fields.get("alert", "unknown"),
        "severity": fields.get("severity", "unknown"),
        "namespace": fields.get("namespace", ""),
        "pod": fields.get("pod", ""),
        "summary": fields.get("summary", ""),
    }


def _build_prompt(ticket, alert, metrics, events, logs):
    return (
        f"Ticket {ticket['key']}: {ticket['summary']}\n\n"
        f"Alert: {alert['alertname']} (severity: {alert['severity']})\n"
        f"Alert summary: {alert['summary']}\n\n"
        f"Recent metrics:\n{metrics}\n\n"
        f"Recent k8s events:\n{events}\n\n"
        f"Recent logs:\n{logs}"
    )


# sends this to prometheus: GET /api/v1/query?query=kube_pod_container_status_restarts_total{namespace="ai-models",pod=~"litellm.*"}
# returns e.g. {"data": {"result": [{"metric": {"pod": "litellm-b75df8d75-qcfwq"}, "value": [1793473, "3"]}]}}
def _query_metrics(namespace, pod):
    queries = {
        "restarts (total)": f'kube_pod_container_status_restarts_total{{namespace="{namespace}",pod=~"{pod}.*"}}',
        "cpu usage (cores, 5m rate)": f'rate(container_cpu_usage_seconds_total{{namespace="{namespace}",pod=~"{pod}.*"}}[5m])',
        "memory usage (bytes)": f'container_memory_working_set_bytes{{namespace="{namespace}",pod=~"{pod}.*"}}',
    }
    lines = []
    for label, query in queries.items():
        try:
            r = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
            r.raise_for_status()
            results = r.json()["data"]["result"]
            values = ", ".join(f"{res['metric'].get('pod', '?')}={res['value'][1]}" for res in results) or "no data"
            lines.append(f"{label}: {values}")
        except requests.RequestException as e:  # prometheus unreachable or a bad query -- keep going, partial data still helps the diagnosis
            lines.append(f"{label}: query failed ({e})")
    return "\n".join(lines)


# sends this to loki: GET /loki/api/v1/query_range?query={namespace="ai-models",pod=~"litellm.*"}&limit=50
# returns e.g. {"data": {"result": [{"stream": {...}, "values": [["<ns_timestamp>", "log line"], ...]}]}}
def _recent_logs(namespace, pod):
    query = f'{{namespace="{namespace}",pod=~"{pod}.*"}}'
    try:
        r = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": query, "limit": 50, "direction": "backward"},
            timeout=10,
        )
        r.raise_for_status()
        lines = []
        for stream in r.json()["data"]["result"]:
            lines.extend(line for _, line in stream["values"])
        return "\n".join(lines[-50:]) or "no recent logs"
    except requests.RequestException as e:  # loki unreachable -- keep going, metrics/events alone still help
        return f"log query failed ({e})"


# reads k8s Events directly rather than through loki -- this is where
# OOMKilled/ImagePullBackOff/FailedScheduling reasons actually show up
def _recent_events(namespace, pod):
    config.load_incluster_config()
    v1 = client.CoreV1Api()
    events = v1.list_namespaced_event(namespace).items
    matching = sorted(
        (e for e in events if e.involved_object.name.startswith(pod)),
        key=lambda e: e.last_timestamp or e.event_time,
    )
    return "\n".join(f"{e.last_timestamp} {e.reason}: {e.message}" for e in matching[-20:]) or "no recent events"


# sends this to litellm's /v1/chat/completions:
#   {"model": "fx-verify", "messages": [{"role": "system", ...}, {"role": "user", ...}]}
# returns e.g. {"choices": [{"message": {"content": "The pod is OOMKilling because..."}}]}
async def _chat(user_prompt):
    url = f"{LITELLM_BASE}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {LITELLM_KEY}"}
    payload = {
        "model": "fx-verify",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
            data = await resp.json()
            return data["choices"][0]["message"]["content"]


if __name__ == "__main__":  # python sre_agent.py KAN-91 -- diagnoses one ops ticket and posts the result
    asyncio.run(diagnose_ticket(sys.argv[1]))
