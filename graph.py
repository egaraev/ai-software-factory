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
    branch = f"ticket-{key}"
    # an unmerged PR already existing means this run is actually a rework
    # request -- a human moved the ticket back to "To Do" and left a "REWORK:"
    # comment asking for a specific change, rather than a fresh ticket (see
    # jira_client.get_rework_feedback and repo_ops.checkout_branch)
    existing_pr = github_client.get_pr_for_branch(branch)
    rework = existing_pr is not None and not existing_pr.get("merged_at")
    review_feedback = jira_client.get_rework_feedback(key) if rework else None

    jira_client.transition_to(key, "In Progress")

    repo_dir = repo_ops.clone_repo()
    if rework:
        repo_ops.checkout_branch(branch)
    else:
        repo_ops.create_branch(branch)

    repo_files = repo_ops.read_repo_files(repo_dir)
    changes = await developer_agent.generate_changes(
        state["ticket"], repo_files, state.get("design_context"), review_feedback=review_feedback
    )
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


# caps automatic rework retries before a ticket falls back to a human --
# a human's own plain "REWORK:" comment is never capped, only ones the
# pipeline posts itself with the "[auto]" marker (see jira_client.count_auto_rework_attempts)
MAX_AUTO_REWORK_ATTEMPTS = 2


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
    approved, review_text = None, None
    try:
        approved, review_text = await reviewer_agent.review_pr(state["ticket"], pr_url, state["test_passed"], tail)
    except Exception as e:  # a reviewer outage shouldn't block the PR from existing
        print(f"warning: reviewer agent failed for {key}: {e}")
    # auto-merge/auto-rework only kicks in once a real second reviewer account is
    # configured (REVIEWER_GITHUB_TOKEN) -- without it there's no formal github
    # review satisfying the branch protection rule, so the PR just waits for a
    # human either way, same as before this feature existed
    if github_client.REVIEWER_GITHUB_TOKEN and approved is not None:
        if approved:
            github_client.merge_pr(github_client.pr_number_from_url(pr_url))
            jira_client.add_comment(key, "Reviewer approved and tests passed -- auto-merged.")
            jira_client.transition_to(key, "Done")
        else:
            attempts = jira_client.count_auto_rework_attempts(key)
            if attempts < MAX_AUTO_REWORK_ATTEMPTS:
                # moving it back to "To Do" is exactly what a human does to
                # trigger a manual rework -- the orchestrator's existing "To Do"
                # handling (and developer_node's existing-PR check) picks this
                # up automatically on its next poll, no new code needed there
                jira_client.add_comment(key, f"REWORK: [auto] {review_text}")
                jira_client.transition_to(key, "To Do")
            else:
                jira_client.add_comment(key, f"Reviewer left CONCERNS after {attempts} automatic rework attempts -- needs a human look.")
                jira_client.transition_to(key, "Needs Info")
    return {**state, "pr_url": pr_url}


def build_graph():
    graph = StateGraph(FactoryState)
    graph.add_node("developer", developer_node)
    graph.add_node("open_pr", pr_node)
    graph.set_entry_point("developer")
    graph.add_edge("developer", "open_pr")
    graph.add_edge("open_pr", END)
    return graph.compile()
