#!/usr/bin/env python3
"""
Standalone script to introspect an MCP server's tools.
This runs in the server's own Python environment to avoid dependency conflicts.
"""

import sys
import json
import inspect
import importlib.util
from pathlib import Path


def load_module(server_path: str, module_name: str):
    """Load a server module."""
    # Add server directory to path for relative imports
    server_dir = str(Path(server_path).parent)
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)

    spec = importlib.util.spec_from_file_location(module_name, server_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {server_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def extract_function_signature(func):
    """Extract function signature as a dictionary."""
    sig = inspect.signature(func)
    params = []

    for name, param in sig.parameters.items():
        param_info = {
            'name': name,
            'has_default': param.default != inspect.Parameter.empty,
            'default': None,
            'annotation': None
        }

        # Extract annotation
        if param.annotation != inspect.Parameter.empty:
            if hasattr(param.annotation, '__name__'):
                param_info['annotation'] = param.annotation.__name__
            else:
                param_info['annotation'] = str(param.annotation)

        # Extract default value
        if param.default != inspect.Parameter.empty:
            if isinstance(param.default, str):
                param_info['default'] = f'"{param.default}"'
            elif isinstance(param.default, bool):
                param_info['default'] = str(param.default)
            elif param.default is None:
                param_info['default'] = 'None'
            else:
                param_info['default'] = str(param.default)

        params.append(param_info)

    return {
        'name': func.__name__,
        'params': params,
        'docstring': inspect.getdoc(func) or ""
    }


def discover_tools(server_path: str, exclude_tools: list = None):
    """Discover tools from a server module."""
    exclude_tools = exclude_tools or []
    tools = []

    try:
        # Load the module
        module_name = f"_introspect_{Path(server_path).stem}"
        module = load_module(server_path, module_name)

        # Try FastMCP.list_tools first (but don't call it since it's async)
        mcp_obj = None
        for name, obj in inspect.getmembers(module):
            if hasattr(obj, '__class__') and obj.__class__.__name__ == 'FastMCP':
                mcp_obj = obj
                break

        # Fallback: introspect all functions
        for name, obj in inspect.getmembers(module):
            if inspect.isfunction(obj):
                # Check if it looks like an MCP tool
                if (hasattr(obj, '__wrapped__') or
                    (hasattr(obj, '__annotations__') and not name.startswith('_'))):
                    if name in exclude_tools:
                        continue

                    sig_info = extract_function_signature(obj)
                    tools.append(sig_info)

        return {
            'success': True,
            'tools': tools,
            'error': None
        }

    except Exception as e:
        return {
            'success': False,
            'tools': [],
            'error': str(e)
        }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({
            'success': False,
            'tools': [],
            'error': 'Usage: introspect_server.py <server_path> [exclude_tool1,exclude_tool2,...]'
        }))
        sys.exit(1)

    server_path = sys.argv[1]
    exclude = sys.argv[2].split(',') if len(sys.argv) > 2 else []

    result = discover_tools(server_path, exclude)
    print(json.dumps(result, indent=2))
