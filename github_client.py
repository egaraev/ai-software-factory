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


# opens a PR from head branch into base, returns the PR's html_url. if one's
# already open for this branch -- e.g. retrying a ticket whose earlier PR never
# got closed -- github refuses a duplicate with 422, so fall back to that PR.
# sends this to github:
#   POST /repos/<repo>/pulls  {"title": "...", "head": "ticket-FACT-12", "base": "main", "body": "..."}
# returns e.g. {"html_url": "https://github.com/egaraev/factory-sample-app/pull/7", ...}
def open_pr(head_branch, title, body, base="main"):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls"
    payload = {"title": title, "head": head_branch, "base": base, "body": body}
    r = requests.post(url, headers=HEADERS, json=payload)
    if r.status_code == 422:
        existing = _find_open_pr(head_branch)
        if existing:
            return existing
    r.raise_for_status()
    return r.json()["html_url"]


# sends this to github:
#   GET /repos/<repo>/pulls?head=egaraev:ticket-FACT-12&state=open
# returns e.g. [{"html_url": "https://github.com/egaraev/factory-sample-app/pull/7", ...}]
def _find_open_pr(head_branch):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls"
    params = {"head": f"{GITHUB_OWNER}:{head_branch}", "state": "open"}
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    results = r.json()
    return results[0]["html_url"] if results else None


# fetches a PR's diff as raw text instead of the default JSON representation --
# github returns the diff format when the request asks for it via Accept
# sends this to github:
#   GET /repos/<repo>/pulls/7  (Accept: application/vnd.github.v3.diff)
# returns e.g. "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ ..."
def get_pr_diff(pr_number):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_number}"
    headers = {**HEADERS, "Accept": "application/vnd.github.v3.diff"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.text


# posts a plain comment on a PR, not a formal review -- github won't let an
# account formally "approve" a PR it also authored, and everything here pushes
# under one account, so a comment is how the reviewer agent's verdict shows up
# sends this to github:
#   POST /repos/<repo>/issues/7/comments  {"body": "..."}
def post_pr_comment(pr_number, body):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{pr_number}/comments"
    r = requests.post(url, headers=HEADERS, json={"body": body})
    r.raise_for_status()


# submits a formal review (Approve/Request changes/Comment) using the second
# collaborator account's token -- this is what actually satisfies a branch
# protection rule that requires an approving review, unlike post_pr_comment
# sends this to github:
#   POST /repos/<repo>/pulls/7/reviews  {"body": "...", "event": "APPROVE"}
def submit_pr_review(pr_number, event, body):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_number}/reviews"
    r = requests.post(url, headers=REVIEWER_HEADERS, json={"body": body, "event": event})
    r.raise_for_status()


# looks up the PR for a given branch, in any state -- used by the orchestrator
# to tell whether a ticket's PR has actually been merged yet, without having to
# track PR numbers anywhere itself (the branch name is always ticket-<key>)
# sends this to github:
#   GET /repos/<repo>/pulls?head=egaraev:ticket-KAN-23&state=all
# returns e.g. {"number": 7, "html_url": "...", "merged_at": "2026-10-30T20:00:00Z"}
def get_pr_for_branch(head_branch, state="all"):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls"
    params = {"head": f"{GITHUB_OWNER}:{head_branch}", "state": state}
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    results = r.json()
    return results[0] if results else None
