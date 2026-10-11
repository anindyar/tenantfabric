"""Example shared MCP tool server for tenantfabric: plain tools any team may be granted.

It knows nothing about teams. The gateway decides which team may list or call each tool
(mcpAuthorization on apiKey.team + mcp.tool.name), so this is the shape of any off-the-shelf
MCP server you register. Only the gateway should be able to reach it."""
import ast, datetime, operator, os
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tools", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8050")),
              stateless_http=True)

OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
       ast.USub: operator.neg, ast.UAdd: operator.pos}


MAX_BITS = 4096                          # keeps one call from eating the CPU/memory every team shares


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) * max(abs(left), 2).bit_length() > MAX_BITS:
            raise ValueError("result too large")
        if isinstance(node.op, ast.Mult) and isinstance(left, int) and isinstance(right, int) \
                and left.bit_length() + right.bit_length() > MAX_BITS:
            raise ValueError("result too large")
        return OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_eval(node.operand))
    raise ValueError("only numbers and + - * / // % ** are allowed")


@mcp.tool()
def utc_now() -> str:
    """Current date and time in UTC (ISO 8601)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@mcp.tool()
def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression, e.g. "(17 * 3) / 4". Numbers and + - * / // % ** only."""
    return str(_eval(ast.parse(expression, mode="eval").body))


@mcp.tool()
async def http_get(url: str) -> str:
    """Fetch an http(s) URL and return the status code and up to 4000 characters of the body."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("only http:// and https:// URLs")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, max_redirects=5) as c:
        async with c.stream("GET", url) as r:
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) >= 16000:
                    break
    return f"HTTP {r.status_code}\n{body.decode(errors='replace')[:4000]}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
