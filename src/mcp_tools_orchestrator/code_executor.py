"""
Code Executor - Executes policy code with access to the unified API.
"""

import os
import sys
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any


class CodeExecutor:
    """Executes Python policy code with access to the unified API."""

    def __init__(self, api_path: str, ipc_url: str):
        """Initialize code executor.

        Args:
            api_path: Path to the generated unified_api.py
            ipc_url: URL of the IPC server for tool calls
        """
        self.api_path = Path(api_path)
        self.ipc_url = ipc_url

    def execute_code(self, code: str, timeout: int = 3600) -> Dict[str, Any]:
        """Execute policy code with access to unified API.

        Args:
            code: Python code to execute
            timeout: Execution timeout in seconds

        Returns:
            Dictionary with execution results (output, status, returncode)
        """
        try:
            # Create a temporary Python file
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False
            ) as f:
                # Wrap the user code with necessary imports and setup
                wrapped_code = self._wrap_code(code)
                f.write(wrapped_code)
                temp_file = f.name

            # Set up environment
            env = self._setup_execution_environment()

            # Get the venv Python interpreter (fixes missing 'requests' module issue)
            venv_python = self._get_venv_python()

            # Execute the code
            result = subprocess.run(
                [venv_python, temp_file],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )

            # Clean up temp file
            try:
                os.unlink(temp_file)
            except:
                pass

            # Return results
            output = result.stdout if result.stdout else ""
            if result.stderr:
                output += "\n" + result.stderr

            return {
                "output": output,
                "returncode": result.returncode,
                "status": "success" if result.returncode == 0 else "failed",
            }

        except subprocess.TimeoutExpired:
            # Clean up temp file
            try:
                os.unlink(temp_file)
            except:
                pass

            return {
                "output": f"Error: Code execution timed out after {timeout} seconds",
                "returncode": -1,
                "status": "timeout",
            }

        except Exception as e:
            return {
                "output": f"Error: Failed to execute code: {str(e)}",
                "returncode": -1,
                "status": "error",
            }

    def _wrap_code(self, code: str) -> str:
        """Wrap user code with necessary imports and setup.

        Args:
            code: User's policy code

        Returns:
            Wrapped code ready for execution
        """
        api_dir = str(self.api_path.parent)

        wrapped = f"""import sys
import os
from typing import Dict, Any, List, Optional

# Add the generated API directory to path
sys.path.insert(0, '{api_dir}')

# Standard imports that policies might need
import math
import json
from datetime import datetime, timedelta

# Set the IPC URL environment variable
os.environ['MCP_ORCHESTRATOR_IPC_URL'] = '{self.ipc_url}'

# User's policy code:
{code}
"""
        return wrapped

    def _setup_execution_environment(self) -> Dict[str, str]:
        """Set up execution environment with necessary variables.

        Returns:
            Environment dictionary
        """
        env = os.environ.copy()
        env["MCP_ORCHESTRATOR_IPC_URL"] = self.ipc_url

        return env

    def _get_venv_python(self) -> str:
        """Get the Python interpreter from the virtual environment.

        This ensures the subprocess has access to packages installed in the venv,
        such as 'requests' which is required by the generated unified_api.py.

        Returns:
            Path to Python interpreter (venv if available, otherwise sys.executable)
        """
        # Check if we're in a venv
        venv_path = os.getenv("VIRTUAL_ENV")

        if venv_path:
            # Use the venv's Python
            python_path = os.path.join(venv_path, "bin", "python")
            if os.path.exists(python_path):
                return python_path

        # Fallback: use current interpreter
        return sys.executable
