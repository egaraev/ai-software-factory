import time

from dotenv import load_dotenv

load_dotenv()

from kubernetes import client, config
from kubernetes.client.rest import ApiException

import github_client
import jira_client
import sre_agent

NAMESPACE = "pipeline"
IMAGE = "ghcr.io/egaraev/ai-software-factory:latest"
SECRET_NAME = "factory-pipeline-env"
PULL_SECRET_NAME = "ghcr-pull-secret"  # ai-software-factory's GHCR package is private

# walks every epic in the project (see jira_client.get_epics) instead of one
# hardcoded epic key -- this is what makes "just create an epic in Jira" the
# whole workflow, no more editing cronjob.yaml per epic. also walks every
# standalone "ops" ticket (alert_webhook.py creates these from alertmanager
# firings) -- those live outside any epic, so they get their own loop here
def run_orchestrator():
    epics = jira_client.get_epics()
    for epic in epics:
        _process_epic(epic["key"], epic["assigned"])
    ops_tickets = jira_client.get_ops_tickets()
    for ticket in ops_tickets:
        _process_ops_ticket(ticket["key"], ticket["status"], ticket["assigned"])
    if not epics and not ops_tickets:
        print("nothing to do")

# looks at one epic's tasks in key order and acts on the first one that isn't
# Done yet -- To Do spawns its k8s job, In Review checks whether its PR got
# merged (and closes the ticket if so, then checks the next one too), anything
# else (In Progress/Needs Info) just waits. an epic with no tasks yet (brand
# new, just created in Jira) gets a planning job instead, which is what turns
# its title/description into real tasks the loop below can then walk.
# assigning a ticket or epic (to anyone) is how a human opts it into
# automation -- an unassigned "To Do" ticket, or an unassigned taskless epic,
# just sits there untouched until someone assigns it
def _process_epic(epic_key, assigned):
    tasks = jira_client.get_epic_tasks(epic_key)
    if not tasks:
        if not assigned:
            return  # new, taskless, unassigned -- waiting for a human to opt it in
        _spawn_planning_job(epic_key)
        return
    for task in tasks:
        key, status = task["key"], task["status"]
        if status == "Done":
            continue
        if status == "To Do":
            if not task["assigned"]:
                return  # waiting for a human to assign it before the pipeline touches it
            _spawn_job(key)
            return
        if status == "In Review":
            pr = github_client.get_pr_for_branch(f"ticket-{key}")
            if pr and pr.get("merged_at"):
                jira_client.add_comment(key, "PR merged -- closing ticket.")
                jira_client.transition_to(key, "Done")
                continue
            return  # still waiting on the merge (manual, or auto-merge not configured)
        if status == "In Progress" and _job_crashed_or_missing(key):
            jira_client.add_comment(key, "Its job crashed or disappeared while In Progress -- resetting to To Do for a retry.")
            jira_client.transition_to(key, "To Do")
            return
        return  # In Progress (a job's genuinely still running) or Needs Info -- leave it
    print(f"{epic_key}: all tasks are Done")

# same assignee-gate as a dev ticket's "To Do" handling. life cycle: To Do ->
# spawn the sre-agent job -> In Progress -> the agent posts a diagnosis and
# either leaves the ticket at Needs Info (no fix it was confident enough to
# propose) or opens a fix PR against factory-manifests and moves it to In
# Review. In Review is then the same merge-check as a dev ticket's (see
# _process_epic) -- a human still has to merge before ArgoCD applies anything.
# same stuck-job recovery as a dev ticket too, just checking the sre-agent's
# own job prefix and branch-naming convention (sre-<key>, not ticket-<key>)
def _process_ops_ticket(key, status, assigned):
    if status == "To Do":
        if not assigned:
            return  # waiting for a human to assign it to the sre-agent bot
        _spawn_sre_job(key)
        return
    if status == "In Progress" and _job_crashed_or_missing(key, prefix="factory-sre-"):
        jira_client.add_comment(key, "Its job crashed or disappeared while In Progress -- resetting to To Do for a retry.")
        jira_client.transition_to(key, "To Do")
        return
    if status == "In Review":
        pr = github_client.get_pr_for_branch(f"sre-{key.lower()}", repo=sre_agent.MANIFESTS_REPO)
        if pr and pr.get("merged_at"):
            jira_client.add_comment(key, "Fix PR merged -- closing ticket.")
            jira_client.transition_to(key, "Done")
        return  # still waiting on the merge (manual, or auto-merge not configured)
    return  # In Progress (genuinely running) or Needs Info (diagnosis already posted, no fix proposed) -- leave it

# an "In Progress" ticket with no live job behind it is stuck -- its job either
# crashed (backoffLimit:0 means one failure is final) or got cleaned up/never
# spawned at all, and nothing will ever move it forward on its own. resetting
# it to "To Do" routes it back through the normal To Do handling above, which
# already picks the right branch (fresh vs. rework) via graph.py's own check
# for an existing PR -- this only covers a job actually crashing or vanishing,
# not a ticket whose job succeeded but whose own Jira status update failed.
# prefix distinguishes a dev ticket's job name from an sre-agent ticket's
def _job_crashed_or_missing(key, prefix="factory-ticket-"):
    config.load_incluster_config()
    batch = client.BatchV1Api()
    job_name = f"{prefix}{key.lower()}"
    try:
        job = batch.read_namespaced_job(name=job_name, namespace=NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return True  # never spawned, or already cleaned up -- stuck either way
        raise
    return bool(job.status.failed)

# both ticket jobs and planning jobs share the same GPU (llama-swap pins it to
# one process at a time, see k3s-switch's own comments on this), so only one
# of either kind is ever allowed to run at once across every open epic.
# sre-agent jobs are deliberately NOT part of this guard -- fx-verify (the
# model sre_agent.py uses) routes through OpenRouter, not the local GPU, so
# a diagnosis job never contends with a dev/planning job for GPU time
def _any_active_pipeline_job():
    config.load_incluster_config()
    batch = client.BatchV1Api()
    jobs = batch.list_namespaced_job(namespace=NAMESPACE).items
    return any(
        job.metadata.name.startswith(("factory-ticket-", "factory-epic-"))
        and (job.status.active or 0) > 0
        for job in jobs
    )

def _spawn_job(key):
    if _any_active_pipeline_job():
        print(f"a pipeline job is already running -- not starting {key} yet (shared GPU)")
        return
    config.load_incluster_config()
    batch = client.BatchV1Api()
    job_name = f"factory-ticket-{key.lower()}"
    # a ticket moved back to "To Do" (e.g. a manual or auto retry) can outlive its
    # old job's own ttlSecondsAfterFinished cleanup window (1hr), and k8s refuses
    # to create a job with a name that's still in use even if that old one already
    # finished -- delete it first and wait for it to actually disappear
    _delete_job_if_exists(batch, job_name)
    try:
        batch.create_namespaced_job(namespace=NAMESPACE, body=_job_manifest(job_name, ["python", "main.py", key]))
        print(f"spawned job for {key}")
    except ApiException as e:
        if e.status == 409:  # two overlapping polls both tried to spawn this one
            print(f"job for {key} already exists, skipping")
        else:
            raise

# turns a brand-new epic's title/description into real tasks by running the
# architect agent (plan_and_create_tasks.py) as its own k8s job -- same
# shared-GPU guard as a ticket job, so planning never fights a ticket run
def _spawn_planning_job(epic_key):
    if _any_active_pipeline_job():
        print(f"a pipeline job is already running -- not planning {epic_key} yet (shared GPU)")
        return
    config.load_incluster_config()
    batch = client.BatchV1Api()
    job_name = f"factory-epic-{epic_key.lower()}"
    _delete_job_if_exists(batch, job_name)
    try:
        batch.create_namespaced_job(namespace=NAMESPACE, body=_job_manifest(job_name, ["python", "plan_and_create_tasks.py", epic_key]))
        print(f"spawned planning job for {epic_key}")
    except ApiException as e:
        if e.status == 409:
            print(f"planning job for {epic_key} already exists, skipping")
        else:
            raise

# runs sre_agent.py against one ops ticket -- no shared-GPU guard (see
# _any_active_pipeline_job's comment), and runs under the sre-agent
# ServiceAccount instead of the default one, since this job is the one that
# actually needs read access to the cluster (prometheus/loki/k8s events --
# dev/planning jobs never touch the k8s API at all, so they don't need it)
def _spawn_sre_job(key):
    config.load_incluster_config()
    batch = client.BatchV1Api()
    job_name = f"factory-sre-{key.lower()}"
    _delete_job_if_exists(batch, job_name)
    try:
        manifest = _job_manifest(job_name, ["python", "sre_agent.py", key], service_account="sre-agent")
        batch.create_namespaced_job(namespace=NAMESPACE, body=manifest)
        print(f"spawned sre-agent job for {key}")
    except ApiException as e:
        if e.status == 409:
            print(f"sre-agent job for {key} already exists, skipping")
        else:
            raise

def _delete_job_if_exists(batch, job_name):
    try:
        batch.delete_namespaced_job(name=job_name, namespace=NAMESPACE, propagation_policy="Background")
        print(f"deleted stale job {job_name} before respawning")
    except ApiException as e:
        if e.status != 404:  # 404 just means there was nothing to clean up
            raise
        return
    for _ in range(10):  # wait up to ~10s for the delete to actually finish
        try:
            batch.read_namespaced_job(name=job_name, namespace=NAMESPACE)
            time.sleep(1)
        except ApiException as e:
            if e.status == 404:
                return
            raise

# shared by every job type -- name is the k8s job name, command is the
# container's entrypoint. service_account is left unset (the namespace's
# default SA, which has no RBAC bound to it) for dev/planning jobs, since
# those never call the k8s API -- only sre-agent jobs pass one
def _job_manifest(name, command, service_account=None):
    container = {
        "name": "pipeline",
        "image": IMAGE,
        "command": command,
        "envFrom": [{"secretRef": {"name": SECRET_NAME}}],
    }
    pod_spec = {
        "containers": [container],
        "restartPolicy": "Never",
        "imagePullSecrets": [{"name": PULL_SECRET_NAME}],
    }
    if service_account:
        pod_spec["serviceAccountName"] = service_account
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 3600,
            "template": {"spec": pod_spec},
        },
    }

if __name__ == "__main__":  # python orchestrator.py -- walks every open epic and ops ticket, one step each
    run_orchestrator()
