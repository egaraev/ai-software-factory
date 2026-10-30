import os

import requests

JIRA_URL = os.environ.get("JIRA_URL")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")


# ticket_key is like "KAN-12", returns {"key", "summary", "description", "parent_key"}.
# parent_key is set when this ticket is a task under an epic (None for the epic itself).
def get_ticket(ticket_key):
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}"
    r = requests.get(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), headers={"Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    summary = data["fields"]["summary"]
    description = _extract_text(data["fields"].get("description"))
    parent = data["fields"].get("parent")
    return {"key": ticket_key, "summary": summary, "description": description,
            "parent_key": parent["key"] if parent else None}


# creates a task under project_key, linked to epic_key if given (team-managed
# projects use the "parent" field for epic links). returns the new ticket's key.
# sends this to jira:
#   POST /rest/api/3/issue  {"fields": {"project": {"key": "KAN"}, "issuetype": {"name": "Task"},
#     "summary": "...", "description": <ADF doc>, "parent": {"key": "KAN-5"}}}
# returns e.g. {"id": "10050", "key": "KAN-9", "self": "https://..."}
def create_task(project_key, summary, description, epic_key=None):
    url = f"{JIRA_URL}/rest/api/3/issue"
    fields = {
        "project": {"key": project_key},
        "issuetype": {"name": "Task"},
        "summary": summary,
        "description": _to_adf(description),
    }
    if epic_key:
        fields["parent"] = {"key": epic_key}
    r = requests.post(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), json={"fields": fields})
    r.raise_for_status()
    return r.json()["key"]


# appends extra_text to a ticket's existing description -- used to persist the
# architect agent's approved design doc onto the epic, so later developer tickets
# can read it back as context via get_ticket's parent_key
def append_description(ticket_key, extra_text):
    current = get_ticket(ticket_key)
    combined = f"{current['description']}\n\n{extra_text}" if current["description"] else extra_text
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}"
    r = requests.put(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), json={"fields": {"description": _to_adf(combined)}})
    r.raise_for_status()


# posts a comment on a ticket -- used so the ticket itself shows what the
# developer agent actually did (PR link, or that nothing changed), instead of
# that only being visible in the pipeline's terminal output or on GitHub
# sends this to jira:
#   POST /rest/api/3/issue/KAN-9/comment  {"body": <ADF doc>}
def add_comment(ticket_key, text):
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}/comment"
    r = requests.post(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), json={"body": _to_adf(text)})
    r.raise_for_status()


# Jira's description field is Atlassian Document Format (nested blocks of text nodes),
# not plain text -- this walks it and joins every text node it finds.
def _extract_text(adf):
    if not adf:
        return ""
    parts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(adf)
    return "\n".join(parts)


# plain text -> ADF, one paragraph per non-empty line (Jira writes require this format too)
def _to_adf(text):
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            for line in text.split("\n") if line.strip()
        ],
    }
