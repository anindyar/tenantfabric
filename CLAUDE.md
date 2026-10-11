# CLAUDE.md: context for AI coding assistants

You are working on **tenantfabric**, a reference multi-team AI platform. Read this whole file before changing or deploying anything. `AGENTS.md` is a symlink to this file.

## What it is

One API key per team, issued by **agentgateway**, scopes three planes at once:

1. **Inference.** Teams call model *aliases* (`chat-general`, `chat-vision`, `embed`). Each alias has an `authorization` rule on `apiKey.team`.
2. **Tools (MCP).** Every MCP server is a target under `mcp.targets`, merged into one endpoint `/mcp` (`prefixMode: never`, so tool names must be unique). `mcpAuthorization` rules on `apiKey.team`, `mcp.tool.target` and `mcp.tool.name` decide which tools a team sees and can call. The example stack registers memsvc and `tools` (`toolbox/tools.py`: `utc_now`, `calculate`, `http_get`).
2b. **CLI runners.** Each team may get its own runner container (`toolbox/runner.py`, tools `run_command` + `runner_info`) at `/cli/<team>`. It runs allowlisted programs (`RUNNER_COMMANDS`, e.g. `curl,aws,jq`) without a shell, with only that team's credentials in its environment. The route admits only that team's key, sets `x-team`, and the runner refuses any other `x-team`.
3. **Memory.** The **memsvc** MCP server (image `ghcr.io/anindyar/memsvc`, source at https://github.com/anindyar/memsvc) stores memory in Postgres + pgvector. The gateway sets the `x-team` request header from `apiKey.team`, overwriting whatever the client sent. memsvc reads the team only from that header, and Postgres row-level security restricts every query to it.

Components: `agentgateway` (only exposed service, port 8080, UI at `/ui`), `memsvc` (8041), `postgres` (pgvector/pgvector:pg17, 5432), `ollama` (example model server, 11434), a one-shot `models` job that pulls the example models, `tools` (8050) and one `cli-<team>` runner per team (8060). `tools` and the runners use image `ghcr.io/anindyar/tenantfabric-toolbox`, built from `toolbox/` by GitHub Actions.

## Security invariants: never break these

- **No memsvc tool may take a tenant/team argument.** The tenant comes only from the gateway-set `x-team` header.
- **The gateway must keep `transformations.request.set.x-team: 'apiKey.team'` on the MCP policy.** Without it, memsvc rejects calls. If it were replaced by a client-supplied value, isolation would be gone.
- **Only the gateway may reach memsvc, `tools` or any runner.** On Kubernetes keep the `memsvc-from-gateway-only` and `tools-from-gateway-only` NetworkPolicies (the latter selects `app.kubernetes.io/component in (mcp-tools, cli-runner)`, so new runners need that label). On Compose, never publish their ports, and keep `tools` and each runner on their own network shared only with the gateway.
- **One runner per team, one team's credentials per runner.** Never mount two teams' secrets in one runner, and never point two teams' routes at the same runner. Every `/cli/<team>` route needs an `exact` path match, `authorization: require apiKey.team == "<team>"` and the `x-team` transform, and the runner's `RUNNER_TEAM` must equal `<team>`.
- **Runners never get a shell.** Keep `asyncio.create_subprocess_exec(*argv)` (never a shell), the program allowlist, the per-call temporary `HOME`/cwd, the read-only `AWS_CONFIG_FILE`/`AWS_SHARED_CREDENTIALS_FILE`, and the denied curl options (`-K`/`--config`/`--engine`). Keep runners non-root with a read-only root FS, no capabilities, and (Kubernetes) `automountServiceAccountToken: false`. Never add `sh`, `bash`, `python` or other interpreters to `RUNNER_COMMANDS`, and treat any CLI that loads plugins, config or scripts as one.
- **Tools and runners never reach the cloud metadata service.** Keep the `tools-egress-no-metadata` NetworkPolicy (blocks `169.254.0.0/16`); on a cloud node it would hand any team the node's credentials.
- **memsvc connects as the non-owner role `memsvc`**, so RLS applies. Never point `MEM_DB` at the table owner or a superuser.
- **Keep RLS enabled** on the `memory` table, with the `tenant_isolation` policy.
- `tests/e2e.py` must stay green (52/52). Add a check for any new behaviour.

## Files

| Path | Contents |
|---|---|
| `docker-compose.yml` | all services. The gateway config (`configs.agentgateway`) and the DB schema (`configs.schema`) are **inline** |
| `k8s/tenantfabric.yaml` | the same stack for Kubernetes. Gateway config is in Secret `agentgateway-config`, schema in ConfigMap `postgres-init` |
| `tests/e2e.py` | end-to-end checks. Runs **inside the memsvc container** (it has the MCP SDK, httpx, psycopg, the memory volume and `MEM_GATEWAY`) |
| `toolbox/` | `tools.py` (shared example MCP tools), `runner.py` (per-team CLI runner), `Dockerfile` (python + curl + jq + AWS CLI v2). `.github/workflows/toolbox-image.yml` publishes it multi-arch |
| `examples/agent.py` | minimal tool-calling loop: MCP tools → OpenAI `tools`, model via `/v1/chat/completions`, `tool_calls` executed over MCP |

**The gateway config and the schema are duplicated** between `docker-compose.yml` and `k8s/tenantfabric.yaml`. When you change one, make the same change in the other. The only indentation difference is the YAML nesting.

## Deploy

Docker Compose (Docker 24+ with Compose v2.23+, needed for inline `configs.content`):
```bash
docker compose up -d
docker compose logs -f models                            # wait for exit 0
docker compose exec -T memsvc python - < tests/e2e.py    # expect: RESULT: 52 passed, 0 failed
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

### Register an MCP server (option 1: shared tools)
1. Run the server where only the gateway can reach it (Compose: a service with no `ports`, on a network shared only with the gateway like `tools`; Kubernetes: Deployment + Service, labelled `app.kubernetes.io/component: mcp-tools` so `tools-from-gateway-only` covers it).
2. Add a target under `mcp.targets` in the gateway config (both files): `- name: <unique>` with `mcp: { host: "http://<service>:<port>/mcp" }`.
3. Grant its tools: extend `mcp.policies.mcpAuthorization.rules`, e.g. `'apiKey.team == "beta" && mcp.tool.target == "<name>" && mcp.tool.name in ["tool_a"]'`. Rules are OR-ed; a tool no rule matches is hidden and refused.
4. Tool names must be unique across all targets (`prefixMode: never`). On a clash, rename upstream or switch to `prefixMode: always` and tell teams the new `<target>_<tool>` names.
5. Add e2e checks: the granted team sees and calls the tool, another team doesn't see it and is refused.

### Give a team a CLI runner (option 2)
Example: team `data` gets `curl,aws,jq` with its own AWS credentials.
- Compose: copy the `cli-alpha` service to `cli-data` (`RUNNER_TEAM: data`, its own `DATA_AWS_*` variables), add a `cli-data` network to `networks:`, put `cli-data` on only that network, and add it to the gateway's `networks` and `depends_on`.
- Kubernetes: copy Secret `cli-alpha-aws` to `cli-data-aws`, and the `cli-alpha` Deployment + Service to `cli-data` (keep the `app.kubernetes.io/component: cli-runner` label and the security settings).
- Gateway config (both files): copy the `cli-alpha` route to `cli-data`, with path `/cli/data`, `require: 'apiKey.team == "data"'` and host `http://cli-data:8060/mcp`.
- Verify: `data`'s key gets `team: data` from `runner_info` on `/cli/data`, and another team's key gets 403 there.

### Add a program to the runners
Install it in `toolbox/Dockerfile` (multi-arch: use `TARGETARCH`), push so the image rebuilds, then add it to `RUNNER_COMMANDS` for the teams that should have it. Never add a shell or interpreter. If it needs a credential that is not `AWS_*`, set the variable from the team's Secret and list its name in `RUNNER_PASS_ENV`.

### Limit what a runner can do
The gateway's `mcpAuthorization` sees the tool name and the caller, **not the arguments** (verified on agentgateway 1.5: `mcp.tool.arguments` is empty during authorization), so you cannot write a rule that allows `aws s3 ls` but blocks `aws s3 rm`. Restrict by:
- the credentials: a least-privilege IAM policy (or a short-lived STS/IRSA role) is the real control,
- the program list: `RUNNER_COMMANDS` per team,
- egress: replace the open `ipBlock` in `tools-egress-no-metadata` with a per-team policy allowing only the endpoints the team needs (keep DNS and the metadata block).

### Apply config changes
- Compose: `docker compose up -d --force-recreate agentgateway memsvc` (add `tools cli-alpha cli-beta` if you changed them). Inline configs are fixed when a container is created.
- Kubernetes: `kubectl apply -f k8s/tenantfabric.yaml && kubectl -n tenantfabric rollout restart deploy/agentgateway deploy/memsvc` (add the runner deployments if their Secret changed; env from a Secret is read only at pod start).

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
| a tool is missing from `tools/list` | no `mcpAuthorization` rule grants it to that team, or the target is down (the gateway fails closed) |
| MCP call fails with "served by multiple targets" | two targets expose the same tool name under `prefixMode: never`. Rename one, or switch to `prefixMode: always` |
| `/cli/<team>` returns 403 | the key's team is not the route's team |
| runner says "this runner serves team ... only" | the route's `x-team` transform is missing, or `RUNNER_TEAM` doesn't match the route |
| `aws` says "Unable to locate credentials" | the team's Secret/`.env` values are missing, or the runner wasn't restarted after they changed |
| tool calling does nothing with the demo model | `qwen2.5:0.5b` calls tools unreliably and `moondream` not at all. Use a real model |
