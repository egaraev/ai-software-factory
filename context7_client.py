import os

import aiohttp

CONTEXT7_BASE = "https://context7.com"
CONTEXT7_API_KEY = os.environ.get("CONTEXT7_API_KEY", "")

# this project's own stack (Python only, FastAPI + Reflex -- see the AI factory
# design notes) -- a ticket only triggers a Context7 lookup if it mentions one
# of these by name, so most tickets skip this entirely. ids are resolved once
# per process and cached here instead of searching Context7 on every ticket
KNOWN_LIBRARIES = {"fastapi": None, "reflex": None, "sqlalchemy": None, "pytest": None}


def _headers():
    return {"Authorization": f"Bearer {CONTEXT7_API_KEY}"} if CONTEXT7_API_KEY else {}


# sends this to Context7:
#   GET https://context7.com/api/v2/libs/search?libraryName=fastapi
# returns e.g. {"results": [{"id": "/tiangolo/fastapi", "title": "FastAPI"}]}
async def resolve_library_id(name):
    url = f"{CONTEXT7_BASE}/api/v2/libs/search"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_headers(), params={"libraryName": name}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            results = data.get("results") or []
            return results[0]["id"] if results else None


# sends this to Context7:
#   GET https://context7.com/api/v2/context?libraryId=/tiangolo/fastapi&query=background tasks&type=json
# returns e.g. {"codeSnippets": [{"codeTitle": "Background tasks", "codeList": [{"code": "..."}]}],
#   "infoSnippets": [{"content": "..."}]}
async def get_library_docs(library_id, query):
    url = f"{CONTEXT7_BASE}/api/v2/context"
    params = {"libraryId": library_id, "query": query, "type": "json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_headers(), params=params) as resp:
            if resp.status != 200:
                return ""
            data = await resp.json()
    parts = []
    for snippet in (data.get("codeSnippets") or [])[:3]:
        code = "\n".join(c.get("code", "") for c in snippet.get("codeList", []))
        parts.append(f"{snippet.get('codeTitle', '')}\n{code}".strip())
    for snippet in (data.get("infoSnippets") or [])[:2]:
        parts.append(snippet.get("content", ""))
    return "\n\n".join(p for p in parts if p)


# ticket is {"key", "summary", "description"} (see jira_client.get_ticket).
# returns a prompt-ready string of relevant library docs, or "" if the ticket
# doesn't mention any library from KNOWN_LIBRARIES (the common case -- most
# tickets don't touch an external library at all)
async def get_docs_for_ticket(ticket):
    text = f"{ticket['summary']} {ticket['description']}".lower()
    matched = [name for name in KNOWN_LIBRARIES if name in text]
    if not matched:
        print("context7: ticket doesn't mention a known library, skipping")
        return ""
    print(f"context7: ticket mentions {', '.join(matched)} -- looking up docs")
    sections = []
    for name in matched:
        try:
            if KNOWN_LIBRARIES[name] is None:
                KNOWN_LIBRARIES[name] = await resolve_library_id(name)
            library_id = KNOWN_LIBRARIES[name]
            if not library_id:
                print(f"context7: couldn't resolve a library id for {name}, skipping")
                continue
            docs = await get_library_docs(library_id, ticket["summary"])
            if docs:
                print(f"context7: got {len(docs)} chars of docs for {name} ({library_id})")
                sections.append(f"--- {name} docs (Context7) ---\n{docs}")
            else:
                print(f"context7: resolved {name} to {library_id} but got no docs back for this query")
        except aiohttp.ClientError as e:  # Context7 being down/slow shouldn't block a ticket -- just skip it
            print(f"warning: Context7 lookup for {name} failed ({e}), skipping")
    return "\n\n".join(sections)
