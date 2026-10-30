import os
from typing import TypedDict

from langgraph.graph import END, StateGraph

import developer_agent
import github_client
import jira_client
import repo_ops
import reviewer_agent


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
    key = state["ticket"]["key"]
    jira_client.transition_to(key, "In Progress")

    repo_dir = repo_ops.clone_repo()
    branch = f"ticket-{key}"
    repo_ops.create_branch(branch)

    repo_files = repo_ops.read_repo_files(repo_dir)
    changes = await developer_agent.generate_changes(state["ticket"], repo_files, state.get("design_context"))
    _write_changes(repo_dir, changes)

    passed, output = repo_ops.run_tests()
    if not passed:
        # one retry, same idea as the reformat-retry already in developer_agent.py --
        # feed the model its own failing pytest output plus the files it just wrote,
        # and give it one more shot before giving up and pushing whatever we have
        repo_files = repo_ops.read_repo_files(repo_dir)
        changes = await developer_agent.generate_changes(
            state["ticket"], repo_files, state.get("design_context"), test_feedback=output
        )
        _write_changes(repo_dir, changes)
        passed, output = repo_ops.run_tests()

    pushed = repo_ops.commit_and_push(branch, f"{key}: {state['ticket']['summary']}")

    return {**state, "repo_dir": repo_dir, "branch": branch, "pushed": pushed, "test_passed": passed, "test_output": output}


# writes a path -> content changeset to disk under repo_dir -- split out of
# developer_node since the test-failure retry above needs to run it twice
def _write_changes(repo_dir, changes):
    for path, content in changes.items():
        full_path = os.path.join(repo_dir, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)


async def pr_node(state):
    key = state["ticket"]["key"]
    # nothing was pushed -- the ticket's outcome was already delivered by a
    # prior ticket, so there's no branch/diff to open a PR for
    if not state.get("pushed"):
        status = "passed" if state["test_passed"] else "FAILED"
        jira_client.add_comment(key, f"No changes needed -- already satisfied by prior work, nothing to commit. Tests {status} locally.")
        # only safe to call it Done when the existing code actually still passes --
        # otherwise leave the status alone so a human notices something's off
        if state["test_passed"]:
            jira_client.transition_to(key, "Done")
        return {**state, "pr_url": None}
    title = f"{key}: {state['ticket']['summary']}"
    tail = state["test_output"][-2000:]
    status = "passed" if state["test_passed"] else "FAILED"
    body = f"Automated change for {key}.\n\nTests {status} locally:\n```\n{tail}\n```"
    pr_url = github_client.open_pr(state["branch"], title, body)
    jira_client.add_comment(key, f"PR opened: {pr_url}\n\nTests {status} locally.")
    jira_client.transition_to(key, "In Review")
    try:
        await reviewer_agent.review_pr(state["ticket"], pr_url, state["test_passed"], tail)
    except Exception as e:  # a reviewer outage shouldn't block the PR from existing
        print(f"warning: reviewer agent failed for {key}: {e}")
    return {**state, "pr_url": pr_url}


def build_graph():
    graph = StateGraph(FactoryState)
    graph.add_node("developer", developer_node)
    graph.add_node("open_pr", pr_node)
    graph.set_entry_point("developer")
    graph.add_edge("developer", "open_pr")
    graph.add_edge("open_pr", END)
    return graph.compile()
