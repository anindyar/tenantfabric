"""End-to-end checks for tenantfabric, run inside the memsvc container (it already has the MCP SDK,
httpx, psycopg, the memory volume and the gateway URL):

    docker compose exec -T memsvc python - < tests/e2e.py
    kubectl -n tenantfabric exec -i deploy/memsvc -- python - < tests/e2e.py

Wipes the demo teams' memory (alpha, beta) first, so it can be re-run. Exit code 0 = all passed."""
import asyncio, logging, os, re, shutil, sys
import httpx, psycopg
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

GW = os.environ.get("GATEWAY_URL", os.environ.get("MEM_GATEWAY", "http://agentgateway:8080"))
DB = os.environ["MEM_DB"]                       # memsvc's own role: RLS limits it to one tenant at a time
ROOT = os.environ.get("MEM_ROOT", "/srv/memory")
ALPHA, BETA = "sk-alpha-demo", "sk-beta-demo"
passed = failed = 0
logging.disable(logging.WARNING)       # the MCP SDK logs a harmless warning when the gateway closes a session


def result(ok, name, got=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}\n      got: {str(got)[:300]}")


def check(name, want, got):   # output contains
    result(want in got, name, got)


def absent(name, bad, got):   # output does not contain
    result(bad not in got, name, got)


def http(method, path, key=None, body=None):
    h = {"Authorization": f"Bearer {key}"} if key else {}
    r = httpx.request(method, GW + path, headers=h, json=body, timeout=180)
    return f"{r.text} HTTP {r.status_code}"


def chat(key, model):
    return http("POST", "/v1/chat/completions", key,
                {"model": model, "max_tokens": 20, "messages": [{"role": "user", "content": "Reply with the word ok"}]})


def mcp(key, cmd, tool=None, args=None, extra_headers=None):
    """list | call. Returns text; gateway rejections come back as 'DENIED ...'."""
    async def run():
        headers = {"Authorization": f"Bearer {key}", **(extra_headers or {})}
        async with streamablehttp_client(GW + "/mcp", headers=headers) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                if cmd == "list":
                    return "\n".join(t.name for t in (await s.list_tools()).tools)
                res = await s.call_tool(tool, args or {})
                return ("DENIED " if res.isError else "") + "\n".join(getattr(c, "text", str(c)) for c in res.content)
    try:
        return asyncio.run(run())
    except BaseException as e:
        while isinstance(e, BaseExceptionGroup):
            e = e.exceptions[0]
        resp = getattr(e, "response", None)
        return f"DENIED by gateway: HTTP {resp.status_code}" if resp is not None else f"ERROR: {e!r}"


def sql(tenant, q):
    with psycopg.connect(DB) as c:
        c.execute("select set_config('app.tenant', %s, false)", (tenant,))
        return "\n".join("|".join("" if v is None else str(v) for v in row) for row in c.execute(q).fetchall())


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return "none"


# clean start
for t in ("alpha", "beta"):
    sql(t, "delete from memory returning id")
    shutil.rmtree(f"{ROOT}/{t}", ignore_errors=True)
alpha_md = f"{ROOT}/alpha/MEMORY.md"

print("== 1. Identity")
check("no key -> 401", "HTTP 401", http("GET", "/v1/models"))
check("unknown key -> 401 on MCP", "HTTP 401", mcp("sk-nope", "list"))

print("== 2. Inference plane")
models = http("GET", "/v1/models", ALPHA)
check("alpha model list has chat-general", '"chat-general"', models)
absent("alpha model list hides chat-vision", "chat-vision", models)
check("alpha -> chat-general allowed", "HTTP 200", chat(ALPHA, "chat-general"))
check("alpha -> chat-vision denied", "HTTP 403", chat(ALPHA, "chat-vision"))
check("beta  -> chat-vision allowed", "HTTP 200", chat(BETA, "chat-vision"))
check("beta  -> chat-general denied", "HTTP 403", chat(BETA, "chat-general"))
check("embeddings allowed for both teams", "HTTP 200", http("POST", "/v1/embeddings", BETA, {"model": "embed", "input": "test"}))

print("== 3. Tool plane (MCP)")
check("alpha sees memory_forget", "memory_forget", mcp(ALPHA, "list"))
absent("beta does not see memory_forget", "memory_forget", mcp(BETA, "list"))
check("beta call memory_forget denied", "DENIED", mcp(BETA, "call", "memory_forget", {"id": 1}))

print("== 4. Memory, md mode (alpha): markdown is the source of truth")
check("alpha writes a memory", "saved to alpha/MEMORY.md (md)",
      mcp(ALPHA, "call", "memory_write", {"text": "The staging cluster is patched every Sunday 02:00-04:00 UTC; expect API downtime"}))
check("alpha finds it by meaning", "staging cluster",
      mcp(ALPHA, "call", "memory_search", {"query": "when is the maintenance window for staging?", "k": 1}))
check("it is a line in alpha/MEMORY.md", "staging cluster", read(alpha_md))
with open(alpha_md, "a") as f:                      # a human edits the file directly
    f.write("- On-call handover is every Monday at 09:00 in the #ops-oncall channel\n")
check("hand edit + memory_reindex", "reindexed", mcp(ALPHA, "call", "memory_reindex", {}))
check("hand-edited fact is searchable", "handover",
      mcp(ALPHA, "call", "memory_search", {"query": "what time is the on-call handover", "k": 1}))

print("== 5. Memory, pg mode (beta): pgvector is the store, no files")
check("beta writes a memory", "(pg)",
      mcp(BETA, "call", "memory_write", {"text": "Purchase orders above 50,000 USD need CFO approval before release", "tags": ["finance", "policy"]}))
check("beta finds it by meaning", "CFO approval",
      mcp(BETA, "call", "memory_search", {"query": "who signs off on large purchases?", "k": 1}))
check("no markdown file exists for beta", "none", read(f"{ROOT}/beta/MEMORY.md"))
check("it is a row in pgvector (source=pg)", "beta|pg|",
      sql("beta", "select tenant, source, path from memory where content like 'Purchase orders%'"))

print("== 6. Isolation between teams")
absent("beta cannot find alpha's memory", "staging",
       mcp(BETA, "call", "memory_search", {"query": "staging maintenance window", "k": 5}))
absent("alpha cannot find beta's memory", "CFO",
       mcp(ALPHA, "call", "memory_search", {"query": "purchase approval", "k": 5}))
spoof = mcp(BETA, "call", "memory_search", {"query": "staging maintenance window"}, {"x-team": "alpha"})
absent("beta spoofing X-Team: alpha gets nothing of alpha's", "staging", spoof)
check("... and gets its own memory instead", "CFO", spoof)

print("== 7. Delete path (alpha, md mode)")
hits = mcp(ALPHA, "call", "memory_search", {"query": "staging maintenance window", "k": 1})
m = re.search(r'"id":\s*(\d+)', hits)
mem_id = int(m.group(1)) if m else 0
check("memory_forget removes the line", "removed alpha/MEMORY.md", mcp(ALPHA, "call", "memory_forget", {"id": mem_id}))
absent("line is gone from the markdown file", "staging cluster", read(alpha_md))
absent("and from the index", "staging cluster", sql("alpha", "select content from memory"))

print(f"\nRESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
