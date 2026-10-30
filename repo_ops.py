import os
import subprocess
import sys

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "egaraev/factory-sample-app")
WORKDIR = os.environ.get("FACTORY_WORKDIR", "/tmp/factory-sample-app")
# kept separate from this factory's own venv -- tickets can add any packages
# to the sample app over time, and those shouldn't mix with our dependencies.
# lives next to WORKDIR, not inside it, so `git add -A` never picks it up
VENV_DIR = WORKDIR.rstrip("/") + "-venv"


def clone_repo():
    if os.path.exists(WORKDIR):
        run(["git", "fetch", "origin"], cwd=WORKDIR)
        run(["git", "checkout", "main"], cwd=WORKDIR)
        run(["git", "reset", "--hard", "origin/main"], cwd=WORKDIR)
        return WORKDIR
    url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
    run(["git", "clone", url, WORKDIR])
    return WORKDIR


# -B (not -b) resets the branch to start fresh from main if a previous run already
# created it -- WORKDIR persists between runs, so a retry would otherwise find the
# branch still sitting there from the attempt that failed partway through
def create_branch(branch_name):
    run(["git", "checkout", "-B", branch_name], cwd=WORKDIR)


def commit_and_push(branch_name, message):
    run(["git", "add", "-A"], cwd=WORKDIR)
    run(["git", "commit", "-m", message], cwd=WORKDIR)
    run(["git", "push", "-u", "origin", branch_name], cwd=WORKDIR)


# installs requirements.txt into the sample app's own venv (creating it if needed)
# and runs its tests with that venv's pytest, not whatever's on the caller's PATH
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
    req_file = os.path.join(WORKDIR, "requirements.txt")
    if os.path.exists(req_file):
        pip_bin = os.path.join(VENV_DIR, "bin", "pip")
        run([pip_bin, "install", "-r", req_file])


def run(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)
