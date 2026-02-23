# Tool Orchestration for LLM Agents: Research & Landscape

## Context

This research was conducted to understand the broader landscape of how LLM agents
orchestrate multiple tool calls efficiently. mcp-tools-orchestrator was built
independently to solve a practical problem — batching robot assembly tool calls without
N LLM round-trips. This survey explores what others have built and how the approaches
compare.

---

## The Problem

When an LLM agent needs to call multiple tools, the standard MCP approach requires a
full LLM round-trip per tool call. Tool schemas, inputs, outputs, and reasoning all
accumulate in the context window.

| Problem | Impact |
|---------|--------|
| Schema serialization | Tool definitions consume tokens, repeated each turn |
| Intermediate outputs in context | Tool results occupy space needed for reasoning |
| Sequential round-trips | N tool calls = N LLM inference passes |
| Error accumulation | More turns = more chances for the LLM to lose track |

For batch operations (e.g., repeating a known sequence across 10 items), this means
10x the token cost and latency for no added value — the LLM already knows the sequence.

---

## Approaches

### 1. Traditional MCP (Context-Coupled)

The standard approach. LLM calls one tool at a time, receives the result in context,
reasons, then calls the next.

- **Token cost**: O(N) — schemas re-serialized each turn
- **LLM round-trips**: N
- **Strengths**: Adaptive — can react to each result, change strategy, retry
- **Weaknesses**: Doesn't scale for batch operations or known sequences

### 2. Code Execution (Context-Decoupled)

The LLM generates a single executable program. Tools are exposed as callable functions
in a sandbox. Only the final result enters the LLM's context. The academic literature
calls this pattern "CE-MCP" (Code Execution MCP) [1].

- **Token cost**: Near-constant regardless of tool count
- **LLM round-trips**: 1-2 (write code + read result, optional retry)
- **Strengths**: Batch operations, loops, data transformations, parallel calls
- **Weaknesses**: Must encode all logic upfront; can't adapt mid-execution

mcp-tools-orchestrator uses this approach. Anthropic proposed it conceptually [2],
Cloudflare shipped a production implementation [3], and elusznik built a client-agnostic
version [4].

### 3. DAG-Based Parallel Execution (LLMCompiler)

The LLM writes a structured plan with explicit dependencies (`$1`, `$2`). A framework
parses this into a DAG and executes independent tasks in parallel.

- **LLM round-trips**: 2-3 (plan + join + optional replan)
- **Strengths**: Automatic parallelism without code execution risk
- **Weaknesses**: Limited to tool calls in a DAG — no loops, conditionals, or data
  transforms

Reference: Kim et al., ICML 2024 [5]. Repository: https://github.com/SqueezeAILab/LLMCompiler

### 4. Workflow Engines (Pre-Defined Steps)

Developers pre-define workflows as ordered step sequences. The LLM drives progression
but doesn't design the workflow.

- **LLM round-trips**: N (one per step)
- **Strengths**: Predictable, stateful, pausable/resumable
- **Weaknesses**: Not ad-hoc — workflows must be defined in advance

Not applicable to our use case where the LLM needs to compose tool calls dynamically.

Reference: mcp-workflow — https://github.com/P0u4a/mcp-workflow

---

## Code Execution: Architecture Patterns

### Client-Coupled vs. Client-Agnostic

The fundamental architectural decision for code execution implementations is where the
MCP server connections live.

**Client-coupled**: The sandbox routes tool calls back through the client's existing
MCP connections (via IPC, bindings, etc.). No duplicate server processes, but only works
with that specific client.

**Client-agnostic**: The execution bridge maintains its own independent MCP connections.
Works with any client, but duplicates server connections — problematic for stateful
servers (e.g., isaac-sim connected to a live simulation).

| | Client-Coupled | Client-Agnostic |
|---|---|---|
| **MCP connections owned by** | Client | Bridge/server itself |
| **Duplicate server processes** | No | Yes |
| **Works with any MCP client** | No | Yes |
| **Stateful server conflicts** | None | Possible |
| **Examples** | mcp-tools-orchestrator, Cloudflare Code Mode | elusznik's bridge |

### How Tools Become Callable Functions

Each implementation solves this differently:

**Generated Python module (mcp-tools-orchestrator)** — Pre-generates `unified_api.py`
with function wrappers at initialization. Functions call `_call_tool(server, tool, args)`
which HTTP POSTs to the client's IPC server. LLM imports with
`from unified_api import *`.

**Dynamic proxy modules (elusznik's bridge)** — At runtime, creates synthetic Python
modules (`mcp.servers.filesystem`, etc.). Each attribute is a proxy that sends JSON-RPC
over stdio to the bridge's own `PersistentMCPClient` connections.

**TypeScript interfaces (Cloudflare Code Mode)** — Converts MCP schemas to TypeScript
type definitions. LLM writes TypeScript executed in V8 isolates. Bindings route through
the agent framework's MCP connections.

**Filesystem-based discovery (Anthropic's proposal)** — Tool schemas written as `.tsx`
files on disk. LLM navigates filesystem to discover tools on-demand (zero upfront token
cost). Calls `callMCPTool()` to route to MCP servers. Concept only — no implementation
released.

### Sandbox-to-Server Communication

| Implementation | Transport | Path |
|---|---|---|
| mcp-tools-orchestrator | HTTP POST to IPC server | Subprocess → Client → MCP server |
| elusznik's bridge | JSON-RPC over stdio | Container → Bridge → MCP server (own connections) |
| Cloudflare Code Mode | V8 isolate bindings | Isolate → Agent SDK → MCP server |

### Context Reduction Strategies

A secondary problem: tool schemas themselves consume tokens. Different strategies:

- **mcp-tools-orchestrator**: All tool schemas sent upfront via `list_available_tools`
  (standard approach, higher token cost for large tool sets)
- **elusznik's bridge**: On-demand discovery helpers (~200 tokens). LLM discovers tools
  inside the code via `query_tool_docs()` / `search_tool_docs()`. Reduces ~30K tokens
  to ~200 for tool definitions.
- **Anthropic's proposal**: Tools as files on disk — zero tokens. LLM reads files as
  needed during code generation.

---

## Benchmarks

Felendler et al. [1] benchmarked code execution vs traditional MCP on MCP-Bench across
10 servers with GPT-4o, GPT-4.1, and GPT-4.1 mini:

### Efficiency

| Metric | Traditional MCP | Code Execution | Improvement |
|--------|----------------|----------------|-------------|
| Median token usage | ~50,000 | ~20,000 | ~60% reduction |
| Median turns per task | 10-30 | 1-5 | ~80% reduction |
| Execution time | Higher median | Lower median | Generally faster |

Token savings increase with task complexity — multi-server tasks see the largest gains.

### Task Quality

- Comparable task fulfillment for single and two-server tasks
- Code execution slightly outperforms on some single-server tasks (fewer reasoning
  turns = less error accumulation)
- Code execution slightly underperforms on three-server tasks when the LLM makes
  incorrect global assumptions upfront (no ability to course-correct mid-execution)

### When Each Approach Wins

| Task Type | Better Approach | Why |
|-----------|----------------|-----|
| Structured, data-parallel workflows | Code execution | Single program handles loops, fan-out |
| Linear execution chains | Code execution | Collapses N calls into one pass |
| Tree/fan-out structures | Code execution | Can parallelize downstream calls |
| Iterative refinement | Traditional MCP | Adapts after each observation |
| Open-ended exploration | Traditional MCP | Needs intermediate reasoning |
| Context-sensitive text tasks | Traditional MCP | Benefits from progressive summarization |

> "Neither execution model is universally superior; instead, execution strategy should
> be selected based on the nature of the task." — Felendler et al. [1], Section 8.1

---

## Implementations

### mcp-tools-orchestrator (this project)

- **Type**: Client-coupled code execution
- **Language**: Python subprocess
- **Tool injection**: Pre-generated `unified_api.py` with function wrappers
- **Communication**: HTTP IPC (subprocess → client HTTP server → MCP servers)
- **Discovery**: IPC `/list_tools` endpoint at initialization
- **Sandbox**: Python subprocess (no containerization)
- **Client**: mcp-client-example (custom MCP client)

### elusznik/mcp-server-code-execution-mode

The most architecturally distinct implementation. Client-agnostic — maintains its own
persistent stdio connections to MCP servers.

- **Type**: Client-agnostic code execution
- **Tool injection**: Dynamic proxy modules generated at runtime
- **Communication**: Bidirectional JSON-RPC over stdio (container ↔ bridge)
- **Discovery**: On-demand via discovery helpers (~200 tokens)
- **Sandbox**: Rootless Podman/Docker containers
- **Key feature**: Works with any MCP client (Claude Desktop, Cursor, Claude Code, etc.)
- **Trade-off**: Duplicates MCP server connections
- **Repository**: https://github.com/elusznik/mcp-server-code-execution-mode

### Cloudflare Code Mode

- **Type**: Client-coupled code execution
- **Language**: TypeScript
- **Sandbox**: V8 isolates (millisecond startup, MB memory — lighter than containers)
- **Communication**: Isolate bindings → Cloudflare Agents SDK → MCP servers
- **Key feature**: Typed TypeScript APIs; lightweight V8 isolation
- **Reference**: Varda & Pai [3]
- **URL**: https://blog.cloudflare.com/code-mode/

### Anthropic CE-MCP

- **Type**: Concept only — no released implementation
- **Published**: November 4, 2025
- **Key idea**: Tools as `.tsx` files on disk, filesystem discovery, `callMCPTool()` routing
- **Reference**: Jones & Kelly [2]
- **URL**: https://simonwillison.net/2025/Nov/4/code-execution-with-mcp/

### LLMCompiler

- **Type**: DAG-based parallel execution (not code execution)
- **Key idea**: LLM outputs structured plan with `$id` dependencies; framework builds
  DAG and executes independent tasks in parallel via asyncio
- **Components**: Planner (LLM) → Task Fetching Unit (dependency resolver) → Executor
- **Reference**: Kim et al. [5]
- **Repository**: https://github.com/SqueezeAILab/LLMCompiler

### mcp-workflow

- **Type**: Pre-defined workflow engine
- **Key pattern**: Developer defines activity chains; LLM drives via
  `start`/`continue`/`pause`/`cancel` tools
- **State management**: Session-based memory map persisted across steps
- **Repository**: https://github.com/P0u4a/mcp-workflow

---

## Open Questions in the Community

The official MCP project has an open discussion on where code execution should live,
with no consensus reached:

1. **Client-side**: Client manages sandbox and routes tool calls. Enables cross-server
   orchestration. Risk: every client must reimplement.
2. **Server-side**: Each MCP server provides its own execution environment. Standardized.
   Risk: every server must implement sandboxing; cannot coordinate across servers.
3. **Middleware**: Proxy/gateway layer handles execution. Low fragmentation. Risk:
   architectural complexity.

See: Discussion #639 [6], Discussion #1780 [7].

---

## References

1. Felendler, Y., Gandhi, P.A., Habler, I., Elovici, Y., & Shabtai, A. (2026).
   "From Tool Orchestration to Code Execution: A Study of MCP Design Choices."
   arXiv:2602.15945. https://arxiv.org/abs/2602.15945
   Code: https://anonymous.4open.science/r/cemcpsec-C1F2/

2. Jones, A. & Kelly, C. (2025). "Code execution with mcp: Building more efficient
   agents." Anthropic Engineering Blog, 2025-11-04.

3. Varda, K. & Pai, S. (2025). "Code mode: the better way to use MCP." Cloudflare
   Blog, 2025-09-26. https://blog.cloudflare.com/code-mode/

4. elusznik. mcp-server-code-execution-mode.
   https://github.com/elusznik/mcp-server-code-execution-mode

5. Kim, S. et al. (2024). "An LLM Compiler for Parallel Function Calling." ICML 2024.
   https://github.com/SqueezeAILab/LLMCompiler

6. "Architecture for MCP Code Execution Mode - Client-Side vs Server-Side."
   modelcontextprotocol Discussion #639.
   https://github.com/orgs/modelcontextprotocol/discussions/639

7. "Code execution with MCP: Building more efficient agents."
   modelcontextprotocol Discussion #1780.
   https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/1780
