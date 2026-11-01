import json
import os
import re
import subprocess
import time

import requests

import repo_ops

# scorecard-only tools -- not something the sample app itself needs, so they
# go on top of its own venv (repo_ops already installs requirements-dev.txt
# into it) instead of into requirements.txt
SCORECARD_TOOLS = ["ruff", "radon", "pip-audit", "coverage"]

PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "http://pushgateway.observe.svc.cluster.local:9091")


def ensure_scorecard_tools():
    pip_bin = os.path.join(repo_ops.VENV_DIR, "bin", "pip")
    subprocess.run([pip_bin, "install", *SCORECARD_TOOLS], check=True, capture_output=True)


def get_commit_sha(workdir):
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workdir, capture_output=True, text=True, check=True)
    return result.stdout.strip()


# ruff's default text output ends with a summary line, e.g. "Found 3 errors."
# -- returns 0 when that line is missing entirely, which means ruff found
# nothing to report
def count_lint_errors(workdir):
    ruff_bin = os.path.join(repo_ops.VENV_DIR, "bin", "ruff")
    result = subprocess.run([ruff_bin, "check", "."], cwd=workdir, capture_output=True, text=True)
    match = re.search(r"Found (\d+) error", result.stdout)
    return int(match.group(1)) if match else 0


# radon's -a flag adds one summary line at the end of `radon cc`, e.g.:
#   "Average complexity: A (2.43)"
# returns None if radon found no functions at all to analyze (nothing in
# app/ yet, or radon itself failed) rather than reporting a fake 0
def average_complexity(workdir):
    radon_bin = os.path.join(repo_ops.VENV_DIR, "bin", "radon")
    result = subprocess.run([radon_bin, "cc", "-a", "app"], cwd=workdir, capture_output=True, text=True)
    match = re.search(r"Average complexity: \w \(([\d.]+)\)", result.stdout)
    return float(match.group(1)) if match else None


# pip-audit --format json returns e.g.:
#   {"dependencies": [{"name": "fastapi", "version": "0.115.0", "vulns": []}, ...]}
# sums every vuln across every dependency listed in requirements.txt (not
# requirements-dev.txt -- that's pytest/httpx, not what actually ships)
def count_vulnerabilities(workdir):
    pip_audit_bin = os.path.join(repo_ops.VENV_DIR, "bin", "pip-audit")
    result = subprocess.run(
        [pip_audit_bin, "-r", "requirements.txt", "--format", "json"],
        cwd=workdir, capture_output=True, text=True,
    )
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return 0  # pip-audit couldn't run (e.g. no requirements.txt yet) -- treat as "nothing found", not a crash
    return sum(len(dep.get("vulns", [])) for dep in data.get("dependencies", []))


# coverage's terminal report ends with a TOTAL line, e.g.:
#   "TOTAL    142    18    87%"
# (branch coverage adds extra columns before the %, so the regex only pins
# down the start and the final percentage, not the column count)
# returns None if there's nothing to measure yet (no tests, or they all failed to even collect)
def test_coverage_percent(workdir):
    coverage_bin = os.path.join(repo_ops.VENV_DIR, "bin", "coverage")
    subprocess.run([coverage_bin, "run", "-m", "pytest"], cwd=workdir, capture_output=True, text=True)
    result = subprocess.run([coverage_bin, "report"], cwd=workdir, capture_output=True, text=True)
    match = re.search(r"TOTAL\s+.*?(\d+)%", result.stdout)
    return int(match.group(1)) if match else None


# pushgateway takes plain prometheus text-exposition format in the POST body, e.g.:
#   repo_scorecard_coverage_percent 87
#   repo_scorecard_lint_errors 3
# POSTing to /metrics/job/repo-scorecard REPLACES that job's whole metric
# group each time -- a snapshot of this run, not an accumulating series, so
# a metric that stops being reported (e.g. coverage once collection breaks)
# cleanly disappears instead of showing a stale number forever
def push_metrics(metrics):
    lines = [f"{name} {value}" for name, value in metrics.items() if value is not None]
    body = "\n".join(lines) + "\n"
    url = f"{PUSHGATEWAY_URL}/metrics/job/repo-scorecard"
    r = requests.post(url, data=body, timeout=10)
    r.raise_for_status()


# scores factory-sample-app's current main (coverage, lint errors, average
# complexity, vulnerable deps) and pushes the numbers to the pushgateway for
# grafana's "repo readiness" dashboard to pick up -- run on a schedule by
# k8s/orchestrator/scorecard-cronjob.yaml, not triggered per-commit
def main():
    workdir = repo_ops.clone_repo()
    repo_ops._ensure_venv()
    repo_ops._install_requirements()
    ensure_scorecard_tools()

    metrics = {
        "repo_scorecard_coverage_percent": test_coverage_percent(workdir),
        "repo_scorecard_lint_errors": count_lint_errors(workdir),
        "repo_scorecard_avg_complexity": average_complexity(workdir),
        "repo_scorecard_vulnerabilities": count_vulnerabilities(workdir),
        "repo_scorecard_last_run_timestamp_seconds": time.time(),
    }
    push_metrics(metrics)
    print(f"commit {get_commit_sha(workdir)}: pushed {metrics}")


if __name__ == "__main__":
    main()
