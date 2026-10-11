# CLAUDE.md: context for AI coding assistants

You are working on **tenantfabric**, a reference multi-team AI platform. Read this whole file before changing or deploying anything. `AGENTS.md` is a symlink to this file.

## What it is

One API key per team, issued by **agentgateway**, scopes three planes at once:

1. **Inference.** Teams call model *aliases* (`chat-general`, `chat-vision`, `embed`). Each alias has an `authorization` rule on `apiKey.team`.
2. **Tools (MCP).** `mcpAuthorization` rules on `apiKey.team` and `mcp.tool.name` decide which tools a team sees and can call.
3. **Memory.** The **memsvc** MCP server (image `ghcr.io/anindyar/memsvc`, source at https://github.com/anindyar/memsvc) stores memory in Postgres + pgvector. The gateway sets the `x-team` request header from `apiKey.team`, overwriting whatever the client sent. memsvc reads the team only from that header, and Postgres row-level security restricts every query to it.

Components: `agentgateway` (only exposed service, port 8080, UI at `/ui`), `memsvc` (8041), `postgres` (pgvector/pgvector:pg17, 5432), `ollama` (example model server, 11434), and a one-shot `models` job that pulls the example models.

## Security invariants: never break these

- **No memsvc tool may take a tenant/team argument.** The tenant comes only from the gateway-set `x-team` header.
- **The gateway must keep `transformations.request.set.x-team: 'apiKey.team'` on the MCP policy.** Without it, memsvc rejects calls. If it were replaced by a client-supplied value, isolation would be gone.
- **Only the gateway may reach memsvc.** On Kubernetes keep the `memsvc-from-gateway-only` NetworkPolicy. On Compose, never publish memsvc's port.
- **memsvc connects as the non-owner role `memsvc`**, so RLS applies. Never point `MEM_DB` at the table owner or a superuser.
- **Keep RLS enabled** on the `memory` table, with the `tenant_isolation` policy.
- `tests/e2e.py` must stay green (28/28). Add a check for any new behaviour.

## Files

| Path | Contents |
|---|---|
| `docker-compose.yml` | all services. The gateway config (`configs.agentgateway`) and the DB schema (`configs.schema`) are **inline** |
| `k8s/tenantfabric.yaml` | the same stack for Kubernetes. Gateway config is in Secret `agentgateway-config`, schema in ConfigMap `postgres-init` |
| `tests/e2e.py` | end-to-end checks. Runs **inside the memsvc container** (it has the MCP SDK, httpx, psycopg, the memory volume and `MEM_GATEWAY`) |

**The gateway config and the schema are duplicated** between `docker-compose.yml` and `k8s/tenantfabric.yaml`. When you change one, make the same change in the other. The only indentation difference is the YAML nesting.

## Deploy

Docker Compose (Docker 24+ with Compose v2.23+, needed for inline `configs.content`):
```bash
docker compose up -d
docker compose logs -f models                            # wait for exit 0
docker compose exec -T memsvc python - < tests/e2e.py    # expect: RESULT: 28 passed, 0 failed
```

Kubernetes (needs a default StorageClass):
```bash
kubectl apply -f k8s/tenantfabric.yaml
kubectl -n tenantfabric wait --for=condition=complete job/models --timeout=30m
kubectl -n tenantfabric rollout status deploy/agentgateway deploy/memsvc
kubectl -n tenantfabric exec -i deploy/memsvc -- python - < tests/e2e.py
```

To expose the gateway on Kubernetes, add an Ingress or Gateway route to `svc/agentgateway:8080`, or change the Service type. Use TLS for anything beyond a laptop.

## Recipes

### Swap a model backend
Edit the alias under `llm.models` in the gateway config (both files):
```yaml
- name: chat-general
  provider: { custom: { formats: [ { type: completions } ] } }
  params: { apiKey: dummy, model: "Qwen/Qwen3-32B", baseUrl: "http://vllm.ai.svc:8000" }
  authorization:
    rules: [ 'apiKey.team == "alpha"' ]
```
`baseUrl` is the server root, without `/v1`. Any OpenAI-compatible server works. For a backend that needs a real key, set `params.apiKey`. Prefer the gateway's file or env-var key options over a literal key. If nothing else uses Ollama, you can remove the `ollama` and `models` services. **Do not remove them if `embed` still points at Ollama.**

### Change the embedding model
The vector size is fixed in the schema (`embedding vector(768)`). If the new embedder has a different size:
1. Change the schema in both files.
2. Recreate the table. On a fresh deploy, delete the Postgres volume/PVC first, because the schema only runs on first init.
3. Call `memory_reindex` once per team: md mode rebuilds from the files, pg mode re-embeds every row.

### Add a team
Example: team `data`, models `chat-general` + `embed`, all memory tools, pg mode.
1. Gateway config, `llm.policies.apiKey.keys`: add `- key: <generated secret>` with `metadata: { team: data }`. The MCP policy reuses these keys through the `&teamKeys` anchor, so there is nothing to add there.
2. Add `data` to the rule of each model it may use, e.g. `'apiKey.team in ["alpha", "data"]'`. Always add it to `embed`'s rule: memsvc embeds with its own `platform` key, not the team key, but teams usually need embeddings too.
3. Add a rule to `mcp.policies.mcpAuthorization.rules`: `'apiKey.team == "data"'` for all tools, or `'apiKey.team == "data" && mcp.tool.name in ["memory_search", "memory_get"]'` for read-only.
4. memsvc env `MEM_MODES`: add `data=pg` (or `data=md`). Teams that aren't listed get `MEM_DEFAULT_MODE` (default `pg`).
5. Team names must be alphanumeric. memsvc rejects anything else.
6. Apply the change (next section), then confirm the team works: `/v1/models` with the new key lists only its models, and an MCP `tools/list` shows only its tools.

### Apply config changes
- Compose: `docker compose up -d --force-recreate agentgateway memsvc`. Inline configs are fixed when a container is created.
- Kubernetes: `kubectl apply -f k8s/tenantfabric.yaml && kubectl -n tenantfabric rollout restart deploy/agentgateway deploy/memsvc`.

### Use an existing Postgres
Run the schema block (the `create database agwlogs` line is only needed for gateway logs). Point memsvc `MEM_DB` at it **as the `memsvc` role**, and the gateway `config.database.url` at the `agwlogs` database. Then drop the `postgres` service. The pgvector extension must be available.

### Production hardening checklist
- Replace the API keys with JWT/OIDC (e.g. Keycloak) that has a `team` claim, and change `apiKey.team` to `jwt.team` in every rule and in the `x-team` transform.
- Generate real secrets for the Postgres passwords and the `sk-memsvc-demo` key. Use Secrets/ExternalSecrets, not literals.
- Pin image tags to digests (`ollama/ollama:latest` and `memsvc:latest` float).
- Add per-key token budgets on the gateway, plus TLS and an ingress.
- Back up Postgres and, for md-mode teams, the memory volume (or keep `/srv/memory/<team>` in git).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| e2e chat checks time out or return 5xx right after startup | the `models` job hasn't finished pulling, or the first call is loading the model on CPU. Wait and retry |
| `memory_*` calls fail with "no team identity on request" | memsvc was called directly, not through the gateway, or the `x-team` transform is missing |
| memory_write fails with an embeddings error | the `embed` alias is down, or `sk-memsvc-demo` lost access to `embed` (its key is team `platform`) |
| `memory_forget` says the file "changed since it was indexed" | someone hand-edited the markdown. Call `memory_reindex`, search again, then forget by the new id |
| schema changes don't apply | the Postgres init scripts only run on an empty data volume |
| a team sees nothing in the gateway Logs by user/group | expected with API keys. Showback per team needs JWT identity |
