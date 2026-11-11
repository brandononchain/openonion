"""A tiny MCP server used by the test suite (and handy for trying connectors by hand):

    python tests/mcp_test_server.py            # stdio
    python tests/mcp_test_server.py --http 8765  # streamable HTTP at http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

server = MCPServer("notes-test")
NOTES: dict[str, str] = {"tor": "Tor routes traffic through three relays.", "groceries": "eggs, oat milk, rice"}

READ_ONLY = ToolAnnotations(read_only_hint=True)


@server.tool(annotations=READ_ONLY)
def search_notes(query: str) -> str:
    """Search the user's saved notes by keyword and return matching notes."""
    hits = [f"{k}: {v}" for k, v in NOTES.items() if query.lower() in (k + " " + v).lower()]
    return "\n".join(hits) or "No notes match."


@server.tool(annotations=ToolAnnotations(destructive_hint=False))
def add_note(title: str, body: str) -> str:
    """Save a new note with a title and body."""
    NOTES[title] = body
    return f"Saved note '{title}'."


@server.tool(annotations=READ_ONLY)
def get_weather(city: str) -> str:
    """Get the current weather forecast for a city."""
    return f"{city}: 12°C, light rain."


@server.tool(annotations=READ_ONLY)
def convert_currency(amount: float, from_currency: str, to_currency: str) -> str:
    """Convert an amount of money between currencies."""
    return f"{amount} {from_currency} ≈ {round(amount * 0.92, 2)} {to_currency}"


@server.tool()
def create_calendar_event(title: str, start: str) -> str:
    """Create an event on the user's calendar."""
    return f"Created '{title}' at {start}."


@server.tool(annotations=READ_ONLY)
def list_github_issues(repo: str) -> str:
    """List open GitHub issues for a repository."""
    return f"{repo}: #1 Add MCP support, #2 Settings modal"


@server.tool(annotations=READ_ONLY)
def translate_text(text: str, language: str) -> str:
    """Translate text into another language."""
    return f"[{language}] {text}"


@server.tool(annotations=READ_ONLY)
def fail_on_purpose() -> str:
    """Always fails (used to test error handling)."""
    raise ValueError("this tool always fails")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--http":
        server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        server.run("stdio")
