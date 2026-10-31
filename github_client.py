import os

import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "egaraev/factory-sample-app")
# a second collaborator account's token, used only to submit formal PR reviews --
# github refuses to let an account approve a PR it also authored, so the reviewer
# agent needs a different identity than the one that opens PRs (GITHUB_TOKEN above)
REVIEWER_GITHUB_TOKEN = os.environ.get("REVIEWER_GITHUB_TOKEN")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}
REVIEWER_HEADERS = {
    "Authorization": f"Bearer {REVIEWER_GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

GITHUB_OWNER = GITHUB_REPO.split("/")[0]


# every function below takes an optional repo override -- defaults to GITHUB_REPO
# (the sample app, used by the dev-ticket pipeline) so existing call sites that
# don't pass one keep working unchanged, but sre_agent.py passes its own
# MANIFESTS_REPO to open fix PRs against factory-manifests instead

# opens a PR from head branch into base, returns the PR's html_url. if one's
# already open for this branch -- e.g. retrying a ticket whose earlier PR never
# got closed -- github refuses a duplicate with 422, so fall back to that PR and
# refresh its body, since otherwise it's left showing whichever run's test
# result happened to create it (seen on KAN-25: a PR still said "Tests FAILED"
# from its first run, even after a later retry actually fixed and passed it)
# sends this to github:
#   POST /repos/<repo>/pulls  {"title": "...", "head": "ticket-FACT-12", "base": "main", "body": "..."}
# returns e.g. {"html_url": "https://github.com/egaraev/factory-sample-app/pull/7", ...}
def open_pr(head_branch, title, body, base="main", repo=None):
    repo = repo or GITHUB_REPO
    url = f"https://api.github.com/repos/{repo}/pulls"
    payload = {"title": title, "head": head_branch, "base": base, "body": body}
    r = requests.post(url, headers=HEADERS, json=payload)
    if r.status_code == 422:
        existing = _find_open_pr(head_branch, repo=repo)
        if existing:
            _update_pr_body(existing["number"], body, repo=repo)
            return existing["html_url"]
    r.raise_for_status()
    return r.json()["html_url"]


# sends this to github:
#   GET /repos/<repo>/pulls?head=egaraev:ticket-FACT-12&state=open
# returns e.g. [{"number": 7, "html_url": "https://github.com/egaraev/factory-sample-app/pull/7", ...}]
def _find_open_pr(head_branch, repo=None):
    repo = repo or GITHUB_REPO
    owner = repo.split("/")[0]
    url = f"https://api.github.com/repos/{repo}/pulls"
    params = {"head": f"{owner}:{head_branch}", "state": "open"}
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    results = r.json()
    return results[0] if results else None


# sends this to github:
#   PATCH /repos/<repo>/pulls/7  {"body": "..."}
def _update_pr_body(pr_number, body, repo=None):
    repo = repo or GITHUB_REPO
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    r = requests.patch(url, headers=HEADERS, json={"body": body})
    r.raise_for_status()


# fetches a PR's diff as raw text instead of the default JSON representation --
# github returns the diff format when the request asks for it via Accept
# sends this to github:
#   GET /repos/<repo>/pulls/7  (Accept: application/vnd.github.v3.diff)
# returns e.g. "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ ..."
def get_pr_diff(pr_number, repo=None):
    repo = repo or GITHUB_REPO
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = {**HEADERS, "Accept": "application/vnd.github.v3.diff"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.text


# posts a plain comment on a PR, not a formal review -- github won't let an
# account formally "approve" a PR it also authored, and everything here pushes
# under one account, so a comment is how the reviewer agent's verdict shows up
# sends this to github:
#   POST /repos/<repo>/issues/7/comments  {"body": "..."}
def post_pr_comment(pr_number, body, repo=None):
    repo = repo or GITHUB_REPO
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    r = requests.post(url, headers=HEADERS, json={"body": body})
    r.raise_for_status()


# submits a formal review (Approve/Request changes/Comment) using the second
# collaborator account's token -- this is what actually satisfies a branch
# protection rule that requires an approving review, unlike post_pr_comment
# sends this to github:
#   POST /repos/<repo>/pulls/7/reviews  {"body": "...", "event": "APPROVE"}
def submit_pr_review(pr_number, event, body, repo=None):
    repo = repo or GITHUB_REPO
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    r = requests.post(url, headers=REVIEWER_HEADERS, json={"body": body, "event": event})
    r.raise_for_status()


# looks up the PR for a given branch, in any state -- used by the orchestrator
# to tell whether a ticket's PR has actually been merged yet, without having to
# track PR numbers anywhere itself (the branch name is always ticket-<key> for a
# dev ticket, sre-<key> for an sre-agent fix PR -- see orchestrator.py)
# sends this to github:
#   GET /repos/<repo>/pulls?head=egaraev:ticket-KAN-23&state=all
# returns e.g. {"number": 7, "html_url": "...", "merged_at": "2026-10-30T20:00:00Z"}
def get_pr_for_branch(head_branch, state="all", repo=None):
    repo = repo or GITHUB_REPO
    owner = repo.split("/")[0]
    url = f"https://api.github.com/repos/{repo}/pulls"
    params = {"head": f"{owner}:{head_branch}", "state": state}
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    results = r.json()
    return results[0] if results else None


# a PR's html_url always ends in its number (".../pull/7") -- pulled out here
# so graph.py and reviewer_agent.py don't each need their own copy
def pr_number_from_url(pr_url):
    return int(pr_url.rstrip("/").rsplit("/", 1)[-1])


# merges a PR -- used for auto-merge once the formal reviewer account approves
# and local tests passed (see graph.py's pr_node)
# sends this to github:
#   PUT /repos/<repo>/pulls/7/merge  {"merge_method": "merge"}
# returns e.g. {"merged": true, "sha": "...", "message": "Pull Request successfully merged"}
def merge_pr(pr_number, merge_method="merge", repo=None):
    repo = repo or GITHUB_REPO
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/merge"
    r = requests.put(url, headers=HEADERS, json={"merge_method": merge_method})
    r.raise_for_status()
