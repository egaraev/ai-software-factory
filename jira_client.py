import os

import requests

JIRA_URL = os.environ.get("JIRA_URL")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")
PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "KAN")


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


# lists every task under an epic, in ascending key order -- used by the phase-2
# orchestrator to walk an epic's tickets in sequence. uses the newer JQL search
# endpoint, since jira cloud removed the old /rest/api/3/search endpoint
# sends this to jira:
#   POST /rest/api/3/search/jql  {"jql": "parent = KAN-17 ORDER BY key ASC", "fields": ["status"]}
# returns e.g. {"issues": [{"key": "KAN-18", "fields": {"status": {"name": "Done"}}}, ...]}
def get_epic_tasks(epic_key):
    url = f"{JIRA_URL}/rest/api/3/search/jql"
    payload = {"jql": f"parent = {epic_key} ORDER BY key ASC", "fields": ["status"]}
    r = requests.post(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), json=payload)
    r.raise_for_status()
    return [{"key": issue["key"], "status": issue["fields"]["status"]["name"]} for issue in r.json()["issues"]]


# lists every epic in the project, in ascending key order -- lets the
# orchestrator auto-discover epics instead of needing one hardcoded epic key
# in cronjob.yaml. doesn't filter by the epic issue's own status field (that
# varies by workflow and isn't reliable) -- whether an epic still has work is
# decided by whether all its child tasks are Done (see orchestrator.py)
# sends this to jira:
#   POST /rest/api/3/search/jql  {"jql": "project = KAN AND issuetype = Epic ORDER BY key ASC"}
# returns e.g. {"issues": [{"key": "KAN-24"}, {"key": "KAN-31"}]}
def get_epics():
    url = f"{JIRA_URL}/rest/api/3/search/jql"
    payload = {"jql": f"project = {PROJECT_KEY} AND issuetype = Epic ORDER BY key ASC", "fields": ["status"]}
    r = requests.post(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), json=payload)
    r.raise_for_status()
    return [{"key": issue["key"]} for issue in r.json()["issues"]]


# moves a ticket to the given status, matched by name against whatever
# transitions are currently available on it -- transition ids vary per
# project/workflow, so this looks them up instead of hardcoding one. does
# nothing and returns False if no matching transition is available right
# now (e.g. the workflow has no "In Progress" status, or it's already past
# it), rather than crashing a whole ticket run over a status update
# sends this to jira:
#   GET /rest/api/3/issue/KAN-11/transitions
# returns e.g. {"transitions": [{"id": "21", "name": "In Progress"}, {"id": "31", "name": "Done"}]}
def transition_to(ticket_key, status_name):
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}/transitions"
    r = requests.get(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN))
    r.raise_for_status()
    match = next((t for t in r.json()["transitions"] if t["name"].lower() == status_name.lower()), None)
    if not match:
        return False
    r = requests.post(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), json={"transition": {"id": match["id"]}})
    r.raise_for_status()
    return True


# posts a comment on a ticket -- used so the ticket itself shows what the
# developer agent actually did (PR link, or that nothing changed), instead of
# that only being visible in the pipeline's terminal output or on GitHub
# sends this to jira:
#   POST /rest/api/3/issue/KAN-9/comment  {"body": <ADF doc>}
def add_comment(ticket_key, text):
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}/comment"
    r = requests.post(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), json={"body": _to_adf(text)})
    r.raise_for_status()


# looks for the most recent comment on a ticket that starts with "REWORK:" --
# that's how a human asks the developer agent to revise an already-open PR
# instead of starting over (see graph.py's developer_node, and orchestrator.py
# which spawns a job as soon as the ticket is moved back to "To Do"). returns
# the feedback text with the prefix stripped, or None if there isn't one
# sends this to jira:
#   GET /rest/api/3/issue/KAN-27/comment
# returns e.g. {"comments": [{"created": "2026-10-30T21:00:00.000+0000",
#   "body": <ADF doc containing "REWORK: wrap the fixture restore in a try/finally">}]}
def get_rework_feedback(ticket_key):
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}/comment"
    r = requests.get(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN))
    r.raise_for_status()
    rework_comments = []
    for comment in r.json()["comments"]:
        text = _extract_text(comment["body"]).strip()
        if text.upper().startswith("REWORK:"):
            rework_comments.append((comment["created"], text[len("REWORK:"):].strip()))
    if not rework_comments:
        return None
    rework_comments.sort(key=lambda pair: pair[0])
    return rework_comments[-1][1]  # most recent one, by Jira's own created timestamp


# counts how many automatic rework attempts a ticket has already had -- used to
# cap auto-retry-on-CONCERNS (see graph.py's pr_node) before falling back to a
# human. only counts comments the pipeline itself posted ("REWORK: [auto] ..."),
# not a human's plain "REWORK: ..." comment, so a human asking for rework is
# never blocked by the automatic cap
# sends this to jira:
#   GET /rest/api/3/issue/KAN-27/comment
# returns e.g. {"comments": [{"body": <ADF doc containing "REWORK: [auto] ...">}]}
def count_auto_rework_attempts(ticket_key):
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}/comment"
    r = requests.get(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN))
    r.raise_for_status()
    return sum(1 for c in r.json()["comments"] if _extract_text(c["body"]).strip().upper().startswith("REWORK: [AUTO"))


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
