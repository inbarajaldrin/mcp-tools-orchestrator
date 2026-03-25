"""
Code Executor - Executes policy code with access to the unified API.

Supports persistent sessions: variables from one execution can be saved to disk
and restored in the next call with the same session_id. This avoids redundant
recomputation across sequential calls (e.g., multi-step robot manipulation).
"""

import json
import os
import pickle
import re
import sys
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, Any


# Directory for session state files — follows MCP_CLIENT_OUTPUT_DIR convention
# so the client can manage (inspect, clear) session files externally
_base_output = os.getenv("MCP_CLIENT_OUTPUT_DIR", "").strip()
if _base_output:
    _SESSIONS_DIR = Path(_base_output) / "orchestrator_sessions"
else:
    _SESSIONS_DIR = Path(tempfile.gettempdir()) / "mcp_orchestrator_sessions"


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
        self._hyphen_replacements = self._build_hyphen_replacements()
        # Ensure sessions directory exists
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    def _build_hyphen_replacements(self) -> list:
        """Build a list of (hyphenated, underscored) pairs from the unified API.

        Extracts server prefixes from '# Tools from: server-name' comments
        and function names from 'def prefix__tool(...)' lines in the generated
        unified_api.py. Returns pairs sorted longest-first so replacements
        don't produce partial matches.
        """
        pairs = []
        try:
            with open(self.api_path, 'r') as f:
                content = f.read()

            # Collect hyphenated server names from section headers
            server_names = set()
            for m in re.finditer(r'^# Tools from: (.+)$', content, re.MULTILINE):
                name = m.group(1).strip()
                if '-' in name:
                    server_names.add(name)

            # For each hyphenated server, collect its function names and build
            # the mapping from the hyphenated form to the underscored form.
            for server in server_names:
                underscored_prefix = server.replace('-', '_')
                # Map the bare prefix (e.g. in import statements)
                pairs.append((server, underscored_prefix))

            # Also collect full function names (prefix__tool) that have hyphens
            # in case a model writes them out fully with hyphens
            for m in re.finditer(r'^def ([a-zA-Z_][a-zA-Z0-9_]*__[a-zA-Z0-9_]+)\(', content, re.MULTILINE):
                func_name = m.group(1)
                # Reconstruct the hyphenated version by reversing the prefix substitution
                for server in server_names:
                    underscored_prefix = server.replace('-', '_')
                    if func_name.startswith(underscored_prefix + '__'):
                        hyphenated = server + '__' + func_name[len(underscored_prefix) + 2:]
                        pairs.append((hyphenated, func_name))

        except FileNotFoundError:
            pass

        # Sort longest-first to avoid partial replacements
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for pair in pairs:
            if pair[0] not in seen:
                seen.add(pair[0])
                unique.append(pair)
        return unique

    def _fix_hyphenated_names(self, code: str) -> str:
        """Replace hyphenated MCP tool names with their valid Python equivalents.

        Some models copy tool names verbatim from tool definitions (e.g.
        'ros-mcp-server__move_home') into Python code, where hyphens are
        parsed as subtraction. This fixes those names to their underscored
        form (e.g. 'ros_mcp_server__move_home').

        Only replaces hyphens outside of string literals so that data
        comparisons (e.g. name == 'ros-mcp-server__tool') are preserved.
        """
        # Tokenize code into string literals and non-string segments.
        # Matches single-quoted, double-quoted, triple-single, triple-double,
        # and f-string variants. Longest delimiters first to avoid partial matches.
        string_pattern = re.compile(
            r"f?'''[\s\S]*?'''|f?\"\"\"[\s\S]*?\"\"\"|f?'[^'\n]*'|f?\"[^\"\n]*\""
        )

        result = []
        last_end = 0
        for m in string_pattern.finditer(code):
            # Process the non-string segment before this match
            segment = code[last_end:m.start()]
            result.append(self._replace_hyphens(segment))
            # Keep the string literal unchanged
            result.append(m.group(0))
            last_end = m.end()

        # Process any remaining non-string segment after the last match
        result.append(self._replace_hyphens(code[last_end:]))
        return ''.join(result)

    def _replace_hyphens(self, segment: str) -> str:
        """Apply hyphen-to-underscore replacements on a non-string code segment."""
        # Apply known exact replacements (longest-first)
        for hyphenated, underscored in self._hyphen_replacements:
            segment = segment.replace(hyphenated, underscored)

        # Catch-all: replace any remaining hyphens in identifiers before '__'
        segment = re.sub(
            r'([A-Za-z0-9_]*)-([A-Za-z0-9_-]*__)',
            lambda m: m.group(0).replace('-', '_'),
            segment,
        )
        return segment

    def _session_path(self, session_id: str) -> Path:
        """Get the pickle file path for a session."""
        # Sanitize session_id to be filesystem-safe
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', session_id)
        return _SESSIONS_DIR / f"{safe_id}.pkl"

    def execute_code(
        self,
        code: str,
        timeout: int = 3600,
        session_id: str = "",
        persistent: bool = False,
    ) -> Dict[str, Any]:
        """Execute policy code with access to unified API.

        Args:
            code: Python code to execute
            timeout: Execution timeout in seconds
            session_id: Session identifier for persistent state
            persistent: If True, save/restore variables across calls

        Returns:
            Dictionary with execution results (output, status, returncode)
        """
        temp_file = None
        stdout_file = None
        stderr_file = None
        process = None
        try:
            # Fix hyphenated tool names before wrapping (toggle via DISABLE_HYPHEN_FIX=1)
            if not os.environ.get("DISABLE_HYPHEN_FIX"):
                code = self._fix_hyphenated_names(code)

            # Create a temporary Python file
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False
            ) as f:
                # Wrap the user code with necessary imports and setup
                wrapped_code = self._wrap_code(code, session_id=session_id, persistent=persistent)
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

                    result = {
                        "output": output,
                        "returncode": returncode,
                        "status": "success" if returncode == 0 else "failed",
                    }

                    # Include session info if persistent
                    if persistent and session_id and returncode == 0:
                        sess_path = self._session_path(session_id)
                        if sess_path.exists():
                            try:
                                saved = pickle.loads(sess_path.read_bytes())
                                result["session_id"] = session_id
                                result["session_vars"] = list(saved.keys())
                            except Exception:
                                pass

                    return result
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

    def _wrap_code(
        self, code: str, session_id: str = "", persistent: bool = False
    ) -> str:
        """Wrap user code with necessary imports and setup.

        When persistent=True, injects:
        - A preamble that restores saved variables from the session pickle
        - An epilogue that saves all user-defined variables back to disk

        Args:
            code: User's policy code
            session_id: Session identifier (empty = no session)
            persistent: Whether to inject session save/restore logic

        Returns:
            Wrapped code ready for execution
        """
        api_dir = str(self.api_path.parent)
        sess_path = str(self._session_path(session_id)) if session_id else ""

        # Base preamble (always present)
        preamble = f"""import sys
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
"""

        # Session restore (injected before user code)
        if persistent and sess_path:
            preamble += f"""
# --- Session restore ---
import pickle as _pkl
_session_path = '{sess_path}'
_session_vars_before = set()
try:
    if os.path.exists(_session_path):
        with open(_session_path, 'rb') as _f:
            _saved = _pkl.load(_f)
        for _k, _v in _saved.items():
            globals()[_k] = _v
        _session_vars_before = set(_saved.keys())
        print(f"[session] Restored {{len(_saved)}} vars from session '{session_id}': {{sorted(_saved.keys())}}")
except Exception as _e:
    print(f"[session] Warning: could not restore session: {{_e}}")
# --- End session restore ---
"""

        # Session save (injected after user code)
        epilogue = ""
        if persistent and sess_path:
            epilogue = f"""

# --- Session save ---
import types as _types
_skip = {{'_pkl', '_f', '_k', '_v', '_saved', '_session_path', '_session_vars_before',
          '_skip', '_to_save', '_types', '_e',
          # Standard preamble symbols (not user-defined)
          'sys', 'os', 'math', 'json', 'datetime', 'timedelta',
          'Dict', 'Any', 'List', 'Optional'}}
_to_save = {{}}
for _k, _v in dict(globals()).items():
    if _k.startswith('_') or _k in _skip:
        continue
    if isinstance(_v, _types.ModuleType):
        continue
    # Skip all callables — functions/classes defined in __main__ can't be
    # unpickled in a different subprocess
    if callable(_v):
        continue
    try:
        _pkl.dumps(_v)  # Test if picklable
        _to_save[_k] = _v
    except Exception:
        pass  # Skip unpicklable objects silently

try:
    os.makedirs(os.path.dirname(_session_path), exist_ok=True)
    with open(_session_path, 'wb') as _f:
        _pkl.dump(_to_save, _f)
    _new_vars = set(_to_save.keys()) - _session_vars_before
    if _new_vars:
        print(f"[session] Saved {{len(_to_save)}} vars ({{len(_new_vars)}} new: {{sorted(_new_vars)}})")
    else:
        print(f"[session] Saved {{len(_to_save)}} vars")
except Exception as _e:
    print(f"[session] Warning: could not save session: {{_e}}")
# --- End session save ---
"""

        return preamble + "\n# User's policy code:\n" + code + epilogue

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
