"""Per-team CLI runner for tenantfabric, exposed as an MCP server behind agentgateway.

Run one container per team. Each container holds only that team's credentials (for example
AWS_* from the team's secret), so a command can only ever act as that team.

  RUNNER_TEAM      the one team this runner serves (required)
  RUNNER_COMMANDS  allowed programs, comma-separated (default "curl,aws,jq")
  RUNNER_TIMEOUT   seconds per command (default 60)
  RUNNER_PASS_ENV  extra env var names passed to commands, comma-separated (AWS_* always pass)

Three layers keep a team on its own runner:
  1. the gateway routes /cli/<team> to this runner and authorizes only that team's key
  2. the gateway sets X-Team from the key; this server refuses any other team
  3. the network: only the gateway can reach this container
Commands run without a shell (no pipes, redirects or substitution). Feed data to a command
through the `stdin` argument instead, e.g. pipe curl output into jq in two calls.
Every call gets a fresh, empty HOME and working directory that is deleted afterwards, and the
AWS CLI reads its config only from read-only files, so one call cannot plant config (aliases,
credential_process, .curlrc) that a later call would execute."""
import asyncio, os, shlex, tempfile
from mcp.server.fastmcp import FastMCP, Context

TEAM = os.environ["RUNNER_TEAM"]
COMMANDS = [c.strip() for c in os.environ.get("RUNNER_COMMANDS", "curl,aws,jq").split(",") if c.strip()]
TIMEOUT = int(os.environ.get("RUNNER_TIMEOUT", "60"))
PASS_ENV = {c.strip() for c in os.environ.get("RUNNER_PASS_ENV", "").split(",") if c.strip()}
MAX_OUT = 20000
# Options that make a program load code or config from a file. Short flags may be bundled (-sK).
DENY_LONG = {"curl": {"--config", "--engine"}}
DENY_SHORT = {"curl": set("K")}

mcp = FastMCP("cli", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8060")),
              stateless_http=True)


def caller(ctx: Context) -> str:
    # Set by the gateway from the caller's API key; clients cannot choose it.
    t = ctx.request_context.request.headers.get("x-team", "")
    if t != TEAM:
        raise PermissionError(f"this runner serves team {TEAM!r} only (call through the gateway)")
    return t


def check(argv: list[str]) -> None:
    if not argv or argv[0] not in COMMANDS:
        raise ValueError(f"not allowed: {argv[0] if argv else '(empty)'}. Allowed: {', '.join(COMMANDS)}")
    prog = argv[0]
    for a in argv[1:]:
        if a == "--":
            break
        if a.startswith("--") and a.split("=", 1)[0] in DENY_LONG.get(prog, ()):
            raise ValueError(f"option not allowed: {a.split('=', 1)[0]}")
        if a.startswith("-") and not a.startswith("--") and set(a[1:]) & DENY_SHORT.get(prog, set()):
            raise ValueError(f"option not allowed: {a}")


def child_env(home: str) -> dict:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8",
           "AWS_PAGER": "", "AWS_CLI_AUTO_PROMPT": "off",
           # read-only, empty by default; mount real files here (or set these vars) for profiles/roles
           "AWS_CONFIG_FILE": "/etc/runner/aws/config", "AWS_SHARED_CREDENTIALS_FILE": "/etc/runner/aws/credentials"}
    env.update({k: v for k, v in os.environ.items() if k.startswith("AWS_") or k in PASS_ENV})
    return env


async def read_capped(stream: asyncio.StreamReader) -> str:
    buf, total = bytearray(), 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        if len(buf) < MAX_OUT:
            buf += chunk[:MAX_OUT - len(buf)]
    text = buf.decode(errors="replace")
    return text if total <= MAX_OUT else text + f"\n... [truncated, {total} bytes]"


@mcp.tool(description=f"Run one command-line program for your team and return its exit code and output. "
                      f"Allowed programs: {', '.join(COMMANDS)}. No shell: pipes, redirects, globs and $VARS "
                      f"are not interpreted. Pass data to the program's standard input with `stdin`. "
                      f"Each call starts in a fresh empty directory. Timeout {TIMEOUT}s. "
                      f"Example: command='aws sts get-caller-identity'.")
async def run_command(command: str, ctx: Context, stdin: str = "") -> str:
    caller(ctx)
    argv = shlex.split(command)
    check(argv)
    with tempfile.TemporaryDirectory(prefix="run-") as home:
        p = await asyncio.create_subprocess_exec(
            *argv, cwd=home, env=child_env(home), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        async def talk():
            if stdin:
                p.stdin.write(stdin.encode())
                await p.stdin.drain()
            p.stdin.close()
            out, err = await asyncio.gather(read_capped(p.stdout), read_capped(p.stderr))
            return out, err, await p.wait()
        try:
            out, err, code = await asyncio.wait_for(talk(), TIMEOUT)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()
            raise TimeoutError(f"timed out after {TIMEOUT}s")
    return f"exit {code}\n--- stdout\n{out}\n--- stderr\n{err}"


@mcp.tool()
def runner_info(ctx: Context) -> str:
    """Which team this runner belongs to, the programs it may run, and which credential variables are set (names only)."""
    caller(ctx)
    creds = sorted(k for k in os.environ if k.startswith("AWS_") or k in PASS_ENV)
    return f"team: {TEAM}\nprograms: {', '.join(COMMANDS)}\ncredential variables: {', '.join(creds) or 'none'}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
