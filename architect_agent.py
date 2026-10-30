import os
import re

import aiohttp

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm.ai-models.svc.cluster.local:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")

SYSTEM_PROMPT = """You are a software architect and project manager. Given a set of
business requirements, produce:
1. A short design: overall approach, key components, and technology choices --
   justified by the requirements, not assumed up front. The app can be built with
   any stack that fits the requirements well.
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
# returns (design_text, [{"title", "description"}, ...])
async def generate_plan(epic):
    user_prompt = f"Business requirements ({epic['key']}: {epic['summary']}):\n\n{epic['description']}"
    reply = await _chat(user_prompt)
    return _parse_plan(reply)


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
