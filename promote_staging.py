import os
import re
import requests

import repo_ops

# same clone_repo/commit_and_push target repo as sre_agent.py's manifest
# fixes, but its own workdir -- each CronJob run gets a fresh pod anyway so
# this can't actually collide with sre_agent's /tmp/factory-manifests, but
# keeping separate names avoids any confusion if that ever changes
MANIFESTS_REPO = os.environ.get("PROMOTE_MANIFESTS_REPO", "egaraev/factory-manifests")
MANIFESTS_WORKDIR = os.environ.get("PROMOTE_MANIFESTS_WORKDIR", "/tmp/factory-manifests-promote")

STAGING_DEPLOYMENT = "k8s/sample-app-staging/deployment.yaml"
PRODUCTION_DEPLOYMENT = "k8s/sample-app/deployment.yaml"

# in-cluster DNS name for the staging Service (ClusterIP, sample-app-staging
# namespace) -- this CronJob runs in-cluster itself, so it can hit this
# directly instead of needing anything exposed externally
STAGING_HEALTH_URL = "http://factory-sample-app-staging.sample-app-staging.svc.cluster.local:8000/hello"

IMAGE_RE = re.compile(r"image:\s*ghcr\.io/egaraev/factory-sample-app:([0-9a-f]+)")


# returns the git sha pinned in a deployment.yaml's image line, or None if
# the line isn't there in the shape expected
def read_image_sha(workdir, deployment_path):
    full_path = os.path.join(workdir, deployment_path)
    with open(full_path) as f:
        content = f.read()
    match = IMAGE_RE.search(content)
    if not match:
        return None
    return match.group(1)


# staging has nothing external pointing at it, so this is the only check that
# a candidate build actually boots before it reaches production. short
# timeout -- if staging isn't answering, that's a real "not healthy" result,
# not something worth waiting around for; the next cron tick just tries again
def staging_is_healthy():
    try:
        response = requests.get(STAGING_HEALTH_URL, timeout=5)
        return response.status_code == 200
    except requests.RequestException as e:  # staging unreachable/not ready yet -- treat as unhealthy
        print(f"staging health check failed: {e}")
        return False


def promote_sha_to_production(workdir, sha):
    full_path = os.path.join(workdir, PRODUCTION_DEPLOYMENT)
    with open(full_path) as f:
        content = f.read()
    new_content = IMAGE_RE.sub(f"image: ghcr.io/egaraev/factory-sample-app:{sha}", content)
    with open(full_path, "w") as f:
        f.write(new_content)


def main():
    # clone_repo resets hard to origin/main every time, so this always starts
    # from whatever ci's deploy-staging job (or a prior promote run) last pushed
    workdir = repo_ops.clone_repo(repo=MANIFESTS_REPO, workdir=MANIFESTS_WORKDIR)

    staging_sha = read_image_sha(workdir, STAGING_DEPLOYMENT)
    production_sha = read_image_sha(workdir, PRODUCTION_DEPLOYMENT)
    print(f"staging={staging_sha} production={production_sha}")

    if staging_sha is None:
        print("couldn't find an image line in the staging deployment -- nothing to do")
        return
    if staging_sha == production_sha:
        print("production already matches staging -- nothing to promote")
        return

    if not staging_is_healthy():
        print(f"staging not healthy -- not promoting {staging_sha} yet, will retry next tick")
        return

    print(f"staging healthy -- promoting {staging_sha} to production")
    promote_sha_to_production(workdir, staging_sha)
    pushed = repo_ops.commit_and_push("main", f"promote: factory-sample-app@{staging_sha}", workdir=workdir)
    if pushed:
        print(f"promoted {staging_sha} to production")
    else:
        print("nothing staged to commit -- production file already matched (race with another run?)")


# runs once: clones factory-manifests fresh, and if staging is ahead of
# production and passing its health check, bumps production's image line and
# pushes. called every 2 minutes by the promote-staging CronJob -- a failed
# or skipped run just means the next tick tries again
if __name__ == "__main__":
    main()
