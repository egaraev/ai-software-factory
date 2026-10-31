import os

import aiohttp

import github_client

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm.ai-models.svc.cluster.local:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")

SYSTEM_PROMPT = """You are reviewing a pull request before a human decides whether to merge it.
Check the diff against the ticket's requirements: does it do what was asked, are there obvious
bugs, missing tests, or anything that looks unsafe. You're also given the local test run's
result -- failing tests always mean CONCERNS, never APPROVE. Be concise -- a few sentences, not
an essay. End with one line, exactly "VERDICT: APPROVE" or "VERDICT: CONCERNS", nothing else on
that line."""

# used for the sre-agent's fix PRs instead of SYSTEM_PROMPT above -- there's no test
# run to weigh here (these are manifest fixes, not code with a pytest suite), but
# there IS a specific failure mode worth watching for: an agent fixing an RBAC error
# by widening the grant rather than narrowing what the workload actually needs. this
# reviewer runs under fx-review (Sonnet, via OpenRouter/LiteLLM) -- a different model
# family than fx-verify, which both diagnosed the problem and wrote this fix, so a
# second opinion isn't just asking the same model to grade its own homework
FIX_REVIEW_SYSTEM_PROMPT = """You are reviewing a Kubernetes manifest fix another AI agent just
proposed, before a human decides whether to merge it. You cannot merge or reject it yourself --
only leave your assessment as a PR comment. You're given that agent's own diagnosis (why it
thinks this is broken) and the diff it opened. Check: does the diff actually fix the stated root
cause, is it the smallest change that does so, and -- this matters most -- does it follow least
privilege. Don't wave through a fix that grants broader access than the root cause requires,
especially read access to Secrets or other sensitive resources cluster-wide, when a narrower fix
(e.g. limiting what the workload tries to collect/watch in the first place) would have worked
just as well. Flag anything in the diff that looks unrelated to the stated problem. Be concise --
a few sentences, not an essay. End with one line, exactly "VERDICT: APPROVE" or "VERDICT:
CONCERNS", nothing else on that line."""


# ticket is {"key","summary","description"}. pr_url is the opened PR's html_url.
# tests_passed/test_output are the same local test results already posted to the
# PR body -- without these the reviewer has no way to know tests failed and can
# approve a broken PR purely from how the diff looks (seen on KAN-25: it approved
# a change that failed 18 tests, since it only ever saw the diff before this fix)
# fetches the PR's diff from github, asks fx-verify (a different model family
# than fx-code/fx-review, kept independent on purpose) to review it. if a second
# collaborator account's token is configured, posts a real github review (Approve/
# Request changes) under that account, which is what can actually satisfy a branch
# protection rule -- otherwise falls back to a plain comment under the main
# account. returns (approved, reply) -- graph.py uses approved to decide
# whether to auto-merge or auto-rework the PR
async def review_pr(ticket, pr_url, tests_passed, test_output):
    pr_number = github_client.pr_number_from_url(pr_url)
    diff = github_client.get_pr_diff(pr_number)
    reply = await _chat(SYSTEM_PROMPT, _build_prompt(ticket, diff, tests_passed, test_output))
    # failing tests always block approval no matter what the model's verdict line
    # says -- a deterministic floor under the model's judgment. fx-verify doesn't
    # reliably follow the "failing tests = CONCERNS" system prompt instruction on
    # its own (seen on KAN-25: it approved right under a comment saying tests
    # FAILED), so rewrite its own verdict line too -- not just the github review
    # event below -- so a misleading "VERDICT: APPROVE" can never get posted,
    # whether or not a second reviewer account is configured
    approved = tests_passed and "VERDICT: APPROVE" in reply
    if not approved and "VERDICT: APPROVE" in reply:
        reply = reply.replace("VERDICT: APPROVE", "VERDICT: CONCERNS (overridden -- local tests failed)")
    body = f"**AI review (fx-verify):**\n\n{reply}"
    if github_client.REVIEWER_GITHUB_TOKEN:
        github_client.submit_pr_review(pr_number, "APPROVE" if approved else "REQUEST_CHANGES", body)
    else:
        github_client.post_pr_comment(pr_number, body)
    return approved, reply


def _build_prompt(ticket, diff, tests_passed, test_output):
    status = "passed" if tests_passed else "FAILED"
    tests_text = f"Local test run: {status}\n```\n{test_output}\n```\n\n"
    return f"Ticket {ticket['key']}: {ticket['summary']}\n\n{ticket['description']}\n\n{tests_text}Diff:\n\n{diff}"


# used for the sre-agent's fix PRs -- ticket_key/summary/diagnosis describe the ops
# ticket and the sre-agent's own diagnosis text (see sre_agent.py), pr_url is the
# fix PR it opened, repo is the manifests repo (not the sample app) that PR lives in.
# ALWAYS posts a plain comment, never a formal github review -- this is advisory
# only, on purpose: nothing here can approve, request changes on, or merge the PR,
# whatever the model's verdict says. a human reads this plus the sre-agent's own
# diagnosis and decides whether to merge. returns (approved, reply) so a caller can
# log the verdict, but neither sre_agent.py nor this function ever acts on it
async def review_fix_pr(ticket_key, summary, diagnosis, pr_url, repo):
    pr_number = github_client.pr_number_from_url(pr_url)
    diff = github_client.get_pr_diff(pr_number, repo=repo)
    reply = await _chat(FIX_REVIEW_SYSTEM_PROMPT, _build_fix_review_prompt(ticket_key, summary, diagnosis, diff), model="fx-review")
    approved = "VERDICT: APPROVE" in reply
    github_client.post_pr_comment(pr_number, f"**AI review (fx-review):**\n\n{reply}", repo=repo)
    return approved, reply


def _build_fix_review_prompt(ticket_key, summary, diagnosis, diff):
    return f"Ticket {ticket_key}: {summary}\n\nThe sre-agent's own diagnosis:\n{diagnosis}\n\nDiff:\n\n{diff}"


# sends this to litellm's /v1/chat/completions:
#   {"model": "fx-verify", "messages": [{"role": "system", ...}, {"role": "user", ...}]}
# returns e.g. {"choices": [{"message": {"content": "Looks correct...\n\nVERDICT: APPROVE"}}]}
async def _chat(system_prompt, user_prompt, model="fx-verify"):
    url = f"{LITELLM_BASE}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {LITELLM_KEY}"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
            data = await resp.json()
            return data["choices"][0]["message"]["content"]
