import os

import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "egaraev/factory-sample-app")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
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
