# Per-turn credential reminders

CLI, ACP and Python SDK turns share a credential-notice generator. It recognizes
known API-key/token prefixes, AWS access IDs, JWT/bearer shapes and password
assignments, including quoted YAML/JSON keys and values. Recognized environment
references such as `${DB_PASSWORD}` or `process.env.DB_PASSWORD` do not trigger it.

The reminder is enabled by default for matching user messages. Passwords and
unprefixed secret material are fully masked; long known prefixed tokens can be
identified with a shortened form. The notice asks the model to avoid full values
in replies, files and commands and use environment-backed configuration instead.
At most eight masked examples are shown to keep large inputs from expanding the
reminder; the instruction still applies to every credential in the message.
A token containing a separately supplied password (or contained in one) is also
fully masked, including when that password appears after the example limit.
Language and credential reminders are combined once. SDK/CLI routing still sees
raw user input. ACP derives reminders before adding host prefixes; its existing
message-preparation path owns appending them. Explicit utility sessions and
continuations do not receive these reminders. No new dependency/configuration.

This is model guidance, not mandatory redaction or enforcement. It can miss
credential shapes, and a model can ignore it. Original user messages and session
logs still contain the original input; this change must not be marketed as log
sanitization. Source tests and a Python build do not update an installed desktop:
consuming hosts need the usual runtime build/install/restart and live validation.
Roll back by reverting the credential-hint change.
