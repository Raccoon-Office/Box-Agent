# Resource Skill plugins

Plugin authors should start with the [Chinese development guide](RESOURCE_PLUGIN_DEVELOPMENT_CN.md),
the [copyable example](../examples/resource-skill-plugin/README.md), and the
[acceptance record](RESOURCE_PLUGIN_ACCEPTANCE_TEMPLATE.md). The guide includes
an isolated authoring check and separates it from real host and model acceptance.

This first phase loads external Skill resources and their HTML settings pages.
Tools, Hooks, executable plugin entry points, and persistent Workflows are not
accepted by this manifest version. Skills use the host's existing tools and
permissions; installing a package does not grant capabilities.

## Package and identity

A package contains `plugin.yaml`, `config.schema.json`, `ui/config.html`, and
the explicitly declared Skill directories. `plugins/ppt/` is the reference
package, containing the eight PPT Skills, exporters, fonts, and notices.

`examples/resource-skill-plugin/` is a minimal installable authoring template.
Its dependency-free HTML page supports Schema-validated JSON configuration and
an informational view for a parameter-free plugin. Copy the directory, change
its package and Skill identities, and follow its README before installing it.
The example and this guide ship with the source distribution. The wheel and
standalone runtime contain the host implementation and builtin PPT compatibility
resources; the independent PPT plugin remains a separate distribution artifact.
Upgrading the core does not install or activate that external package.

The installer copies a package into a content-addressed, read-only directory.
The digest covers resource names, executable flags, and bytes, including the
configuration page and Schema. Editing a development directory does not change
an installed package. The installer accepts an extracted directory; extract
distribution ZIP files outside the managed package store first.

- `id` identifies a package, such as `com.box.ppt`.
- `instance_id` identifies one business configuration of that package.
- A Skill's stable identity is `instance_id/contribution_id`.
- Model-facing names are bounded, namespaced names. Deployment can bind a
  public alias such as `pptx` to one instance's contribution.
- Reserved system Skill names cannot be replaced. Two instances cannot own
  the same public alias. Disabling an instance keeps its aliases unavailable;
  old builtin or user resources with those names cannot reappear.

## Migrating an existing builtin capability

The builtin manifest retains `pptx`, `ppt-fast`, `sn-ppt-entry`, `sn-ppt-story`,
`sn-ppt-standard`, `sn-ppt-dazzle`, `sn-ppt-tools`, and `sn-ppt-doctor` as a
compatibility path. A core-only upgrade with an empty plugin store must retain
these entry points and their resources. Package authoring, installation, and
candidate preparation alone do not complete a migration.

A complete PPT migration installs the fixed external package, prepares its host
dependencies, exposes all eight contributions, binds all eight legacy names
using the [PPT alias mapping](../plugins/ppt/README.md), and confirms activation
through a real host restart. A deployment with partial or no aliases is
coexistence: only the bound names are replaced, and unbound builtin entry points
remain available. Namespaced plugin entries can coexist with those builtins.

| Deployment or startup state | Expected PPT entry points |
| --- | --- |
| New core install, or core-only upgrade; empty plugin store | All eight builtin compatibility entries remain discoverable and callable, subject to ordinary Skill preferences and access checks. |
| External package installed, configuration saved, or candidate prepared; not activated | The previous active state remains in use; a previously builtin-only deployment keeps its builtin entries. |
| Full eight-name migration confirmed by the host | The active plugin owns all eight public names; the builtin copies are hidden. |
| Partial/no alias deployment confirmed by the host | Bound names use the plugin; unbound builtin names remain. This is not a complete PPT migration. |
| Candidate fails validation or startup | Retain the prior active snapshot, including the builtin path if no external generation was previously active. |
| Instance disabled and applied | Its aliases remain unavailable as tombstones; do not reveal same-name builtin or user resources. |
| Active snapshot is readable, but package verification or runtime loading fails | Keep its known aliases suppressed and report the plugin failure; unbound builtin capabilities remain available. |
| Plugin state cannot be read reliably | Suppress the host manifest's `plugin_compatibility_names` conservatively and report the failure. Unknown alias ownership is not evidence of a fresh install. |
| Explicit safe mode: `BOX_AGENT_DISABLE_PLUGINS=1` | Bypass external state/packages and intentionally restore builtin compatibility entries, subject to ordinary Skill preferences and access checks. Do not rewrite plugin state or claim successful migration. |

`plugin_compatibility_names` belongs to the generated **host builtin manifest**,
not `plugin.yaml`. It lists builtin fallback names to suppress only when plugin
state cannot be trusted; the current list is the eight PPT names above. A
readable snapshot supplies its own exact alias ownership instead. If the host
compatibility catalog itself cannot be verified, repair the host runtime rather
than treating it as an empty list and silently restoring old capabilities.

The platform owns delivery of the external package and dependencies and must
verify the actual core/client artifacts across these states. Preserve the
compatibility resources and public discovery entries until the supported
upgrade paths provide and activate the replacement. A successful authoring
check, an installable package, or a documented manual migration does not justify
removing an existing default capability. Keep the compatibility resources
maintained while they remain a supported path.

The protected PPT image-manifest synchronizer follows each exposed `pptx`
entry contribution, including namespaced entries without public aliases. Its
script must belong to that same instance's exposed `ppt_fast` contribution. The host checks
the loader's frozen contribution identities and the installed package digest
again before accepting the script path. Package IDs and display aliases alone
do not grant trust; unrelated instances, changed packages, and escaped resources
cannot supply this protected script. Existing builtin and explicitly bound
expert resources retain their established behavior.

## Configuration and loading

`plugin_platform` in the host configuration selects the management store,
target/profile identity, and administrator-owned runtime checks. Instance
parameters and enablement live in the management store. Environment overrides
`BOX_AGENT_PLUGIN_STATE_DIR`, `BOX_AGENT_PLUGIN_TARGET_ID`, and
`BOX_AGENT_PLUGIN_PROFILE_ID` apply to both management and runtime processes.
With `BOX_AGENT_HOME`, a custom state directory must remain inside that profile.

The desktop settings page and CLI use the same management methods:

| Method | Purpose |
| --- | --- |
| `plugins.list` / `plugins.install` / `plugins.ui` | List instances, install a fixed package, fetch its settings assets |
| `config.get` / `config.validate` / `config.save` | Read, validate, and save one instance's parameters |
| `instance.set_enabled` | Change desired enablement using a revision check |
| `profile.import` / `profile.export` | Explicitly import or export deployment YAML and package locks |
| `profile.prepare_apply` | Freeze a candidate and preflight its package, Skills, and runtime dependencies |
| `activation.status` | Read persistent candidate and active-runtime status |
| `profile.fail_apply` | Record a failed or abandoned candidate |
| `profile.rollback` | Prepare the previous verified snapshot for another checked restart |

Run management without a model configuration:

```sh
python -m box_agent.plugin_management <<'JSON'
{"method":"plugins.list","params":{}}
JSON
```

The installed `box-agent plugins` command and the packaged
`box-agent-acp --plugin-management` binary accept the same single JSON request
on stdin and return one JSON response on stdout. Diagnostics go to stderr.
The legacy module spelling `box_agent.plugins.management_cli` is also accepted.
Exit status is nonzero on an unsuccessful request.

An installation request includes `instance_id`, `package_path`, `enabled`,
`request_id`, optional `config`, `expose`, and `aliases`. Updating an existing
instance requires `expected_revision`. Alias keys are public names; values are
contribution IDs from that package. Use the PPT README's complete alias mapping
when migrating all old PPT entry points; partial bindings preserve coexistence
as described above.

An import request provides `config_path`, `expected_profile_revision`, and
`request_id`; `initialize_only: true` rejects an already configured profile.
The YAML's `plugin_platform.instances` and `bindings.skills` refer to entries
in its `lock_file`. A lock has `schema_version: 1` and `packages`, keyed by the
package reference. Each entry fixes `package_id`, `version`, `package_digest`,
optional `schema_digest`, and an extracted-package `path` relative to the lock.
Already installed digests may omit `path`. Import merges explicitly listed
instances and never activates them. Editing the YAML later does nothing until
another explicit import; frontend changes are not overwritten at startup.

## Save, apply, and recovery

Saving increments `saved_revision`; the running process keeps its frozen
`active_revision` and generation. `expected_revision` prevents lost edits.
`request_id` makes retries idempotent and cannot be reused with different data.
The complete profile also has a revision so an apply cannot assemble a mixture
of independently changed instances.

The host manager prepares a candidate, stops accepting new business work,
waits for active work to finish, and starts the candidate with
`BOX_AGENT_PLUGIN_ACTIVATION_ID`. The real host checks its Skill catalog,
resource reads, native tool dependencies, and declared runtime profiles before
confirming the generation. Public management requests cannot claim confirmation.
Configuration saved during this process remains pending for the next apply.

Waiting for idle does not forcibly cancel a task. Timeout releases the admission
gate and leaves the configuration pending. Candidate failure retains the prior
active snapshot. A confirmed version can be rolled back only through another
prepared and verified restart. The frontend reads durable status after a lost
reply or application restart instead of inferring success from elapsed time.

Set `BOX_AGENT_DISABLE_PLUGINS=1` before starting the host to enter safe mode.
This bypasses external resource loading even when plugin state is corrupt.
It explicitly restores the builtin compatibility path; ordinary Skill usage
preferences and permissions still apply. Safe mode does not alter saved or
active plugin state and is not confirmation that an external plugin works.
Management remains a separate process. A package-content error can be handled
by disabling its instance; structurally corrupted state returns `config_corrupt`
and must be recovered from a known backup. In normal mode, unreadable state
keeps the host-declared compatibility names unavailable instead of silently
falling back to builtin PPT. Do not silently rewrite corrupted state or replay
interrupted business operations.

Restoring a task after a Skill becomes unavailable retains the historical
conversation and reports the unavailable capability in the next request.
That warning does not restore the old Skill or authorize replaying past actions.

The local management audit retains the latest 256 events. Entries identify the
target, profile, active generation, and the operating system's effective user ID
where available; this is the local OS principal, not an authenticated frontend
account. Configuration changes record old/new revisions, field names, and a
content digest without the parameter values. Historical audit entries remain
readable; they are not rewritten to invent missing operator information.

The active process reads frozen Skill bodies and configuration. Source changes
are rejected rather than hot-loaded. Package verification failures isolate
external Skills and report an unhealthy plugin runtime; normal host capabilities
remain available except for aliases and compatibility names suppressed by the
failure rules above. Managed resources are protected by the existing file/shell
write guards. These guards and read-only files are not an OS sandbox for hostile
code. Phase one accepts trusted internal packages.

## Ordinary user Skill switches

Plugin installation/activation and ordinary Skill usage are separate controls.
The desktop owns `config/skill-settings.json`. Its optional `disabledPluginSkills`
array contains exact `{plugin_id, instance_id, contribution_id}` objects; aliases
and display names are not persistence keys. Legacy `disabledSkillNames` continues
to control ordinary non-plugin Skills. Neither preference changes package bytes,
plugin parameters, saved/active revisions, nor the frozen plugin generation.

Each `contributes.skills` declaration can specify `enabled_by: [public_entry_id]`.
These are contribution IDs in the same instance. With several entries the rule
is OR: an internal method remains usable while at least one controlling entry is
available. The method's own usage preference, disabled/broken state and normal
access checks still apply. Missing references, hidden controllers, unexposed
controllers, self references and cycles are rejected before activation. Omitted
`enabled_by` retains prior behavior; no implicit whole-package grouping is inferred.
PPT 1.0.3 binds all seven internal methods to its public `pptx` entry.
This package requires a host implementing the usage gate (the V5 test runtime
and later). The prior V4 host rejects the new manifest field; its unchanged
development version string `1.0.0` does not imply this feature is supported.
Test deliveries pin the actual host/package hashes. New hosts can still load
older packages, but PPT 1.0.2 has no helper links and does not acquire linked
disable behavior automatically; use the updated package for that contract.

The host applies preferences to copies of the frozen Skill plan, publishes the
catalog atomically and refreshes it using the existing settings-file signature.
The desktop refuses usage changes during active work and does not restart or
cancel that work. This is not a new kernel-level whole-turn snapshot contract.
Startup checks validate the installed catalog independently of usage preferences;
an intentionally disabled Skill does not make a healthy plugin fail startup.

ACP `_list_skills` accepts `includeDisabled: true` for the capability-management
view, waits for initial discovery and refreshes preferences before reading. Plugin
metadata includes `user_enabled` (personal preference), `disabled` (effective
unavailability), and `unavailable_code`/`unavailable_reason`. Disabled public
entries remain visible in this view so users can enable them again. The default
model catalog and `get_skill` keep availability checks; diagnostic visibility
never grants permission to read a disabled entry or its controlled methods.

## HTML settings contract

Every package includes a static page and JSON Schema. A parameter-free plugin
uses an informational page and an empty object Schema. The client owns the
plugin list, enablement, apply/rollback actions, and saved/active status.

The page runs in an isolated iframe without same-origin or Node privileges.
A nonce-bound MessageChannel exposes only current-instance configuration
get/validate/save. The trusted container binds target/profile/instance and
package/schema digests; the page cannot redirect those identities, request
arbitrary commands, read host credentials, or invoke an Agent. Local resources
are served from the verified package, and external connections are blocked.

Initialization supplies protocol version, language, light/dark theme, and
configuration state. `context.changed` updates language/theme without losing
an unsaved draft. The reference page supports Chinese, English, and Japanese.
Version conflicts keep the draft and require an explicit reload. A broken
page leaves outer enable/disable and recovery controls usable. An iframe is not
a CPU-isolation guarantee; a stuck client can be closed and CLI recovery used.

## Skill parameters and runtime dependencies

`config_keys` selects non-sensitive fields for a Skill. The loader attaches
them as JSON data with package/instance/revision identity and a `skill_names`
mapping. Both `get_skill` and explicit Skill selection use the same projection.
Values never become evaluated Python, shell, or template code. Current user
instructions and previously confirmed task choices take priority over the PPT
language/mode defaults.

For output language, an explicit current request or a previously explicit user
confirmation for this task takes precedence. Otherwise the currently loaded
Skill's activated configuration may supply the default for its own deliverable;
the prompt language is the fallback. The language of existing source material
is not itself a user confirmation. Conversation and progress messages keep the
host's existing UI-language policy. Configuration remains data and grants no
additional authority or tools.

Inside `SKILL.md`, `{{skill:contribution_id}}` resolves to the current instance's
model-facing name. Other resource documents use the supplied `skill_names`
mapping; ordinary file tools do not perform template expansion.

`requires_host_tools` selects already registered and permitted host tools. It
does not add tools or permissions. Each session checks its actual tool set at
read time, including delegated scopes. Startup also validates the host's default
native catalog. Optional task-specific capabilities such as image generation
are handled by the Skill's existing capability/fallback policy.

`requires.runtime_profile` names a deployment-owned entry in
`plugin_platform.runtime_profiles`. A profile can require Python modules,
Node modules, and a real Playwright Chromium render probe. The supplied example
`ppt-host-v1` requires PIL/fontTools/brotli/playwright/pypdf/docx/psutil in the
host Python interpreter, pptxgenjs/jszip and Playwright in the host Node runtime,
and Chromium. Both Standard and Dazzle renderers use Python Playwright;
the Node browser probe does not replace that dependency. Font subsetting writes
WOFF2 files and requires Brotli support in fontTools. Install these dependencies
before activation; model tasks must stop and report missing dependencies, never
install packages or browsers themselves. ECharts and fonts ship in the package.
Runtime probes use host-provided paths,
are bounded, and do not install dependencies or run plugin code. A missing
profile or dependency prevents candidate activation.

## Build and verification

```sh
python scripts/build_ppt_plugin.py --help
python scripts/generate_skills_manifest.py
python -m pytest tests/test_external_plugin_management.py tests/test_plugin_skill_runtime.py tests/test_ppt_plugin_package.py
```

Package tests and exporter smoke tests do not prove packaged-client behavior.
Release acceptance requires the actual frontend package, a dedicated profile,
HTML edit/save/apply, enable/disable and rollback checks, ordinary conversations,
and real PPT tasks with opened/rendered artifacts. Record core/client revision,
dirty-source digest, package digest, active configuration/generation, runtime
environment, model, and output paths. The first supported execution target is
local desktop; remote targets and further frontend adapters need separate tests.
