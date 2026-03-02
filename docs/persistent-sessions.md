# Persistent Sessions for `execute_composed_code`

## Problem

Without state persistence, every `execute_composed_code` call starts from scratch.
In multi-step workflows (e.g., sequential robot manipulation), this causes three
observable failure modes:

### Redundant recomputation

Configuration variables like grasp widths, object names, and assembly sequences must
be redefined in every call. A 6-line dictionary like:

```python
grasp_widths = {
    "u_brown": {1: 35.0, 2: 84.0, 3: 35.0},
    "u_orange": {1: 35.0, 2: 84.0, 3: 35.0},
    "line_green": {1: 39.8},
    "inverted_u_yellow": {1: 63.1, 2: 56.7, 3: 63.1},
}
```

gets copy-pasted identically across sequential calls because no mechanism exists to
carry it forward. Observed in 80% of calls during ablation testing.

### Full rewrites for single-variable fixes

When a call fails (e.g., wrong `grasp_id`), the LLM must copy-paste the entire code
block (50+ lines) just to change one variable. With persistence, the retry would be:

```python
grasp_id = 2  # everything else carried over from the session
```

### Monolithic blocks that crash

The LLM compensates for missing persistence by cramming entire workflows into single
calls — sometimes 100+ lines with 18+ tool calls. These hit connection timeouts and
return `parse_error`. The model is aware that state won't persist, so it avoids
splitting into multiple calls, creating fragile monolithic blocks instead.

## Solution

Added `session_id` and `persistent` parameters to `execute_composed_code`.

**Implementation**:
- `server.py` — tool interface with `session_id` and `persistent` parameters
- `src/mcp_tools_orchestrator/code_executor.py` — pickle-based save/restore logic

### How it works

1. **Subprocess isolation preserved** — code still runs in a separate process. A bad
   script crashes its own subprocess, not the orchestrator.
2. **Pickle-based persistence** — after execution, user-defined variables are pickled
   to disk. Before the next call with the same `session_id`, they're restored into
   the global namespace via `globals()`.
3. **Selective saving** — only data is persisted (dicts, lists, numbers, strings).
   Modules, callables, and standard preamble symbols (`sys`, `os`, `math`, `json`,
   `datetime`, `timedelta`, typing symbols) are skipped.
4. **Transparent logging** — `[session] Restored N vars` / `[session] Saved N vars`
   lines in stdout so the LLM can see what's available.

### Tool interface

```python
execute_composed_code(
    code="results.append({'u_brown': 'success'})",
    session_id="disassembly_run",
    persistent=True,
)
```

Response includes session metadata when active:

```json
{
    "output": "...",
    "status": "success",
    "returncode": 0,
    "session_id": "disassembly_run",
    "session_vars": ["grasp_widths", "results", "base_name"]
}
```

### Session management

**Session directory** follows `MCP_CLIENT_OUTPUT_DIR` convention (same as
`PYTHON_EXECUTIONS_DIR` in the ROS MCP server):
- If `MCP_CLIENT_OUTPUT_DIR` is set: `{MCP_CLIENT_OUTPUT_DIR}/orchestrator_sessions/`
- Otherwise: `/tmp/mcp_orchestrator_sessions/`

The client harness manages sessions by clearing this directory between runs.

### Backward compatibility

Both parameters default to off (`session_id=""`, `persistent=False`). Existing calls
work identically — no migration needed.

## Design Decisions

### Why pickle, not JSON?

Workflow variables include nested dicts, lists of dicts, tuples, and sets. JSON would
require custom serialization for non-JSON types. Pickle handles all native Python types
natively. Session files are ephemeral temp files scoped to a single run, so the opacity
of pickle format is acceptable.

### Why skip callables?

Functions defined in `__main__` of one subprocess cannot be unpickled in a different
subprocess. Pickle stores a reference like `__main__.do_grasp`, but the new process has
no such function, causing `AttributeError`. This is a fundamental pickle limitation.
Data (dicts, lists, numbers, strings) round-trips perfectly.

### Why not in-process `exec()` like Fusion?

The Fusion 360 MCP bridge uses direct `exec()` because it must access the `adsk` API
inside Fusion's own process — there is no alternative. The orchestrator runs arbitrary
user-composed code, so subprocess isolation is a safety requirement: bad code crashes
its own process, not the server. Pickle-to-disk gives persistence without sacrificing
that isolation.

## Comparison: Code Execution Approaches

| Aspect | Fusion Bridge | Orchestrator | ROS MCP Server |
|--------|---------------|--------------|----------------|
| Execution | In-process `exec()` | Subprocess (Popen) | Subprocess (run) |
| Session state | In-memory dict | Pickle to disk | None |
| Isolation | None (shares process) | Full (subprocess) | Full (subprocess) |
| Timeout | 300s (CustomEvent) | 3600s (poll+kill) | 30s (subprocess) |
| Session cleanup | Manual (dict clear) | Client clears dir | N/A |
