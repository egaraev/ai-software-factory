import os
import re

import aiohttp

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm.ai-models.svc.cluster.local:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")

SYSTEM_PROMPT = """You are a software architect and project manager at a Python-only
shop -- every app this factory builds is implemented in Python, so the language is
fixed. You'll be given a set of business requirements and the current contents of the
repo. If the repo already has code in it, design on top of what's already there --
keep using its existing framework, structure, and conventions rather than proposing a
new stack, and make clear in the design which parts are new versus already built. If
the repo is empty, choose a sensible, simple approach based on the requirements.

Produce:
1. A short design: overall approach, key components, and the Python framework/
   libraries chosen (e.g. FastAPI, Flask, Django, plain stdlib) -- justified by the
   requirements.
2. A list of discrete, independently implementable tasks a developer could pick up
   one at a time, each with a clear scope and a done condition.

Respond ONLY in this exact format, nothing else:

===DESIGN===
<design text, markdown>
===END===

===TASK===
Title: <short title>
Description: <what to build and the done condition>
===END===

(repeat the TASK block for every task, ordered sensibly -- e.g. data model and
backend before the features that depend on them)"""


# epic is {"key", "summary", "description"} from jira_client.get_ticket.
# repo_files is path -> content for the app's current repo (from
# repo_ops.read_repo_files), or None/empty for a repo that doesn't exist yet.
# returns (design_text, [{"title", "description"}, ...])
async def generate_plan(epic, repo_files=None):
    user_prompt = _build_prompt(epic, repo_files)
    reply = await _chat(user_prompt)
    return _parse_plan(reply)


def _build_prompt(epic, repo_files):
    files_text = "\n\n".join(f"--- {path} ---\n{content}" for path, content in (repo_files or {}).items())
    files_text = files_text or "(repo is empty)"
    return (
        f"Business requirements ({epic['key']}: {epic['summary']}):\n\n{epic['description']}\n\n"
        f"Current repo files:\n\n{files_text}"
    )


# sends this to litellm's /v1/chat/completions:
#   {"model": "fx-review", "messages": [{"role": "system", ...}, {"role": "user", ...}]}
# returns e.g. {"choices": [{"message": {"content": "===DESIGN===\n...\n===TASK===\n..."}}]}
async def _chat(user_prompt):
    url = f"{LITELLM_BASE}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {LITELLM_KEY}"}
    payload = {
        "model": "fx-review",
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


def _parse_plan(reply):
    design_match = re.search(r"===DESIGN===\n(.*?)\n===END===", reply, re.DOTALL)
    design = design_match.group(1).strip() if design_match else ""

    tasks = []
    for block in re.findall(r"===TASK===\n(.*?)\n===END===", reply, re.DOTALL):
        title_match = re.search(r"Title:\s*(.+)", block)
        desc_match = re.search(r"Description:\s*(.+)", block, re.DOTALL)
        if title_match and desc_match:
            tasks.append({"title": title_match.group(1).strip(), "description": desc_match.group(1).strip()})
    return design, tasks
