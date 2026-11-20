# Host-managed connector restoration

Connector credentials are process-local. A desktop host must restore them with
`_mcp/credential/set` after every ACP process initialization, before publishing
the connector source. Never put secrets in source snapshots or status payloads.

`_mcp/source/replace` accepts optional `connectorIds: string[]` for the connector
source. With this non-empty filter, it replaces only servers owned by those
connectors, including their removals. Other connectors and system/user sources
are untouched. Without the filter, it replaces the complete connector source.
Hosts should use scoped updates for provider-specific credential refresh,
connect/disconnect and enable/disable operations.
An incoming server name cannot replace a server belonging to a connector outside
the filter. Removing a connector revokes its live connection even when a lower
priority user definition has the same name; that user source requires its own
reconciliation before activation.

Servers declaring `credentialRef` wait without creating a transport until the
host provides that credential. Only connector-owned definitions may reference
these credentials; system/user definitions with `credentialRef` are rejected
before creating a transport. Resolving or reconnecting one server must not
advance another server's credential/configuration fingerprint: a later
credential update must still cause that other server to reconnect.
Independent servers start concurrently, with their existing per-server timeout
and reconnect lock. Removal and disable operations share that server's reconnect
lock and return only after its pending connection is revoked; they do not hold
up independent servers in the update. Source-level updates remain serialized.
Connector readiness counts only servers in the current configuration, so a
removed server's historical status does not block the remaining healthy servers.
Missing status for a currently configured server still prevents authorization.
Source updates wait for initial discovery, including its startup gate, before
mutating configuration. A readiness timeout returns failure with state unchanged;
the host can retry after startup completes. Ordinary hot reconnects do not impose
this startup barrier on other servers.

Reapplying unchanged configuration retries servers whose last connection failed,
without restarting healthy or still-loading servers. The returned success value
reflects that new attempt, including another failure.

Repeated `_mcp/credential/set` with the same headers and binding is a no-op:
it does not advance the credential version or invalidate a pending handshake.
Changed credentials or an explicitly changed user binding still advance it.
The connection fingerprint includes source ownership/identity, transport,
URL/command/arguments/environment/cwd, headers/credential reference, and connection
timeouts. Catalog names, descriptions, logos, package version, source-file location,
`alwaysLoad`, and tool execution timeout do not restart a healthy transport.
Tool policy and display metadata are refreshed on that existing connection.
Disabling/removing remains explicit; re-enabling still establishes a connection.

Desktop cloud-package auto-updates download and validate outside user-operation
queues, then use the existing Skill publication idle boundary. A running chat or
scheduled turn finishes before the package, MCP/CLI configuration, and Skills
switch. Waiting for idle is outside the connector mutation queue; disconnect,
disable, unbind, or uninstall invalidates that publication before it can run.
Publication rechecks idle and briefly owns the existing runtime configuration
gate: a newly arriving turn waits for publication, including any required MCP
reconnection, to complete. This is an asynchronous admission wait, not synchronous
Electron work. Download/idle waits are not added to ACP initialize; the initializer
and MCP cold-start readiness contract above are unchanged. No additional ACP
restart is requested for MCP package updates. Standalone source-replace calls
still follow the existing reconnect contract; the host owns the auto-update
publication boundary.

Reconnect retires an already-ended connection even if its cleanup logged an
error, so a historical failure cannot prevent a fresh connection attempt. Cleanup
still runs in the task that opened the SDK contexts. If that task is still closing,
the caller waits up to 30 seconds; timeout or caller cancellation leaves the task
available for a later retry. A cleanup error encountered during that wait is
reported once, without permanently blocking subsequent attempts.

Conversation connector authorization applies to eager and deferred tools,
including child inheritance and calls after a selection is revoked. Catalog
refreshes never add tools to utility sessions, including background discovery.

Package Skills have a separate lifecycle from MCP tools. For versioned MCP and CLI
packages, `connectors/skills/.installations/<id>.json` selects the active revision
under `connectors/skill-versions/connector-<id>/<revision>/`. Existing installations
under `connectors/skills/connector-<id>/` are also supported. Paths are relative to
`BOX_AGENT_HOME`. The desktop host disables or removes the owned Skills when it
disables or uninstalls a connector.

Automatic recommendations, including those for selected connectors, use only
regular Skill sources. Startup, session creation, and ordinary prompts never scan
connector receipts. The model can discover an installed package method through
`list_skills` and read its current enabled version through `get_skill`. These
lookups use a separate catalog and lock; another session can chat while a lookup
is pending. A same-name regular Skill keeps its identity in both recommendations
and tool reads. Explicit child Skill delegation can use the same sources. A temporary
MCP outage does not disable guidance. Reading guidance does not grant MCP tools.

This package lifecycle does not change built-in Lark, WeCom, or DingTalk
integrations, their credentials, or ordinary user/SkillHub Skill settings.

Changing this contract requires rebuilding the packaged ACP runtime and testing
host startup, mixed OAuth/token restoration and process restart. Source tests
do not validate saved credentials or connectivity to real providers.
