# Box-Agent Documentation

Use this index to find the current source-of-truth document for each subsystem.
The root [README](../README.md) and [中文 README](../README_CN.md) are the
product quick start; files here cover implementation, deployment, and host
contracts.

## Build and operate

| Topic | English | 中文 |
| --- | --- | --- |
| Layered architecture and ownership | [Architecture](ARCHITECTURE.md) | [分层架构](ARCHITECTURE_CN.md) |
| Development and extension | [Development Guide](DEVELOPMENT_GUIDE.md) | [开发指南](DEVELOPMENT_GUIDE_CN.md) |
| Understand Anything code map | [Code Map Guide](UNDERSTAND_ANYTHING.md) | [代码图谱指南](UNDERSTAND_ANYTHING_CN.md) |
| Production and runtime packaging | [Production Guide](PRODUCTION_GUIDE.md) | [生产指南](PRODUCTION_GUIDE_CN.md) |
| Maintainer review | [Review Guide](REVIEW_GUIDE.md) | [维护者 Review 指南](REVIEW_GUIDE_CN.md) |
| Current published/unreleased state | [Release State](RELEASE_STATE.md) | Same document |
| Third-party model API behavior | [Third-party API Compatibility](THIRD_PARTY_API_COMPATIBILITY.md) | Same document |

## Core runtime behavior

| Topic | English | 中文 |
| --- | --- | --- |
| Context compaction and summarization | [Context Compression](CONTEXT_COMPRESSION.md) | [上下文压缩](CONTEXT_COMPRESSION_CN.md) |
| Explicit sub-agent capabilities and strategies | [Sub-agent Delegation](SUB_AGENT_DELEGATION.md) | [子 Agent 委派](SUB_AGENT_DELEGATION_CN.md) |
| Persistent memory integration | [Memory Integration](MEMORY_INTEGRATION.md) | Same document |
| Controlled HTML PPTX compiler | [PPTX Architecture](PPTX_CONTROLLED_HTML_ARCHITECTURE.md) | [PPTX 架构](PPTX_CONTROLLED_HTML_ARCHITECTURE_CN.md) |
| Controlled HTML PPTX development and extension | [PPTX Development Guide](PPTX_CONTROLLED_HTML_DEVELOPMENT.md) | [PPTX 开发与扩展手册](PPTX_CONTROLLED_HTML_DEVELOPMENT_CN.md) |

## ACP host integration

Start with the [ACP integration index](INTEGRATION.md), then open the specific
wire contract:

- [Host progress events](integration/host-progress-events.md): sub-agent, plan,
  todo, goal, and other structured tool activity.
- [Artifact protocol](ARTIFACT_PROTOCOL.md): generated file discovery and host
  rendering.
- [Action Hint protocol](ACTION_HINT_PROTOCOL.md): model-generated navigation
  hints.
- [Environment Context protocol](ENV_CONTEXT_PROTOCOL.md): host facts injected
  into a session.
- [Filesystem Policy protocol](FILESYSTEM_POLICY_PROTOCOL.md): workspace roots,
  extra allowed directories, and permission negotiation.
- [Memory Match protocol](MEMORY_MATCH_PROTOCOL.md): explicit and automatic
  recall shown to hosts.
- [Memory Promotion protocol](MEMORY_PROPOSAL_PROTOCOL.md): proposal push, list,
  and apply flows.
- [User Decision protocol](USER_DECISION_PROTOCOL.md) / [用户决策协议](USER_DECISION_PROTOCOL_CN.md):
  public Skill decision cards, runtime-bounded defaults, and same-session resume.

## Documentation maintenance

- Verify behavior against current source and focused tests; the
  `.understand-anything/` graph is navigation help, not the source of truth.
- Keep English/Chinese pairs synchronized in the same change.
- Do not update [Release State](RELEASE_STATE.md) as if development code were
  shipped. Published versions need a real tag, package/release links, artifacts,
  and SHA256 values.
- Changes used by officev3 or another packaged host need runtime
  rebuild/install/probe evidence in addition to source tests.
