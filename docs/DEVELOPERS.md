# Developer guide: building on tenantfabric

For application teams. Your platform team runs tenantfabric; you get **one API key** and call **one gateway**
for models, tools and memory. Replace `http://localhost:8080` below with your gateway's address.

## Your team key

- The platform team sends your key separately. The same key works on every endpoint: `Authorization: Bearer <key>`.
- The key carries your team. The gateway uses it to decide which models you can call, which MCP tools you see,
  which runner you can use and whose memory you read and write. You never send a team or tenant name yourself.
- Keep it in a secret store, load it from an environment variable (say `TEAM_AI_KEY`), and use it only from
  server-side code. Never put it in a browser or mobile app, a repo, a log line or a ticket. Report a leak at once.

## Endpoints

| Endpoint | What it does |
|---|---|
| `GET /v1/models` | The models your key may use |
| `POST /v1/chat/completions` | Chat, streaming, tool calling (OpenAI format) |
| `POST /v1/embeddings` | Embeddings |
| `POST /mcp` | MCP tools and memory (streamable HTTP) |
| `POST /cli/<team>` | Your team's CLI runner (MCP) |
| `GET /docs/` | Interactive API reference: click **Authorize**, paste your key, try any call |

Call models by **alias** (`chat-general`, `chat-vision`, `embed` in the demo). The platform team can move an
alias to another backend without any change in your code.

## Inference

Any OpenAI SDK works: set the base URL to `<gateway>/v1`.

```python
import os
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key=os.environ["TEAM_AI_KEY"])
r = client.chat.completions.create(model="chat-general",
                                   messages=[{"role": "user", "content": "Summarise this ticket in one line: ..."}])
print(r.choices[0].message.content)

e = client.embeddings.create(model="embed", input=["first text", "second text"])
```

```javascript
import OpenAI from "openai";
const client = new OpenAI({ baseURL: "http://localhost:8080/v1", apiKey: process.env.TEAM_AI_KEY });
const r = await client.chat.completions.create({ model: "chat-general", messages: [{ role: "user", content: "Hello" }] });
```

LangChain, LlamaIndex, Spring AI and Semantic Kernel work the same way: point them at the base URL with your key.
Keep one embedding model per index; vectors from different models cannot be mixed.

## Tool calling and agents

Models never run tools. The model returns `tool_calls`; your code runs them and sends the results back, until
the model answers without asking for a tool. [`examples/agent.py`](../examples/agent.py) is the whole loop in
about 60 lines: it takes the gateway's MCP tools, offers them to the model and runs what it picks, with one key.

```bash
GATEWAY_URL=http://localhost:8080 TEAM_KEY=$TEAM_AI_KEY MODEL=chat-general \
  python examples/agent.py "Use the calculate tool to compute (17 * 3) / 4"
```

Cap the number of steps, set timeouts, and treat every tool result as untrusted input.

## MCP tools and memory

`/mcp` merges every MCP server the platform registered. You see only the tools your team may use; the rest are
missing from `tools/list` and refused on `tools/call`. Connect any MCP client with the URL and your key:

```bash
claude mcp add --transport http team http://localhost:8080/mcp --header "Authorization: Bearer $TEAM_AI_KEY"
```

Over raw HTTP: send `initialize`, keep the `Mcp-Session-Id` response header, send `notifications/initialized`,
then `tools/list` / `tools/call` with that header. Always send `Accept: application/json, text/event-stream`.

**Memory** is stored per team: the gateway stamps your team on every call and the database enforces it, so no
team can read another's memory, whatever the client sends.

| Tool | What it does |
|---|---|
| `memory_write(text, tags?)` | Store a fact |
| `memory_search(query, k?)` | Semantic search; hits carry their `id` |
| `memory_get(id?)` | One fact, or the newest ones |
| `memory_forget(id)` | Delete a fact |
| `memory_reindex()` | Rebuild your team's index (needs approval, below) |

Memory is shared by everyone holding your team's key: put a user reference in the text or tags when it matters,
and never store passwords, keys or personal data.

**CLI runner** (`/cli/<team>`): `run_command` runs allowed programs (for example `curl`, `jq`, `aws`) without a
shell, with only your team's credentials. To chain, pass one command's output as the next one's `stdin`.

## Rules the platform enforces

| You get | Meaning |
|---|---|
| `401` | No key, or an unknown key |
| `403` on a model or route | Your team may not use it (`GET /v1/models` shows what you can use) |
| `403` `policy_denied` | The policy service refused the call; the message says why |
| `403` "Blocked by guardrails" | A tool result you sent to the model contained injected instructions |
| A tool missing from `tools/list` | Your team is not granted that tool |

The policy service checks what each call asks for:

- `memory_reindex`, and `run_command` with a destructive command (rm, delete, terminate, destroy, drop, truncate,
  put-bucket-policy), need the header `x-approved: true`. Send it only after a person approved the action.
- `http_get` fetches only `https://` URLs on public hosts: no IP addresses, `localhost`, single-label or
  `.local` / `.svc` / `.internal` / `.lan` names.
- `memory_write` refuses text that contains an API key, access key or private key.
- The optional `x-data-class` header must be `public`, `internal`, `confidential` (the default) or `restricted`.

Tool results passed back to a model are also scanned: prompt-injection phrases are rejected, and emails, phone
numbers, card numbers and secret-looking strings are masked before the model sees them.

## Bringing your own MCP server

Teams do not register MCP servers themselves. **Share the code with your platform team**, who review it, run it
next to the gateway and grant its tools to your team. Your tools then appear on `/mcp` with the same key. Send:

- the repository and commit, and a Dockerfile that builds it;
- each tool with one line on what it does and whether it changes anything;
- the systems it reaches, and the credentials it needs (by name only: never in the code);
- which teams should get which tools, and an owner.

Build it to these rules: streamable HTTP at `/mcp`; unique, descriptive tool names (`invoices_search`, not
`search`); if it needs the caller's team, read the `X-Team` header the gateway sets and never take a team or
tenant as a tool argument; no secrets in tool results; validate every argument; expect to be reachable only
from the gateway.

## Try it

The [Postman collection](../postman/) covers every endpoint, including the refusals above. Import
`tenantfabric.postman_collection.json` and `tenantfabric.postman_environment.json`, set `clientKey`, and run it.
