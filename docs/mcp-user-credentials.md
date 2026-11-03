# Host-managed credentials for user MCP servers

Desktop hosts may implement OAuth discovery and authorization for user-owned URL
MCP servers. Authorization, encrypted persistence, PKCE, refresh and browser
callbacks belong to the host. Box-Agent receives only runtime headers through ACP.

## Binding

`_mcp/credential/set` (ACP extension name `mcp/credential/set`) is shared with
connector OAuth and accepts an optional host-owned user target:

```json
{
  "credentialRef": "user-mcp:user-server",
  "target": {
    "owner": "user",
    "name": "user-server",
    "config": { "url": "https://example.test/mcp", "type": "streamable_http" }
  },
  "headers": { "Authorization": "Bearer <runtime-token>" }
}
```

The response includes `success`, `affectedServers` and a `target` acknowledgement
with only owner/name, never header values. Hosts must require this acknowledgement
for user targets: older Agents may ignore unknown target fields. The host
supplies the complete saved server configuration, including any custom headers.
The binding applies only to a user-owned server of that name whose configuration
matches exactly, excluding the `disabled` flag. URL, transport, header or other
configuration changes invalidate the match. Protected system/connector entries
cannot consume user bindings. `credentialRef` remains forbidden in user files;
the existing connector credential contract is unchanged.

`_mcp/credential/clear` accepts `{ "credentialRef": "user-mcp:user-server" }` and removes the
in-memory binding. It does not erase the user's configuration or persist a token.

## Lifecycle

- Set/clear only change memory and the target server's credential version.
- The host reconnects the target with `_mcp/reconnect` after setting headers.
- Clear does not close an existing transport. On expired/revoked authorization,
  the host also disconnects the target to remove its tools.
- Bindings are lost when the Agent exits. The host restores credentials and
  reconnects authorized user servers in the background after ACP initialize.
- A successful bind is not proof of a successful MCP handshake or tools/list.
- Older Agent builds do not support the user target on the existing credential endpoint. The host must report
  that an updated runtime is required rather than write tokens into `mcp.json` or
  reclassify the server as a connector.

Source tests cover user configuration matching, owner isolation, per-server
fingerprints, secret-free ACP responses and prompt readiness during pending MCP
discovery. A packaged Agent must be rebuilt/installed before a desktop host can
use user targets with that packaged runtime.

Manual `headers.Authorization` in a user MCP JSON remains supported. OAuth headers
are overlaid only in memory while their exact configuration binding matches;
clearing OAuth credentials restores the configured manual headers. Both user and
connector OAuth use one runtime credential store and the same set/clear methods.
The `user-mcp:` reference namespace is reserved for explicit host-owned user targets;
connector configurations cannot use those references.
