from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agent-sdk-vertical-slice")


@mcp.tool()
def echo(text: str) -> dict[str, str]:
    """Echo deterministic text for the Agent SDK vertical slice."""
    return {"echo": text}


if __name__ == "__main__":
    mcp.run(transport="stdio")
