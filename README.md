# MCP Tools Orchestrator

**Compose tools from multiple MCP servers into unified Python policies**

MCP Tools Orchestrator is a meta-MCP server that enables "Code as Policies" across your entire MCP ecosystem. It automatically discovers tools from all your connected MCP servers and provides a unified Python API for writing complex, multi-server workflows.

## 🎯 What Problem Does This Solve?

**Traditional MCP Usage:**
```
Agent: I'll call tool A
→ Wait for result
Agent: Based on A, I'll call tool B
→ Wait for result
Agent: Based on B, I'll call tool C
→ Wait for result
```

**With MCP Tools Orchestrator:**
```python
# Agent writes one policy script that orchestrates everything
for attempt in range(10):
    result_a = server1__tool_a()
    if result_a["success"]:
        result_b = server2__tool_b(result_a["data"])
        if result_b["status"] == "ready":
            server3__tool_c()
            break
    # Complex logic with loops, conditionals, error handling!
```

**Benefits:**
- ✅ **10-100x faster**: One execution instead of N round-trips
- ✅ **Complex logic**: Loops, conditionals, error handling in Python
- ✅ **Multi-server workflows**: Use tools from ANY server in one policy
- ✅ **Immediate feedback**: Scripts see results and adapt without agent involvement

---

## 🏗️ Architecture

### Hybrid Design: No Duplicate Server Processes

MCP Tools Orchestrator leverages **mcp-client's existing server connections** via HTTP IPC instead of creating its own connections. This prevents duplicate server processes and resource conflicts.

```
┌─────────────────────────────────────────────────────────────┐
│                     mcp-client (CLI)                         │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Agent (Claude/GPT)                                   │   │
│  │  Calls: mcp-orchestrator__execute_composed_code(script)  │   │
│  └──────────────────────────────────────────────────────┘   │
│         ↓                                                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  IPC Server (HTTP)                                    │   │
│  │  http://localhost:random_port                         │   │
│  │  Routes tool calls to appropriate MCP servers         │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
         ↓ (MCP stdio)              ↑ (HTTP IPC)
┌──────────────────────┐    ┌────────────────┐
│   mcp-orchestrator       │    │ Python Script  │
│   (server.py)        │    │ (user policy)  │
│                      │    │                │
│ 1. Generates API     │    │ from unified_  │
│ 2. Executes scripts  │←───│ api import *   │
└──────────────────────┘    └────────────────┘

                ↓ (HTTP POST /call_tool)

┌────────────────────────────────────────────┐
│        Actual MCP Servers                  │
│  (ros-mcp-server, isaac-sim, etc.)         │
└────────────────────────────────────────────┘
```

**Key Points:**
- Client manages **all** MCP server connections
- Orchestrator **never** connects directly to MCP servers
- Policy scripts call client's IPC server via HTTP
- Single process per MCP server (no duplicates!)

---

## 📦 Installation

### Prerequisites

1. **Python 3.10+** (developed with Python 3.13)
2. **mcp-client** with IPC support (see [mcp-client-example](https://github.com/yourusername/mcp-client-example))
3. **uv** package manager

### Install MCP Tools Orchestrator

```bash
cd /home/aaugus11/Documents/mcp-orchestrator
uv sync
```

---

## ⚙️ Configuration

### Step 1: Configure mcp-client

Add mcp-orchestrator to your `mcp_config.json` (typically in `~/Documents/mcp-client-example/`):

```json
{
  "mcpServers": {
    "mcp-orchestrator": {
      "disabled": false,
      "timeout": 60,
      "type": "stdio",
      "command": "/home/aaugus11/Documents/mcp-orchestrator/.venv/bin/python",
      "args": ["/home/aaugus11/Documents/mcp-orchestrator/server.py"]
    },
    "ros-mcp-server": {
      "disabled": false,
      "command": "bash",
      "args": ["-c", "source /opt/ros/humble/setup.bash && python server.py"]
    },
    "isaac-sim": {
      "disabled": false,
      "command": "python",
      "args": ["/path/to/isaac-sim-mcp/server.py"]
    }
  }
}
```

**Note**: The client will automatically:
- Start an IPC HTTP server on a random port
- Set `MCP_CLIENT_IPC_URL` environment variable for orchestrator
- Pass the IPC URL when spawning mcp-orchestrator

### Step 2: Configure Orchestrator's Server List

Create `mcp_servers_config.json` in the orchestrator directory:

```json
{
  "mcpServers": {
    "ros-mcp-server": {
      "command": "bash",
      "args": [
        "-c",
        "source /opt/ros/humble/setup.bash && /home/user/.pyenv/versions/3.10.12/bin/python /path/to/server.py"
      ]
    },
    "isaac-sim": {
      "command": "/home/user/.pyenv/versions/3.10.12/bin/python",
      "args": ["/path/to/isaac-sim-mcp/server.py"]
    },
    "Resources": {
      "command": "/home/user/.pyenv/versions/3.10.12/bin/python",
      "args": ["/path/to/grasp_assembly_server/server.py"]
    }
  }
}
```

**Purpose**: This config is used **only for introspection** (extracting tool signatures). Orchestrator doesn't spawn these servers - the client does!

---

## 🚀 Usage

### 1. Basic Workflow

Start the mcp-client with orchestrator enabled:

```bash
cd ~/Documents/mcp-client-example
mcp-client --all  # Connects to all enabled servers including orchestrator
```

**That's it!** The unified API is **automatically generated** when mcp-orchestrator starts. No manual generation step needed.

Enable orchestrator mode (optional but recommended):

```
/orchestrator-on
```

This hides all direct tools and shows only orchestrator tools, reducing context pollution.

### 2. Ask the Agent to Write a Policy

```
User: Write a script to try grasping 5 different objects and report the success rate
```

The agent will use `execute_composed_code` with a Python script:

```python
from unified_api import *

success_count = 0
total = 5

for i in range(total):
    # Move to grasp position
    result = ros_mcp_server__move_to_grasp(
        object_name=f"object_{i}",
        grasp_id=0,
        mode="sim",
        move_to_object=True
    )

    if result.get("success"):
        # Close gripper
        ros_mcp_server__control_gripper("close", mode="sim")

        # Verify grasp
        verify = ros_mcp_server__verify_grasp(f"object_{i}", mode="sim")
        if verify.get("result") == "SUCCESS":
            success_count += 1
            print(f"✓ Object {i} grasped successfully")
        else:
            print(f"✗ Object {i} grasp failed")

print(f"\nSuccess rate: {success_count}/{total} ({success_count/total*100:.1f}%)")
```

### 3. Available Orchestrator Tools

MCP Tools Orchestrator provides 4 tools to the agent:

#### `execute_composed_code(code: str, timeout: int = 3600)`

Execute Python code with access to ALL tools from ALL connected servers.

**Returns**: `{output: str, returncode: int, status: str}`

#### `list_available_tools()`

Get a structured view of all available tools with their signatures.

**Returns**: `{servers: {...}, total_servers: int, total_tools: int}`

#### `refresh_tools()`

Re-discover tools from all servers (useful if servers were updated).

**Returns**: `{status: str, server_count: int, tool_count: int}`

#### `get_api_documentation()`

Get documentation about the generated unified API.

**Returns**: `str` (formatted documentation)

---

## 📚 How It Works

### 1. Initialization (When Orchestrator Starts)

```python
# In server.py
async def initialize():
    # 1. Check for client IPC URL
    client_ipc_url = os.getenv("MCP_CLIENT_IPC_URL")  # Set by client

    # 2. Generate unified API using introspection
    generator = UnifiedAPIGenerator()
    generator.generate_api_from_config(
        "mcp_servers_config.json",
        "generated/unified_api.py",
        client_ipc_url
    )

    # 3. Initialize code executor
    executor = CodeExecutor("generated/unified_api.py", client_ipc_url)
```

### 2. API Generation via Introspection

```python
# In api_generator.py
class UnifiedAPIGenerator:
    def generate_api_from_config(self, config_path, output_path, ipc_url):
        # For each server in config:
        for server_name, server_config in config["mcpServers"].items():

            # 1. Extract Python path and server script path
            python_path, server_path = self._extract_paths(server_config)

            # 2. Run introspection in isolated subprocess
            #    (avoids dependency conflicts between servers)
            tools = subprocess.run([
                python_path,
                "introspect_server.py",  # Isolated introspection script
                server_path,
                server_name
            ])

            # 3. Parse tool signatures (params, types, defaults, docstrings)
            all_tools[server_name] = parse_tools(tools.stdout)

        # 4. Generate unified_api.py using Jinja2 template
        self._generate_api_file(all_tools, output_path, ipc_url)
```

**Why introspection?**
- Previous approach used JSON schemas → functions had **no parameters**
- Introspection uses Python's `inspect.signature()` → **accurate signatures**
- Each server introspected in its own environment → **no dependency conflicts**

### 3. Generated API Structure

```python
# In generated/unified_api.py (auto-generated)
import requests

_IPC_URL = "http://localhost:54321"  # Client's IPC server

# Tools from ros-mcp-server
def ros_mcp_server__move_to_grasp(
    object_name: str,
    grasp_id: int,
    mode: str = "sim",
    move_to_object: bool = False,
    move_to_safe_height: bool = False
) -> dict:
    """Move to grasp position..."""
    return _call_tool("ros-mcp-server", "move_to_grasp", {
        "object_name": object_name,
        "grasp_id": grasp_id,
        "mode": mode,
        "move_to_object": move_to_object,
        "move_to_safe_height": move_to_safe_height
    })

# Helper function
def _call_tool(server: str, tool: str, arguments: dict) -> dict:
    response = requests.post(
        f"{_IPC_URL}/call_tool",
        json={"server": server, "tool": tool, "arguments": arguments},
        timeout=300
    )
    return response.json()
```

### 4. Code Execution Flow

```python
# In code_executor.py
class CodeExecutor:
    def execute_code(self, user_code: str, timeout: int) -> dict:
        # 1. Wrap user code with imports
        wrapped = f"""
import sys
sys.path.insert(0, '{self.api_dir}')
from unified_api import *

{user_code}
"""

        # 2. Create temp file and execute in subprocess
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py') as f:
            f.write(wrapped)
            result = subprocess.run(
                [self.venv_python, f.name],
                capture_output=True,
                timeout=timeout,
                env={"MCP_ORCHESTRATOR_IPC_URL": self.client_ipc_url}
            )

        # 3. Return output and status
        return {
            "output": result.stdout,
            "error": result.stderr,
            "returncode": result.returncode,
            "status": "success" if result.returncode == 0 else "error"
        }
```

---

## 📁 Project Structure

```
mcp-orchestrator/
├── server.py                         # Main FastMCP server entry point
│
├── src/mcp_orchestrator/
│   ├── api_generator.py              # Introspection-based API generator
│   ├── introspect_server.py          # Isolated server introspection script
│   ├── code_executor.py              # Executes policy code in subprocess
│   ├── __init__.py                   # Package initialization
│   └── py.typed                      # Type hints marker (PEP 561)
│
├── generated/
│   └── unified_api.py                # Auto-generated API (63 tools from 3 servers)
│
├── examples/
│   ├── simple_grasp.py               # Basic grasping workflow
│   ├── multi_server_workflow.py      # Cross-server orchestration
│   └── error_recovery.py             # Error handling patterns
│
├── mcp_servers_config.json           # Server config for introspection
├── pyproject.toml                    # Project metadata and dependencies
├── uv.lock                           # Locked dependencies
│
├── README.md                         # This file
│
├── .python-version                   # Python 3.13 (for pyenv)
└── .gitignore                        # Git ignore rules
```

**Active Files (Clean Architecture):**
- `server.py` - Main MCP server
- `src/mcp_orchestrator/api_generator.py` - API generation via introspection
- `src/mcp_orchestrator/introspect_server.py` - Isolated introspection script
- `src/mcp_orchestrator/code_executor.py` - Policy code execution

**Generated Files:**
- `generated/unified_api.py` - **Auto-generated on every server startup** (no manual steps needed)

---

## 🎓 Example Policies

### Simple Grasp with Verification

```python
from unified_api import *

# Move to home position
ros_mcp_server__move_home()

# Open gripper
ros_mcp_server__control_gripper("open", mode="sim")

# Move to grasp
ros_mcp_server__move_to_grasp(
    object_name="block_1",
    grasp_id=0,
    mode="sim",
    move_to_object=True
)

# Close gripper
ros_mcp_server__control_gripper("close", mode="sim")

# Move to safe height
ros_mcp_server__move_to_grasp(
    object_name="block_1",
    grasp_id=0,
    mode="sim",
    move_to_safe_height=True
)

# Verify grasp
result = ros_mcp_server__verify_grasp("block_1", mode="sim")
if result["result"] == "SUCCESS":
    print("✓ Grasp successful!")
else:
    print("✗ Grasp failed")
```

### Multi-Server Workflow with Error Recovery

```python
from unified_api import *

# Save scene state before attempting grasps
scene_id = isaac_sim__save_scene_state()
print(f"Saved scene state: {scene_id}")

# Try multiple grasp poses
for grasp_id in range(5):
    print(f"\nAttempting grasp {grasp_id}...")

    # Move to grasp
    ros_mcp_server__move_to_grasp(
        object_name="gear",
        grasp_id=grasp_id,
        mode="sim",
        move_to_object=True
    )

    # Close gripper
    ros_mcp_server__control_gripper("close", mode="sim")

    # Move to safe height
    ros_mcp_server__move_to_grasp(
        object_name="gear",
        grasp_id=grasp_id,
        mode="sim",
        move_to_safe_height=True
    )

    # Verify
    result = ros_mcp_server__verify_grasp("gear", mode="sim")

    if result["result"] == "SUCCESS":
        print(f"✓ Grasp {grasp_id} succeeded!")
        break
    else:
        print(f"✗ Grasp {grasp_id} failed, restoring scene...")
        isaac_sim__restore_scene_state()
else:
    print("All grasp attempts failed")
```

### Complex Assembly with Resource Tracking

```python
from unified_api import *

# Get successful grasp configurations from resource server
assembly_id = "3"
configs = Resources__get_object_grasp_configs_by_result(
    assembly_id=assembly_id,
    object_name="gear",
    result="SUCCESS"
)

print(f"Found {len(configs)} successful grasp configs")

# Try each successful configuration
for config in configs:
    grasp_id = config["grasp_id"]
    gripper_state = config["gripper_state"]

    print(f"\nTrying grasp {grasp_id} with gripper {gripper_state}")

    # Set gripper state BEFORE grasping (important!)
    ros_mcp_server__control_gripper(gripper_state, mode="sim")

    # Attempt grasp
    ros_mcp_server__move_to_grasp(
        object_name="gear",
        grasp_id=grasp_id,
        mode="sim",
        move_to_object=True
    )

    # Verify
    result = ros_mcp_server__verify_grasp("gear", mode="sim")

    if result["result"] == "SUCCESS":
        print(f"✓ Successfully grasped using config {grasp_id}")

        # Save this trial to resource server
        Resources__write_assembly_resource(
            assembly_id=assembly_id,
            object_name="gear",
            sequence_id=1,
            assembled_into="base",
            tools_trials=[{
                "trial_id": 1,
                "grasp_id": grasp_id,
                "gripper_state": gripper_state,
                "tools": ["move_to_grasp", "verify_grasp"],
                "result": "SUCCESS"
            }]
        )
        break
```

More examples in the `examples/` directory!

---

## 🔧 Development

### Running in Development

```bash
# The server requires MCP_CLIENT_IPC_URL to be set
# Normally set by mcp-client, but for testing:
export MCP_CLIENT_IPC_URL="http://localhost:12345"
python server.py
```

**Note:** The API is automatically generated on startup. The sections below are for development/debugging only.

### Regenerating the API Manually (Development Only)

```bash
python src/mcp_orchestrator/api_generator.py \
    mcp_servers_config.json \
    generated/unified_api.py \
    http://localhost:12345
```

### Testing Introspection

```bash
# Test introspection of a specific server
python src/mcp_orchestrator/introspect_server.py \
    /path/to/server.py \
    server-name
```

---

## 🚨 Important Notes

### Environment Variables

**Required:**
- `MCP_CLIENT_IPC_URL` - Set automatically by mcp-client when spawning orchestrator

**Optional:**
- `MCP_CLIENT_OUTPUT_DIR` - Shared outputs directory (set by client)

### Introspection Requirements

Each server in `mcp_servers_config.json` must:
1. Be a valid Python script
2. Use MCP decorators (`@mcp.tool()`)
3. Have type-hinted function signatures
4. Be runnable in its specified Python environment

### Python Version Compatibility

**Developed with**: Python 3.13
**Minimum required**: Python 3.10

The `.python-version` file specifies 3.13 for consistency. If you encounter issues, ensure your environment matches or update `.python-version` to your Python version.

### Generated API Location

The unified API is always generated at:
```
/home/aaugus11/Documents/mcp-orchestrator/generated/unified_api.py
```

This path is determined by `server.py`:
```python
script_dir = Path(__file__).parent  # Repository root
generated_dir = script_dir / "generated"
```

---

## 💡 Benefits Over Alternatives

### vs. Manual Tool Calls (Traditional MCP)

| Aspect | Manual Tool Calls | MCP Tools Orchestrator |
|--------|------------------|--------------|
| **Speed** | ~2s per tool call | All tools in one execution |
| **Complexity** | Limited to agent's planning | Full Python: loops, conditionals, functions |
| **Knowledge** | Agent must track state | Script has full context |
| **Latency** | N round-trips | 1 execution |

### vs. Per-Server Custom APIs

| Aspect | Custom APIs | MCP Tools Orchestrator |
|--------|-------------|--------------|
| **Maintenance** | Write API for each server | Auto-generated |
| **Updates** | Manual sync | Auto-refresh |
| **Cross-server** | Complex coordination | Natural in policy code |
| **Type safety** | Manual typing | Auto-extracted from servers |

---

## 🐛 Known Limitations

1. **Abort Signal Handling**
   - Client-side abort functionality is fully implemented (press 'a' to abort)
   - Orchestrator's generated API needs update to detect `[ABORTED]` prefix
   - Scripts currently treat abort as normal error instead of immediate termination

2. **Introspection Edge Cases**
   - Bash-wrapped commands require parsing (works but fragile)
   - Very large servers may timeout during introspection

3. **Error Context**
   - Stack traces from policy scripts can be verbose
   - Errors don't always indicate which server/tool failed

---

## 🗺️ Future Enhancements

- [ ] Implement proper abort signal detection in `unified_api.py`
- [ ] Cache introspection results for faster startup
- [ ] WebSocket support for lower IPC latency
- [ ] Script library/registry for reusable policies
- [ ] Better error messages with server/tool context
- [ ] Support for streaming tool results
- [ ] Interactive debugging mode

---

## 📄 License

MIT License - See LICENSE file for details

---

## 👤 Author

**Aldrin Inbaraj**
Email: aaugus11@asu.edu
GitHub: [Your GitHub Profile]

---

## 🙏 Acknowledgments

- Built on the [Model Context Protocol (MCP)](https://modelcontextprotocol.org)
- Uses [FastMCP](https://github.com/jlowin/fastmcp) for server implementation
- Inspired by "Code as Policies" paradigm from robotics research

---

## 📞 Support

For issues, questions, or contributions:

1. Review the documentation in this README

2. Check example policies in `examples/`

3. Open an issue on GitHub with:
   - Clear description of the problem
   - Relevant logs/error messages
   - Steps to reproduce

---

**Happy Policy Writing! 🚀**
