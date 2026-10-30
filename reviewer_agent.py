import os

import aiohttp

import github_client

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm.ai-models.svc.cluster.local:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")

SYSTEM_PROMPT = """You are reviewing a pull request before a human decides whether to merge it.
Check the diff against the ticket's requirements: does it do what was asked, are there obvious
bugs, missing tests, or anything that looks unsafe. Be concise -- a few sentences, not an essay.
End with one line, exactly "VERDICT: APPROVE" or "VERDICT: CONCERNS", nothing else on that line."""


# ticket is {"key","summary","description"}. pr_url is the opened PR's html_url.
# fetches the PR's diff from github, asks fx-verify (a different model family
# than fx-code/fx-review, kept independent on purpose) to review it. if a second
# collaborator account's token is configured, posts a real github review (Approve/
# Request changes) under that account, which is what can actually satisfy a branch
# protection rule -- otherwise falls back to a plain comment under the main
# account. either way, merging the PR itself always stays a human decision
async def review_pr(ticket, pr_url):
    pr_number = _pr_number(pr_url)
    diff = github_client.get_pr_diff(pr_number)
    reply = await _chat(_build_prompt(ticket, diff))
    body = f"**AI review (fx-verify):**\n\n{reply}"
    if github_client.REVIEWER_GITHUB_TOKEN:
        event = "APPROVE" if "VERDICT: APPROVE" in reply else "REQUEST_CHANGES"
        github_client.submit_pr_review(pr_number, event, body)
    else:
        github_client.post_pr_comment(pr_number, body)
    return reply


def _build_prompt(ticket, diff):
    return f"Ticket {ticket['key']}: {ticket['summary']}\n\n{ticket['description']}\n\nDiff:\n\n{diff}"


def _pr_number(pr_url):
    return int(pr_url.rstrip("/").rsplit("/", 1)[-1])


# sends this to litellm's /v1/chat/completions:
#   {"model": "fx-verify", "messages": [{"role": "system", ...}, {"role": "user", ...}]}
# returns e.g. {"choices": [{"message": {"content": "Looks correct...\n\nVERDICT: APPROVE"}}]}
async def _chat(user_prompt):
    url = f"{LITELLM_BASE}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {LITELLM_KEY}"}
    payload = {
        "model": "fx-verify",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
            data = await resp.json()
            return data["choices"][0]["message"]["content"]
