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

### Mi Home control → the native `miloco` integration

The bundled `miot` MCP plugin has been **removed**. Mi Home device control,
family/identity, home events and automations are now provided by the native
**Miloco** integration (`ssr/integrations/miloco.py`), which talks to a local
[Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) service instead of a
third-party MIoT MCP server. See `ssr miloco --help` and the agent tools
`miloco_devices` / `miloco_device_control` / `miloco_family` /
`miloco_activities` / `miloco_automations` / `miloco_sync`.

Miloco runs natively on macOS/Linux only; on Windows run it (and the SSR
gateway) in Docker — `ssr gateway` defaults to a Docker backend on Windows.
