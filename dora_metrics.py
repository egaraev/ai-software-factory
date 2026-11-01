import os
import re
import subprocess
import time
from datetime import datetime

import requests

import jira_client
import repo_ops

PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "http://pushgateway.observe.svc.cluster.local:9091")
MANIFESTS_REPO = "egaraev/factory-manifests"
MANIFESTS_WORKDIR = "/tmp/factory-manifests"

# how many of the most recent deploys/resolved incidents to average over for
# lead time / mttr -- a rolling window, not the full history, so one old
# slow patch doesn't permanently drag today's average down
SAMPLE_SIZE = 10


# every "deploy: factory-sample-app@<sha>" commit factory-manifests' own CI
# ever pushed (see factory-sample-app/.github/workflows/ci.yml's "Commit and
# push manifest update" step -- it only commits when the sed actually changed
# something, so each one here is a real deploy, not a no-op reconcile).
# "git log" output looks like:
#   "1793554167|deploy: factory-sample-app@c3f02a491ec9af34ce955e9ff1dd4b8babe224f5"
# returns [(manifests_commit_unix_ts, sample_app_sha), ...], newest first
def get_deploy_history(workdir):
    result = subprocess.run(
        ["git", "log", "--pretty=format:%ct|%s"],
        cwd=workdir, capture_output=True, text=True, check=True,
    )
    deploys = []
    for line in result.stdout.splitlines():
        ts_str, subject = line.split("|", 1)
        match = re.match(r"deploy: factory-sample-app@([0-9a-f]{40})", subject)
        if match:
            deploys.append((int(ts_str), match.group(1)))
    return deploys


# when a given commit sha was originally authored in factory-sample-app --
# used to compute lead time (deploy timestamp minus this). returns None if
# the sha isn't found there (shouldn't happen, but history can in principle change)
def get_commit_timestamp(workdir, sha):
    result = subprocess.run(
        ["git", "show", "-s", "--format=%ct", sha],
        cwd=workdir, capture_output=True, text=True,
    )
    return int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


# DORA "lead time for changes": averages (deploy_ts - commit_ts) across the
# most recent SAMPLE_SIZE deploys. None if there's nothing to average yet
def average_lead_time_seconds(deploys, sample_app_workdir):
    lead_times = []
    for deploy_ts, sha in deploys[:SAMPLE_SIZE]:
        commit_ts = get_commit_timestamp(sample_app_workdir, sha)
        if commit_ts is not None:
            lead_times.append(deploy_ts - commit_ts)
    return sum(lead_times) / len(lead_times) if lead_times else None


# jira's created/resolutiondate are ISO-8601 with a numeric offset, e.g.
# "2026-10-30T10:30:00.000+0000"
def _parse_jira_timestamp(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f%z")


# DORA "mean time to restore": averages (resolved - created) across the most
# recently resolved "ops" tickets (see alert_webhook.py). None if none are
# resolved yet
def average_mttr_seconds(ops_tickets):
    resolved = [t for t in ops_tickets if t["resolved"]]
    resolved.sort(key=lambda t: t["resolved"], reverse=True)
    deltas = []
    for t in resolved[:SAMPLE_SIZE]:
        created = _parse_jira_timestamp(t["created"])
        resolved_at = _parse_jira_timestamp(t["resolved"])
        deltas.append((resolved_at - created).total_seconds())
    return sum(deltas) / len(deltas) if deltas else None


# pushgateway takes plain prometheus text-exposition format in the POST body.
# deployments_total/incidents_total are pushed as raw CUMULATIVE counts (not
# a rate) so grafana can do increase()/division over whatever window the
# viewer picks, instead of one window being baked in here
def push_metrics(metrics):
    lines = [f"{name} {value}" for name, value in metrics.items() if value is not None]
    body = "\n".join(lines) + "\n"
    url = f"{PUSHGATEWAY_URL}/metrics/job/dora-metrics"
    r = requests.post(url, data=body, timeout=10)
    r.raise_for_status()


# computes the 4 DORA metrics and pushes them for grafana's dora dashboard --
# deployment frequency + lead time from git history (factory-manifests' own
# deploy commits, cross-referenced against factory-sample-app's commit
# timestamps), change failure rate + mttr from jira's "ops" tickets (created
# by alert_webhook.py off real alertmanager firings). change failure rate
# here is deploys-vs-incidents counted over the SAME window, not a strict
# "this specific deploy caused this specific incident" causal link -- a
# reasonable proxy at this scale, not a precise one. run on a schedule by
# k8s/orchestrator/dora-cronjob.yaml
def main():
    manifests_dir = repo_ops.clone_repo(repo=MANIFESTS_REPO, workdir=MANIFESTS_WORKDIR)
    sample_app_dir = repo_ops.clone_repo()

    deploys = get_deploy_history(manifests_dir)
    ops_tickets = jira_client.get_ops_ticket_history()

    metrics = {
        "dora_deployments_total": len(deploys),
        "dora_incidents_total": len(ops_tickets),
        "dora_lead_time_seconds": average_lead_time_seconds(deploys, sample_app_dir),
        "dora_mttr_seconds": average_mttr_seconds(ops_tickets),
        "dora_last_run_timestamp_seconds": time.time(),
    }
    push_metrics(metrics)
    print(f"pushed {metrics}")


if __name__ == "__main__":
    main()
