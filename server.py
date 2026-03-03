"""
MCP Tools Orchestrator Server - Main server entry point.

Discovers tools from all connected MCP servers via the client's IPC endpoint,
generates a unified Python API, and executes composed code against it.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Dict, Any
from pydantic import Field
from mcp.server.fastmcp import FastMCP

from src.mcp_tools_orchestrator.api_generator import UnifiedAPIGenerator
from src.mcp_tools_orchestrator.code_executor import CodeExecutor

# Initialize FastMCP server
mcp = FastMCP("mcp-tools-orchestrator")

# Global state
_generator: UnifiedAPIGenerator = None
_executor: CodeExecutor = None
_api_path: str = None
_client_ipc_url: str = None
_config_path: str = None
_tool_count: int = 0
_server_count: int = 0
_initialized: bool = False


def _load_allowed_servers(config_path: str) -> list:
    """Read mcp_servers_config.json and return enabled server names as a whitelist."""
    with open(config_path, 'r') as f:
        config = json.load(f)

    return [
        name for name, cfg in config.get('mcpServers', {}).items()
        if not cfg.get('disabled', False)
    ]


async def initialize():
    """Initialize the MCP Orchestrator server."""
    global _generator, _executor, _api_path, _client_ipc_url, _config_path, _tool_count, _server_count, _initialized

    if _initialized:
        return

    print("[Orchestrator] Initializing MCP Orchestrator...")

    # Check for client IPC URL
    _client_ipc_url = os.getenv("MCP_CLIENT_IPC_URL")
    if not _client_ipc_url:
        raise RuntimeError(
            "MCP_CLIENT_IPC_URL not set. Please ensure the mcp-client is running with IPC enabled."
        )

    print(f"[Orchestrator] Using client IPC at: {_client_ipc_url}")

    # Get config path - used as a whitelist for which servers to include
    script_dir = Path(__file__).parent
    _config_path = str(script_dir / "mcp_servers_config.json")

    if not Path(_config_path).exists():
        raise RuntimeError(
            f"Config file not found: {_config_path}\n"
            "Please create mcp_servers_config.json with server definitions."
        )

    allowed_servers = _load_allowed_servers(_config_path)
    print(f"[Orchestrator] Server whitelist: {allowed_servers}")

    # Initialize API generator
    _generator = UnifiedAPIGenerator()

    # Discover tools via IPC and generate unified API.
    # Retry until all whitelisted servers are discovered, because the client
    # may still be connecting servers when the orchestrator initializes.
    try:
        generated_dir = script_dir / "generated"
        generated_dir.mkdir(exist_ok=True)
        _api_path = str(generated_dir / "unified_api.py")

        max_retries = 15
        retry_delay = 2  # seconds
        expected = set(allowed_servers)

        for attempt in range(1, max_retries + 1):
            try:
                discovered = _generator.generate_api_from_ipc(
                    _client_ipc_url, _api_path, allowed_servers
                )
            except RuntimeError as e:
                if attempt < max_retries:
                    print(f"[Orchestrator] IPC query failed (attempt {attempt}/{max_retries}): {e}")
                    print(f"[Orchestrator] Retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    continue
                raise

            missing = expected - discovered
            if not missing:
                print(f"[Orchestrator] All whitelisted servers discovered: {sorted(discovered)}")
                break

            if attempt < max_retries:
                print(
                    f"[Orchestrator] Missing servers: {sorted(missing)} "
                    f"(attempt {attempt}/{max_retries}), retrying in {retry_delay}s..."
                )
                await asyncio.sleep(retry_delay)
            else:
                print(
                    f"[Orchestrator] WARNING: Max retries reached. "
                    f"Missing servers: {sorted(missing)}. "
                    f"Proceeding with: {sorted(discovered)}"
                )

        # Count tools by reading the generated file
        with open(_api_path, 'r') as f:
            content = f.read()
            _tool_count = content.count('def ') - 1  # Subtract the _call_tool helper
            _server_count = content.count('# Tools from:')

        # Initialize code executor
        _executor = CodeExecutor(_api_path, _client_ipc_url)

        _initialized = True

        print(f"[Orchestrator] Initialized successfully ({_server_count} servers, {_tool_count} tools)")
        print(f"[Orchestrator] Generated unified API at: {_api_path}")

    except Exception as e:
        print(f"[Orchestrator] Initialization failed: {e}")
        import traceback
        traceback.print_exc()
        raise


@mcp.tool(annotations={
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
})
async def execute_composed_code(
    code: Annotated[str, Field(
        description=(
            "Python code to execute. Has access to all MCP tools via "
            "'from unified_api import *'. Tools are called as Python functions: "
            "server_name__tool_name(param=value). Replace dashes with underscores "
            "in server names."
        ),
        min_length=1,
    )],
    session_id: Annotated[str, Field(
        description=(
            "Session identifier for persistent execution. Use the same ID "
            "across calls to share state between executions."
        ),
        default="",
    )],
    persistent: Annotated[bool, Field(
        description=(
            "If True, variables from this execution are saved and restored "
            "on the next call with the same session_id. Useful for multi-step "
            "workflows where later calls need results from earlier ones."
        ),
        default=False,
    )],
) -> Dict[str, Any]:
    """Execute Python code that orchestrates tools from multiple MCP servers.

    Use after manually figuring out a working sequence for one item, then
    automate that same sequence across multiple items with loops and error handling.

    Example:
        ```python
        from unified_api import *

        items = ["item_a", "item_b", "item_c"]
        results = []

        for item in items:
            server__prepare(target=item, mode="sim")
            result = server__execute_action(name=item, value=123, mode="sim")

            if result.get("result") != "success":
                server__restore_state()
                continue

            server__finalize(item=item)
            results.append({"item": item, "status": "success"})

        print(f"Processed {len(results)}/{len(items)} successfully")
        ```

    Returns dict with keys: output (str), returncode (int), status (str),
    and session_id/session_file if persistent.
    """
    if not _initialized:
        await initialize()

    # Hardcoded timeout: 1 hour (3600 seconds)
    return _executor.execute_code(
        code,
        timeout=3600,
        session_id=session_id if persistent else "",
        persistent=persistent,
    )


@mcp.tool(annotations={
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
})
async def list_available_tools(
    include_descriptions: Annotated[bool, Field(
        description=(
            "If True, include full docstrings for each tool. "
            "Defaults to False to minimize context usage. "
            "Skip if you already have tool descriptions from schemas."
        ),
        default=False,
    )],
) -> Dict[str, Any]:
    """List all available tools from connected MCP servers with their signatures.

    Returns dict with keys: servers (dict mapping server name to tool list),
    total_servers (int), total_tools (int).
    """
    if not _initialized:
        await initialize()

    # Parse the generated API file to extract tool information
    import re

    with open(_api_path, 'r') as f:
        content = f.read()

    # Extract servers and their tools with full signatures
    servers = {}
    current_server = None
    current_function = None
    in_docstring = False
    docstring_lines = []

    for line in content.split('\n'):
        # Match server section headers
        if '# Tools from:' in line:
            current_server = line.split('# Tools from:')[1].strip()
            servers[current_server] = []
            current_function = None

        # Match function definitions
        elif line.strip().startswith('def ') and '__' in line and current_server:
            # Save previous function if exists
            if current_function:
                if include_descriptions:
                    current_function['docstring'] = '\n'.join(docstring_lines).strip()
                servers[current_server].append(current_function)
                docstring_lines = []

            # Extract function name and parameters
            func_match = re.match(r'def (\w+__\w+)\((.*?)\)\s*->\s*(\w+):', line)
            if func_match:
                full_name = func_match.group(1)
                params = func_match.group(2)
                return_type = func_match.group(3)

                current_function = {
                    "function_name": full_name,
                    "name": full_name.split('__')[1],
                    "signature": f"{full_name}({params})",
                    "parameters": params,
                    "return_type": return_type
                }
                in_docstring = True

        # Extract docstring (only if needed)
        elif in_docstring and current_function and include_descriptions:
            if '"""' in line:
                # Extract docstring content between triple quotes
                docstring_match = re.search(r'"""(.*)"""', line)
                if docstring_match:
                    docstring_lines.append(docstring_match.group(1))
                in_docstring = False
            elif 'return _call_tool' in line:
                in_docstring = False
        elif in_docstring and '"""' in line:
            in_docstring = False

    # Don't forget the last function
    if current_function:
        if include_descriptions:
            current_function['docstring'] = '\n'.join(docstring_lines).strip()
        servers[current_server].append(current_function)

    return {
        "servers": servers,
        "total_servers": len(servers),
        "total_tools": sum(len(tools) for tools in servers.values()),
    }


# @mcp.tool()
# async def refresh_tools() -> Dict[str, Any]:
#     """Refresh tool discovery from all connected servers.
#
#     Call this if servers have added/removed tools and you want to update
#     the unified API.
#
#     Returns:
#         Status message with updated tool counts
#     """
#     global _initialized
#     _initialized = False
#     await initialize()
#
#     return {
#         "status": "success",
#         "message": "Tools refreshed successfully",
#         "server_count": _server_count,
#         "tool_count": _tool_count,
#     }


# @mcp.tool()
# def get_api_documentation() -> str:
#     """Get documentation for the generated unified API.
#
#     Returns the path to the generated API file and usage instructions.
#
#     Returns:
#         Documentation string
#     """
#     if not _initialized:
#         return "Server not initialized. Call execute_composed_code or list_available_tools first."
#
#     # Extract server names from API file
#     import re
#     with open(_api_path, 'r') as f:
#         content = f.read()
#
#     servers = []
#     for line in content.split('\n'):
#         if '# Tools from:' in line:
#             server_name = line.split('# Tools from:')[1].strip()
#             servers.append(server_name)
#
#     doc = f"""
# MCP Orchestrator - Unified API Documentation
#
# Generated API Location: {_api_path}
#
# Usage in Policy Code:
# =====================
#
# from unified_api import *
#
# # All tools are available as functions with FULL PARAMETER SIGNATURES
# # Tool naming pattern: server_name__tool_name(...)
# # Example:
# result = server_name__tool_name(param1="value", param2=123)
#
# # All functions return dictionaries with results
# # Use them for decision-making in your policy code
# # Call list_available_tools() to see all available tools and their exact signatures
#
# Available Servers:
# ==================
# {chr(10).join(f'  - {server}' for server in servers)}
#
# Total Tools: {_tool_count}
# Total Servers: {_server_count}
#
# For a complete list of tools, use the list_available_tools tool.
# """
#     return doc


if __name__ == "__main__":
    # Don't initialize eagerly — the stdio transport must start first so the
    # client can complete the handshake. Initialization happens lazily on the
    # first tool call (via `if not _initialized: await initialize()` in each
    # handler), by which time all servers are connected and IPC is available.
    mcp.run(transport="stdio")
