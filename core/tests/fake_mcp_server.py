"""Tiny stdio MCP server for tests: tools ``echo``, ``add``, ``sleep`` and ``die``."""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("fake")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
async def sleep(seconds: float) -> str:
    """Sleep, then answer (a slow call)."""
    import asyncio

    await asyncio.sleep(seconds)
    return "slept"


@mcp.tool()
def die() -> str:
    """Kill the server process (a crash)."""
    import os

    os._exit(3)


if __name__ == "__main__":
    mcp.run("stdio")
