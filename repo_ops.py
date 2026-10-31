import os
import subprocess
import sys

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "egaraev/factory-sample-app")
# kept separate from this factory's own venv -- tickets can add any packages
# to the sample app over time, and those shouldn't mix with our dependencies.
# lives next to WORKDIR, not inside it, so `git add -A` never picks it up
WORKDIR = os.environ.get("FACTORY_WORKDIR", "/tmp/factory-sample-app")
VENV_DIR = WORKDIR.rstrip("/") + "-venv"

# dirs/extensions to skip when walking the repo for an agent's context
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}
SKIP_EXTENSIONS = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".db", ".sqlite"}
MAX_FILE_BYTES = 30000


# every function below takes an optional repo/workdir override -- defaults to
# GITHUB_REPO/WORKDIR (the sample app, used by the dev-ticket pipeline) so
# existing call sites that don't pass one keep working unchanged, but
# sre_agent.py passes its own MANIFESTS_REPO/MANIFESTS_WORKDIR to work against
# factory-manifests instead

def clone_repo(repo=GITHUB_REPO, workdir=WORKDIR):
    if os.path.exists(workdir):
        run(["git", "fetch", "origin"], cwd=workdir)
        run(["git", "checkout", "main"], cwd=workdir)
        run(["git", "reset", "--hard", "origin/main"], cwd=workdir)
        return workdir
    url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{repo}.git"
    run(["git", "clone", url, workdir])
    return workdir


# -B (not -b) resets the branch to start fresh from main if a previous run already
# created it -- workdir persists between runs, so a retry would otherwise find the
# branch still sitting there from the attempt that failed partway through
def create_branch(branch_name, workdir=WORKDIR):
    run(["git", "checkout", "-B", branch_name], cwd=workdir)


# for a rework pass (see graph.py's developer_node) -- unlike create_branch,
# which always restarts fresh from main, this picks up the ticket's branch as
# it was last pushed, so the model sees its own prior attempt (plus whatever
# feedback) instead of losing it
def checkout_branch(branch_name, workdir=WORKDIR):
    run(["git", "fetch", "origin", branch_name], cwd=workdir)
    run(["git", "checkout", "-B", branch_name, f"origin/{branch_name}"], cwd=workdir)


# --force-with-lease since create_branch's -B means a retry's local history won't
# match whatever this ticket's branch already has on the remote from a prior attempt.
# returns False (and skips commit/push entirely) when there's nothing staged --
# a legit outcome, not an error: it means an earlier ticket already delivered
# whatever this one asked for, so the model regenerated identical file content.
def commit_and_push(branch_name, message, workdir=WORKDIR):
    run(["git", "add", "-A"], cwd=workdir)
    nothing_staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=workdir).returncode == 0
    if nothing_staged:
        return False
    run(["git", "commit", "-m", message], cwd=workdir)
    run(["git", "push", "-u", "origin", branch_name, "--force-with-lease"], cwd=workdir)
    return True


# installs requirements.txt into the sample app's own venv (creating it if needed)
# and runs its tests with that venv's pytest, not whatever's on the caller's PATH.
# sample-app-specific (pytest/venv) -- sre_agent.py's manifest fixes don't call this
def run_tests():
    _ensure_venv()
    _install_requirements()
    pytest_bin = os.path.join(VENV_DIR, "bin", "pytest")
    if not os.path.exists(pytest_bin):
        return False, "pytest not installed -- requirements.txt is missing it, or there's nothing to test yet"
    result = subprocess.run([pytest_bin], cwd=WORKDIR, capture_output=True, text=True)
    return result.returncode == 0, result.stdout + result.stderr


def _ensure_venv():
    if not os.path.exists(VENV_DIR):
        run([sys.executable, "-m", "venv", VENV_DIR])


def _install_requirements():
    # prefer requirements-dev.txt when the app splits runtime vs test deps --
    # the plain requirements.txt alone may not have pytest/httpx in that case
    req_file = os.path.join(WORKDIR, "requirements-dev.txt")
    if not os.path.exists(req_file):
        req_file = os.path.join(WORKDIR, "requirements.txt")
    if os.path.exists(req_file):
        pip_bin = os.path.join(VENV_DIR, "bin", "pip")
        run([pip_bin, "install", "-r", req_file])


def run(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


# walks the whole repo instead of a fixed file list, since the app grows new
# files/services (backend, frontend, compose) ticket by ticket. used both by
# the developer agent (to edit the right files) and the architect agent (to
# design on top of what's already there instead of in a vacuum) -- and by
# sre_agent.py, to see factory-manifests' current manifests before proposing a fix
def read_repo_files(repo_dir):
    files = {}
    for root, dirs, filenames in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in filenames:
            if os.path.splitext(name)[1] in SKIP_EXTENSIONS:
                continue
            full_path = os.path.join(root, name)
            if os.path.getsize(full_path) > MAX_FILE_BYTES:
                continue
            rel_path = os.path.relpath(full_path, repo_dir)
            try:
                with open(full_path) as f:
                    files[rel_path] = f.read()
            except UnicodeDecodeError:  # skip binary files we didn't catch by extension
                continue
    return files
