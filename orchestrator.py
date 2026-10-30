import sys
import time

from dotenv import load_dotenv

load_dotenv()

from kubernetes import client, config
from kubernetes.client.rest import ApiException

import github_client
import jira_client

NAMESPACE = "pipeline"
IMAGE = "ghcr.io/egaraev/ai-software-factory:latest"
SECRET_NAME = "factory-pipeline-env"
PULL_SECRET_NAME = "ghcr-pull-secret"  # ai-software-factory's GHCR package is private

# looks at one epic's tasks in key order and acts on the first one that isn't
# Done yet -- To Do spawns its k8s job, In Review checks whether its PR got
# merged (and closes the ticket if so, then checks the next one too), anything
# else (In Progress/Needs Info) just waits. meant to be polled on a cron, so
# each run only needs to move the epic forward by whatever's ready right now
def run_orchestrator(epic_key):
    for task in jira_client.get_epic_tasks(epic_key):
        key, status = task["key"], task["status"]
        if status == "Done":
            continue
        if status == "To Do":
            _spawn_job(key)
            return
        if status == "In Review":
            pr = github_client.get_pr_for_branch(f"ticket-{key}")
            if pr and pr.get("merged_at"):
                jira_client.add_comment(key, "PR merged -- closing ticket.")
                jira_client.transition_to(key, "Done")
                continue
            return  # still waiting on the merge
        return  # In Progress (a job's already running) or Needs Info -- leave it
    print(f"{epic_key}: all tasks are Done")

def _spawn_job(key):
    config.load_incluster_config()
    batch = client.BatchV1Api()
    job_name = f"factory-ticket-{key.lower()}"
    # a ticket moved back to "To Do" (e.g. a manual retry) can outlive its old
    # job's own ttlSecondsAfterFinished cleanup window (1hr), and k8s refuses to
    # create a job with a name that's still in use even if that old one already
    # finished -- delete it first and wait for it to actually disappear
    _delete_job_if_exists(batch, job_name)
    try:
        batch.create_namespaced_job(namespace=NAMESPACE, body=_job_manifest(key))
        print(f"spawned job for {key}")
    except ApiException as e:
        if e.status == 409:  # two overlapping polls both tried to spawn this one
            print(f"job for {key} already exists, skipping")
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

def _job_manifest(key):
    container = {
        "name": "pipeline",
        "image": IMAGE,
        "command": ["python", "main.py", key],
        "envFrom": [{"secretRef": {"name": SECRET_NAME}}],
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": f"factory-ticket-{key.lower()}"},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "spec": {
                    "containers": [container],
                    "restartPolicy": "Never",
                    "imagePullSecrets": [{"name": PULL_SECRET_NAME}],
                }
            },
        },
    }

if __name__ == "__main__":  # python orchestrator.py KAN-10
    run_orchestrator(sys.argv[1])
