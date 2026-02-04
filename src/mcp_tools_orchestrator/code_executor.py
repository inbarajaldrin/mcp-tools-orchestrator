"""
Code Executor - Executes policy code with access to the unified API.
"""

import os
import sys
import subprocess
import tempfile
import time
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
        temp_file = None
        stdout_file = None
        stderr_file = None
        process = None
        try:
            # Create a temporary Python file
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False
            ) as f:
                # Wrap the user code with necessary imports and setup
                wrapped_code = self._wrap_code(code)
                f.write(wrapped_code)
                temp_file = f.name

            # Create temporary files for stdout and stderr to capture partial output
            stdout_fd, stdout_file = tempfile.mkstemp(suffix=".stdout", text=True)
            stderr_fd, stderr_file = tempfile.mkstemp(suffix=".stderr", text=True)

            # Set up environment
            env = self._setup_execution_environment()

            # Get the venv Python interpreter (fixes missing 'requests' module issue)
            venv_python = self._get_venv_python()

            # Use Popen with file redirection to capture output even on timeout
            stdout_handle = open(stdout_file, 'w')
            stderr_handle = open(stderr_file, 'w')
            try:
                process = subprocess.Popen(
                    [venv_python, temp_file],
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    env=env,
                    bufsize=1,  # Line buffered for real-time output
                )

                # Wait for process with timeout using polling
                start_time = time.time()
                timed_out = False
                while process.poll() is None:
                    elapsed = time.time() - start_time
                    if elapsed >= timeout:
                        timed_out = True
                        break
                    time.sleep(0.1)  # Poll every 100ms
                
                # Close file handles to ensure output is flushed before reading
                stdout_handle.close()
                stderr_handle.close()
                
                if timed_out:
                    # Timeout occurred - kill process and read partial output
                    process.kill()
                    process.wait()  # Wait for process to actually terminate
                    
                    # Read whatever output was captured before timeout
                    partial_stdout = ""
                    partial_stderr = ""
                    try:
                        with open(stdout_file, 'r') as f:
                            partial_stdout = f.read()
                        with open(stderr_file, 'r') as f:
                            partial_stderr = f.read()
                    except:
                        pass

                    # Clean up temp files
                    try:
                        os.unlink(temp_file)
                        os.unlink(stdout_file)
                        os.unlink(stderr_file)
                    except:
                        pass

                    # Combine partial output with timeout message
                    output_parts = []
                    if partial_stdout:
                        output_parts.append(partial_stdout)
                    if partial_stderr:
                        output_parts.append(partial_stderr)
                    
                    # Append timeout message
                    timeout_msg = f"\n\n[Timeout] Code execution timed out after {timeout} seconds. Partial results above."
                    output_parts.append(timeout_msg)

                    return {
                        "output": "\n".join(output_parts) if output_parts else timeout_msg.strip(),
                        "returncode": -1,
                        "status": "timeout",
                    }
                else:
                    # Process completed normally
                    returncode = process.returncode

                    # Read output files
                    with open(stdout_file, 'r') as f:
                        stdout = f.read()
                    with open(stderr_file, 'r') as f:
                        stderr = f.read()

                    # Clean up temp files
                    try:
                        os.unlink(temp_file)
                        os.unlink(stdout_file)
                        os.unlink(stderr_file)
                    except:
                        pass

                    # Check for abort marker in stderr
                    abort_info = self._extract_abort_info(stderr)
                    if abort_info:
                        return {
                            "output": stdout.rstrip() if stdout else "",
                            "returncode": returncode,
                            "status": "aborted",
                            "reason": abort_info.get("reason", "Operation cancelled by user"),
                            "tool": abort_info.get("tool", "unknown"),
                        }

                    # Return results
                    output = stdout if stdout else ""
                    if stderr:
                        output += "\n" + stderr if output else stderr

                    return {
                        "output": output,
                        "returncode": returncode,
                        "status": "success" if returncode == 0 else "failed",
                    }
            except Exception as inner_e:
                # Close file handles on inner exception
                try:
                    stdout_handle.close()
                except:
                    pass
                try:
                    stderr_handle.close()
                except:
                    pass
                raise

        except Exception as e:
            # Clean up temp files
            if temp_file:
                try:
                    os.unlink(temp_file)
                except:
                    pass
            if stdout_file:
                try:
                    os.unlink(stdout_file)
                except:
                    pass
            if stderr_file:
                try:
                    os.unlink(stderr_file)
                except:
                    pass
            
            # Clean up process if it exists
            if process:
                try:
                    process.kill()
                except:
                    pass

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

    def _extract_abort_info(self, stderr: str) -> Dict[str, Any] | None:
        """Extract abort information from stderr if present.

        Looks for the structured abort marker:
        __MCP_ABORT_JSON__{"aborted": true, ...}__MCP_ABORT_JSON__

        Args:
            stderr: The stderr output from code execution

        Returns:
            Parsed abort info dict if found, None otherwise
        """
        import json
        import re

        marker_pattern = r'__MCP_ABORT_JSON__(.+?)__MCP_ABORT_JSON__'
        match = re.search(marker_pattern, stderr)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                # Malformed JSON, return basic abort info
                return {"aborted": True, "reason": "Operation cancelled by user"}
        return None
