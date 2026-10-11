# tenantfabric

**A multi-team AI platform in one file.** Give every dev team one API key. That key decides which models the team can call, which MCP tools it can use and whose memory it can read. Deploy it with one Docker Compose file or one Kubernetes manifest.

| Plane | What a team gets | Enforced by |
|---|---|---|
| Inference | only its own models. Other models return 403 and don't appear in `/v1/models` | [agentgateway](https://github.com/agentgateway/agentgateway): a per-model rule on `apiKey.team` |
| Tools (MCP) | only its own tools. Other tools are missing from `tools/list`, and calls to them are rejected | agentgateway `mcpAuthorization` on `apiKey.team` + `mcp.tool.name` |
| Memory | only its own memory, even if the client lies about who it is | [memsvc](https://github.com/anindyar/memsvc): the gateway stamps the team from the key, and Postgres row-level security enforces it |

Everything is open source: agentgateway, Postgres + pgvector, Ollama for the example models, and memsvc for memory. It runs on CPU as-is, amd64 or arm64. Point the model aliases at your own GPU servers or a cloud API when you're ready.

---

## Quick start

### Docker Compose

```bash
git clone https://github.com/anindyar/tenantfabric && cd tenantfabric
docker compose up -d                                       # first start pulls ~3 GB of example models
docker compose logs -f models                              # wait until it exits (models pulled)
docker compose exec -T memsvc python - < tests/e2e.py      # 28 end-to-end checks
```

The gateway UI is at http://localhost:8080/ui. Set `GATEWAY_PORT=9090` to use another port. Run `docker compose down -v` to remove everything.

### Kubernetes

```bash
kubectl apply -f k8s/tenantfabric.yaml
kubectl -n tenantfabric wait --for=condition=complete job/models --timeout=30m
kubectl -n tenantfabric exec -i deploy/memsvc -- python - < tests/e2e.py
kubectl -n tenantfabric port-forward svc/agentgateway 8080:8080   # UI at http://localhost:8080/ui
```

You need a default StorageClass (4 PVCs, about 17 GiB). The NetworkPolicies only take effect if your CNI enforces them (Calico, Cilium, kindnet and others). Run `kubectl delete namespace tenantfabric` to remove everything.

### With an AI coding assistant

Give your assistant (Claude Code, Cursor, Copilot, Codex, ...) the repo link and a goal, for example:

> Clone https://github.com/anindyar/tenantfabric, read CLAUDE.md, and deploy it on my Kubernetes cluster. Point `chat-general` at our vLLM server at http://vllm.ai.svc:8000 serving `Qwen/Qwen3-32B`. Add a team called `data` with access to `chat-general` and all memory tools, in pg memory mode. Run the e2e test when done.

[`CLAUDE.md`](CLAUDE.md) (also available as `AGENTS.md`) gives the assistant the full context: architecture, the security invariants it must keep, and step-by-step recipes.

---

## Architecture

```
 dev team (one API key)
     │  OpenAI SDK / curl                     MCP client (Claude Code, Cursor, agent SDK)
     ▼                                               ▼
 ┌──────────────────────── agentgateway :8080 ─────────────────────────┐
 │ API key → metadata {team: alpha}                                     │
 │ /v1/chat/completions, /v1/models → per-model rule on apiKey.team     │
 │ /v1/embeddings                   → model "embed"                     │
 │ /mcp                             → mcpAuthorization per tool         │
 │                                    + sets header X-Team = apiKey.team │
 │ /ui                              → admin UI, request logs            │
 └───────┬─────────────────────────────────────────┬───────────────────┘
         ▼                                         ▼
   ollama :11434 (example)                  memsvc :8041 (gateway-only)
   chat-general → qwen2.5:0.5b              5 memory_* MCP tools
   chat-vision  → moondream                 md mode: /srv/memory/<team>/*.md + index rows
   embed        → nomic-embed-text          pg mode: rows only
         ▲                                         │
         └──── memsvc embeds through the gateway ──┤
                                                   ▼
                                     postgres + pgvector :5432
                                     memory table with RLS per team, gateway request logs
```

Only the gateway is exposed. memsvc trusts the `X-Team` header, so nothing except the gateway may reach it. On Kubernetes a NetworkPolicy enforces this. On Compose, only the gateway port is published.

### Example teams

| | alpha (e.g. platform engineering) | beta (e.g. finance ops) |
|---|---|---|
| API key | `sk-alpha-demo` | `sk-beta-demo` |
| Models | `chat-general`, `embed` | `chat-vision`, `embed` |
| Memory mode | `md`: markdown files are the truth, pgvector is the index | `pg`: pgvector rows only, no files |
| Memory tools | all five | `memory_write`, `memory_search`, `memory_get` (cannot delete or reindex) |

### Model aliases

Teams call stable names. The gateway maps each one to a backend, so swapping a backend is one line in the gateway config and clients don't change.

| Alias | Example backend (CPU, small) | Replace with |
|---|---|---|
| `chat-general` | `qwen2.5:0.5b` on Ollama | any OpenAI-compatible chat model (vLLM, TGI, llama.cpp, a cloud API) |
| `chat-vision` | `moondream` on Ollama | a vision-language model |
| `embed` | `nomic-embed-text` on Ollama (768-dim) | any embedder. **The vector size in the schema must match** |

---

## Walkthrough

**Models and aliases.** Each alias has one authorization rule:

![LLM models](docs/img/01_llm_models.png)

![model authorization rule](docs/img/08_model_authz.png)

**Team keys.** The same keys also scope the MCP tools:

![virtual API keys](docs/img/02_llm_keys.png)

**MCP: one memory target, per-team tool rules, and the `X-Team` stamp:**

![MCP servers](docs/img/04_mcp_servers.png)

![MCP policies](docs/img/05_mcp_policies.png)

**Memory in both modes, in one table.** alpha's rows point back to lines in its markdown file. beta's rows are the memory itself:

![memory in two modes](docs/img/11_memory_two_modes.png)

**End-to-end test.** It covers identity, model scoping, tool scoping, both memory modes, isolation including a forged team header, and the delete path:

![e2e result](docs/img/10_e2e_result.png)

**Audit trail.** Every model call is logged in the gateway UI:

![LLM logs](docs/img/06_llm_logs.png)

---

## Using it as a team

```bash
# chat
curl http://localhost:8080/v1/chat/completions -H "Authorization: Bearer sk-alpha-demo" \
  -H 'content-type: application/json' -d '{"model":"chat-general","messages":[{"role":"user","content":"hi"}]}'
```

For memory, add the gateway as an MCP server in your client: URL `http://<gateway>:8080/mcp` (streamable HTTP), header `Authorization: Bearer <team key>`. For example, with Claude Code:

```bash
claude mcp add --transport http team-memory http://localhost:8080/mcp --header "Authorization: Bearer sk-alpha-demo"
```

| Tool | md mode | pg mode |
|---|---|---|
| `memory_write(text, tags?, file?)` | appends a dated line to `<team>/<file>` (default `MEMORY.md`) | inserts a row with tags |
| `memory_search(query, k?)` | semantic search. Hits carry `id`, and `path`/`line` for md | same |
| `memory_get(id?, file?)` | one item, or the whole markdown file | one item, or the 20 newest |
| `memory_forget(id)` | removes the line from the file and the index. Refuses if the file was edited since indexing | deletes the row |
| `memory_reindex()` | rebuilds the index from the files (after hand edits) | re-embeds every row (after an embedder change) |

---

## Customizing

All of these are covered step by step in [`CLAUDE.md`](CLAUDE.md):
- **Add a team:** add a key, add the team to the model rules and the MCP rules, and set its memory mode in `MEM_MODES`.
- **Swap a model backend:** edit `params.baseUrl` / `params.model` for the alias.
- **Use your own Postgres:** run the schema, then point `MEM_DB` and the gateway `database.url` at it.
- **Production:** replace the demo keys with JWT/OIDC (rules then read `jwt.team`), move secrets to a secret manager, add TLS/ingress, set per-key token budgets, and pin image digests.

## Why memsvc for memory

The usual open-source memory servers ([Graphiti MCP](https://github.com/getzep/graphiti/tree/main/mcp_server), [mem0](https://github.com/mem0ai/mem0)) take the tenant (`group_id`, `user_id`) as an argument **from the client**. On a platform shared by many teams, any team could pass another team's id and read its memory. memsvc has no tenant argument at all. It uses the team the gateway derived from the API key, and Postgres row-level security enforces it. See the [memsvc README](https://github.com/anindyar/memsvc#why-this-exists) for the source-level comparison.

## Known limits

- The demo keys and passwords are in plain text in the manifests. Replace them before exposing the stack anywhere.
- The gateway's Logs and Analytics pages don't show the team when you use API keys. Per-team showback needs JWT identity.
- Memory search is vector-only: no keyword/hybrid search and no reranking yet.
- md mode is one chunk per line with a single writer. Run one memsvc replica.
- Example models are tiny CPU models chosen so the stack runs anywhere. They are for demos, not for real work.

## Repository layout

| Path | What |
|---|---|
| `docker-compose.yml` | the whole stack for Docker. Gateway config and DB schema are inline |
| `k8s/tenantfabric.yaml` | the whole stack for Kubernetes: namespace, workloads, PVCs, Secrets, NetworkPolicies |
| `tests/e2e.py` | 28 end-to-end checks, run inside the memsvc container |
| `CLAUDE.md` / `AGENTS.md` | context and recipes for AI coding assistants |
| `docs/img/` | screenshots used above |

The memory service source lives in its own repo: [anindyar/memsvc](https://github.com/anindyar/memsvc).

## License

[Apache-2.0](LICENSE). Contributions are welcome. Keep the e2e test green, and add a check for any new behaviour.
