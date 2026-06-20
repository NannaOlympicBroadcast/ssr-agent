# Bundled plugins

A *plugin* packages one or more MCP servers (and optional shared credentials).
The on-disk layout follows the Claude Code plugin convention, so an upstream
plugin can be dropped in unchanged:

```
<plugin>/
  .claude-plugin/plugin.json   # declaration: name / version / description
  .mcp.json                    # {"mcpServers": { ... }}
```

The MCP servers may instead be declared inline as an `mcpServers` key inside
`plugin.json` (the Chrome DevTools MCP style); a flat `plugin.json` /
`ssr.plugin.json` at the plugin root is accepted too.

Bundled plugins here are copied into `~/.ssr/plugins` on `ssr init`. User plugins
live in `~/.ssr/plugins/<name>/` and override bundled ones of the same name. The
servers a plugin contributes are merged with `~/.ssr/mcp.json` (`mcp.json` wins
on a name clash) and started by the same MCP manager, exposed to the model as
`mcp__<server>__<tool>`.

Set `"disabled": true` in `plugin.json` to ship a plugin without activating it.

## Shared credentials

A plugin may declare `"credentials": "<namespace>"` in `plugin.json`. Any
`${<namespace>.key}` placeholder appearing in its server `command`/`args`/`env`
is filled from the shared JSON file `~/.ssr/<namespace>.json`. (Placeholders for
any namespace are resolved on demand, so a plugin's `.mcp.json` can reference
`${xiaomi.account}` directly.)

### `chrome-devtools`

Wraps [`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp)
for browser automation, debugging and performance analysis. No credentials.

### `miot` (shares the `xiaomi` namespace)

The `miot` plugin controls MiOT / Mi Home devices and **shares credentials with
the `xiaomi` channel**: both read `~/.ssr/xiaomi.json` (MiService convention —
`MI_USER` / `MI_PASS` / `MI_DID`). The plugin's own `.claude-plugin/plugin.json`
and `.mcp.json` are carried with the plugin; this loader injects the shared
`${xiaomi.*}` credentials into them automatically.
