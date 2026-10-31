import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

load_dotenv()

import jira_client

PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "KAN")
PORT = int(os.environ.get("PORT", "8000"))


class AlertHandler(BaseHTTPRequestHandler):
    # alertmanager posts its whole batch of currently-firing/resolved alerts in
    # one request -- see k8s/observe/alertmanager.yaml's webhook_configs. only
    # firing alerts turn into tickets; a resolved one is just dropped, the human
    # investigating the open ticket is the one who closes it
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        for alert in payload.get("alerts", []):
            if alert.get("status") == "firing":
                _create_ticket(alert)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")


# alert is one entry from alertmanager's webhook payload, e.g.:
#   {"status": "firing", "labels": {"alertname": "PodCrashLooping", "namespace": "ai-models",
#    "pod": "litellm-b75df8d75-fpdxj", "severity": "warning"},
#    "annotations": {"summary": "litellm is crash-looping"}, "startsAt": "2026-11-01T09:00:00Z"}
# creates an unassigned "ops"-labeled jira task -- a human assigns it to the
# sre-agent bot when they want it investigated, same opt-in gate every other
# ticket already uses (see orchestrator.py's assignee checks). dedupes against
# an already-open ticket for the same alert so a still-firing alert doesn't
# spawn a new ticket every time alertmanager re-notifies
def _create_ticket(alert):
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    alertname = labels.get("alertname", "UnknownAlert")
    namespace = labels.get("namespace", "")
    pod = _workload_name(labels.get("pod", ""))
    summary_line = f"[ops] {alertname}: {namespace}/{pod}" if namespace else f"[ops] {alertname}"

    if jira_client.find_open_ticket_by_summary(summary_line):
        print(f"skipping duplicate ticket for {summary_line}")
        return

    description = (
        f"Alert: {alertname}\n"
        f"Severity: {labels.get('severity', 'unknown')}\n"
        f"Namespace: {namespace}\n"
        f"Pod: {pod}\n"
        f"Summary: {annotations.get('summary', '')}\n"
        f"Started: {alert.get('startsAt', '')}"
    )
    key = jira_client.create_task(PROJECT_KEY, summary_line, description)
    jira_client.add_label(key, "ops")
    print(f"created {key} for alert {alertname}")


# strips a pod's replicaset+pod hash suffixes so sre_agent.py's later
# prometheus/loki queries (which use this as a `pod=~"<name>.*"` regex) still
# match after the pod that triggered the alert gets replaced, e.g.
# "litellm-b75df8d75-fpdxj" -> "litellm"
def _workload_name(pod_name):
    return re.sub(r"(-[a-f0-9]{8,10})?-[a-z0-9]{5}$", "", pod_name) if pod_name else ""


if __name__ == "__main__":  # python alert_webhook.py -- listens for alertmanager's webhook posts
    print(f"listening on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), AlertHandler).serve_forever()
