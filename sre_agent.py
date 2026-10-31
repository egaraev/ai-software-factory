import asyncio
import json
import os
import sys

import aiohttp
import requests
from dotenv import load_dotenv

load_dotenv()

from kubernetes import client, config

import github_client
import jira_client
import repo_ops
import reviewer_agent

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm.ai-models.svc.cluster.local:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus.observe.svc.cluster.local:9090")
LOKI_URL = os.environ.get("LOKI_URL", "http://loki.observe.svc.cluster.local:3100")

# the git repo ArgoCD syncs the cluster's manifests from (see
# k8s/bootstrap/*-app.yaml) -- the agent reads this to ground its diagnosis in
# what's actually deployed, and pushes a fix branch here when it proposes one
MANIFESTS_REPO = os.environ.get("SRE_MANIFESTS_REPO", "egaraev/factory-manifests")
MANIFESTS_WORKDIR = os.environ.get("SRE_MANIFESTS_WORKDIR", "/tmp/factory-manifests")

# caps automatic rework retries before a fix PR falls back to a human -- same
# idea and same number as the dev pipeline's own cap (graph.py's
# MAX_AUTO_REWORK_ATTEMPTS), just triggered by fx-review's verdict instead of
# a formal github review
MAX_AUTO_REWORK_ATTEMPTS = 2

# caps how many rounds of tool calls fx-verify gets during investigation (see
# _chat_with_tools) before it's forced to answer with whatever it's found --
# stops a confused model from burning requests in an endless "let me check one
# more thing" loop
MAX_TOOL_ROUNDS = 6

SYSTEM_PROMPT = """You are an SRE diagnosing a Kubernetes alert on a small homelab
cluster. You're given the alert details, recent metrics, recent pod events, recent
log lines, and (when any were found) the actual manifest files for the workload,
pulled straight from the cluster's own GitOps repo -- treat that as your starting
point, not the final word.

You also have read-only tools to investigate further: get_pod_status, get_pod_logs
(set previous=true for a crashed container's last logs, often the only place the
real crash reason shows up), get_resource_yaml (the live deployed object, which can
differ from what's in git if something changed since the last ArgoCD sync),
get_endpoints, and arbitrary promql_query/logql_query. None of them can change
anything on the cluster. Use them like a real SRE would -- in particular, check
whether the problem is actually still happening right now before you diagnose it,
since by the time you're looking at a ticket the underlying issue may have already
cleared. You have a limited number of tool calls, so don't repeat a query you've
already run, and stop investigating once you have enough to answer.

First write a plain-English diagnosis: what's actually wrong, the most likely root
cause, and why -- point at the specific evidence (a log line, an event reason, a
metric value, something you found with a tool) rather than generic explanations. Be
concise, a few short paragraphs. If your tools show the problem isn't actually
happening anymore, say that plainly instead of inventing a cause to match the ticket.

Then, only when you are confident the fix is a specific, small change to one of the
manifest files you were shown, propose it -- after your diagnosis -- as one or more
file blocks in exactly this format:

===FILE: path/to/file===
<full new content of the file>
===END===

Make the smallest change that fixes the root cause. A human reviews and merges every
file block you propose before anything actually changes on the cluster, so it's fine
to propose one whenever you're confident -- but never guess at a file's content you
weren't shown, and don't propose a file block at all when: no manifest was found for
this workload, the right fix needs a human judgment call (more than one reasonable
option, a real tradeoff, missing information), or the problem looks transient rather
than config-caused. In those cases just explain that in your diagnosis and leave it
for a human -- don't force a fix that isn't there."""


# ticket_key is like "KAN-91" -- an ops ticket alert_webhook.py created. pulls
# metrics/events/logs for the namespace+pod named in it, plus any manifests for
# that workload from factory-manifests, asks fx-verify to diagnose and (when
# confident) propose a fix, posts the diagnosis as a jira comment. a proposed
# fix becomes a PR against factory-manifests -- the agent never touches the
# live cluster itself, a human still merges before ArgoCD applies anything.
#
# a rework pass needs BOTH an open (unmerged) fix PR AND actual REWORK:
# feedback on the ticket -- an open PR alone isn't enough (seen on KAN-65: a
# human re-triggering a ticket that already had an open PR, with no feedback
# attached, got treated as rework and shown its own old unmerged branch as if
# it were current reality, which the model then reasoned about as if it were
# already-deployed fact). with real feedback to act on, the existing branch is
# checked out (not reset from main) so the model sees its own prior attempt
# plus the feedback, and revises instead of starting over. without feedback,
# this is just a fresh run -- reset clean from main, same as a first attempt
async def diagnose_ticket(ticket_key):
    jira_client.transition_to(ticket_key, "In Progress")
    ticket = jira_client.get_ticket(ticket_key)
    alert = _parse_alert(ticket["description"])

    branch = f"sre-{ticket_key.lower()}"
    existing_pr = github_client.get_pr_for_branch(branch, repo=MANIFESTS_REPO)
    has_open_pr = existing_pr is not None and not existing_pr.get("merged_at")
    review_feedback = jira_client.get_rework_feedback(ticket_key) if has_open_pr else None
    rework = has_open_pr and review_feedback is not None

    repo_ops.clone_repo(repo=MANIFESTS_REPO, workdir=MANIFESTS_WORKDIR)
    if rework:
        repo_ops.checkout_branch(branch, workdir=MANIFESTS_WORKDIR)

    if alert["namespace"] and alert["pod"]:
        metrics = _query_metrics(alert["namespace"], alert["pod"])
        events = _recent_events(alert["namespace"], alert["pod"])
        logs = _recent_logs(alert["namespace"], alert["pod"])
        manifests = _relevant_manifests(alert["pod"])
    else:
        metrics = events = logs = "no namespace/pod on this ticket -- alert didn't carry pod-level labels"
        manifests = {}

    reply = await _chat_with_tools(_build_prompt(ticket, alert, metrics, events, logs, manifests, review_feedback))
    diagnosis, fix = _parse_diagnosis_and_fix(reply)
    jira_client.add_comment(ticket_key, f"**SRE agent diagnosis (fx-verify):**\n\n{diagnosis}")

    if fix:
        pr_url = _open_fix_pr(ticket_key, ticket, fix, branch, rework)
        if pr_url:
            jira_client.add_comment(ticket_key, f"Proposed fix: {pr_url}")
            jira_client.transition_to(ticket_key, "In Review")
            try:
                # a second opinion from a different model family (Sonnet, via
                # fx-review) than the one that wrote the fix (DeepSeek, via
                # fx-verify) -- posted straight to the PR as a comment, not to
                # Jira, same as the dev pipeline's reviewer. this NEVER merges,
                # approves, or requests changes itself -- only _handle_review_verdict
                # below reacts to the verdict, and even that only ever sends the
                # ticket back for another attempt or to a human, never merges.
                # a reviewer outage shouldn't block the PR from existing, same as
                # graph.py's own try/except around the dev pipeline's reviewer call
                approved, review_text = await reviewer_agent.review_fix_pr(
                    ticket_key, ticket["summary"], diagnosis, pr_url, repo=MANIFESTS_REPO
                )
                _handle_review_verdict(ticket_key, approved, review_text)
            except Exception as e:
                print(f"warning: reviewer agent failed for {ticket_key}: {e}")
            print(f"posted diagnosis + fix PR on {ticket_key}")
            return
        # fix generated but nothing actually differed once written to disk (e.g.
        # a prior run already pushed the identical content) -- fall through to
        # the normal Human Review ending, same as having no fix at all
        jira_client.add_comment(ticket_key, "Proposed fix matched what's already in factory-manifests -- nothing to open a PR for.")

    # no fix proposed (or nothing to PR) -- this needs a human to look at the
    # diagnosis and decide what to do, so it goes to the dedicated Human Review
    # column rather than Needs Info (which is for "ticket's missing info", a
    # different situation from "agent diagnosed it, a human has to judge it")
    jira_client.transition_to(ticket_key, "Human Review")
    print(f"posted diagnosis on {ticket_key}")


# APPROVE just leaves the ticket at In Review for a human to merge -- nothing
# to do here. CONCERNS sends it back to To Do with fx-review's own text as
# "REWORK: [auto] ..." feedback (same marker the dev pipeline uses, so
# jira_client.count_auto_rework_attempts counts these too) -- capped so a
# reviewer that keeps objecting doesn't loop forever; past the cap it falls
# back to Human Review for an actual human to look at instead
def _handle_review_verdict(ticket_key, approved, review_text):
    if approved:
        return
    attempts = jira_client.count_auto_rework_attempts(ticket_key)
    if attempts < MAX_AUTO_REWORK_ATTEMPTS:
        jira_client.add_comment(ticket_key, f"REWORK: [auto] {review_text}")
        jira_client.transition_to(ticket_key, "To Do")
    else:
        jira_client.add_comment(ticket_key, f"fx-review left CONCERNS after {attempts} automatic rework attempts -- needs a human look.")
        jira_client.transition_to(ticket_key, "Human Review")


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


# review_feedback is fx-review's critique of this ticket's previous fix attempt,
# when this is a rework pass (see diagnose_ticket) -- None on a first attempt.
# when it's set, the manifests above already reflect that previous attempt
# (diagnose_ticket checked out the existing branch before gathering them), so
# this just points the model at the feedback and asks it to revise, not restart
def _build_prompt(ticket, alert, metrics, events, logs, manifests, review_feedback=None):
    manifests_text = (
        "\n\n".join(f"--- {path} ---\n{content}" for path, content in manifests.items())
        or "(no manifest in factory-manifests mentions this workload -- it may have been deployed by hand and never added to git)"
    )
    review_text = (
        f"A reviewer (fx-review, a different model from you) REJECTED your previous fix "
        f"attempt for this same ticket -- this is not a style note, your diff did not pass. "
        f"The manifests shown below already reflect that attempt -- it is YOUR OWN proposed "
        f"fix, sitting on an unmerged PR. It is NOT yet on main, NOT yet applied to the "
        f"cluster, and not evidence the problem is already solved -- the pod is still "
        f"crash-looping right now with that PR unmerged, which is exactly why you're being "
        f"asked again.\n\n"
        f"Act on every concrete, actionable instruction in the feedback below INSIDE your new "
        f"file block(s) -- not just acknowledged in your diagnosis prose. Agreeing with a "
        f"specific suggestion (a flag to add, a resource to drop, a line to revert) in your "
        f"explanation and then submitting a file block that doesn't actually contain that "
        f"change is exactly the mistake that got this rejected last time. Only the file block "
        f"gets reviewed next, not your explanation of it -- if you say you're adding something, "
        f"it has to actually be in the file:\n\n"
        f"{review_feedback}\n\n" if review_feedback else ""
    )
    return (
        f"{review_text}Ticket {ticket['key']}: {ticket['summary']}\n\n"
        f"Alert: {alert['alertname']} (severity: {alert['severity']})\n"
        f"Alert summary: {alert['summary']}\n\n"
        f"Recent metrics:\n{metrics}\n\n"
        f"Recent k8s events:\n{events}\n\n"
        f"Recent logs:\n{logs}\n\n"
        f"Manifests for this workload (from factory-manifests):\n{manifests_text}"
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


# returns path -> content for every file in the currently checked-out
# factory-manifests working copy that mentions this workload by name -- a plain
# substring grep, but it's what lets the model ground its diagnosis and fix in
# actual config instead of guessing from general knowledge of the image. the
# caller (diagnose_ticket) has already cloned the repo and checked out the
# right ref -- main for a fresh diagnosis, this ticket's own branch for a
# rework pass -- so this sees either the deployed original or the agent's own
# prior attempt, whichever applies. empty if nothing in the repo mentions the
# workload (deployed by hand, never added to git -- the diagnosis prompt tells
# the model to say so rather than invent a fix in that case)
def _relevant_manifests(pod):
    if not pod:
        return {}
    all_files = repo_ops.read_repo_files(MANIFESTS_WORKDIR)
    return {path: content for path, content in all_files.items() if pod in content}


# splits the model's reply into the plain-English diagnosis (everything before
# the first ===FILE:=== marker) and a path -> new content changeset for
# whatever fix it proposed (same block format developer_agent.py's
# _parse_file_blocks uses, so it's one convention across the whole factory).
# line-by-line, not regex, for the same reason as that function: a regex
# spanning ===FILE:===...===END=== gets confused by an intentionally empty file
def _parse_diagnosis_and_fix(reply):
    diagnosis_lines = []
    changes = {}
    path = None
    file_lines = []
    for line in reply.splitlines():
        if line.startswith("===FILE: ") and line.endswith("==="):
            path = line[len("===FILE: "):-len("===")].strip()
            file_lines = []
        elif line.strip() == "===END===":
            if path:
                changes[path] = "\n".join(file_lines)
                path = None
        elif path is not None:
            file_lines.append(line)
        else:
            diagnosis_lines.append(line)
    return "\n".join(diagnosis_lines).strip(), changes


# writes a proposed fix to a branch on factory-manifests and opens a PR for it
# (or updates the existing one, on a rework pass -- see diagnose_ticket, which
# has already cloned the repo and, for rework, checked out this branch).
# returns the PR url, or None if there was nothing to actually commit (the
# model's proposed content turned out identical to what's already there)
def _open_fix_pr(ticket_key, ticket, fix, branch, rework):
    if not rework:
        repo_ops.create_branch(branch, workdir=MANIFESTS_WORKDIR)
    for path, content in fix.items():
        full_path = os.path.join(MANIFESTS_WORKDIR, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)
    pushed = repo_ops.commit_and_push(branch, f"{ticket_key}: {ticket['summary']}", workdir=MANIFESTS_WORKDIR)
    if not pushed:
        return None
    title = f"{ticket_key}: {ticket['summary']}"
    body = f"Automated fix proposed by the SRE agent for {ticket_key}.\n\nReview the diff before merging -- ArgoCD applies it to the cluster as soon as it lands on main."
    return github_client.open_pr(branch, title, body, repo=MANIFESTS_REPO)


# ---- live investigation tools ----------------------------------------------
# exposed to fx-verify as tool calls during diagnosis (see _chat_with_tools).
# all read-only, matching what's actually granted to the sre-agent
# ServiceAccount (k8s/sre-agent/rbac.yaml in factory-manifests): pods,
# pods/log, events, services, configmaps, endpoints, deployments/replicasets/
# daemonsets, in the 4 namespaces this factory owns -- nothing here can
# create, patch, or delete anything on the cluster

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_pod_status",
            "description": "Live status of a pod right now -- phase, conditions (including Ready), and per-container state/restart count. Use this to check whether the problem the ticket describes is still actually happening before diagnosing it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string", "description": "pod name or name prefix -- matches any pod whose name starts with this"},
                },
                "required": ["namespace", "pod"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pod_logs",
            "description": "Container logs read directly from the pod via the Kubernetes API, not Loki. Set previous=true to read the crashed container's logs from before its last restart -- often the only place the real crash reason shows up.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string"},
                    "previous": {"type": "boolean", "description": "true reads the previous (crashed) container instance instead of the current one"},
                    "tail": {"type": "integer", "description": "number of lines from the end, default 100"},
                },
                "required": ["namespace", "pod"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_yaml",
            "description": "The live, as-deployed spec of a Kubernetes object. Use this to see actual current config rather than only what's in git -- they can differ if something changed since the last ArgoCD sync, and it's the only reliable way to confirm what a ClusterRole/Role actually grants right now rather than inferring it from forbidden-watch log noise. Namespaced kinds: deployment, replicaset, daemonset, statefulset, service, configmap, serviceaccount, role, rolebinding. Cluster-scoped kinds (omit namespace): clusterrole, clusterrolebinding, node, persistentvolume, storageclass.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "name": {"type": "string"},
                    "namespace": {"type": "string", "description": "required for namespaced kinds, omit for cluster-scoped kinds"},
                },
                "required": ["kind", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_endpoints",
            "description": "Which pod IPs a Service currently considers ready vs not-ready. Directly useful for a PodNotReady alert -- shows whether the pod is actually missing from the Service's endpoint list right now.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "service": {"type": "string"},
                },
                "required": ["namespace", "service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "promql_query",
            "description": "Run an arbitrary instant PromQL query against Prometheus -- for metrics beyond the restarts/cpu/memory you were already shown, e.g. a different pod, or a specific alert's current value.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "logql_query",
            "description": "Run an arbitrary LogQL query against Loki -- for logs beyond the recent window you were already shown, e.g. a different pod, a longer range, or filtering for a specific string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "description": "max lines, default 50"},
                },
                "required": ["query"],
            },
        },
    },
]


def _tool_get_pod_status(namespace, pod):
    config.load_incluster_config()
    v1 = client.CoreV1Api()
    matches = [p for p in v1.list_namespaced_pod(namespace).items if p.metadata.name.startswith(pod)]
    if not matches:
        return f"no pod starting with '{pod}' found in namespace '{namespace}'"
    lines = []
    for p in matches:
        conditions = ", ".join(f"{c.type}={c.status}" for c in (p.status.conditions or []))
        containers = []
        for cs in (p.status.container_statuses or []):
            if cs.state.running:
                state = "running"
            elif cs.state.waiting:
                state = f"waiting ({cs.state.waiting.reason})"
            else:
                state = f"terminated ({cs.state.terminated.reason})"
            containers.append(f"{cs.name}: ready={cs.ready} restarts={cs.restart_count} {state}")
        lines.append(f"{p.metadata.name}: phase={p.status.phase}\nconditions: {conditions}\ncontainers: {'; '.join(containers)}")
    return "\n\n".join(lines)


def _tool_get_pod_logs(namespace, pod, previous=False, tail=100):
    config.load_incluster_config()
    v1 = client.CoreV1Api()
    matches = [p for p in v1.list_namespaced_pod(namespace).items if p.metadata.name.startswith(pod)]
    if not matches:
        return f"no pod starting with '{pod}' found in namespace '{namespace}'"
    pod_name = matches[0].metadata.name
    try:
        return v1.read_namespaced_pod_log(pod_name, namespace, previous=previous, tail_lines=tail)
    except client.exceptions.ApiException as e:  # e.g. no previous instance exists yet
        return f"log read failed: {e.reason}"


# value is (needs_namespace, reader) -- reader always takes (api, name, ns),
# cluster-scoped ones just ignore ns. api is {"apps","core","rbac","storage"},
# built once per call below rather than per-entry since most calls only need
# one of the four clients
RESOURCE_READERS = {
    "deployment": (True, lambda api, name, ns: api["apps"].read_namespaced_deployment(name, ns)),
    "replicaset": (True, lambda api, name, ns: api["apps"].read_namespaced_replica_set(name, ns)),
    "daemonset": (True, lambda api, name, ns: api["apps"].read_namespaced_daemon_set(name, ns)),
    "statefulset": (True, lambda api, name, ns: api["apps"].read_namespaced_stateful_set(name, ns)),
    "service": (True, lambda api, name, ns: api["core"].read_namespaced_service(name, ns)),
    "configmap": (True, lambda api, name, ns: api["core"].read_namespaced_config_map(name, ns)),
    "serviceaccount": (True, lambda api, name, ns: api["core"].read_namespaced_service_account(name, ns)),
    "role": (True, lambda api, name, ns: api["rbac"].read_namespaced_role(name, ns)),
    "rolebinding": (True, lambda api, name, ns: api["rbac"].read_namespaced_role_binding(name, ns)),
    "clusterrole": (False, lambda api, name, ns: api["rbac"].read_cluster_role(name)),
    "clusterrolebinding": (False, lambda api, name, ns: api["rbac"].read_cluster_role_binding(name)),
    "node": (False, lambda api, name, ns: api["core"].read_node(name)),
    "persistentvolume": (False, lambda api, name, ns: api["core"].read_persistent_volume(name)),
    "storageclass": (False, lambda api, name, ns: api["storage"].read_storage_class(name)),
}


def _tool_get_resource_yaml(kind, name, namespace=None):
    config.load_incluster_config()
    entry = RESOURCE_READERS.get(kind.lower())
    if not entry:
        return f"unsupported kind '{kind}' -- supported: {', '.join(RESOURCE_READERS)}"
    needs_namespace, reader = entry
    if needs_namespace and not namespace:
        return f"'{kind}' is namespaced -- pass a namespace"
    api = {"apps": client.AppsV1Api(), "core": client.CoreV1Api(), "rbac": client.RbacAuthorizationV1Api(), "storage": client.StorageV1Api()}
    try:
        obj = reader(api, name, namespace)
    except client.exceptions.ApiException as e:
        return f"read failed: {e.reason}"
    return json.dumps(client.ApiClient().sanitize_for_serialization(obj), indent=2, default=str)


def _tool_get_endpoints(namespace, service):
    config.load_incluster_config()
    v1 = client.CoreV1Api()
    try:
        ep = v1.read_namespaced_endpoints(service, namespace)
    except client.exceptions.ApiException as e:
        return f"read failed: {e.reason}"
    if not ep.subsets:
        return f"service '{service}' has no endpoint subsets -- no pods currently match+ready"
    lines = []
    for s in ep.subsets:
        ready = [a.ip for a in (s.addresses or [])]
        not_ready = [a.ip for a in (s.not_ready_addresses or [])]
        lines.append(f"ready: {ready or 'none'}, not ready: {not_ready or 'none'}")
    return "\n".join(lines)


# same prometheus instant-query endpoint as _query_metrics above, but with the
# query coming straight from the model instead of one of the 3 fixed ones
def _tool_promql_query(query):
    try:
        r = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
        r.raise_for_status()
        results = r.json()["data"]["result"]
        return "\n".join(f"{res['metric']}={res['value'][1]}" for res in results) or "no data"
    except requests.RequestException as e:
        return f"query failed: {e}"


def _tool_logql_query(query, limit=50):
    try:
        r = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": query, "limit": limit, "direction": "backward"},
            timeout=10,
        )
        r.raise_for_status()
        lines = []
        for stream in r.json()["data"]["result"]:
            lines.extend(line for _, line in stream["values"])
        return "\n".join(lines[-limit:]) or "no results"
    except requests.RequestException as e:
        return f"query failed: {e}"


TOOL_DISPATCH = {
    "get_pod_status": _tool_get_pod_status,
    "get_pod_logs": _tool_get_pod_logs,
    "get_resource_yaml": _tool_get_resource_yaml,
    "get_endpoints": _tool_get_endpoints,
    "promql_query": _tool_promql_query,
    "logql_query": _tool_logql_query,
}


# runs the tool-calling investigation loop, then returns fx-verify's final
# diagnosis text once it stops asking for tools (or hits MAX_TOOL_ROUNDS).
# messages grows each round: the model's own tool_calls, then one "tool" role
# message per call carrying that call's result -- standard OpenAI-style
# tool-calling transcript shape, which litellm passes through to fx-verify's
# underlying model (deepseek-v4-pro-0813 via openrouter)
async def _chat_with_tools(user_prompt):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    for _ in range(MAX_TOOL_ROUNDS):
        message = await _chat_once(messages)
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return message.get("content") or ""
        messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": tool_calls})
        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
                result = TOOL_DISPATCH[name](**args)
            except Exception as e:  # bad args, unknown tool, or the tool itself raised -- tell the model, don't crash the run
                result = f"tool call failed: {e}"
            print(f"fx-verify tool call: {name}({call['function']['arguments']})")
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)[:4000]})
    # used up all its investigation turns -- one more call with no tools
    # offered, forcing a final answer from whatever it's gathered so far
    messages.append({"role": "user", "content": "You're out of investigation turns -- give your diagnosis now based on everything you've found so far."})
    message = await _chat_once(messages, offer_tools=False)
    return message.get("content") or ""


# sends this to litellm's /v1/chat/completions:
#   {"model": "fx-verify", "messages": [...], "tools": [...]}
# returns e.g. {"role": "assistant", "content": "...", "tool_calls": [{"id":
#   "call_1", "function": {"name": "get_pod_status", "arguments":
#   "{\"namespace\": \"observe\", \"pod\": \"kube-state-metrics\"}"}}]}
# (tool_calls is absent once the model is done investigating and just answers)
#
# retries a slow/flaky call rather than letting one timeout crash the whole
# run -- by the later rounds of a tool-calling investigation this request
# carries a lot of accumulated context, and openrouter/fx-verify occasionally
# just takes a while. caught here, this keeps the in-progress tool-call
# history intact (see _chat_with_tools) instead of losing it to a crashed
# job that then has to start the whole investigation over from scratch (see
# orchestrator.py's own crash recovery, which is the second line of defense
# after this one)
CHAT_RETRY_DELAYS = [5, 15, 30]  # seconds, one retry per entry


async def _chat_once(messages, offer_tools=True):
    url = f"{LITELLM_BASE}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {LITELLM_KEY}"}
    payload = {"model": "fx-verify", "messages": messages}
    if offer_tools:
        payload["tools"] = TOOLS_SCHEMA
    timeout = aiohttp.ClientTimeout(total=180)
    for attempt in range(len(CHAT_RETRY_DELAYS) + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                    data = await resp.json()
                    return data["choices"][0]["message"]
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:  # openrouter/litellm being slow or flaky -- retry rather than crash the whole run
            if attempt == len(CHAT_RETRY_DELAYS):
                raise
            delay = CHAT_RETRY_DELAYS[attempt]
            print(f"fx-verify request failed ({e}), retrying in {delay}s...")
            await asyncio.sleep(delay)


if __name__ == "__main__":  # python sre_agent.py KAN-91 -- diagnoses one ops ticket, opens a fix PR if confident, and posts the result
    asyncio.run(diagnose_ticket(sys.argv[1]))
