# Hosted login recovery

Requests using a hosted `auth.json` token refresh before sending when the JWT
expires within five minutes. If the provider still rejects the credential with
HTTP 401 or business code 200003, refresh once and retry before consuming a stream.
Concurrent rejections of the same file token reuse a replacement already written
by another caller. Explicit tokens and real API keys do not use this recovery.

A rejected refresh credential or a second provider rejection requires login.
Refresh transport failures and server failures retain refresh-error diagnostics;
they are not reported as proof that the user must log in again. No stream is
replayed after content has been consumed. Non-stream OpenAI output limits are
preserved across the authentication retry.

This is runtime source behavior. Packaged hosts require a rebuilt and installed
runtime, process restart, and a fresh live task before deployment is verified.

## Private desktop deployments

A host may opt in with `RACCOON_PRIVATE_DEPLOYMENT=true` and set
`RACCOON_PRIVATE_OFFICE_URL` to the current backend's `settings.office_home`.
These are runtime environment values, not packaging defaults. The service URL
must be absolute HTTP(S), without credentials, query or fragment. Missing or
invalid configuration grants no service access to the login token.

In this mode, LLM and MCP login headers are limited to the configured scheme,
host, port and base path. LLM organization and client metadata headers use the
same boundary. Public Raccoon endpoints do not receive private login credentials.
Explicit provider API keys and existing Authorization headers retain their
original behavior. Without the opt-in flag, existing hosted/client behavior is
unchanged, including the existing official-domain token refresh flow.

LLM requests and MCP SSE/Streamable HTTP connections to the configured private
service do not automatically follow redirects. This prevents a redirect from
forwarding login credentials, organization/client headers or request bodies
outside the configured path or origin. Configure the final service endpoint;
even redirects within the allowed private service are returned as errors.
Default hosted and custom-provider redirect behavior outside this private
service remains unchanged.

The desktop host owns private login and token refresh because the authentication
service may differ from `office_home`. Box-Agent rereads the host-owned `auth.json`
before each request; it does not infer a private refresh endpoint. A private host
should also set the existing `BOX_AGENT_HOSTED_SEARCH_URL` to its actual search
MCP endpoint when initializing managed MCP configuration. This flag alone does
not rewrite a stored MCP URL or make a private search service available.

No package defaults, dependency versions or existing runtime artifacts change.
Source verification still requires a rebuilt runtime, host restart and a fresh
live task before the installed client can be considered fixed.
