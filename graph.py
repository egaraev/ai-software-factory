import os
from typing import TypedDict

from langgraph.graph import END, StateGraph

import developer_agent
import github_client
import jira_client
import repo_ops

# dirs to skip when walking the repo for the model's context
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}
# binary/lockfile-ish extensions that add tokens without helping the model
SKIP_EXTENSIONS = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".db", ".sqlite"}
MAX_FILE_BYTES = 30000


# LangGraph needs a schema to track state between nodes -- this is the one spot
# this project uses type hints, since the framework requires it, not a style choice.
class FactoryState(TypedDict):
    ticket: dict
    design_context: str
    repo_dir: str
    branch: str
    pr_url: str
    pushed: bool
    test_passed: bool
    test_output: str


async def developer_node(state):
    repo_dir = repo_ops.clone_repo()
    branch = f"ticket-{state['ticket']['key']}"
    repo_ops.create_branch(branch)

    repo_files = _read_repo_files(repo_dir)
    changes = await developer_agent.generate_changes(state["ticket"], repo_files, state.get("design_context"))
    for path, content in changes.items():
        full_path = os.path.join(repo_dir, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)

    passed, output = repo_ops.run_tests()
    pushed = repo_ops.commit_and_push(branch, f"{state['ticket']['key']}: {state['ticket']['summary']}")

    return {**state, "repo_dir": repo_dir, "branch": branch, "pushed": pushed, "test_passed": passed, "test_output": output}


def pr_node(state):
    key = state["ticket"]["key"]
    # nothing was pushed -- the ticket's outcome was already delivered by a
    # prior ticket, so there's no branch/diff to open a PR for
    if not state.get("pushed"):
        jira_client.add_comment(key, "No changes needed -- already satisfied by prior work, nothing to commit.")
        return {**state, "pr_url": None}
    title = f"{key}: {state['ticket']['summary']}"
    tail = state["test_output"][-2000:]
    status = "passed" if state["test_passed"] else "FAILED"
    body = f"Automated change for {key}.\n\nTests {status} locally:\n```\n{tail}\n```"
    pr_url = github_client.open_pr(state["branch"], title, body)
    jira_client.add_comment(key, f"PR opened: {pr_url}\n\nTests {status} locally.")
    return {**state, "pr_url": pr_url}


# walks the whole repo instead of a fixed file list, since the app grows new
# files/services (backend, frontend, compose) ticket by ticket
def _read_repo_files(repo_dir):
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


def build_graph():
    graph = StateGraph(FactoryState)
    graph.add_node("developer", developer_node)
    graph.add_node("open_pr", pr_node)
    graph.set_entry_point("developer")
    graph.add_edge("developer", "open_pr")
    graph.add_edge("open_pr", END)
    return graph.compile()
