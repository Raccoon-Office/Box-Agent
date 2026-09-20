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
