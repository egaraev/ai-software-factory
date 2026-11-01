import os

import aiohttp

import knowledge_client

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm.ai-models.svc.cluster.local:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")

SYSTEM_PROMPT = """You are a test engineer writing the spec for a ticket BEFORE any
implementation exists -- these tests get locked and the implementation has to satisfy
them, not the other way around. Write pytest tests that actually exercise the behavior
described in the ticket: call the real functions/endpoints the ticket asks for, assert
on real expected values. Never write a vacuous test (assert True, a test with no
assertions, or one that only checks something trivially already true) -- a test that
can't fail isn't a test. Use only pytest and whatever test libraries are already used
in the repo (don't introduce a new test framework). Respond ONLY with one or more file
blocks in this exact format, nothing else:

===FILE: path/to/test_file.py===
<full content of the test file>
===END===

Every path must be a real pytest test file -- start with test_ or end with _test.py.
Do not write any non-test file."""


# ticket is {"key", "summary", "description"}. repo_files is path -> current content
# (sparse/empty on a brand-new app). design_context is the architect agent's design
# doc, or None. returns path -> new content, filtered down to test files only. same
# knowledge_client lookup (testing standard, etc.) as developer_agent/architect_agent
async def generate_tests(ticket, repo_files, design_context=None):
    standards_text = await knowledge_client.search(
        f"{ticket['summary']}\n\n{ticket['description']}", exclude_production_only=True
    )
    user_prompt = _build_prompt(ticket, repo_files, design_context, standards_text)
    reply = await _chat(user_prompt)
    changes = _parse_file_blocks(reply)
    if not changes:
        # same reformat-retry idea as developer_agent.py -- the model sometimes has
        # real test content but explains it in prose/markdown fences instead of the
        # required block format
        reply = await _chat(user_prompt + REFORMAT_REMINDER)
        changes = _parse_file_blocks(reply)
    changes = _keep_test_files_only(changes, ticket["key"])
    if not changes:
        print(f"warning: test_writer_agent produced no usable test files for {ticket['key']} -- proceeding without locked spec tests")
    return changes


REFORMAT_REMINDER = (
    "\n\nYour previous reply did not use the required format. Re-send the same "
    "content, but ONLY as one or more blocks in this exact format, nothing else -- "
    "no explanations, no markdown code fences:\n\n"
    "===FILE: path/to/test_file.py===\n<full content of the test file>\n===END==="
)


def _build_prompt(ticket, repo_files, design_context, standards_text=None):
    files_text = "\n\n".join(f"--- {path} ---\n{content}" for path, content in repo_files.items())
    files_text = files_text or "(repo is empty)"
    design_text = f"Project design notes:\n\n{design_context}\n\n" if design_context else ""
    standards_section = f"Relevant internal engineering standards:\n\n{standards_text}\n\n" if standards_text else ""
    return (
        f"{design_text}{standards_section}Ticket {ticket['key']}: {ticket['summary']}\n\n{ticket['description']}\n\n"
        f"Current repo files:\n\n{files_text}"
    )


# drops anything that doesn't look like a real pytest test file -- the system prompt
# asks for test-only output, but nothing stops the model from writing an
# implementation file anyway (developer_agent.py hits the same class of format
# slip), and a non-test file here would get "locked" by write_tests_node and
# silently block the developer agent from ever touching it
def _keep_test_files_only(changes, ticket_key):
    kept = {}
    for path, content in changes.items():
        name = os.path.basename(path)
        is_test_name = name.startswith("test_") or name.endswith("_test.py")
        if is_test_name and path.endswith(".py"):
            kept[path] = content
        else:
            print(f"warning: test_writer_agent wrote a non-test path ({path}) for {ticket_key} -- dropping it, not locking it")
    return kept


# sends this to litellm's /v1/chat/completions:
#   {"model": "fx-code", "messages": [{"role": "system", ...}, {"role": "user", ...}]}
# returns e.g. {"choices": [{"message": {"content": "===FILE: test_app.py===\n...===END==="}}]}
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


# same line-by-line parser as developer_agent.py -- duplicated rather than
# imported, matching this codebase's existing per-agent style (each agent file
# already duplicates its own _chat/LITELLM_BASE/LITELLM_KEY too)
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
