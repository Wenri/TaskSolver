# Hybrid tools / mcp_context (native config path)

agy is an **MCP client** and supports **custom agents**, so custom tools and
context can be delivered by configuration — no binary patching. This is the
robust half of the hybrid design (the books are the fallback for what config
can't express).

## Custom tools + context via MCP

`../pyagy/agy_mcp_server.py` is a minimal stdio MCP server exposing an example
tool (`tasksolver_query`) and a context resource (`agy://context/notes`). Back
the tool with TaskSolver by filling in the marked spot in that file, or let
`pyagy.config.write_mcp_config()` / `pyagy.write_mcp_servers()` generate a spec
and register it programmatically (used by `Agent(mcp_servers=...)` and
`pyagy.Session(mcp_servers=...)`).

To register `mcp.json` manually:

1. Substitute the placeholders:
   ```bash
   sed -e "s#<AGYBOOK>#$(cd .. && pwd)#" -e "s#<TASKSOLVER>#$(cd ../.. && pwd)#" \
       mcp.json > mcp.local.json
   ```
2. Merge `mcp.local.json`'s `mcpServers` block into `~/.gemini/config/mcp_config.json`
   (or `<data_dir>/.gemini/config/mcp_config.json` for a scoped `HOME`;
   `AGY_MCP_CONFIG_PATH` overrides `pyagy.config.detect_config_path()`).
3. Sanity-check the server standalone:
   ```bash
   printf '%s\n%s\n' \
     '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
     '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
     | python3 ../pyagy/agy_mcp_server.py
   ```

## When to use books instead

Use the static trampoline books (see ../README.md) when config can't do it:
injecting context *invisibly* into prompt assembly, overriding a tool result, or
observing/rewriting the model traffic. Candidate book points (MCP manager, tool-spec
builders, prompt assembly) are in `../symbols/funcmap.tsv.gz`, the full addr→name map
`pixi run shim-symbols` writes — e.g. `zcat ../symbols/funcmap.tsv.gz | grep GetPluginMCPSpecs`.

## Custom agents (`agents.json` vs `.agents/agents/`)

agy discovers workspace custom agents from `<workspace>/.agents/agents/<name>/agent.md`
(YAML frontmatter + prompt body; see `pyagy.model.CHAT_AGENT_MD` and
`../README.md` for the `tasksolver-chat` definition used by `AgyModel(chat=True)`).
`agents.json` here is an older stub from initial binary inspection and is not used
by `pyagy`.
