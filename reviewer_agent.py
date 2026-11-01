import os

import aiohttp

import github_client
import knowledge_client

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm.ai-models.svc.cluster.local:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")

SYSTEM_PROMPT = """You are reviewing a pull request before a human decides whether to merge it.
Check the diff against the ticket's requirements: does it do what was asked, are there obvious
bugs, missing tests, or anything that looks unsafe. You're also given the local test run's
result -- failing tests always mean CONCERNS, never APPROVE. If a mutation-testing summary is
included, it's telling you whether the ticket's own spec tests actually catch bugs in the new
code, not just that they pass -- weigh a lot of surviving mutants as a sign the tests are too
weak to trust, but this is advisory context for your judgment, not a separate hard gate on its
own. Be concise -- a few sentences, not an essay. End with one line, exactly "VERDICT: APPROVE"
or "VERDICT: CONCERNS", nothing else on that line."""

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
just as well. Flag anything in the diff that looks unrelated to the stated problem. When you have
a specific fix in mind, state the literal change -- the exact flag/arg/line, not a description of
the category of fix -- since your feedback goes straight to another model that has to turn it
into a real file block, not a human who can fill in the details themselves. Be concise -- a few
sentences, not an essay. End with one line, exactly "VERDICT: APPROVE" or "VERDICT: CONCERNS",
nothing else on that line."""


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
# account. mutation_summary is graph.py's _mutation_summary_text output, or None
# when mutation testing was skipped/inconclusive for this ticket -- advisory
# context only, see SYSTEM_PROMPT. returns (approved, reply) -- graph.py uses
# approved to decide whether to auto-merge or auto-rework the PR
async def review_pr(ticket, pr_url, tests_passed, test_output, mutation_summary=None):
    pr_number = github_client.pr_number_from_url(pr_url)
    diff = github_client.get_pr_diff(pr_number)
    # internal standards relevant to this diff (code review standard, python
    # standards, testing standard, etc.), fetched here in code the same way
    # developer_agent.py/architect_agent.py do it. production_only standards
    # are filtered out -- most PRs here are day-to-day homelab app code
    standards_text = await knowledge_client.search(f"{ticket['summary']}\n\n{diff}", exclude_production_only=True)
    reply = await _chat(SYSTEM_PROMPT, _build_prompt(ticket, diff, tests_passed, test_output, standards_text, mutation_summary))
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


def _build_prompt(ticket, diff, tests_passed, test_output, standards_text=None, mutation_summary=None):
    status = "passed" if tests_passed else "FAILED"
    tests_text = f"Local test run: {status}\n```\n{test_output}\n```\n\n"
    mutation_text = f"{mutation_summary}\n\n" if mutation_summary else ""
    standards_section = f"Relevant internal engineering standards:\n\n{standards_text}\n\n" if standards_text else ""
    return f"{standards_section}Ticket {ticket['key']}: {ticket['summary']}\n\n{ticket['description']}\n\n{tests_text}{mutation_text}Diff:\n\n{diff}"


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
    # internal standards relevant to this fix -- RBAC/secrets policy, k8s
    # troubleshooting runbook, etc. NOT filtered for production_only: an RBAC
    # least-privilege check is exactly the kind of thing worth seeing in full
    # even on a homelab, since this reviewer's whole job here is catching an
    # overly-broad grant
    standards_text = await knowledge_client.search(f"{summary}\n\n{diagnosis}\n\n{diff}")
    reply = await _chat(
        FIX_REVIEW_SYSTEM_PROMPT,
        _build_fix_review_prompt(ticket_key, summary, diagnosis, diff, standards_text),
        model="fx-review",
    )
    approved = "VERDICT: APPROVE" in reply
    github_client.post_pr_comment(pr_number, f"**AI review (fx-review):**\n\n{reply}", repo=repo)
    return approved, reply


def _build_fix_review_prompt(ticket_key, summary, diagnosis, diff, standards_text=None):
    standards_section = f"Relevant internal engineering standards:\n\n{standards_text}\n\n" if standards_text else ""
    return f"{standards_section}Ticket {ticket_key}: {summary}\n\nThe sre-agent's own diagnosis:\n{diagnosis}\n\nDiff:\n\n{diff}"


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
