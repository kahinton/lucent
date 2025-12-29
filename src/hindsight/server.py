"""Hindsight MCP Server - Memory functionality for LLMs."""

import multiprocessing
import os
import sys

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from hindsight.auth import is_dev_mode
from hindsight.prompts.memory_usage import get_memory_system_prompt, get_memory_system_prompt_short
from hindsight.tools.memories import register_tools


# Load environment variables
load_dotenv()

# Server configuration
HOST = os.environ.get("HINDSIGHT_HOST", "0.0.0.0")
PORT = int(os.environ.get("HINDSIGHT_PORT", "8765"))
API_PORT = int(os.environ.get("HINDSIGHT_API_PORT", "8766"))

# Create the MCP server
mcp = FastMCP("Hindsight")

# Register all memory tools
register_tools(mcp)


# Register prompts
@mcp.prompt()
def memory_usage_guide() -> str:
    """Get comprehensive guidance on how to effectively use the memory system.
    
    This prompt provides detailed instructions on memory types, importance ratings,
    best practices for creating and searching memories, and example usage patterns.
    """
    return get_memory_system_prompt()


@mcp.prompt()
def memory_usage_guide_short() -> str:
    """Get a condensed guide on memory system usage.
    
    A shorter version of the memory usage guide for contexts with limited prompt space.
    """
    return get_memory_system_prompt_short()


def run_api_server():
    """Run the Admin API server in a separate process."""
    import uvicorn
    from hindsight.api.app import app
    uvicorn.run(app, host=HOST, port=API_PORT, log_level="info")


def main() -> None:
    """Main entry point for the Hindsight MCP server."""
    # Validate DATABASE_URL is set
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is required.", file=sys.stderr)
        print("Example: postgresql://user:password@localhost:5432/hindsight", file=sys.stderr)
        sys.exit(1)
    
    # Show dev mode status
    if is_dev_mode():
        print("Running in DEVELOPMENT MODE - authentication disabled", file=sys.stderr)
    
    # Run the MCP server with streamable HTTP transport
    print(f"Starting Hindsight MCP server on http://{HOST}:{PORT}", file=sys.stderr)
    print(f"Starting Hindsight Admin API on http://{HOST}:{API_PORT}", file=sys.stderr)
    print(f"API documentation at http://{HOST}:{API_PORT}/api/docs", file=sys.stderr)
    
    # Start the Admin API in a separate process
    api_process = multiprocessing.Process(target=run_api_server, daemon=True)
    api_process.start()
    
    # Run the MCP server (this blocks)
    mcp.settings.host = HOST
    mcp.settings.port = PORT
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
