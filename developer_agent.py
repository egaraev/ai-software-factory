import os

import aiohttp

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm.ai-models.svc.cluster.local:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")

SYSTEM_PROMPT = """You are a software developer. Implement the ticket using the
language, framework, and conventions already present in the repo -- if the repo is
empty, choose a sensible, simple approach based on the ticket and any design notes
given. Make the smallest change that satisfies the ticket. Respond ONLY with one or
more file blocks in this exact format, nothing else:

===FILE: path/to/file===
<full new content of the file>
===END===

Repeat the block for every file you touch. Do not explain your changes outside the blocks."""


# repo_files is path -> current content for every file the model should see.
# ticket is {"key", "summary", "description"}. design_context is the architect
# agent's approved design doc for this project, or None if there isn't one yet.
# test_feedback is the previous attempt's own pytest output, when this is a
# retry after that attempt's tests failed (see graph.py's developer_node) --
# None on a first attempt. review_feedback is a human's "REWORK:" comment on
# the ticket, when this is a rework pass on an already-open PR instead of a
# fresh ticket (see jira_client.get_rework_feedback) -- also None otherwise.
# returns path -> new content.
async def generate_changes(ticket, repo_files, design_context=None, test_feedback=None, review_feedback=None):
    user_prompt = _build_prompt(ticket, repo_files, design_context, test_feedback, review_feedback)
    reply = await _chat(user_prompt)
    changes = _parse_file_blocks(reply)
    if not changes:
        # an empty parse isn't always "nothing to do" -- sometimes the model has
        # real content but explains it in prose/markdown fences instead of the
        # required format (seen on KAN-22: it wrote the right CI job, just not as
        # a ===FILE:===/===END=== block). give it one chance to reformat before
        # falling back to a no-op, so real work doesn't get silently dropped
        reply = await _chat(user_prompt + REFORMAT_REMINDER)
        changes = _parse_file_blocks(reply)
    if not changes:
        # still nothing after the retry -- now it's most likely a genuine no-op
        # (ticket already satisfied by prior work). print the reply so a real
        # parsing failure is still visible, but don't crash: repo_ops/graph
        # already treat an empty changeset as a clean no-op
        print(
            f"warning: model returned no ===FILE:===/===END=== blocks for {ticket['key']} "
            f"even after a reformat retry, treating as no-op. Raw reply:\n\n{reply[:2000]}\n"
        )
    return changes


REFORMAT_REMINDER = (
    "\n\nYour previous reply did not use the required format. Re-send the same "
    "content, but ONLY as one or more blocks in this exact format, nothing else -- "
    "no explanations, no markdown code fences:\n\n"
    "===FILE: path/to/file===\n<full new content of the file>\n===END==="
)


def _build_prompt(ticket, repo_files, design_context, test_feedback=None, review_feedback=None):
    files_text = "\n\n".join(f"--- {path} ---\n{content}" for path, content in repo_files.items())
    files_text = files_text or "(repo is empty)"
    design_text = f"Project design notes:\n\n{design_context}\n\n" if design_context else ""
    # a rework pass -- the current files already reflect the branch's prior PR,
    # and this is a human asking for a specific change on top of it (not a test
    # failure, see feedback_text below for that case)
    review_text = (
        f"A reviewer left this feedback on your previous attempt at this ticket. The current "
        f"files below already reflect that attempt -- address the feedback:\n\n"
        f"{review_feedback}\n\n" if review_feedback else ""
    )
    # the current files below already reflect the previous (failing) attempt,
    # so this just points the model at the real pytest output and asks it to
    # fix the bug -- same idea as REFORMAT_REMINDER but for a wrong answer
    # instead of a malformed one
    feedback_text = (
        f"Your previous attempt at this ticket failed its own tests. The current files below "
        f"already reflect that attempt -- fix the bug. Here is the actual pytest output:\n\n"
        f"{test_feedback}\n\n" if test_feedback else ""
    )
    return (
        f"{design_text}{review_text}{feedback_text}Ticket {ticket['key']}: {ticket['summary']}\n\n{ticket['description']}\n\n"
        f"Current files:\n\n{files_text}"
    )


# sends this to litellm's /v1/chat/completions:
#   {"model": "fx-code", "messages": [{"role": "system", ...}, {"role": "user", ...}]}
# returns e.g. {"choices": [{"message": {"content": "===FILE: app.py===\n...===END==="}}]}
async def _chat(user_prompt):
    url = f"{LITELLM_BASE}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {LITELLM_KEY}"}
    payload = {
        "model": "fx-code",
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


# line-by-line instead of regex -- a regex spanning ===FILE:===...===END=== gets
# confused when a file is meant to be empty (no blank line between the markers,
# e.g. an __init__.py) and swallows the END marker itself as file content
def _parse_file_blocks(reply):
    files = {}
    path = None
    lines = []
    for line in reply.splitlines():
        if line.startswith("===FILE: ") and line.endswith("==="):
            path = line[len("===FILE: "):-len("===")].strip()
            lines = []
        elif line.strip() == "===END===":
            if path:
                files[path] = "\n".join(lines)
                path = None
        elif path is not None:
            lines.append(line)
    return files
