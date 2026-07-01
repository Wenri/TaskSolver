# Hybrid tools / mcp_context (native config path)

agy is an **MCP client** and supports **custom agents**, so custom tools and
context can be delivered by configuration — no binary patching. This is the
robust half of the hybrid design (the hooks are the fallback for what config
can't express).

## Custom tools + context via MCP

`../python/agy_mcp_server.py` is a minimal stdio MCP server exposing an example
tool (`tasksolver_query`) and a context resource (`agy://context/notes`). Back
the tool with TaskSolver by filling in the marked spot in that file.

Register it with `mcp.json` (template here). Steps:

1. Substitute the placeholders:
   ```bash
   sed -e "s#<AGYHOOK>#$(cd .. && pwd)#" -e "s#<TASKSOLVER>#$(cd ../.. && pwd)#" \
       mcp.json > mcp.local.json
   ```
2. Place `mcp.local.json`'s `mcpServers` block where your agy build reads MCP
   config. **Verify the path/schema against your agy version** — candidates seen
   in the binary: `~/.antigravity/…`, `~/.agy/…`, `~/.config/antigravity/…`, or a
   per-project `.mcp.json` / `mcpServers` key in agy's `config.json`. (This host
   wasn't onboarded, so the exact location is documented, not yet verified.)
3. Sanity-check the server standalone:
   ```bash
   printf '%s\n%s\n' \
     '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
     '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
     | python3 ../python/agy_mcp_server.py
   ```

## When to use hooks instead

Use the Frida/LD_PRELOAD hooks (see ../README.md) when config can't do it:
injecting context *invisibly* into prompt assembly, overriding a tool result, or
observing/rewriting the model traffic. Catalog of candidate hook points (MCP
manager, tool-spec builders, prompt assembly) is in `../symbols/symbols.json`
under `catalog` — e.g. `...backend.(*ServerBackend).GetPluginMCPSpecs`.

## agents.json

`agents.json` is a placeholder for agy's custom-agent config (the binary
references `agents.json` / `custom_agent`). Schema is version-specific and
unverified here; treat it as a stub to fill once confirmed against your agy.
