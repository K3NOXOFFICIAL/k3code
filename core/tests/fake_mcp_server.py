"""Tiny stdio MCP server for tests: tools ``echo`` and ``add``."""

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


if __name__ == "__main__":
    mcp.run("stdio")
