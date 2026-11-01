import os

import aiohttp

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm.ai-models.svc.cluster.local:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")
QDRANT_BASE = os.environ.get("QDRANT_BASE", "http://qdrant.knowledge.svc.cluster.local:6333")
EMBED_MODEL = "nomic-embed-text-v1.5"
COLLECTION_NAME = "factory_standards"


# sends this to litellm's OpenAI-compatible embeddings endpoint:
#   {"model": "nomic-embed-text-v1.5", "input": ["search_query: add a background job that retries failed webhook deliveries"]}
# returns e.g. {"data": [{"index": 0, "embedding": [0.01, ...]}]}
async def _embed(query_text):
    url = f"{LITELLM_BASE}/v1/embeddings"
    headers = {"Authorization": f"Bearer {LITELLM_KEY}"}
    payload = {"model": EMBED_MODEL, "input": [f"search_query: {query_text[:2000]}"]}  # bounded -- a full ticket/diff can run long
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                # the generic "embedding request failed" message in search() below
                # doesn't say why -- print the real status + body here so the next
                # failure is diagnosable straight from the job log, not another
                # round of guessing/reproducing
                body = await resp.text()
                print(f"knowledge base: embedding request got HTTP {resp.status} -- {body[:300]}")
                return None
            data = await resp.json()
            return data["data"][0]["embedding"]


# sends this to qdrant's search endpoint:
#   {"vector": [0.01, ...], "limit": 4, "with_payload": true}
# returns e.g. {"result": [{"score": 0.81, "payload": {"source": "python-style.md",
#   "text": "...", "title": "...", "homelab_relevance": "core"}}]}
async def _search_qdrant(vector, top_k, exclude_production_only):
    url = f"{QDRANT_BASE}/collections/{COLLECTION_NAME}/points/search"
    body = {"vector": vector, "limit": top_k, "with_payload": True}
    if exclude_production_only:
        body["filter"] = {"must_not": [{"key": "homelab_relevance", "match": {"value": "production_only"}}]}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("result") or []


def _format_points(points):
    blocks = []
    for point in points:
        payload = point["payload"]
        title = payload.get("title") or payload.get("source", "untitled")
        relevance = payload.get("homelab_relevance")
        tag = f" [{relevance}]" if relevance else ""
        blocks.append(f"### {title}{tag}\n{payload['text']}")
    return "\n\n".join(blocks)


# query_text is normally raw ticket/epic/diff text, not a hand-crafted search
# query -- same "just embed what you already have" approach as
# context7_client.get_docs_for_ticket. exclude_production_only=True drops any
# chunk whose homelab_relevance is production_only, for callers building
# day-to-day app code rather than hardening something for production. returns
# a prompt-ready text block, or "" if nothing matched or litellm/qdrant
# couldn't be reached (same "skip it, don't block the ticket" fallback as
# context7_client.get_docs_for_ticket)
async def search(query_text, top_k=4, exclude_production_only=False):
    try:
        vector = await _embed(query_text)
        if not vector:
            print("knowledge base: embedding request failed (litellm unreachable, or embedding model not returning), skipping")
            return ""
        points = await _search_qdrant(vector, top_k, exclude_production_only)
    except aiohttp.ClientError as e:  # qdrant/litellm being down/slow shouldn't block a ticket -- just skip it
        print(f"warning: knowledge base lookup failed ({e}), skipping")
        return ""
    # one line either way -- this agent's own job log is the only place to see
    # whether the knowledge base actually matched anything for a given ticket,
    # since a silent "" (no match vs. a real outage) looked identical before this
    if points:
        titles = ", ".join(p["payload"].get("title") or p["payload"].get("source", "untitled") for p in points)
        print(f"knowledge base: matched {len(points)} chunk(s) -- {titles}")
    else:
        print("knowledge base: no matches for this query")
    return _format_points(points)
