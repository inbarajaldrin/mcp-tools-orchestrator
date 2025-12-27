"""
MCP Tools Orchestrator Server - Main server entry point.

Updated to use Python introspection for tool discovery - directly imports
server modules to extract function signatures, then routes calls through IPC.
"""

import asyncio
import os
from pathlib import Path
from typing import Dict, Any
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

    # Get config path - use mcp-tools-orchestrator's own config
    script_dir = Path(__file__).parent
    _config_path = str(script_dir / "mcp_servers_config.json")

    if not Path(_config_path).exists():
        raise RuntimeError(
            f"Config file not found: {_config_path}\n"
            "Please create mcp_servers_config.json with server definitions."
        )

    print(f"[Orchestrator] Using config: {_config_path}")

    # Initialize API generator
    _generator = UnifiedAPIGenerator()

    # Discover tools and generate API using Python introspection
    try:
        # Generate unified API
        generated_dir = script_dir / "generated"
        generated_dir.mkdir(exist_ok=True)
        _api_path = str(generated_dir / "unified_api.py")

        # Generate unified API using introspection
        _generator.generate_api_from_config(_config_path, _api_path, _client_ipc_url)

        # Count tools by reading the generated file
        with open(_api_path, 'r') as f:
            content = f.read()
            _tool_count = content.count('def ') - 1  # Subtract the _call_tool helper
            _server_count = content.count('# Tools from:')

        # Initialize code executor
        _executor = CodeExecutor(_api_path, _client_ipc_url)

        _initialized = True

        print(f"[Orchestrator] ✓ Initialized successfully")
        print(f"[Orchestrator] ✓ Generated unified API at: {_api_path}")

    except Exception as e:
        print(f"[Orchestrator] ✗ Initialization failed: {e}")
        import traceback
        traceback.print_exc()
        raise


@mcp.tool()
async def execute_composed_code(code: str, timeout: int = 3600) -> Dict[str, Any]:
    """Execute Python code with access to tools from all connected MCP servers.

    This tool enables "Code as Policies" - write Python code that orchestrates
    tools from multiple MCP servers with complex control flow (loops, conditionals,
    error handling, etc.).

    IMPORTANT: Before writing code, call list_available_tools() to see the exact
    function signatures with their parameters. Each function shows:
    - Full signature: function_name(param1: type, param2: type = default)
    - Parameters: the parameter list
    - Docstring: what the function does

    The code has access to all tools via the auto-generated unified API.
    Import with: from unified_api import *

    Example:
    ```python
    from unified_api import *

    # Use tools from ros-mcp-server (check list_available_tools for exact signatures)
    ros_mcp_server__move_home()
    ros_mcp_server__control_gripper("open", mode="sim")

    # Use tools from isaac-sim
    isaac_sim__save_scene_state()

    # Complex control flow
    for grasp_id in range(3):
        result = ros_mcp_server__move_to_grasp("block_1", grasp_id, mode="sim", move_to_object=True)
        if result.get("status") == "success":
            break
    ```

    Args:
        code: Python code to execute
        timeout: Maximum execution time in seconds (default: 3600)

    Returns:
        Dictionary with execution results (output, status, returncode)
    """
    if not _initialized:
        await initialize()

    return _executor.execute_code(code, timeout)


@mcp.tool()
async def list_available_tools() -> Dict[str, Any]:
    """List all available tools from all connected MCP servers.

    Returns a structured view of what tools are available for use in composed code,
    including their full function signatures and documentation.

    Returns:
        Dictionary mapping server names to their available tools with signatures
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

        # Extract docstring
        elif in_docstring and current_function:
            if '"""' in line:
                # Extract docstring content between triple quotes
                docstring_match = re.search(r'"""(.*)"""', line)
                if docstring_match:
                    docstring_lines.append(docstring_match.group(1))
                in_docstring = False
            elif 'return _call_tool' in line:
                in_docstring = False

    # Don't forget the last function
    if current_function:
        current_function['docstring'] = '\n'.join(docstring_lines).strip()
        servers[current_server].append(current_function)

    return {
        "servers": servers,
        "total_servers": len(servers),
        "total_tools": sum(len(tools) for tools in servers.values()),
    }


@mcp.tool()
async def refresh_tools() -> Dict[str, Any]:
    """Refresh tool discovery from all connected servers.

    Call this if servers have added/removed tools and you want to update
    the unified API.

    Returns:
        Status message with updated tool counts
    """
    global _initialized
    _initialized = False
    await initialize()

    return {
        "status": "success",
        "message": "Tools refreshed successfully",
        "server_count": _server_count,
        "tool_count": _tool_count,
    }


@mcp.tool()
def get_api_documentation() -> str:
    """Get documentation for the generated unified API.

    Returns the path to the generated API file and usage instructions.

    Returns:
        Documentation string
    """
    if not _initialized:
        return "Server not initialized. Call execute_composed_code or list_available_tools first."

    # Extract server names from API file
    import re
    with open(_api_path, 'r') as f:
        content = f.read()

    servers = []
    for line in content.split('\n'):
        if '# Tools from:' in line:
            server_name = line.split('# Tools from:')[1].strip()
            servers.append(server_name)

    doc = f"""
MCP Orchestrator - Unified API Documentation

Generated API Location: {_api_path}

Usage in Policy Code:
=====================

from unified_api import *

# All tools are available as functions with FULL PARAMETER SIGNATURES
# Example: Call a tool from ros-mcp-server WITH parameters
result = ros_mcp_server__move_home()
ros_mcp_server__control_gripper("open", mode="sim")
ros_mcp_server__move_to_grasp("fork_orange", 6, mode="sim", move_to_object=True)

# Example: Call a tool from isaac-sim
isaac_sim__save_scene_state()

# All functions return dictionaries with results
# Use them for decision-making in your policy code

Available Servers:
==================
{chr(10).join(f'  - {server}' for server in servers)}

Total Tools: {_tool_count}
Total Servers: {_server_count}

For a complete list of tools, use the list_available_tools tool.
"""
    return doc


if __name__ == "__main__":
    # Initialize on startup
    asyncio.run(initialize())

    # Run the MCP server
    mcp.run(transport="stdio")
