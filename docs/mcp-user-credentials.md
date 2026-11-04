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

`_mcp/credential/clear` accepts
`{ "credentialRef": "user-mcp:user-server", "target": { "owner": "user", "name": "user-server" } }`
and removes the user-owned in-memory binding. It does not erase the user's
configuration or persist a token. Both set and clear default to connector-owned
credentials when `target` is omitted. Hosts must include a user target on clear,
as on set; otherwise a historical connector reference with the same text would
be ambiguous. Connector and user credentials and version counters are isolated
by owner, even when their references are identical.

For a user target, `affectedServers` lists the server only when the current
resolved configuration identifies it as user-owned. A protected or unresolved
same-name server yields an empty list even when the user credential was removed.

## Lifecycle

- Before launching the Agent, a host may set `BOX_AGENT_USER_MCP_CREDENTIAL_TARGETS`
  to a JSON object mapping names to their saved URL configurations (excluding
  `disabled` and `alwaysLoad`), for example `{"remote":{"url":"https://example.test/mcp"}}`.
  Declare only services already confirmed to require host-managed OAuth, using
  the host's discovery cache. Never put runtime headers or tokens in this declaration.
  Cold loading leaves only exact matching user configurations as connecting,
  without creating a transport. After restoring credentials through the existing
  API the host calls `_mcp/reconnect`. Unlisted services, changed configurations,
  system/connector sources, stdio and explicit Authorization headers retain their
  existing startup behavior. Invalid declarations are ignored. Agent/conversation
  readiness never waits for these credentials. This requires an updated runtime.
- Set/clear only change memory and the target owner's credential version, and
  retire pending connection publications for affected servers. Cold-start and
  hot-reconnect handshakes publish only if their per-server generation is still
  current; obsolete transports are closed. Disconnect also retires pending
  handshakes so delayed success or failure cannot restore tools or overwrite status.
- A server configuration construction failure is recorded for that server and
  does not prevent other valid servers from connecting during cold startup.
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

Manual `headers.Authorization` in a user MCP JSON remains supported. Runtime
header names override configured header names case-insensitively. OAuth headers
are overlaid only in memory while their exact configuration binding matches;
clearing OAuth credentials restores the configured manual headers. Both user and
connector OAuth use one runtime credential store and the same set/clear methods.
The `user-mcp:` prefix is a host convention for user targets, not a reserved
connector namespace. Existing connector references with that prefix remain valid
and cannot read or invalidate user-owned credentials with the same reference.
