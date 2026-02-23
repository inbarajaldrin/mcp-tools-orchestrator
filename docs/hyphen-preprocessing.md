# Hyphen-to-Underscore Preprocessing in Code Execution

## Problem

MCP server names commonly use hyphens (e.g., `ros-mcp-server`). When an LLM writes
Python code to call tools, hyphens in identifiers are parsed as subtraction:

```python
ros-mcp-server__move_home()
# Python parses as: ros - mcp - server__move_home()
# → NameError: name 'ros' is not defined
```

This was discovered during testing with Gemini 2.5 Flash, which repeatedly failed
writing `ros-mcp-server__move_home()` in orchestrator code while other models
(Claude Sonnet, GPT-4o, Ollama) produced valid underscored names.

## Root Cause

The root cause is **schema-biased token generation** in the model's function calling
mechanism.

Investigation revealed that the model correctly identifies the problem in its
reasoning text and explicitly writes the underscored form as its intended fix. For
example, in one assistant message:

- **Reasoning text**: "I will replace all instances of `ros-mcp-server__` with
  `ros_mcp_server__`"
- **Function call code** (same message): contains only `ros-mcp-server__` with zero
  underscored equivalents

The model's reasoning knows the answer, but its structured output generation
reproduces the exact tool name tokens from the schema definitions. Comparing the raw
API response (`content_blocks[].function_call.args.code`) with the received tool input
(`toolInput.code`) confirms they are byte-for-byte identical — no intermediate
rewriting occurs. The model itself emits hyphenated names in its function call
arguments despite reasoning otherwise.

This pattern repeated across many consecutive attempts, each time:
1. Correctly diagnosing the problem in reasoning text
2. Explicitly writing the underscored form as the intended fix
3. Producing hyphenated names in the actual code argument
4. Receiving `NameError: name 'ros' is not defined`

This suggests the function calling mechanism is strongly biased toward reproducing
tool name tokens from the schema, overriding the model's own reasoning about valid
Python syntax.

## Solution

The orchestrator preprocesses all code before execution, replacing hyphenated server
name patterns with their underscored equivalents.

**Implementation**: `src/mcp_tools_orchestrator/code_executor.py`

### Phase 1: Exact replacement from known names

At initialization, `_build_hyphen_replacements()` reads `unified_api.py` and builds a
mapping of every hyphenated server prefix and function name to its underscored
equivalent:

```
ros-mcp-server         → ros_mcp_server
ros-mcp-server__move_home → ros_mcp_server__move_home
```

These are applied as exact string replacements, longest first to avoid partial matches.

### Phase 2: Catch-all regex for partial/hybrid forms

Some models produce partially corrected names like `ros_mcp-server__move_ee_top_down`
(mixed hyphens and underscores). A catch-all regex handles these:

```python
re.sub(
    r'([A-Za-z0-9_]*)-([A-Za-z0-9_-]*__)',
    lambda m: m.group(0).replace('-', '_'),
    code,
)
```

This targets only patterns ending with `__` (the server-tool separator), preserving
legitimate hyphens in string arguments like `"half-open"` or `"pick-and-place"`.

### String safety

Both phases only operate on non-string segments of the code. Before applying any
replacements, `_fix_hyphenated_names()` tokenizes the source into string literals
and code segments using a regex that matches Python string delimiters (`'''`, `"""`,
`'`, `"`, and their `f`-string variants). Only the code segments are passed through
`_replace_hyphens()`; string literals are re-joined unchanged.

This is necessary because models sometimes write code that compares runtime data
against hyphenated tool names in string literals:

```python
# Runtime data from stored tool sequences keeps hyphens
name = call_str.split("(")[0]  # → "ros-mcp-server__control_gripper"

# String literal must keep hyphens to match
if name == 'ros-mcp-server__control_gripper':
    fn = ros_mcp_server__control_gripper  # ← identifier, gets underscored
```

Without string-aware tokenization, both the identifier and the string literal
would be converted to underscores, breaking the comparison against runtime data.

## Comparison with Other Implementations

elusznik/mcp-server-code-execution-mode [4] uses a similar approach but applies it at
alias generation time rather than code preprocessing:

```python
# elusznik's _sanitize_identifier
re.sub(r"[^0-9a-zA-Z_]+", "_", value.strip()).lower()
```

Server names are sanitized when the proxy modules are created, and the LLM is told
to use the sanitized alias (`mcp_ros_mcp_server`). However, this does not protect
against schema-biased generation — if the model's function calling mechanism
reproduces hyphenated tool names from the schema (as observed with Gemini), the
sanitized alias in the code gets overwritten and the same breakage occurs.

| Approach | When it runs | Protects against schema-biased generation |
|----------|-------------|------------------------------------------|
| mcp-tools-orchestrator (preprocessing) | At execution time, on submitted code | Yes |
| elusznik (alias sanitization) | At setup time, on proxy names | No |

The preprocessing approach is more robust because it catches hyphenated names
regardless of source — schema-biased generation, copy-paste, or any other path.

## References

[4] elusznik/mcp-server-code-execution-mode —
    https://github.com/elusznik/mcp-server-code-execution-mode
