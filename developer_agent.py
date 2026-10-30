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
# returns path -> new content.
async def generate_changes(ticket, repo_files, design_context=None):
    user_prompt = _build_prompt(ticket, repo_files, design_context)
    reply = await _chat(user_prompt)
    return _parse_file_blocks(reply)


def _build_prompt(ticket, repo_files, design_context):
    files_text = "\n\n".join(f"--- {path} ---\n{content}" for path, content in repo_files.items())
    files_text = files_text or "(repo is empty)"
    design_text = f"Project design notes:\n\n{design_context}\n\n" if design_context else ""
    return (
        f"{design_text}Ticket {ticket['key']}: {ticket['summary']}\n\n{ticket['description']}\n\n"
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
