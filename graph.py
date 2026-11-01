import json
import os
from typing import TypedDict

from langgraph.graph import END, StateGraph

import developer_agent
import github_client
import jira_client
import repo_ops
import reviewer_agent
import test_writer_agent


# LangGraph needs a schema to track state between nodes -- this is the one spot
# this project uses type hints, since the framework requires it, not a style choice.
class FactoryState(TypedDict):
    ticket: dict
    design_context: str
    repo_dir: str
    branch: str
    rework: bool
    locked_test_paths: list
    changed_paths: list
    mutation_stats: dict
    pr_url: str
    pushed: bool
    test_passed: bool
    test_output: str


# tracked in git on the ticket's own branch -- the list of spec-test paths
# write_tests_node locked in on the first attempt, so a rework pass (a human
# left "REWORK:" feedback) or a crash-retry reuses the exact same tests
# instead of test_writer_agent generating (and locking in) different ones
# each time the branch is picked back up
LOCKED_TESTS_MANIFEST = ".factory/locked-tests.json"


def _read_locked_test_paths(repo_dir):
    path = os.path.join(repo_dir, LOCKED_TESTS_MANIFEST)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _write_locked_test_paths(repo_dir, test_paths):
    full_path = os.path.join(repo_dir, LOCKED_TESTS_MANIFEST)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w") as f:
        json.dump(test_paths, f, indent=2)


# tests-first spec stage: writes pytest tests from the ticket BEFORE
# developer_node writes any implementation, confirms they actually fail
# first (a spec test that already passes against an empty/unrelated repo is
# probably vacuous -- see test_writer_agent.py), then locks them in via
# LOCKED_TESTS_MANIFEST so developer_node can never silently rewrite them to
# make them pass. runs the same clone/branch setup developer_node used to do
# itself -- moved up here since this is now the real first step of work on
# a ticket
async def write_tests_node(state):
    key = state["ticket"]["key"]
    branch = f"ticket-{key}"
    # an unmerged PR already existing means this run is actually a rework
    # request -- a human moved the ticket back to "To Do" and left a "REWORK:"
    # comment asking for a specific change, rather than a fresh ticket (see
    # jira_client.get_rework_feedback and repo_ops.checkout_branch)
    existing_pr = github_client.get_pr_for_branch(branch)
    rework = existing_pr is not None and not existing_pr.get("merged_at")

    jira_client.transition_to(key, "In Progress")

    repo_dir = repo_ops.clone_repo()
    if rework:
        repo_ops.checkout_branch(branch)
    else:
        repo_ops.create_branch(branch)

    locked_test_paths = _read_locked_test_paths(repo_dir)
    if locked_test_paths is not None:
        # rework or crash-retry on a ticket already past this stage -- the
        # spec tests from the first attempt are already committed on this
        # branch, reuse them as-is
        return {**state, "repo_dir": repo_dir, "branch": branch, "rework": rework, "locked_test_paths": locked_test_paths}

    repo_files = repo_ops.read_repo_files(repo_dir)
    test_changes = await test_writer_agent.generate_tests(state["ticket"], repo_files, state.get("design_context"))
    locked_test_paths = _write_changes(repo_dir, test_changes)

    if locked_test_paths:
        already_passes, _ = repo_ops.run_scoped_tests(locked_test_paths)
        if already_passes:
            jira_client.add_comment(
                key,
                "Heads up: the spec tests written for this ticket already pass before any "
                "implementation exists -- they may not actually be exercising the feature. "
                "Locking them in anyway; implementation is still required.",
            )
        _write_locked_test_paths(repo_dir, locked_test_paths)
        repo_ops.commit_and_push(branch, f"{key}: spec tests (tests-first)")

    return {**state, "repo_dir": repo_dir, "branch": branch, "rework": rework, "locked_test_paths": locked_test_paths}


async def developer_node(state):
    key = state["ticket"]["key"]
    branch = state["branch"]
    repo_dir = state["repo_dir"]
    locked_test_paths = state["locked_test_paths"]
    review_feedback = jira_client.get_rework_feedback(key) if state["rework"] else None

    repo_files = repo_ops.read_repo_files(repo_dir)
    changes = await developer_agent.generate_changes(
        state["ticket"], repo_files, state.get("design_context"), review_feedback=review_feedback
    )
    changed_paths = _write_changes(repo_dir, changes, locked_paths=locked_test_paths)

    passed, output = repo_ops.run_tests()
    if not passed:
        # one retry, same idea as the reformat-retry already in developer_agent.py --
        # feed the model its own failing pytest output plus the files it just wrote,
        # and give it one more shot before giving up and pushing whatever we have
        repo_files = repo_ops.read_repo_files(repo_dir)
        changes = await developer_agent.generate_changes(
            state["ticket"], repo_files, state.get("design_context"), test_feedback=output
        )
        changed_paths = _write_changes(repo_dir, changes, locked_paths=locked_test_paths)
        passed, output = repo_ops.run_tests()

    pushed = repo_ops.commit_and_push(branch, f"{key}: {state['ticket']['summary']}")

    return {**state, "pushed": pushed, "test_passed": passed, "test_output": output, "changed_paths": changed_paths}


# advisory mutation-testing pass: checks whether write_tests_node's locked
# spec tests actually catch bugs in developer_node's implementation, not
# just that they pass. never blocks the PR -- mutmut's import-resolution can
# be fussy about a repo's exact package layout, and a mutation-testing
# hiccup should never be the thing that stops a ticket from shipping. skips
# outright (not an error) when there's nothing meaningful to check
async def mutation_node(state):
    key = state["ticket"]["key"]
    if not state.get("test_passed") or not state.get("locked_test_paths") or not state.get("changed_paths"):
        return state
    try:
        stats = repo_ops.run_mutation_tests(state["repo_dir"], state["changed_paths"], state["locked_test_paths"])
    except Exception as e:  # advisory only -- see module docstring above
        print(f"warning: mutation testing failed for {key}, skipping (advisory only): {e}")
        return state
    return {**state, "mutation_stats": stats}


def _mutation_summary_text(stats):
    if not stats or not stats.get("total"):
        return ""
    killed, total = stats.get("killed", 0), stats["total"]
    score = killed / total * 100
    lines = [f"Mutation testing (spec tests vs. this ticket's own changes): {killed}/{total} mutants killed ({score:.0f}%)."]
    if stats.get("no_tests"):
        lines.append(f"{stats['no_tests']} mutant(s) had no test covering that code at all.")
    if stats.get("surviving"):
        lines.append("Surviving:\n" + "\n".join(f"  {s}" for s in stats["surviving"]))
    return "\n".join(lines)


# writes a path -> content changeset to disk under repo_dir, skipping any
# path in locked_paths (write_tests_node's own call never passes this --
# nothing is locked yet on the very first write). developer_agent sometimes
# tries to "fix" a failing locked test instead of its own implementation;
# this is the enforcement for "locked" actually meaning locked. returns the
# list of paths actually written, so callers can tell what a ticket's own
# changes were (mutation_node uses this to know what to mutate)
def _write_changes(repo_dir, changes, locked_paths=None):
    locked_paths = locked_paths or []
    written = []
    for path, content in changes.items():
        if path in locked_paths:
            print(f"warning: a change to locked spec test file {path} was ignored -- spec tests stay fixed once written")
            continue
        full_path = os.path.join(repo_dir, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)
        written.append(path)
    return written


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
    mutation_text = _mutation_summary_text(state.get("mutation_stats"))
    mutation_section = f"\n\n{mutation_text}" if mutation_text else ""
    body = f"Automated change for {key}.\n\nTests {status} locally:\n```\n{tail}\n```{mutation_section}"
    pr_url = github_client.open_pr(state["branch"], title, body)
    jira_client.add_comment(key, f"PR opened: {pr_url}\n\nTests {status} locally." + (f" {mutation_text}" if mutation_text else ""))
    jira_client.transition_to(key, "In Review")
    approved, review_text = None, None
    try:
        approved, review_text = await reviewer_agent.review_pr(
            state["ticket"], pr_url, state["test_passed"], tail, mutation_summary=mutation_text or None
        )
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
                # same Human Review column sre_agent.py now uses for this case --
                # one board-wide meaning for "an agent diagnosed/attempted this,
                # a human has to judge it" regardless of which pipeline sent it
                jira_client.transition_to(key, "Human Review")
    return {**state, "pr_url": pr_url}


def build_graph():
    graph = StateGraph(FactoryState)
    graph.add_node("write_tests", write_tests_node)
    graph.add_node("developer", developer_node)
    graph.add_node("mutation_test", mutation_node)
    graph.add_node("open_pr", pr_node)
    graph.set_entry_point("write_tests")
    graph.add_edge("write_tests", "developer")
    graph.add_edge("developer", "mutation_test")
    graph.add_edge("mutation_test", "open_pr")
    graph.add_edge("open_pr", END)
    return graph.compile()
