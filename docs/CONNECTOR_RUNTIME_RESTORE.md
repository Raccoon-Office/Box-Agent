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

Conversation connector authorization applies to eager and deferred tools,
including child inheritance and calls after a selection is revoked. Catalog
refreshes never add tools to utility sessions, including background discovery.

Changing this contract requires rebuilding the packaged ACP runtime and testing
host startup, mixed OAuth/token restoration and process restart. Source tests
do not validate saved credentials or connectivity to real providers.
