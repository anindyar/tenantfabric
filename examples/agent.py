"""Minimal tool-calling agent for tenantfabric: OpenAI-style function calling, tools served over MCP.

The model is called through the gateway (/v1/chat/completions); the tools it may use are whatever
the team's key can see on an MCP endpoint (/mcp for shared tools + memory, /cli/<team> for the
team's CLI runner). The gateway enforces both, so this script needs no permission logic of its own.

    pip install "mcp>=1.10" httpx
    GATEWAY_URL=http://localhost:8080 TEAM_KEY=sk-alpha-demo MODEL=chat-general \\
      python examples/agent.py "What is 17 * 3 / 4? Use the calculate tool."
    MCP_PATH=/cli/alpha python examples/agent.py "Which AWS account am I? Use run_command."
"""
import asyncio, json, logging, os, sys
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

GW = os.environ.get("GATEWAY_URL", "http://localhost:8080")
KEY = os.environ.get("TEAM_KEY", "sk-alpha-demo")
MODEL = os.environ.get("MODEL", "chat-general")
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")
AUTH = {"Authorization": f"Bearer {KEY}"}
logging.disable(logging.WARNING)       # the MCP SDK warns harmlessly when the gateway closes a session


async def main(prompt: str) -> None:
    async with streamablehttp_client(GW + MCP_PATH, headers=AUTH) as (r, w, _):
        async with ClientSession(r, w) as mcp:
            await mcp.initialize()
            # 1. MCP tools -> OpenAI "tools". The list is already filtered to what this team may call.
            tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                       "parameters": t.inputSchema}}
                     for t in (await mcp.list_tools()).tools]
            print(f"[{len(tools)} tools] " + ", ".join(t["function"]["name"] for t in tools))
            messages = [{"role": "user", "content": prompt}]
            for _ in range(5):                                   # a few rounds of tool use, then stop
                # 2. Ask the model; it either answers or asks for tool calls.
                resp = httpx.post(f"{GW}/v1/chat/completions", headers=AUTH, timeout=300, json={
                    "model": MODEL, "messages": messages, "tools": tools})
                resp.raise_for_status()
                msg = resp.json()["choices"][0]["message"]
                messages.append(msg)
                if not msg.get("tool_calls"):
                    print(msg.get("content"))
                    return
                # 3. Run each tool call through the gateway and hand the result back to the model.
                for call in msg["tool_calls"]:
                    name, args = call["function"]["name"], json.loads(call["function"]["arguments"] or "{}")
                    res = await mcp.call_tool(name, args)
                    text = "\n".join(getattr(c, "text", str(c)) for c in res.content)
                    print(f"[tool] {name}({json.dumps(args)}) -> {text[:200]!r}")
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            print("(stopped after 5 rounds)")


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "What is (17 * 3) / 4? Use the calculate tool."))
