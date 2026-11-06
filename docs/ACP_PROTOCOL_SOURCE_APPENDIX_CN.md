# ACP 协议源码核对附录

固定 PR #250 提交：`9f0db4746fd9fba08adbe5580811bab946b6f069`（不代表已合入 main）。与 [协议总表](ACP_PROTOCOL_CATALOG_MAIN_CN.md) 配套阅读。

本附录由固定提交的 AST 枚举生成，不读取当前工作区的 Python 实现。结构示例中的 `<runtime: ...>` 表示运行时表达式，不是可直接发送的字面参数；条件字段及动态展开以链接实现为准。工具输出是开放合同，外部 MCP/插件可携带任意 JSON，静态种类不能限制动态数据。

## 1. 路由与协议消费点

上行私有请求（wire 名称补一个 `_`）：`_cancel_inject`, `_goal`, `_inject`, `_list_skills`, `_llm/prompt`, `_mcp/credential/clear`, `_mcp/credential/set`, `_mcp/disconnect`, `_mcp/reconcile`, `_mcp/reconnect`, `_mcp/source/replace`, `_mcp/status`, `_memory_proposal_apply`, `_memory_proposal_list`, `_request_wrap_up`, `_workspace/get`, `_workspace/list`, `_workspace/set`

共 18 个。

### 参数归一化的精确实现

以下是规范化/别名/默认值的完整边界，避免概览遗漏兼容字段。

#### _normalize_llm_binding

[box_agent/acp/__init__.py:485](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L485)

```python
def _normalize_llm_binding(meta: Any) -> dict[str, Any] | None:
    """Parse the host-owned, session-scoped LLM binding extension."""
    if not isinstance(meta, dict):
        return None
    raw = meta.get("llm_binding") or meta.get("llmBinding")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("llm_binding must be an object")

    source = str(raw.get("source") or "").strip()
    model = str(raw.get("model") or "").strip()
    binding_version = raw.get("version", 1)
    if source not in {"builtin", "profile"}:
        raise ValueError(f"unsupported llm_binding source: {source or '<empty>'}")
    if source == "profile" and binding_version != 2:
        raise ValueError("llm_binding.version is invalid")
    if not model or len(model) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in model):
        raise ValueError("llm_binding.model is invalid")
    raw_max_tokens = raw.get("maxTokens", raw.get("max_tokens"))
    raw_context_window = raw.get("contextWindow", raw.get("context_window"))
    if raw_max_tokens is not None and (
        isinstance(raw_max_tokens, bool)
        or not isinstance(raw_max_tokens, int)
        or raw_max_tokens <= 0
    ):
        raise ValueError("llm_binding.maxTokens is invalid")
    if raw_context_window is not None and (
        isinstance(raw_context_window, bool)
        or not isinstance(raw_context_window, int)
        or raw_context_window <= 0
    ):
        raise ValueError("llm_binding.contextWindow is invalid")
    if (
        raw_context_window is not None
        and raw_max_tokens is not None
        and raw_max_tokens >= raw_context_window
    ):
        raise ValueError("llm_binding.maxTokens must be smaller than contextWindow")
    binding: dict[str, Any] = {"source": source, "model": model}
    routing_mode = str(raw.get("routingMode", raw.get("routing_mode")) or "").strip()
    if source == "profile":
        profile_id = str(raw.get("profileId", raw.get("profile_id")) or "").strip()
        profile_revision = str(
            raw.get("profileRevision", raw.get("profile_revision")) or ""
        ).strip()
        if (
            not profile_id
            or not profile_revision
            or routing_mode not in {"auto", "manual"}
        ):
            raise ValueError("llm_binding model profile is invalid")
        binding.update(
            {
                "version": 2,
                "profileId": profile_id,
                "profileRevision": profile_revision,
                "routingMode": routing_mode,
            }
        )
    if raw_context_window is not None:
        binding["contextWindow"] = raw_context_window
    if raw_max_tokens is not None:
        binding["maxTokens"] = raw_max_tokens
    # An explicit manual choice overrides even a stale or malformed automatic pool.
    # Legacy builtin bindings without a routing mode retain pool-based routing.
    if routing_mode != "manual":
        auto_routing = normalize_auto_routing(
            raw.get("autoRouting", raw.get("auto_routing"))
        )
        if auto_routing is not None:
            if raw_context_window is not None:
                # Route and budget the selected model with the same session window.
                for candidate in auto_routing["models"]:
                    if candidate["model"] == model:
                        candidate["contextWindow"] = raw_context_window
                        break
            binding["autoRouting"] = auto_routing
    return binding
```

#### _plan_approval_from_meta

[box_agent/acp/__init__.py:566](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L566)

```python
def _plan_approval_from_meta(meta: Any) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None
    raw = meta.get("planApproval") or meta.get("plan_approval")
    if isinstance(raw, dict):
        return dict(raw)
    decision = meta.get("planApprovalDecision") or meta.get("plan_approval_decision")
    if isinstance(decision, str) and decision.strip():
        request_id = meta.get("planApprovalRequestId") or meta.get("plan_approval_request_id")
        payload: dict[str, Any] = {"decision": decision.strip()}
        if isinstance(request_id, str) and request_id.strip():
            payload["request_id"] = request_id.strip()
        return payload
    return None
```

#### _user_decision_response_from_meta

[box_agent/acp/__init__.py:582](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L582)

```python
def _user_decision_response_from_meta(meta: Any) -> dict[str, str] | None:
    """Normalize the host response to a public ``request_user_decision`` call."""
    if not isinstance(meta, dict):
        return None
    raw = meta.get("userDecision") or meta.get("user_decision")
    if not isinstance(raw, dict):
        return None

    request_id = str(raw.get("request_id") or raw.get("requestId") or "").strip()
    decision_kind = str(
        raw.get("decision_kind") or raw.get("decisionKind") or ""
    ).strip()
    option_id = str(
        raw.get("selected_option_id") or raw.get("selectedOptionId") or ""
    ).strip()
    option_label = str(
        raw.get("selected_option_label") or raw.get("selectedOptionLabel") or ""
    ).strip()
    custom_text = str(raw.get("custom_text") or raw.get("customText") or "").strip()
    trigger = str(raw.get("trigger") or "user").strip().lower()
    if not request_id or not decision_kind or not (option_id or custom_text):
        return None
    if trigger not in {"user", "timeout"}:
        trigger = "user"
    return {
        "request_id": request_id[:128],
        "decision_kind": decision_kind[:128],
        "selected_option_id": option_id[:128],
        "selected_option_label": option_label[:500],
        "custom_text": custom_text[:2_000],
        "trigger": trigger,
    }
```

#### _deprecated_artifact_fields

[box_agent/acp/__init__.py:841](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L841)

```python
def _deprecated_artifact_fields(meta: Any) -> list[str]:
    """Return legacy host fields that are accepted but ignored."""
    if not isinstance(meta, dict):
        return []
    fields: list[str] = []
    for key in (
        "artifact_mode",
        "artifactMode",
        "artifact_root",
        "artifactRoot",
        "artifact_root_dir",
        "artifactRootDir",
        "session_workspace_dir",
        "sessionWorkspaceDir",
    ):
        if key in meta:
            fields.append(key)
    layout = meta.get("workspace_layout") or meta.get("workspaceLayout")
    if isinstance(layout, dict):
        for key in (
            "artifact_root",
            "artifactRoot",
            "artifact_root_dir",
            "artifactRootDir",
            "session_workspace_dir",
            "sessionWorkspaceDir",
        ):
            if key in layout:
                fields.append(f"workspace_layout.{key}")
    return fields
```

#### _goal_request_from_meta

[box_agent/acp/__init__.py:900](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L900)

```python
def _goal_request_from_meta(meta: Any) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None

    raw_goal = meta.get("goal")
    if isinstance(raw_goal, str):
        objective = raw_goal.strip()
        return {"action": "set", "objective": objective} if objective else None
    if isinstance(raw_goal, dict):
        request = dict(raw_goal)
        if "action" not in request and isinstance(request.get("objective"), str):
            request["action"] = "set"
        return request

    raw_objective = meta.get("goal_objective") or meta.get("goalObjective")
    if isinstance(raw_objective, str) and raw_objective.strip():
        return {"action": "set", "objective": raw_objective.strip()}
    return None
```

#### _connector_ids_from_meta

[box_agent/acp/__init__.py:1065](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1065)

```python
def _connector_ids_from_meta(meta: Any) -> set[str] | None:
    """Read the host's per-conversation connector selection without guessing IDs."""
    if not isinstance(meta, dict):
        return None
    marker = meta.get("selected_connector_ids", meta.get("selectedConnectorIds"))
    if marker is None:
        return None
    if not isinstance(marker, list):
        raise ValueError("selected_connector_ids must be an array")
    selected: set[str] = set()
    for raw in marker:
        if not isinstance(raw, str):
            raise ValueError("selected_connector_ids must contain strings")
        connector_id = raw.strip().lower()
        if not _CONNECTOR_ID_PATTERN.fullmatch(connector_id):
            raise ValueError("selected_connector_ids contains an invalid connector id")
        selected.add(connector_id)
    return selected
```

#### _connector_statuses_from_meta

[box_agent/acp/__init__.py:1085](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1085)

```python
def _connector_statuses_from_meta(
    meta: Any,
) -> tuple[tuple[str, str, str], ...] | None:
    """Read the host's complete connector catalog snapshot for one turn."""
    if not isinstance(meta, dict):
        return None
    marker = meta.get("connector_statuses", meta.get("connectorStatuses"))
    if marker is None:
        return None
    if not isinstance(marker, list):
        raise ValueError("connector_statuses must be an array")
    if len(marker) > _MAX_CONNECTOR_STATUSES:
        raise ValueError("connector_statuses contains too many entries")

    statuses: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for raw in marker:
        if not isinstance(raw, dict):
            raise ValueError("connector_statuses entries must be objects")
        raw_id = raw.get("id")
        raw_name = raw.get("name")
        raw_status = raw.get("status")
        if not isinstance(raw_id, str):
            raise ValueError("connector_statuses id must be a string")
        connector_id = raw_id.strip().lower()
        if not _CONNECTOR_ID_PATTERN.fullmatch(connector_id):
            raise ValueError("connector_statuses contains an invalid connector id")
        if connector_id in seen:
            raise ValueError("connector_statuses contains a duplicate connector id")
        if (
            not isinstance(raw_name, str)
            or not raw_name.strip()
            or len(raw_name.strip()) > 128
            or any(character in raw_name for character in "\r\n")
        ):
            raise ValueError("connector_statuses contains an invalid connector name")
        if not isinstance(raw_status, str) or raw_status not in _CONNECTOR_STATUS_VALUES:
            raise ValueError("connector_statuses contains an invalid status")
        seen.add(connector_id)
        statuses.append((connector_id, raw_name.strip(), raw_status))
    return tuple(statuses)
```

#### _connector_status_unavailable_from_meta

[box_agent/acp/__init__.py:1128](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1128)

```python
def _connector_status_unavailable_from_meta(meta: Any) -> bool | None:
    """Read the host signal that the connector snapshot could not be refreshed."""
    if not isinstance(meta, dict):
        return None
    marker = meta.get(
        "connector_status_unavailable", meta.get("connectorStatusUnavailable")
    )
    if marker is None:
        return None
    if not isinstance(marker, bool):
        raise ValueError("connector_status_unavailable must be a boolean")
    return marker
```

#### initialize

[box_agent/acp/__init__.py:1718](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1718)

```python
async def initialize(
    self, protocol_version: int, client_capabilities: Any = None,
    client_info: Implementation | None = None, **meta: Any,
) -> InitializeResponse:
    log.info("initialize", message="ACP initialize request received")
    if isinstance(meta, dict):
        self._client_info = ClientInfo.from_meta(meta.get("host_client_info"))
    self._default_llm_binding = _normalize_llm_binding(meta)
    self._start_memory_bootstrap()
    kwargs: dict[str, Any] = dict(
        protocol_version=PROTOCOL_VERSION,
        agent_capabilities=AgentCapabilities(
            load_session=True, prompt_capabilities=PromptCapabilities(image=True),
        ),
        agent_info=Implementation(name="box-agent", title="Box-Agent", version=__version__),
    )
    skills = self._skills_meta()
    if skills is not None:
        # Pydantic alias: _meta ↔ field_meta
        kwargs["field_meta"] = {"skills": skills}
    resp = InitializeResponse(**kwargs)
    log.info("initialize", message=f"Initialized box-agent v{__version__}, skills={len(skills) if skills else 0}")
    return resp
```

#### new_session

[box_agent/acp/__init__.py:1742](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1742)

```python
async def new_session(
    self, cwd: str, mcp_servers: list[Any] | None = None,
    additional_directories: list[str] | None = None, **meta: Any,
) -> NewSessionResponse:
    self._require_accepting_requests()
    params = NewSessionRequest(
        cwd=cwd or self._config.agent.workspace_dir or ".",
        mcp_servers=mcp_servers or [], additional_directories=additional_directories,
        field_meta=meta or None,
    )
    task = asyncio.create_task(
        self._new_session_with_binding(params), name="acp-session-create",
    )
    self._session_creation_tasks.add(task)
    # Also release tasks cancelled before their coroutine ever starts.
    task.add_done_callback(self._session_creation_tasks.discard)
    return await task
```

### ACP 消费字段逐项索引

| 所在函数 | 当前读取表达式（含别名及默认值） | 来源 |
|---|---|---|
| `_meta_bool` | `meta.get(key, False)` | [box_agent/acp/__init__.py:455](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L455) |
| `_meta_string` | `meta.get(key)` | [box_agent/acp/__init__.py:462](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L462) |
| `_meta_string_list` | `meta.get(key)` | [box_agent/acp/__init__.py:472](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L472) |
| `_normalize_llm_binding` | `meta.get('llmBinding')` | [box_agent/acp/__init__.py:489](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L489) |
| `_normalize_llm_binding` | `meta.get('llm_binding')` | [box_agent/acp/__init__.py:489](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L489) |
| `_plan_approval_from_meta` | `meta.get('planApproval')` | [box_agent/acp/__init__.py:569](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L569) |
| `_plan_approval_from_meta` | `meta.get('plan_approval')` | [box_agent/acp/__init__.py:569](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L569) |
| `_plan_approval_from_meta` | `meta.get('planApprovalDecision')` | [box_agent/acp/__init__.py:572](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L572) |
| `_plan_approval_from_meta` | `meta.get('plan_approval_decision')` | [box_agent/acp/__init__.py:572](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L572) |
| `_plan_approval_from_meta` | `meta.get('planApprovalRequestId')` | [box_agent/acp/__init__.py:574](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L574) |
| `_plan_approval_from_meta` | `meta.get('plan_approval_request_id')` | [box_agent/acp/__init__.py:574](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L574) |
| `_user_decision_response_from_meta` | `meta.get('userDecision')` | [box_agent/acp/__init__.py:586](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L586) |
| `_user_decision_response_from_meta` | `meta.get('user_decision')` | [box_agent/acp/__init__.py:586](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L586) |
| `_deprecated_artifact_fields` | `meta.get('workspaceLayout')` | [box_agent/acp/__init__.py:858](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L858) |
| `_deprecated_artifact_fields` | `meta.get('workspace_layout')` | [box_agent/acp/__init__.py:858](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L858) |
| `_goal_request_from_meta` | `meta.get('goal')` | [box_agent/acp/__init__.py:904](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L904) |
| `_goal_request_from_meta` | `meta.get('goalObjective')` | [box_agent/acp/__init__.py:914](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L914) |
| `_goal_request_from_meta` | `meta.get('goal_objective')` | [box_agent/acp/__init__.py:914](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L914) |
| `_connector_ids_from_meta` | `meta.get('selectedConnectorIds')` | [box_agent/acp/__init__.py:1069](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1069) |
| `_connector_ids_from_meta` | `meta.get('selected_connector_ids', meta.get('selectedConnectorIds'))` | [box_agent/acp/__init__.py:1069](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1069) |
| `_connector_statuses_from_meta` | `meta.get('connectorStatuses')` | [box_agent/acp/__init__.py:1091](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1091) |
| `_connector_statuses_from_meta` | `meta.get('connector_statuses', meta.get('connectorStatuses'))` | [box_agent/acp/__init__.py:1091](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1091) |
| `_connector_status_unavailable_from_meta` | `meta.get('connector_status_unavailable', meta.get('connectorStatusUnavailable'))` | [box_agent/acp/__init__.py:1132](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1132) |
| `_connector_status_unavailable_from_meta` | `meta.get('connectorStatusUnavailable')` | [box_agent/acp/__init__.py:1133](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1133) |
| `_utility_llm_for_meta` | `meta.get('session_id')` | [box_agent/acp/__init__.py:1470](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1470) |
| `initialize` | `meta.get('host_client_info')` | [box_agent/acp/__init__.py:1724](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1724) |
| `_new_session_with_binding` | `meta.get('session_id')` | [box_agent/acp/__init__.py:1762](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1762) |
| `_new_session` | `(getattr(params, 'field_meta', None) or {}).get('session_id')` | [box_agent/acp/__init__.py:1801](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1801) |
| `_new_session` | `meta.get('memory', {})` | [box_agent/acp/__init__.py:1834](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1834) |
| `_new_session` | `meta.get('host_client_info')` | [box_agent/acp/__init__.py:1838](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1838) |
| `_new_session` | `meta.get('session_mode')` | [box_agent/acp/__init__.py:1839](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1839) |
| `_new_session` | `meta.get('deep_think', False)` | [box_agent/acp/__init__.py:1840](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1840) |
| `_new_session` | `meta.get('execution_profile')` | [box_agent/acp/__init__.py:1842](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1842) |
| `_new_session` | `meta.get('utility', False)` | [box_agent/acp/__init__.py:1844](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1844) |
| `_new_session` | `meta.get('hostCapabilities')` | [box_agent/acp/__init__.py:1868](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1868) |
| `_new_session` | `meta.get('host_capabilities')` | [box_agent/acp/__init__.py:1868](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1868) |
| `_new_session` | `meta.get('env_context')` | [box_agent/acp/__init__.py:1892](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1892) |
| `_new_session` | `meta.get('taskId')` | [box_agent/acp/__init__.py:1895](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1895) |
| `_new_session` | `meta.get('task_id')` | [box_agent/acp/__init__.py:1895](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1895) |
| `_new_session` | `meta.get('permission_mode')` | [box_agent/acp/__init__.py:1966](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1966) |
| `_new_session` | `meta.get('officev3_permissions_override')` | [box_agent/acp/__init__.py:2006](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2006) |
| `_new_session` | `meta.get('filesystem_policy')` | [box_agent/acp/__init__.py:2021](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2021) |
| `_new_session` | `fs_meta.get('session_workspace_root')` | [box_agent/acp/__init__.py:2023](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2023) |
| `_new_session` | `fs_meta.get('allowed_directories')` | [box_agent/acp/__init__.py:2024](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2024) |
| `_new_session` | `fs_meta.get('filesystem_scope')` | [box_agent/acp/__init__.py:2025](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2025) |
| `_new_session` | `memory_meta.get('tenant_id')` | [box_agent/acp/__init__.py:2230](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2230) |
| `_new_session` | `memory_meta.get('user_id')` | [box_agent/acp/__init__.py:2231](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2231) |
| `_new_session` | `meta.get('workspaceLayout')` | [box_agent/acp/__init__.py:2235](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2235) |
| `_new_session` | `meta.get('workspace_layout')` | [box_agent/acp/__init__.py:2235](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2235) |
| `load_session` | `meta.get('session_id')` | [box_agent/acp/__init__.py:2431](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2431) |
| `prompt` | `meta.get('sessionContinuation')` | [box_agent/acp/__init__.py:2601](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2601) |
| `prompt` | `meta.get('session_continuation')` | [box_agent/acp/__init__.py:2601](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2601) |
| `prompt` | `meta.get('history_edit')` | [box_agent/acp/__init__.py:2606](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2606) |
| `_prompt_for_session` | `prompt_meta.get('sessionContinuation')` | [box_agent/acp/__init__.py:2896](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2896) |
| `_prompt_for_session` | `prompt_meta.get('session_continuation')` | [box_agent/acp/__init__.py:2896](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2896) |
| `_prompt_for_session` | `prompt_meta.get('taskId')` | [box_agent/acp/__init__.py:2961](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2961) |
| `_prompt_for_session` | `prompt_meta.get('task_id')` | [box_agent/acp/__init__.py:2961](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2961) |
| `_llm_prompt` | `meta.get('host_client_info')` | [box_agent/acp/__init__.py:4162](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4162) |
| `_llm_prompt` | `meta.get('purpose')` | [box_agent/acp/__init__.py:4166](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4166) |
| `_llm_prompt` | `meta.get('session_id')` | [box_agent/acp/__init__.py:4174](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4174) |
| `_record_skill_invocation` | `metadata.get(key)` | [box_agent/acp/__init__.py:4678](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4678) |
| `_record_skill_invocation` | `skill.metadata.get('version')` | [box_agent/acp/__init__.py:4693](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4693) |
| `_skill_usage_attribution` | `metadata.get('dependency_of')` | [box_agent/acp/__init__.py:4727](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4727) |
| `_skill_usage_attribution` | `metadata.get('usage_role')` | [box_agent/acp/__init__.py:4727](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4727) |
| `_sync_explicit_skill_deliveries` | `metadata.get('reason')` | [box_agent/acp/__init__.py:4899](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4899) |

## 2. 共享结构化输入及嵌套元数据

### box_agent/input_context.py

[box_agent/input_context.py:17](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L17)

```python
class _Facts(BaseModel):

    model_config = ConfigDict(extra="forbid", strict=True)
```

[box_agent/input_context.py:21](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L21)

```python
class _TextSource(_Facts):
    text: str = Field(min_length=1, max_length=200_000)
```

[box_agent/input_context.py:25](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L25)

```python
class _ConversationSource(_Facts):
    sessionId: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=1024)
```

[box_agent/input_context.py:30](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L30)

```python
class _ReferencedConversations(_TextSource):
    sources: list[_ConversationSource] = Field(default_factory=list, max_length=3)
```

[box_agent/input_context.py:34](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L34)

```python
class _TeamTask(_Facts):
    # A complete task snapshot aggregates many resources, unlike a single excerpt.
    # Preserve it like userRequest; ACP frame and model context budgets still apply.
    text: str = Field(min_length=1)
```

[box_agent/input_context.py:40](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L40)

```python
class _InputContext(_Facts):
    schemaVersion: Literal[1]
    userRequest: str
    appshot: _TextSource | None = None
    referencedConversations: _ReferencedConversations | None = None
    teamTask: _TeamTask | None = None
```

[box_agent/input_context.py:48](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L48)

```python
class _DocumentSource(_Facts):
    fileName: str = Field(min_length=1, max_length=1024)
    relativePath: str = Field(min_length=1, max_length=4096)

    @field_validator("relativePath")
    @classmethod
    def relative_path(cls, value: str) -> str:
        path = value.replace("\\", "/")
        if (not path.strip() or path.startswith("/") or ":" in path.split("/")[0]
                or ".." in PurePosixPath(path).parts):
            raise ValueError("selection source must be workspace-relative")
        return value
```

[box_agent/input_context.py:62](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L62)

```python
class _Preview(_Facts):
    strategy: Literal["regenerate_cached", "direct_pdf"]
```

[box_agent/input_context.py:66](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L66)

```python
class _Selection(_Facts):
    format: Literal["pdf"]
    pageStart: StrictInt = Field(ge=1)
    pageEnd: StrictInt = Field(ge=1)
    text: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def ordered_pages(self):
        if self.pageEnd < self.pageStart or not self.text.strip():
            raise ValueError("invalid document selection")
        return self
```

[box_agent/input_context.py:79](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L79)

```python
class _DocumentSelection(_Facts):
    schemaVersion: Literal[1]
    kind: Literal["document_selection"]
    scope: Literal["selection"]
    source: _DocumentSource
    preview: _Preview
    selection: _Selection
```

[box_agent/input_context.py:88](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L88)

```python
class _DatabaseConnection(_Facts):
    connection: str = Field(min_length=1, max_length=128)
    allowedTables: list[str] = Field(max_length=16)

    @field_validator("allowedTables")
    @classmethod
    def table_names(cls, value: list[str]) -> list[str]:
        if any(not name or len(name) > 256 for name in value):
            raise ValueError("invalid table name")
        return value
```

[box_agent/input_context.py:100](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L100)

```python
class _DatabaseContext(_Facts):
    schemaVersion: Literal[1]
    connections: list[_DatabaseConnection] = Field(max_length=8)
    truncated: bool
```

### box_agent/session_continuation.py

#### _parse_image_refs — [box_agent/session_continuation.py:51](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_continuation.py#L51)

```python
def _parse_image_refs(value: Any, role: str) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or (value and role != "user"):
        raise ValueError("INVALID_CONTINUATION_IMAGE_REFERENCES")
    references = []
    seen = set()
    for item in value:
        if not isinstance(item, dict) or "data" in item:
            raise ValueError("INVALID_CONTINUATION_IMAGE_REFERENCES")
        image_id = item.get("imageId")
        if not isinstance(image_id, str) or not image_id or image_id in seen:
            raise ValueError("INVALID_CONTINUATION_IMAGE_REFERENCES")
        reference: dict[str, Any] = {"imageId": image_id}
        for key in ("contentRef", "sha256", "media_type", "messageSeq"):
            if key in item:
                reference[key] = item[key]
        digest = reference.get("sha256")
        path = reference.get("contentRef")
        sequence = reference.get("messageSeq")
        if ((digest is not None and (not isinstance(digest, str)
                                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None))
                or (path is not None and (not isinstance(path, str)
                                         or re.fullmatch(r"images/[0-9a-f]{64}\.(png|jpg)", path) is None))
                or ("media_type" in reference and reference["media_type"] not in {"image/png", "image/jpeg"})
                or (sequence is not None and (type(sequence) is not int or sequence < 0))):
            raise ValueError("INVALID_CONTINUATION_IMAGE_REFERENCES")
        seen.add(image_id)
        references.append(reference)
    return tuple(references)
```

#### _source_session_id — [box_agent/session_continuation.py:83](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_continuation.py#L83)

```python
def _source_session_id(value: dict[str, Any], messages: list[ContinuationMessage]) -> str:
    source = value.get("source_session_id", "")
    if not isinstance(source, str):
        raise ValueError("INVALID_CONTINUATION_IMAGE_SOURCE")
    source = source.strip()
    if any(message.image_refs for message in messages) and not source:
        raise ValueError("CONTINUATION_IMAGE_SOURCE_REQUIRED")
    return source
```

#### parse_session_continuation — [box_agent/session_continuation.py:93](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_continuation.py#L93)

```python
def parse_session_continuation(value: Any) -> SessionContinuation | None:
    """Return one bounded v1 continuation snapshot or ``None``."""

    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    product_session_id = value.get("product_session_id")
    if not isinstance(product_session_id, str) or not product_session_id.strip():
        return None

    raw_messages = value.get("messages")
    if not isinstance(raw_messages, list):
        return None

    messages: list[ContinuationMessage] = []
    total_chars = 0
    truncated = bool(value.get("truncated"))
    for raw in raw_messages[:MAX_MESSAGES]:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        content = raw.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        image_refs = _parse_image_refs(raw.get("image_refs"), role)
        remaining = MAX_TOTAL_CHARS - total_chars
        if remaining <= 0:
            truncated = True
            break
        limit = min(MAX_MESSAGE_CHARS, remaining)
        if len(content) > limit:
            content = content[:limit]
            truncated = True
        if messages and messages[-1].role == role and not image_refs and not messages[-1].image_refs:
            merged = f"{messages[-1].content}\n\n{content}"
            if len(merged) > MAX_MESSAGE_CHARS:
                merged = merged[:MAX_MESSAGE_CHARS]
                truncated = True
            total_chars -= len(messages[-1].content)
            messages[-1] = ContinuationMessage(role=role, content=merged)
            total_chars += len(merged)
        else:
            messages.append(ContinuationMessage(role=role, content=content, image_refs=image_refs))
            total_chars += len(content)

    if len(raw_messages) > MAX_MESSAGES:
        truncated = True
    if not messages:
        return None

    def _text(name: str) -> str:
        raw = value.get(name)
        return raw.strip() if isinstance(raw, str) else ""

    return SessionContinuation(
        product_session_id=product_session_id.strip(),
        reason=_text("reason")[:64],
        messages=tuple(messages),
        truncated=truncated,
        source_task_id=_text("source_task_id")[:128],
        target_task_id=_text("target_task_id")[:128],
        source_session_id=_source_session_id(value, messages),
    )
```

### box_agent/experts.py

#### from_meta — [box_agent/experts.py:89](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L89)

```python
def from_meta(cls, raw: Any) -> "ExpertProfile | None":
    if not isinstance(raw, dict):
        return None
    name = _clean_text(raw.get("name"), max_len=120)
    expert_id = _clean_text(raw.get("id"), max_len=80) or name
    if not name:
        return None
    internal_rules = _clean_list(
        raw.get("internalRules") or raw.get("internal_rules"),
        max_items=16,
        max_len=520,
    )
    constraints = _remove_known_items(
        _clean_list(raw.get("constraints"), max_items=16, max_len=520),
        internal_rules,
    )
    return cls(
        id=expert_id,
        name=name,
        role=_clean_text(raw.get("role"), max_len=320),
        description=_clean_text(raw.get("description"), max_len=640),
        starter_prompt=_clean_block(
            raw.get("starterPrompt") or raw.get("starter_prompt"),
            max_len=1000,
        ),
        instructions=_clean_list(raw.get("instructions"), max_items=16, max_len=520),
        visible_rules=_clean_list(
            raw.get("visibleRules") or raw.get("visible_rules"),
            max_items=16,
            max_len=520,
        ),
        internal_rules=internal_rules,
        default_skills=_clean_list(raw.get("defaultSkills") or raw.get("default_skills")),
        required_skills=_clean_list(raw.get("requiredSkills") or raw.get("required_skills")),
        optional_skills=_clean_list(raw.get("optionalSkills") or raw.get("optional_skills")),
        output_format=_clean_block(raw.get("outputFormat") or raw.get("output_format"), max_len=1200),
        constraints=constraints,
        revision=_clean_text(raw.get("revision"), max_len=120),
    )
```

#### from_meta — [box_agent/experts.py:219](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L219)

```python
def from_meta(cls, raw: Any) -> "ExpertTeamWorkflowStage | None":
    if not isinstance(raw, dict):
        return None
    stage_id = _clean_text(raw.get("id"), max_len=80)
    title = _clean_text(raw.get("title"), max_len=120)
    if not stage_id or not title:
        return None
    return cls(
        id=stage_id,
        title=title,
        owner=_clean_text(raw.get("owner"), max_len=120),
        goal=_clean_text(raw.get("goal"), max_len=420),
        deliverable=_clean_text(raw.get("deliverable"), max_len=420),
    )
```

#### from_meta — [box_agent/experts.py:269](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L269)

```python
def from_meta(cls, raw: Any) -> "ExpertTeamWorkstream | None":
    if not isinstance(raw, dict):
        return None
    member_id = _clean_text(raw.get("memberId") or raw.get("member_id"), max_len=80)
    title = _clean_text(raw.get("title"), max_len=120)
    if not member_id or not title:
        return None
    return cls(
        member_id=member_id,
        title=title,
        brief=_clean_text(raw.get("brief"), max_len=520),
        deliverable=_clean_text(raw.get("deliverable"), max_len=520),
        required=_clean_bool(raw.get("required")),
    )
```

#### from_meta — [box_agent/experts.py:321](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L321)

```python
def from_meta(cls, raw: Any) -> "ExpertTeamOrchestration | None":
    if not isinstance(raw, dict):
        return None
    stages = [
        stage
        for stage in (ExpertTeamWorkflowStage.from_meta(item) for item in raw.get("stages", []))
        if stage is not None
    ][:_MAX_ITEMS]
    workstreams = [
        stream
        for stream in (ExpertTeamWorkstream.from_meta(item) for item in raw.get("workstreams", []))
        if stream is not None
    ][:_MAX_ITEMS]
    review_checklist = _clean_list(
        raw.get("reviewChecklist") or raw.get("review_checklist"),
        max_items=10,
        max_len=420,
    )
    trigger = _clean_text(raw.get("trigger"), max_len=520)
    if not trigger and not stages and not workstreams and not review_checklist:
        return None
    return cls(
        trigger=trigger,
        stages=stages,
        workstreams=workstreams,
        review_checklist=review_checklist,
    )
```

#### from_meta — [box_agent/experts.py:411](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L411)

```python
def from_meta(cls, raw: Any) -> "ExpertTeamMember | None":
    if not isinstance(raw, dict):
        return None
    name = _clean_text(raw.get("name"), max_len=120)
    member_id = _clean_text(raw.get("id"), max_len=80) or name
    if not name:
        return None
    return cls(
        id=member_id,
        name=name,
        role=_clean_text(raw.get("role"), max_len=320),
        source_expert_id=_clean_text(raw.get("sourceExpertId") or raw.get("source_expert_id"), max_len=80),
        instructions=_clean_list(raw.get("instructions"), max_items=6),
        default_skills=_clean_list(raw.get("defaultSkills") or raw.get("default_skills"), max_items=8),
    )
```

#### from_meta — [box_agent/experts.py:478](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L478)

```python
def from_meta(cls, raw: Any) -> "ExpertTeamProfile | None":
    if not isinstance(raw, dict):
        return None
    name = _clean_text(raw.get("name"), max_len=120)
    team_id = _clean_text(raw.get("id"), max_len=80) or name
    if not name:
        return None
    members = [
        member
        for member in (ExpertTeamMember.from_meta(item) for item in raw.get("members", []))
        if member is not None
    ][:_MAX_ITEMS]
    internal_rules = _clean_list(
        raw.get("internalRules") or raw.get("internal_rules"),
        max_items=16,
        max_len=520,
    )
    review_rules = _remove_known_items(
        _clean_list(raw.get("reviewRules") or raw.get("review_rules"), max_items=16, max_len=520),
        internal_rules,
    )
    return cls(
        id=team_id,
        name=name,
        description=_clean_text(raw.get("description"), max_len=640),
        team_persona=_clean_block(raw.get("teamPersona") or raw.get("team_persona"), max_len=1200),
        starter_prompt=_clean_block(
            raw.get("starterPrompt") or raw.get("starter_prompt"),
            max_len=1000,
        ),
        execution_mode=_clean_execution_mode(raw.get("executionMode") or raw.get("execution_mode")),
        leader=ExpertTeamMember.from_meta(raw.get("leader")),
        members=members,
        workflow=_clean_list(raw.get("workflow"), max_items=10, max_len=480),
        orchestration=ExpertTeamOrchestration.from_meta(raw.get("orchestration")),
        visible_rules=_clean_list(
            raw.get("visibleRules") or raw.get("visible_rules"),
            max_items=16,
            max_len=520,
        ),
        internal_rules=internal_rules,
        quality_gates=_clean_list(
            raw.get("qualityGates") or raw.get("quality_gates"),
            max_items=16,
            max_len=520,
        ),
        blocked_conditions=_clean_list(
            raw.get("blockedConditions") or raw.get("blocked_conditions"),
            max_items=12,
            max_len=520,
        ),
        review_rules=review_rules,
        output_format=_clean_block(raw.get("outputFormat") or raw.get("output_format"), max_len=1200),
        revision=_clean_text(raw.get("revision"), max_len=120),
    )
```

#### from_meta — [box_agent/experts.py:703](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L703)

```python
def from_meta(cls, raw_meta: Any) -> "ExpertSessionContext | None":
    if not isinstance(raw_meta, dict):
        return None
    expert = ExpertProfile.from_meta(raw_meta.get("expert"))
    team = ExpertTeamProfile.from_meta(raw_meta.get("expert_team") or raw_meta.get("expertTeam"))
    if expert is None and team is None:
        return None
    directories: list[Path] = []
    bindings: dict[Path, frozenset[str]] = {}
    bound_paths: dict[Path, str] = {}
    for container in (raw_meta.get("expert"), raw_meta.get("expert_team") or raw_meta.get("expertTeam")):
        if not isinstance(container, dict):
            continue
        profiles = [container, container.get("leader"), *(container.get("members") or [])]
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            snapshot = profile.get("packageSnapshot")
            if snapshot is None:
                snapshot = container.get("packageSnapshot")
            if not isinstance(snapshot, dict):
                continue
            # 绑定属于当前包的专家，不能套用专家团合并后的技能名单。
            names = frozenset(
                name for camel, snake in (
                    ("requiredSkills", "required_skills"),
                    ("defaultSkills", "default_skills"),
                    ("optionalSkills", "optional_skills"),
                ) for name in _clean_list(profile.get(camel) or profile.get(snake))
            )
            root = Path(snapshot.get("directory", "")).resolve()
            for skill in snapshot.get("skills") or []:
                if not isinstance(skill, dict) or not isinstance(skill.get("directory"), str):
                    continue
                directory = Path(skill["directory"])
                if not directory.is_absolute() or not directory.resolve().is_relative_to(root):
                    raise ValueError("Skill directory is outside the expert package")
                directory = directory.resolve()
                if directory not in directories:
                    directories.append(directory)
                bound_names = names
                if "skillBindings" in profile:
                    # Lab 按 key 绑定随包技能；同名的其他版本或 TOOL/MARKET 不获得包绑定。
                    declared = profile.get("skillBindings")
                    bound_names = frozenset(
                        binding["slug"] for binding in (declared if isinstance(declared, list) else [])
                        if isinstance(binding, dict)
                        and binding.get("source") == "BUNDLED"
                        and isinstance(skill.get("key"), str) and skill["key"]
                        and binding.get("key") == skill["key"]
                        and isinstance(binding.get("slug"), str)
                        and binding["slug"] == skill.get("slug")
                        and binding["slug"] in names
                    )
                # 未携带 skillBindings 的旧宿主保留按当前专家技能名称绑定的行为。
                bindings[directory] = bindings.get(directory, frozenset()) | bound_names
                slug = skill.get("slug")
                if isinstance(slug, str) and slug in bound_names:
                    bound_paths[directory / "SKILL.md"] = slug
    return cls(expert=expert, team=team, skill_directories=directories,
               skill_bindings_by_directory=bindings, skill_bindings_by_path=bound_paths)
```

### box_agent/env_context.py

#### _sanitize_path — [box_agent/env_context.py:71](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/env_context.py#L71)

```python
def _sanitize_path(owner: str, raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        logger.warning("env_context.%s: drop — value is %s, not str/null", owner, type(raw).__name__)
        return None
    if len(raw) > _MAX_PATH_LEN:
        logger.warning("env_context.%s: drop — path exceeds %d chars", owner, _MAX_PATH_LEN)
        return None
    if _has_unsafe_chars(raw):
        logger.warning("env_context.%s: drop — unsafe chars in path", owner)
        return None
    if not _is_absolute_path(raw):
        logger.warning("env_context.%s: drop — path %r is not absolute", owner, raw)
        return None
    return raw
```

#### _sanitize_cli — [box_agent/env_context.py:89](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/env_context.py#L89)

```python
def _sanitize_cli(raw: Any) -> dict[str, str | None]:
    """Filter the cli mapping. Returns only entries that pass validation.

    Drops (with a warning log) any entry whose name has unsafe chars / is
    too long, or whose value is non-null but is not an absolute path /
    contains unsafe chars / exceeds the length cap.
    """
    if not isinstance(raw, dict):
        return {}

    cleaned: dict[str, str | None] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or len(name) > _MAX_NAME_LEN:
            logger.warning("env_context.cli: drop entry with invalid name %r", name)
            continue
        if _has_unsafe_chars(name):
            logger.warning("env_context.cli: drop entry %r — unsafe chars in name", name)
            continue

        if value is None:
            cleaned[name] = None
            continue
        path = _sanitize_path(f"cli[{name}]", value)
        if path is not None:
            cleaned[name] = path
    return cleaned
```

#### _sanitize_platform — [box_agent/env_context.py:117](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/env_context.py#L117)

```python
def _sanitize_platform(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    if not raw or len(raw) > _MAX_PLATFORM_LEN:
        logger.warning("env_context.platform: drop — length %d", len(raw))
        return None
    if not all(ch in _PLATFORM_ALLOWED_CHARS for ch in raw):
        logger.warning("env_context.platform: drop — disallowed chars in %r", raw)
        return None
    return raw
```

#### _sanitize_provider — [box_agent/env_context.py:129](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/env_context.py#L129)

```python
def _sanitize_provider(raw: Any) -> str | None:
    if raw is None or not isinstance(raw, str):
        return None
    if not raw or len(raw) > _MAX_NAME_LEN:
        return None
    if _has_unsafe_chars(raw):
        return None
    if not all(ch in _PLATFORM_ALLOWED_CHARS for ch in raw):
        return None
    return raw
```

#### _sanitize_label — [box_agent/env_context.py:141](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/env_context.py#L141)

```python
def _sanitize_label(raw: Any, *, max_len: int = _MAX_NAME_LEN) -> str | None:
    if raw is None or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or len(raw) > max_len:
        return None
    if _has_unsafe_chars(raw):
        return None
    return raw
```

#### _sanitize_runtimes — [box_agent/env_context.py:152](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/env_context.py#L152)

```python
def _sanitize_runtimes(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}

    allowed_runtime_fields = {
        "python": ("path", "shell_path", "sandbox_path", "ready", "provider"),
        "node": ("path", "npm", "npx", "node_modules", "ready", "provider"),
    }
    path_fields = {"path", "shell_path", "sandbox_path", "npm", "npx", "node_modules"}
    cleaned: dict[str, dict[str, Any]] = {}

    for kind, value in raw.items():
        if kind not in allowed_runtime_fields or not isinstance(value, dict):
            logger.warning("env_context.runtimes: drop unsupported runtime %r", kind)
            continue

        runtime: dict[str, Any] = {}
        for field_name in allowed_runtime_fields[kind]:
            if field_name not in value:
                continue
            field_value = value[field_name]
            if field_name in path_fields:
                path = _sanitize_path(f"runtimes.{kind}.{field_name}", field_value)
                if path is not None:
                    runtime[field_name] = path
            elif field_name == "ready":
                if isinstance(field_value, bool):
                    runtime[field_name] = field_value
            elif field_name == "provider":
                provider = _sanitize_provider(field_value)
                if provider is not None:
                    runtime[field_name] = provider

        if runtime:
            cleaned[kind] = runtime
    return cleaned
```

#### _sanitize_obsidian — [box_agent/env_context.py:190](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/env_context.py#L190)

```python
def _sanitize_obsidian(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    cleaned: dict[str, Any] = {}
    for field_name in ("enabled", "cli_available", "app_running"):
        value = raw.get(field_name)
        if isinstance(value, bool):
            cleaned[field_name] = value

    vault_name = _sanitize_label(raw.get("vault_name"))
    if vault_name is not None:
        cleaned["vault_name"] = vault_name

    for field_name in ("vault_path", "cli_path", "app_path"):
        value = raw.get(field_name)
        if value is None:
            continue
        path = _sanitize_path(f"obsidian.{field_name}", value)
        if path is not None:
            cleaned[field_name] = path

    return cleaned
```

#### _sanitize_hyperframes — [box_agent/env_context.py:215](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/env_context.py#L215)

```python
def _sanitize_hyperframes(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    cleaned: dict[str, Any] = {}
    for field_name in ("installed", "available", "template", "ffmpeg", "ffprobe"):
        value = raw.get(field_name)
        if isinstance(value, bool):
            cleaned[field_name] = value

    version = _sanitize_label(raw.get("version"), max_len=32)
    if version is not None:
        cleaned["version"] = version

    browser = _sanitize_label(raw.get("browser"), max_len=64)
    if browser is not None:
        cleaned["browser"] = browser

    return cleaned
```

#### from_meta — [box_agent/env_context.py:345](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/env_context.py#L345)

```python
def from_meta(cls, raw: Any) -> "EnvContext | None":
    """Parse ``_meta.env_context``. Returns ``None`` for missing/invalid input."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning("env_context must be an object, got %s", type(raw).__name__)
        return None

    known: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _KNOWN_TOP_LEVEL_KEYS:
            known[key] = value
        else:
            extras[key] = value
            logger.info("env_context: passthrough unknown key %s (not rendered into prompt)", key)

    # Sanitize known fields up-front so prompt renderer can trust them.
    if "cli" in known:
        known["cli"] = _sanitize_cli(known["cli"])
    if "platform" in known:
        known["platform"] = _sanitize_platform(known["platform"])
    if "runtimes" in known:
        known["runtimes"] = _sanitize_runtimes(known["runtimes"])
    if "obsidian" in known:
        known["obsidian"] = _sanitize_obsidian(known["obsidian"])
    if "hyperframes" in known:
        known["hyperframes"] = _sanitize_hyperframes(known["hyperframes"])

    try:
        ctx = cls.model_validate(known)
    except Exception as exc:
        logger.warning("env_context validation failed: %s", exc)
        return None
    ctx.extras = extras
    return ctx
```

### box_agent/client_info.py

#### from_meta — [box_agent/client_info.py:62](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/client_info.py#L62)

```python
def from_meta(cls, raw: Any) -> ClientInfo | None:
    if not isinstance(raw, Mapping):
        return None
    client_info = cls(
        name=_read_alias(raw, "name"),
        platform=_read_alias(raw, "platform"),
        version=_read_alias(raw, "version"),
        os_version=_read_alias(raw, "os_version", "osVersion"),
        channel=_read_alias(raw, "channel"),
        device_id=_read_alias(raw, "device_id", "deviceId"),
    )
    return client_info if any(
        (
            client_info.name,
            client_info.platform,
            client_info.version,
            client_info.os_version,
            client_info.channel,
            client_info.device_id,
        )
    ) else None
```

## 3. 下行 rawOutput 静态类型及结构示例

下面列出协议适配器及工具/事件生产者中的业务 `type` 字典。它们通过 `tool_call_update.rawOutput` 或其嵌套字段下行；少数安全/图片中间类型先被工具包装，不能把每个内部字典误当成独立 ACP method。普通工具无 type 的输出由总表的开放工具结果合同覆盖。

### `artifact` — box_agent/acp/__init__.py:274

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:277](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L277)。

参数：`type`, `kind`, `placement`, `filename`, `rel_path`, `abs_path`, `uri`, `mime`, `size`, `sha256`, `produced_at`, `tool_call_id`

结构示例：
```json
{
  "type": "artifact",
  "kind": "<runtime: art.kind>",
  "placement": "<runtime: art.placement>",
  "filename": "<runtime: art.filename>",
  "rel_path": "<runtime: art.rel_path>",
  "abs_path": "<runtime: art.abs_path>",
  "uri": "<runtime: art.uri>",
  "mime": "<runtime: art.mime>",
  "size": "<runtime: art.size>",
  "sha256": "<runtime: art.sha256>",
  "produced_at": "<runtime: art.produced_at>",
  "tool_call_id": "<runtime: art.tool_call_id>"
}
```

### `skills_usage` — box_agent/acp/__init__.py:4777

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:4763](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4763)。

参数：`type`, `skills`, `current`

结构示例：
```json
{
  "type": "skills_usage",
  "skills": "<runtime: list(used_skill_names)>",
  "current": "<runtime: skill_name>"
}
```

### `turn_usage` — box_agent/acp/__init__.py:4846

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:4832](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4832)。

参数：`type`, `version`, `sessionId`, `session_id`, `acpSessionId`, `taskId`, `task_id`, `turnId`, `turn_id`, `skills`, `skillInvocations`, `tools`, `mcp`, `tokenUsage`

结构示例：
```json
{
  "type": "turn_usage",
  "version": 3,
  "sessionId": "<runtime: billing_session_id or state.upstream_session_id or session_id>",
  "session_id": "<runtime: billing_session_id or state.upstream_session_id or session_id>",
  "acpSessionId": "<runtime: session_id>",
  "taskId": "<runtime: task_context.task_id>",
  "task_id": "<runtime: task_context.task_id>",
  "turnId": "<runtime: turn_id>",
  "turn_id": "<runtime: turn_id>",
  "skills": "<runtime: list(used_skill_names)>",
  "skillInvocations": "<runtime: list(skill_invocations)>",
  "tools": "<runtime: [{'name': name, 'count': count} for name, count in used_tool_counts.items()]>",
  "mcp": "<runtime: [{'server': server_name, 'tool': tool_name, 'name': f'{server_name}.{tool_name}', 'count': count} for (server_name, tool_name), count in used_mcp_tool_counts.items()]>",
  "tokenUsage": "<runtime: token_usage>"
}
```

### `skills_usage` — box_agent/acp/__init__.py:4926

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:4912](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4912)。

参数：`type`, `skills`, `current`, `activationSource`

结构示例：
```json
{
  "type": "skills_usage",
  "skills": "<runtime: list(used_skill_names)>",
  "current": "<runtime: name>",
  "activationSource": "explicit"
}
```

### `structured_image_attachment_result` — box_agent/acp/__init__.py:2557

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:2570](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2570)。

参数：`type`, `success`, `imageCount`, `content`

结构示例：
```json
{
  "type": "structured_image_attachment_result",
  "success": "<runtime: ok>",
  "imageCount": "<runtime: len(image_paths)>",
  "content": "<runtime: text>"
}
```

### `agent_progress` — box_agent/acp/__init__.py:5150

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:5136](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5136)。

参数：`type`, `step`, `content`

结构示例：
```json
{
  "type": "agent_progress",
  "step": "<runtime: s>",
  "content": "<runtime: text>"
}
```

### `llm_output` — box_agent/acp/__init__.py:5199

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:5185](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5185)。

参数：`type`, `messageDisplay`, `step`, `content`, `thinking`, `tool_calls`, `finish_reason`, `usage`, `provider_request_id`

结构示例：
```json
{
  "type": "llm_output",
  "messageDisplay": "<runtime: display_meta>",
  "step": "<runtime: s>",
  "content": "<runtime: content>",
  "thinking": "<runtime: thinking>",
  "tool_calls": "<runtime: tool_calls>",
  "finish_reason": "<runtime: finish_reason>",
  "usage": "<runtime: usage>",
  "provider_request_id": "<runtime: provider_request_id>"
}
```

### `agent_activity_v1` — box_agent/acp/__init__.py:5226

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:5212](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5212)。

参数：`动态展开`, `type`, `step`

结构示例：
```json
{
  "<dynamic-key>": "<runtime: activity>",
  "type": "agent_activity_v1",
  "step": "<runtime: s>"
}
```

### `web_search` — box_agent/acp/__init__.py:5398

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:5384](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5384)。

参数：`动态展开`, `type`

结构示例：
```json
{
  "<dynamic-key>": "<runtime: payload>",
  "type": "web_search"
}
```

### `sub_agent_progress` — box_agent/acp/__init__.py:5522

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:5513](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5513)。

参数：`type`, `parent_tool_call_id`, `sub_agent_id`, `task_preview`, `title`

结构示例：
```json
{
  "type": "sub_agent_progress",
  "parent_tool_call_id": "<runtime: tid>",
  "sub_agent_id": "<runtime: sub_agent_id>",
  "task_preview": "<runtime: preview>",
  "title": "<runtime: sub_title or preview>"
}
```

### `follow_up_suggestions` — box_agent/acp/__init__.py:5002

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:4988](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4988)。

参数：`type`, `turn_id`, `suggestions`

结构示例：
```json
{
  "type": "follow_up_suggestions",
  "turn_id": "<runtime: expected_turn_id>",
  "suggestions": "<runtime: suggestions>"
}
```

### `web_search` — box_agent/acp/__init__.py:5516

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:5507](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5507)。

参数：`动态展开`, `type`

结构示例：
```json
{
  "<dynamic-key>": "<runtime: inner.payload>",
  "type": "web_search"
}
```

### `follow_up_suggestions` — box_agent/acp/__init__.py:5468

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/acp/__init__.py:5459](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5459)。

参数：`type`, `suggestions`

结构示例：
```json
{
  "type": "follow_up_suggestions",
  "suggestions": "<runtime: suggestions>"
}
```

### `goal_snapshot` — box_agent/agent.py:193

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/agent.py:193](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L193)。

参数：`type`, `goal`

结构示例：
```json
{
  "type": "goal_snapshot",
  "goal": "<runtime: goal_payload(agent.goal)>"
}
```

### `intermediate_asset` — box_agent/artifact_publication.py:275

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/artifact_publication.py:275](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/artifact_publication.py#L275)。

参数：`动态展开`, `type`

结构示例：
```json
{
  "<dynamic-key>": "<runtime: raw_output>",
  "type": "intermediate_asset"
}
```

### `intermediate_asset` — box_agent/artifact_publication.py:279

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/artifact_publication.py:279](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/artifact_publication.py#L279)。

参数：`动态展开`, `type`

结构示例：
```json
{
  "<dynamic-key>": "<runtime: raw_output>",
  "type": "intermediate_asset"
}
```

### `intermediate_asset` — box_agent/artifact_publication.py:215

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/artifact_publication.py:215](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/artifact_publication.py#L215)。

参数：`type`

结构示例：
```json
{
  "type": "intermediate_asset"
}
```

### `intermediate_asset` — box_agent/artifact_publication.py:233

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/artifact_publication.py:233](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/artifact_publication.py#L233)。

参数：`type`, `size_bytes`, `sha256`

结构示例：
```json
{
  "type": "intermediate_asset",
  "size_bytes": "<runtime: size>",
  "sha256": "<runtime: digest>"
}
```

### `expert_team_progress` — box_agent/experts.py:652

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/experts.py:652](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L652)。

参数：`type`, `event`, `team`, `leader`, `members`, `workflow`, `visible_rules`

结构示例：
```json
{
  "type": "expert_team_progress",
  "event": "team_start",
  "team": {
    "id": "<runtime: self.id>",
    "name": "<runtime: self.name>",
    "revision": "<runtime: self.revision>",
    "execution_mode": "<runtime: self.execution_mode>"
  },
  "leader": "<runtime: self.leader.to_progress_metadata() if self.leader else None>",
  "members": "<runtime: [member.to_progress_metadata() for member in self.members]>",
  "workflow": "<runtime: self.workflow>",
  "visible_rules": "<runtime: self.visible_rules>"
}
```

### `plan_snapshot` — box_agent/kernel/loop.py:417

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/kernel/loop.py:417](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L417)。

参数：`type`, `version`, `action`, `plan`, `summary`

结构示例：
```json
{
  "type": "plan_snapshot",
  "version": 1,
  "action": "start",
  "plan": "<runtime: plan>",
  "summary": {
    "steps": 0,
    "verification": 0,
    "risks": 0,
    "assumptions": 0
  }
}
```

### `memory_search` — box_agent/kernel/loop.py:489

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/kernel/loop.py:489](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L489)。

参数：`type`, `trigger`, `query`, `matched_memories`

结构示例：
```json
{
  "type": "memory_search",
  "trigger": "auto",
  "query": "<runtime: user_text>",
  "matched_memories": "<runtime: matches>"
}
```

### `plan_snapshot` — box_agent/kernel/loop.py:371

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/kernel/loop.py:371](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L371)。

参数：`type`, `version`, `action`, `plan`, `summary`

结构示例：
```json
{
  "type": "plan_snapshot",
  "version": 1,
  "action": "set",
  "plan": null,
  "summary": {
    "steps": 0,
    "verification": 0,
    "risks": 0,
    "assumptions": 0
  }
}
```

### `permission_request` — box_agent/tools/bash_tool.py:517

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/bash_tool.py:517](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/bash_tool.py#L517)。

参数：`type`, `scope`, `requested_scope`, `path`, `reason`, `temporary_supported`, `persistent_supported`, `persistent_label`, `command`, `risk`

结构示例：
```json
{
  "type": "permission_request",
  "scope": "<runtime: _SAFETY_SCOPE>",
  "requested_scope": "<runtime: _DANGEROUS_COMMAND_SCOPE>",
  "path": "",
  "reason": "<runtime: f'Dangerous command detected: {risk}'>",
  "temporary_supported": true,
  "persistent_supported": false,
  "persistent_label": "",
  "command": "<runtime: command>",
  "risk": "<runtime: risk>"
}
```

### `permission_request` — box_agent/tools/chat_references.py:523

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/chat_references.py:523](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/chat_references.py#L523)。

参数：`type`, `scope`, `requested_scope`, `reason`, `risk`, `command`, `path`, `temporary_supported`, `persistent_supported`, `chat_reference_delete`

结构示例：
```json
{
  "type": "permission_request",
  "scope": "safety",
  "requested_scope": "dangerous_command",
  "reason": "用户明确要求删除聊天引用的文件。确认后相关历史文件或页面依赖可能不可用。",
  "risk": "<runtime: risk>",
  "command": "<runtime: command>",
  "path": "<runtime: '; '.join(sorted(map(str, paths)))>",
  "temporary_supported": true,
  "persistent_supported": false,
  "chat_reference_delete": {
    "paths": "<runtime: sorted(map(str, paths))>",
    "fingerprints": "<runtime: fingerprints>",
    "invocation": "<runtime: hashlib.sha256(json.dumps([name, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()>",
    "request": "<runtime: hashlib.sha256(self._user_request().encode()).hexdigest()>"
  }
}
```

### `policy_decision` — box_agent/tools/engine/execution.py:187

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/engine/execution.py:187](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/engine/execution.py#L187)。

参数：`type`, `tool_name`, `decision`, `retry_count`, `动态展开`

结构示例：
```json
{
  "type": "policy_decision",
  "tool_name": "<runtime: tool_name>",
  "decision": "<runtime: decision>",
  "retry_count": "<runtime: retry_count>",
  "<dynamic-key>": "<runtime: _permission_event_kwargs(permission_request)>"
}
```

### `execution_result` — box_agent/tools/execution_result_tool.py:315

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/execution_result_tool.py:315](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/execution_result_tool.py#L315)。

参数：`type`, `version`, `outcome`, `summary`, `changes`, `checks`, `criteriaEvaluations`, `knownLimitations`, `questions`

结构示例：
```json
{
  "type": "execution_result",
  "version": 1,
  "outcome": "<runtime: normalized_outcome>",
  "summary": "<runtime: normalized_summary>",
  "changes": "<runtime: normalized_changes>",
  "checks": "<runtime: normalized_checks>",
  "criteriaEvaluations": "<runtime: [{'criterionIndex': criterion.get('criterion_index'), 'status': criterion.get('status'), 'summary': str(criterion.get('summary', '')).strip(), 'evidence': _text_list(criterion.get('evidence'))} for criterion in normalized_criteria]>",
  "knownLimitations": "<runtime: normalized_limitations>",
  "questions": "<runtime: normalized_questions>"
}
```

### `write_file_transaction_discarded` — box_agent/tools/file_tools.py:974

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/file_tools.py:974](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L974)。

参数：`type`, `path`, `transaction_state`, `transaction_discarded`, `reason`, `next_chunk_index`, `size_bytes`, `chunks`, `final`

结构示例：
```json
{
  "type": "write_file_transaction_discarded",
  "path": "<runtime: str(state.target)>",
  "transaction_state": "discarded",
  "transaction_discarded": true,
  "reason": "<runtime: reason>",
  "next_chunk_index": "<runtime: state.next_index>",
  "size_bytes": "<runtime: state.size_bytes>",
  "chunks": "<runtime: state.next_index>",
  "final": "<runtime: final>"
}
```

### `write_file_transaction_discarded` — box_agent/tools/file_tools.py:1375

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/file_tools.py:1375](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1375)。

参数：`type`, `path`, `transaction_state`, `transaction_discarded`, `reason`, `next_chunk_index`, `size_bytes`, `chunks`, `final`

结构示例：
```json
{
  "type": "write_file_transaction_discarded",
  "path": "<runtime: str(state.target)>",
  "transaction_state": "discarded",
  "transaction_discarded": true,
  "reason": "<runtime: reason>",
  "next_chunk_index": "<runtime: state.next_index>",
  "size_bytes": "<runtime: state.size_bytes>",
  "chunks": "<runtime: state.next_index>",
  "final": false
}
```

### `write_file_final_chunk_conflict` — box_agent/tools/file_tools.py:1247

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/file_tools.py:1247](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1247)。

参数：`type`, `path`, `transaction_state`, `next_chunk_index`, `restart_required`, `action`, `final`

结构示例：
```json
{
  "type": "write_file_final_chunk_conflict",
  "path": "<runtime: str(target)>",
  "transaction_state": "none",
  "next_chunk_index": 0,
  "restart_required": true,
  "action": "restart",
  "final": true
}
```

### `write_file_chunk` — box_agent/tools/file_tools.py:1354

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/file_tools.py:1354](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1354)。

参数：`type`, `path`, `transaction_state`, `chunk_index`, `next_chunk_index`, `size_bytes`, `duplicate`, `final`

结构示例：
```json
{
  "type": "write_file_chunk",
  "path": "<runtime: str(target)>",
  "transaction_state": "active",
  "chunk_index": "<runtime: chunk_index>",
  "next_chunk_index": "<runtime: state.next_index>",
  "size_bytes": "<runtime: state.size_bytes>",
  "duplicate": false,
  "final": false
}
```

### `artifact` — box_agent/tools/file_tools.py:1513

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/file_tools.py:1513](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1513)。

参数：`type`, `path`

结构示例：
```json
{
  "type": "artifact",
  "path": "<runtime: str(file_path)>"
}
```

### `write_file_chunk` — box_agent/tools/file_tools.py:1036

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/file_tools.py:1036](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1036)。

参数：`type`, `path`, `transaction_state`, `chunk_index`, `next_chunk_index`, `size_bytes`, `动态展开`, `final`

结构示例：
```json
{
  "type": "write_file_chunk",
  "path": "<runtime: str(state.target)>",
  "transaction_state": "active",
  "chunk_index": "<runtime: chunk_index>",
  "next_chunk_index": "<runtime: state.next_index>",
  "size_bytes": "<runtime: state.size_bytes>",
  "<dynamic-key>": "<runtime: receipt>",
  "final": false
}
```

### `artifact` — box_agent/tools/file_tools.py:1665

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/file_tools.py:1665](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1665)。

参数：`type`, `path`

结构示例：
```json
{
  "type": "artifact",
  "path": "<runtime: str(file_path)>"
}
```

### `write_file_committed_state_changed` — box_agent/tools/file_tools.py:1225

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/file_tools.py:1225](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1225)。

参数：`type`, `path`, `transaction_state`, `final`

结构示例：
```json
{
  "type": "write_file_committed_state_changed",
  "path": "<runtime: str(target)>",
  "transaction_state": "none",
  "final": true
}
```

### `write_file_transaction_missing` — box_agent/tools/file_tools.py:1312

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/file_tools.py:1312](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1312)。

参数：`type`, `path`, `transaction_state`, `next_chunk_index`, `restart_required`, `action`, `final`

结构示例：
```json
{
  "type": "write_file_transaction_missing",
  "path": "<runtime: str(target)>",
  "transaction_state": "none",
  "next_chunk_index": 0,
  "restart_required": true,
  "action": "restart",
  "final": "<runtime: final>"
}
```

### `image_inspection` — box_agent/tools/image_inspection_tool.py:324

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/image_inspection_tool.py:324](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_inspection_tool.py#L324)。

参数：`type`, `schema_version`, `images`, `instruction`, `model_output`

结构示例：
```json
{
  "type": "image_inspection",
  "schema_version": 1,
  "images": "<runtime: self._image_metadata(candidate_images)>",
  "instruction": "<runtime: normalized_instruction>",
  "model_output": "<runtime: content>"
}
```

### `image_attachment_snapshot` — box_agent/tools/image_inspection_tool.py:694

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/image_inspection_tool.py:694](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_inspection_tool.py#L694)。

参数：`type`, `schema_version`, `images`, `动态展开`

结构示例：
```json
{
  "type": "image_attachment_snapshot",
  "schema_version": 1,
  "images": "<runtime: self._image_metadata(images, detailed=True)>",
  "<dynamic-key>": "<runtime: {} if eligible else {'not_delivered_reason': 'image_model_unavailable'}>"
}
```

### `image_inspection_native` — box_agent/tools/image_inspection_tool.py:238

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/image_inspection_tool.py:238](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_inspection_tool.py#L238)。

参数：`type`, `schema_version`, `images`, `instruction`

结构示例：
```json
{
  "type": "image_inspection_native",
  "schema_version": 1,
  "images": "<runtime: image_metadata>",
  "instruction": "<runtime: normalized_instruction>"
}
```

### `memory_search` — box_agent/tools/memory_tool.py:141

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/memory_tool.py:141](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L141)。

参数：`type`, `query`, `matched_memories`

结构示例：
```json
{
  "type": "memory_search",
  "query": "<runtime: query>",
  "matched_memories": "<runtime: [{'id': str(index), 'source': self._backend.backend_type, 'category': 'history', 'text': line} for index, line in enumerate(lines)]>"
}
```

### `memory_search` — box_agent/tools/memory_tool.py:408

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/memory_tool.py:408](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L408)。

参数：`type`, `query`, `动态展开`, `matched_memories`

结构示例：
```json
{
  "type": "memory_search",
  "query": "<runtime: query>",
  "<dynamic-key>": "<runtime: {'topic': topic} if topic else {}>",
  "matched_memories": "<runtime: [{'id': f'context:{index}', 'source': 'context', 'category': 'context', 'text': line} for index, line in enumerate(results, start=1)]>"
}
```

### `memory_list_corrections` — box_agent/tools/memory_tool.py:501

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/memory_tool.py:501](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L501)。

参数：`type`, `corrections`

结构示例：
```json
{
  "type": "memory_list_corrections",
  "corrections": "<runtime: [{'id': e.id, 'status': e.status, 'subject_kind': e.subject_kind, 'subject_name': e.subject_name, 'subject_version': e.subject_version, 'error_fingerprint': e.error_fingerprint, 'content': e.content} for e in entries]>"
}
```

### `memory_write_correction` — box_agent/tools/memory_tool.py:637

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/memory_tool.py:637](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L637)。

参数：`type`, `id`, `status`

结构示例：
```json
{
  "type": "memory_write_correction",
  "id": "<runtime: entry.id>",
  "status": "<runtime: entry.status>"
}
```

### `memory_supersede_correction` — box_agent/tools/memory_tool.py:690

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/memory_tool.py:690](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L690)。

参数：`type`, `id`, `status`

结构示例：
```json
{
  "type": "memory_supersede_correction",
  "id": "<runtime: entry.id>",
  "status": "<runtime: entry.status>"
}
```

### `memory_delete_correction` — box_agent/tools/memory_tool.py:737

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/memory_tool.py:737](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L737)。

参数：`type`, `id`, `status`

结构示例：
```json
{
  "type": "memory_delete_correction",
  "id": "<runtime: entry.id>",
  "status": "<runtime: entry.status>"
}
```

### `memory_search` — box_agent/tools/memory_tool.py:397

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/memory_tool.py:397](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L397)。

参数：`type`, `query`, `动态展开`, `matched_memories`

结构示例：
```json
{
  "type": "memory_search",
  "query": "<runtime: query>",
  "<dynamic-key>": "<runtime: {'topic': topic} if topic else {}>",
  "matched_memories": []
}
```

### `permission_request` — box_agent/tools/obsidian_tool.py:33

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/obsidian_tool.py:33](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/obsidian_tool.py#L33)。

参数：`type`, `scope`, `requested_scope`, `path`, `reason`, `temporary_supported`, `persistent_supported`, `persistent_label`

结构示例：
```json
{
  "type": "permission_request",
  "scope": "<runtime: OBSIDIAN_PERMISSION_SCOPE>",
  "requested_scope": "<runtime: OBSIDIAN_LAUNCH_SCOPE>",
  "path": "",
  "reason": "<runtime: reason>",
  "temporary_supported": true,
  "persistent_supported": false,
  "persistent_label": ""
}
```

### `permission_request` — box_agent/tools/permissions.py:299

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/permissions.py:299](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/permissions.py#L299)。

参数：`type`, `scope`, `requested_scope`, `path`, `reason`, `temporary_supported`, `persistent_supported`, `persistent_label`

结构示例：
```json
{
  "type": "permission_request",
  "scope": "filesystem",
  "requested_scope": "<runtime: escalation>",
  "path": "<runtime: str(path)>",
  "reason": "<runtime: f'Path is outside {scope}'>",
  "temporary_supported": true,
  "persistent_supported": true,
  "persistent_label": "始终允许此目录"
}
```

### `permission_request` — box_agent/tools/permissions.py:442

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/permissions.py:442](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/permissions.py#L442)。

参数：`type`, `scope`, `requested_scope`, `reason`, `temporary_supported`, `persistent_supported`

结构示例：
```json
{
  "type": "permission_request",
  "scope": "memory",
  "requested_scope": "openclaw_import",
  "reason": "OpenClaw memory import is disabled",
  "temporary_supported": true,
  "persistent_supported": true
}
```

### `plan_snapshot` — box_agent/tools/plan_tool.py:87

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/plan_tool.py:87](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/plan_tool.py#L87)。

参数：`type`, `version`, `plan`, `summary`

结构示例：
```json
{
  "type": "plan_snapshot",
  "version": 1,
  "plan": "<runtime: dict(plan) if plan else None>",
  "summary": "<runtime: summary>"
}
```

### `artifact` — box_agent/tools/publish_artifact_tool.py:196

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/publish_artifact_tool.py:196](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/publish_artifact_tool.py#L196)。

参数：`动态展开`, `type`

结构示例：
```json
{
  "<dynamic-key>": "<runtime: read_metadata(file, self.workspace_dir)>",
  "type": "artifact"
}
```

### `artifact` — box_agent/tools/publish_artifact_tool.py:198

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/publish_artifact_tool.py:198](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/publish_artifact_tool.py#L198)。

参数：`type`, `abs_path`, `placement`

结构示例：
```json
{
  "type": "artifact",
  "abs_path": "<runtime: str(file)>",
  "placement": "primary"
}
```

### `artifact` — box_agent/tools/publish_artifact_tool.py:179

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/publish_artifact_tool.py:179](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/publish_artifact_tool.py#L179)。

参数：`type`, `sha256`, `size_bytes`

结构示例：
```json
{
  "type": "artifact",
  "sha256": "<runtime: digest.hexdigest()>",
  "size_bytes": "<runtime: sealed.stat().st_size>"
}
```

### `artifact` — box_agent/tools/publish_artifact_tool.py:186

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/publish_artifact_tool.py:186](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/publish_artifact_tool.py#L186)。

参数：`type`, `abs_path`, `placement`, `sha256`

结构示例：
```json
{
  "type": "artifact",
  "abs_path": "<runtime: str(target)>",
  "placement": "primary",
  "sha256": "<runtime: digest.hexdigest()>"
}
```

### `user_decision_request` — box_agent/tools/request_user_decision_tool.py:271

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/request_user_decision_tool.py:271](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/request_user_decision_tool.py#L271)。

参数：`type`, `schemaVersion`, `requestId`, `status`, `question`, `decisionKind`, `options`, `allowFreeform`, `reason`, `autoSubmit`, `resumeBehavior`

结构示例：
```json
{
  "type": "user_decision_request",
  "schemaVersion": 1,
  "requestId": "<runtime: request_id>",
  "status": "waiting",
  "question": "<runtime: normalized_question>",
  "decisionKind": "<runtime: normalized_kind>",
  "options": "<runtime: normalized_options>",
  "allowFreeform": "<runtime: allow_freeform is True>",
  "reason": "<runtime: normalized_reason>",
  "autoSubmit": "<runtime: auto_submit>",
  "resumeBehavior": "continue_existing_task"
}
```

### `user_input_request` — box_agent/tools/request_user_input_tool.py:114

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/request_user_input_tool.py:114](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/request_user_input_tool.py#L114)。

参数：`type`, `version`, `status`, `question`, `missingFields`, `reason`, `resumeBehavior`

结构示例：
```json
{
  "type": "user_input_request",
  "version": 1,
  "status": "waiting",
  "question": "<runtime: normalized_question>",
  "missingFields": "<runtime: normalized_fields>",
  "reason": "<runtime: normalized_reason>",
  "resumeBehavior": "continue_existing_task"
}
```

### `runtime_component_required` — box_agent/tools/runtime_components.py:18

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/runtime_components.py:18](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/runtime_components.py#L18)。

参数：`type`, `schema_version`, `component_id`, `code`

结构示例：
```json
{
  "type": "runtime_component_required",
  "schema_version": 1,
  "component_id": "<runtime: component_id>",
  "code": "<runtime: code>"
}
```

### `permission_request` — box_agent/tools/skillhub_install_tool.py:155

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/skillhub_install_tool.py:155](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_install_tool.py#L155)。

参数：`type`, `scope`, `requested_scope`, `reason`, `temporary_supported`, `persistent_supported`, `skill_id`, `slug`, `name`, `version`

结构示例：
```json
{
  "type": "permission_request",
  "scope": "skillhub",
  "requested_scope": "<runtime: f'install:{normalized_id}'>",
  "reason": "<runtime: f\"Install '{normalized_candidate['name']}' from the Skill marketplace, published by {publisher}\">",
  "temporary_supported": true,
  "persistent_supported": false,
  "skill_id": "<runtime: normalized_id>",
  "slug": "<runtime: normalized_candidate['slug']>",
  "name": "<runtime: normalized_candidate['name']>",
  "version": "<runtime: normalized_candidate['currentVersion']>"
}
```

### `staged_file_write` — box_agent/tools/staged_file_write_tool.py:227

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/staged_file_write_tool.py:227](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/staged_file_write_tool.py#L227)。

参数：`type`, `action`, `write_id`, `next_index`, `chunk_limit`, `expected_chunks`

结构示例：
```json
{
  "type": "staged_file_write",
  "action": "begin",
  "write_id": "<runtime: write_id>",
  "next_index": 0,
  "chunk_limit": "<runtime: MAX_GENERATED_BODY_CHARS>",
  "expected_chunks": "<runtime: expected_chunks>"
}
```

### `staged_file_write` — box_agent/tools/staged_file_write_tool.py:343

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/staged_file_write_tool.py:343](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/staged_file_write_tool.py#L343)。

参数：`type`, `action`, `write_id`, `next_index`, `size_bytes`

结构示例：
```json
{
  "type": "staged_file_write",
  "action": "append",
  "write_id": "<runtime: write_id>",
  "next_index": "<runtime: state.next_index>",
  "size_bytes": "<runtime: state.size_bytes>"
}
```

### `artifact` — box_agent/tools/staged_file_write_tool.py:398

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/staged_file_write_tool.py:398](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/staged_file_write_tool.py#L398)。

参数：`type`, `path`, `size_bytes`, `sha256`

结构示例：
```json
{
  "type": "artifact",
  "path": "<runtime: str(state.target)>",
  "size_bytes": "<runtime: state.size_bytes>",
  "sha256": "<runtime: digest>"
}
```

### `staged_file_write` — box_agent/tools/staged_file_write_tool.py:203

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/staged_file_write_tool.py:203](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/staged_file_write_tool.py#L203)。

参数：`type`, `action`, `write_id`, `next_index`, `size_bytes`, `expected_chunks`

结构示例：
```json
{
  "type": "staged_file_write",
  "action": "begin_rejected",
  "write_id": "<runtime: active_write_id>",
  "next_index": "<runtime: active.next_index>",
  "size_bytes": "<runtime: active.size_bytes>",
  "expected_chunks": "<runtime: active.expected_chunks>"
}
```

### `sub_agent_delegation_error` — box_agent/tools/sub_agent_capabilities.py:206

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/sub_agent_capabilities.py:206](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_capabilities.py#L206)。

参数：`type`, `code`, `message`, `retryable`, `invalid_fields`, `defaults_applied`

结构示例：
```json
{
  "type": "sub_agent_delegation_error",
  "code": "<runtime: self.code>",
  "message": "<runtime: self.message>",
  "retryable": "<runtime: self.retryable>",
  "invalid_fields": "<runtime: list(self.invalid_fields)>",
  "defaults_applied": "<runtime: list(self.defaults_applied)>"
}
```

### `sub_agent_delegation` — box_agent/tools/sub_agent_tool.py:1507

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/sub_agent_tool.py:1507](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1507)。

参数：`type`, `动态展开`

结构示例：
```json
{
  "type": "sub_agent_delegation",
  "<dynamic-key>": "<runtime: resolved.diagnostic_payload()>"
}
```

### `sub_agent_delegation_error` — box_agent/tools/sub_agent_tool.py:1193

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/sub_agent_tool.py:1193](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1193)。

参数：`动态展开`, `type`, `code`, `message`, `retryable`, `failures`, `model_calls`, `tool_calls`, `usage`

结构示例：
```json
{
  "<dynamic-key>": "<runtime: diagnostic>",
  "type": "sub_agent_delegation_error",
  "code": "BATCH_FILES_PREFETCH_FAILED",
  "message": "One or more required files could not be proven complete; no synthesis model call was made.",
  "retryable": true,
  "failures": "<runtime: failures>",
  "model_calls": 0,
  "tool_calls": "<runtime: len(files)>",
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0
  }
}
```

### `sub_agent_delegation_error` — box_agent/tools/sub_agent_tool.py:1295

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/sub_agent_tool.py:1295](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1295)。

参数：`动态展开`, `type`, `code`, `message`, `retryable`, `timeout_seconds`, `model_calls`, `tool_calls`, `usage`

结构示例：
```json
{
  "<dynamic-key>": "<runtime: diagnostic>",
  "type": "sub_agent_delegation_error",
  "code": "BATCH_SYNTHESIS_TIMEOUT",
  "message": "<runtime: f'The batch synthesis model call exceeded the configured {self._batch_synthesis_timeout_seconds:g} second runtime limit.'>",
  "retryable": true,
  "timeout_seconds": "<runtime: self._batch_synthesis_timeout_seconds>",
  "model_calls": 1,
  "tool_calls": "<runtime: len(files)>",
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0
  }
}
```

### `todo_snapshot` — box_agent/tools/todo_tool.py:38

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/todo_tool.py:38](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/todo_tool.py#L38)。

参数：`type`, `items`, `summary`

结构示例：
```json
{
  "type": "todo_snapshot",
  "items": "<runtime: normalized>",
  "summary": {
    "total": "<runtime: total>",
    "completed": "<runtime: completed>",
    "in_progress": "<runtime: in_progress>",
    "pending": "<runtime: pending>"
  }
}
```

### `todo_snapshot` — box_agent/tools/todo_tool.py:160

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/todo_tool.py:160](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/todo_tool.py#L160)。

参数：`type`, `summary`, `items`, `preview`

结构示例：
```json
{
  "type": "todo_snapshot",
  "summary": "<runtime: snapshot['summary']>",
  "items": [],
  "preview": true
}
```

### `video_read` — box_agent/tools/video_tool.py:251

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/video_tool.py:251](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/video_tool.py#L251)。

参数：`type`, `schema_version`, `path`, `动态展开`, `start_seconds`, `end_seconds`, `has_more`, `next_start_seconds`, `sampling`, `media_cache_hit`, `frames`, `visual`, `audio`, `limitations`

结构示例：
```json
{
  "type": "video_read",
  "schema_version": 1,
  "path": "<runtime: str(source)>",
  "<dynamic-key>": "<runtime: metadata>",
  "start_seconds": "<runtime: start_seconds>",
  "end_seconds": "<runtime: end>",
  "has_more": "<runtime: end < requested_end>",
  "next_start_seconds": "<runtime: end if end < requested_end else None>",
  "sampling": "uniform_sparse",
  "media_cache_hit": "<runtime: cached>",
  "frames": "<runtime: [{k: v for k, v in item.items() if k != 'file'} for item in prepared.frames]>",
  "visual": {
    "status": "<runtime: 'native_frames' if native else 'analyzed' if vision.success else 'unavailable'>",
    "text": "<runtime: vision.content if vision.success else vision.error>"
  },
  "audio": "<runtime: audio>",
  "limitations": [
    "Only sampled frames were read; unsampled events may be missed.",
    "Speech transcription does not analyze non-speech sounds."
  ]
}
```

### `web_search` — box_agent/tools/web_search_runtime.py:245

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/web_search_runtime.py:245](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/web_search_runtime.py#L245)。

参数：`type`, `refs`

结构示例：
```json
{
  "type": "web_search",
  "refs": "<runtime: refs>"
}
```

### `web_search` — box_agent/tools/web_search_runtime.py:290

方向：下行（结果或嵌套载荷）；功能/生成条件见 [box_agent/tools/web_search_runtime.py:290](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/web_search_runtime.py#L290)。

参数：`type`, `citation_instructions`

结构示例：
```json
{
  "type": "web_search",
  "citation_instructions": "<runtime: citation_data['citation_instructions']>"
}
```

## 4. 开放结果和附加字段的写入点

这些位置包括无 type 的工具结果、动态 payload、后续增加的字段。不能仅按上节的 type 常量做封闭解析。

| 文件/位置 | 当前表达式 |
|---|---|
| [box_agent/acp/__init__.py:292](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L292) | `payload['description'] = art.description` |
| [box_agent/acp/__init__.py:294](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L294) | `payload['layout_id'] = art.layout_id` |
| [box_agent/acp/__init__.py:296](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L296) | `payload['edit_mode'] = art.edit_mode` |
| [box_agent/acp/__init__.py:298](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L298) | `payload['session_id'] = session_id` |
| [box_agent/acp/__init__.py:299](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L299) | `payload['sessionId'] = session_id` |
| [box_agent/acp/__init__.py:301](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L301) | `payload['task_id'] = task_id` |
| [box_agent/acp/__init__.py:302](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L302) | `payload['taskId'] = task_id` |
| [box_agent/acp/__init__.py:304](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L304) | `payload['turn_id'] = turn_id` |
| [box_agent/acp/__init__.py:305](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L305) | `payload['turnId'] = turn_id` |
| [box_agent/acp/__init__.py:307](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L307) | `payload['artifact_id'] = lineage.artifact_id` |
| [box_agent/acp/__init__.py:308](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L308) | `payload['artifactId'] = lineage.artifact_id` |
| [box_agent/acp/__init__.py:309](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L309) | `payload['artifact_revision_id'] = lineage.artifact_revision_id` |
| [box_agent/acp/__init__.py:310](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L310) | `payload['artifactRevisionId'] = lineage.artifact_revision_id` |
| [box_agent/acp/__init__.py:311](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L311) | `payload['sha256'] = lineage.sha256` |
| [box_agent/acp/__init__.py:312](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L312) | `payload['manifest_path'] = lineage.manifest_path` |
| [box_agent/acp/__init__.py:1784](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1784) | `self._session_bindings_in_progress[upstream_session_id] = existing` |
| [box_agent/acp/__init__.py:2384](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2384) | `response_meta['capabilities'] = {'session_continuation_versions': [1], 'history_revision_versions': [1], 'managed_mcp_config_versions': [1]}` |
| [box_agent/acp/__init__.py:2389](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2389) | `response_meta['history_revision_id'] = session_log.current_revision() if session_log else 'initial'` |
| [box_agent/acp/__init__.py:3618](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L3618) | `response_meta['history_revision_id'] = state.agent.session_log.current_revision() if state.agent.session_log else 'initial'` |
| [box_agent/acp/__init__.py:3619](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L3619) | `response_meta['messageDisplay'] = message_display.receipt(completed=not failed and (not waiting_for_user) and (state.pending_plan_approval is None) and (stop_reason == StopReason.END_TURN.value) and (not autopilot.budget_exhausted) and (not autopilot.no_progress_exhausted) and (not wrap_up_incomplete), duration_ms=duration_ms)` |
| [box_agent/acp/__init__.py:577](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L577) | `payload['request_id'] = request_id.strip()` |
| [box_agent/acp/__init__.py:686](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L686) | `payload[key] = value.strip()` |
| [box_agent/acp/__init__.py:961](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L961) | `payload['policy_decision'] = policy_decision` |
| [box_agent/acp/__init__.py:2391](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2391) | `response_meta['capabilities']['skillhub_search_versions'] = [SKILLHUB_SEARCH_CAPABILITY_VERSION]` |
| [box_agent/acp/__init__.py:2395](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2395) | `response_meta['capabilities']['skillhub_install_versions'] = [SKILLHUB_INSTALL_CAPABILITY_VERSION]` |
| [box_agent/acp/__init__.py:2399](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2399) | `response_meta['capabilities']['runtime_environment_versions'] = [1]` |
| [box_agent/acp/__init__.py:2406](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2406) | `response_meta['skills'] = skills` |
| [box_agent/acp/__init__.py:2408](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2408) | `response_meta['expert_context'] = expert_context.to_metadata()` |
| [box_agent/acp/__init__.py:2410](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2410) | `response_meta['goal'] = _goal_payload(agent)` |
| [box_agent/acp/__init__.py:3632](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L3632) | `response_meta['wrapUp'] = {'requested': True, 'goalIncomplete': wrap_up_incomplete}` |
| [box_agent/acp/__init__.py:3634](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L3634) | `response_meta['goalAutopilot'] = {'enabled': auto_enabled, 'continuations': autopilot.continuations, 'budgetExhausted': autopilot.budget_exhausted, 'noProgressExhausted': autopilot.no_progress_exhausted, 'noProgressTurns': autopilot.no_progress_turns, 'lastStopReason': stop_reason}` |
| [box_agent/acp/__init__.py:3643](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L3643) | `response_meta['taskRegistryError'] = state.task_registry_error` |
| [box_agent/acp/__init__.py:3645](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L3645) | `response_meta['errorCode'] = state.last_error_code` |
| [box_agent/acp/__init__.py:3647](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L3647) | `response_meta['errorCategory'] = state.last_error_category` |
| [box_agent/acp/__init__.py:3649](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L3649) | `response_meta['errorDetails'] = state.last_error_details` |
| [box_agent/acp/__init__.py:5964](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5964) | `payload['plan'] = {'currentCore': plan.current_core, 'newCore': plan.new_core, 'consumedEntryIds': list(plan.consumed_entry_ids), 'rationale': plan.rationale}` |
| [box_agent/acp/__init__.py:4860](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4860) | `payload['current'] = current` |
| [box_agent/acp/__init__.py:2567](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2567) | `raw_output=raw_output or {'type': 'structured_image_attachment_result', 'success': ok, 'imageCount': len(image_paths), 'content': text}` |
| [box_agent/acp/__init__.py:944](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L944) | `payload['type'] = 'intermediate_asset'` |
| [box_agent/acp/__init__.py:4873](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4873) | `raw_output=payload` |
| [box_agent/acp/__init__.py:4888](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4888) | `raw_output=payload` |
| [box_agent/acp/__init__.py:4986](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4986) | `raw_output={'type': 'follow_up_suggestions', 'turn_id': expected_turn_id, 'suggestions': suggestions}` |
| [box_agent/acp/__init__.py:4614](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4614) | `raw_output=team_progress` |
| [box_agent/acp/__init__.py:5522](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5522) | `progress['event'] = 'step_start'` |
| [box_agent/acp/__init__.py:5523](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5523) | `progress['step'] = s` |
| [box_agent/acp/__init__.py:5524](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5524) | `progress['max_steps'] = mx` |
| [box_agent/acp/__init__.py:5526](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5526) | `progress['event'] = 'tool_start'` |
| [box_agent/acp/__init__.py:5527](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5527) | `progress['tool_name'] = name` |
| [box_agent/acp/__init__.py:5529](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5529) | `progress['event'] = 'tool_result'` |
| [box_agent/acp/__init__.py:5530](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5530) | `progress['tool_name'] = name` |
| [box_agent/acp/__init__.py:5531](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5531) | `progress['success'] = ok` |
| [box_agent/acp/__init__.py:5540](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5540) | `progress['event'] = 'artifact'` |
| [box_agent/acp/__init__.py:5553](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5553) | `progress['artifact'] = _artifact_envelope(art, session_id=state.upstream_session_id, task_id=task_context.task_id, turn_id=task_context.turn_id, lineage=lineage)` |
| [box_agent/acp/__init__.py:5561](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5561) | `progress['event'] = 'error'` |
| [box_agent/acp/__init__.py:5562](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5562) | `progress['message'] = msg` |
| [box_agent/acp/__init__.py:5564](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5564) | `progress['event'] = 'agent_progress'` |
| [box_agent/acp/__init__.py:5565](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5565) | `progress['step'] = s` |
| [box_agent/acp/__init__.py:5566](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5566) | `progress['content'] = content` |
| [box_agent/acp/__init__.py:5576](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5576) | `progress['event'] = 'llm_output'` |
| [box_agent/acp/__init__.py:5577](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5577) | `progress['step'] = s` |
| [box_agent/acp/__init__.py:5578](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5578) | `progress['content'] = content` |
| [box_agent/acp/__init__.py:5579](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5579) | `progress['thinking'] = thinking` |
| [box_agent/acp/__init__.py:5580](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5580) | `progress['tool_calls'] = tool_calls` |
| [box_agent/acp/__init__.py:5581](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5581) | `progress['finish_reason'] = finish_reason` |
| [box_agent/acp/__init__.py:5582](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5582) | `progress['usage'] = usage` |
| [box_agent/acp/__init__.py:5583](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5583) | `progress['provider_request_id'] = provider_request_id` |
| [box_agent/acp/__init__.py:5585](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5585) | `progress['event'] = type(inner).__name__` |
| [box_agent/acp/__init__.py:5144](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5144) | `raw_output=payload` |
| [box_agent/acp/__init__.py:5168](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5168) | `raw_output=payload` |
| [box_agent/acp/__init__.py:5206](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5206) | `raw_output=payload` |
| [box_agent/acp/__init__.py:5219](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5219) | `raw_output=payload` |
| [box_agent/acp/__init__.py:5334](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5334) | `raw_output=output` |
| [box_agent/acp/__init__.py:5386](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5386) | `raw_output=web_search_payload` |
| [box_agent/acp/__init__.py:5378](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5378) | `raw_output=artifact_meta` |
| [box_agent/acp/__init__.py:5509](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5509) | `raw_output=web_search_payload` |
| [box_agent/acp/__init__.py:5590](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5590) | `raw_output=progress` |
| [box_agent/acp/__init__.py:5457](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5457) | `raw_output={'type': 'follow_up_suggestions', 'suggestions': suggestions}` |
| [box_agent/tools/base.py:72](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/base.py#L72) | `raw_output={'code': self.code}` |
| [box_agent/tools/base.py:183](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/base.py#L183) | `raw_output={'code': 'INVALID_TOOL_SCHEMA', 'tool': self.name, 'issues': [{'path': '/', 'keyword': 'schema', 'message': message}]}` |
| [box_agent/tools/base.py:206](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/base.py#L206) | `raw_output={'code': 'INVALID_TOOL_ARGUMENTS', 'tool': self.name, 'issues': [issue.to_dict() for issue in issues]}` |
| [box_agent/tools/bash_tool.py:716](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/bash_tool.py#L716) | `raw_output=raw_output` |
| [box_agent/tools/bash_tool.py:2049](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/bash_tool.py#L2049) | `raw_output=raw_output` |
| [box_agent/tools/engine/engine.py:315](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/engine/engine.py#L315) | `raw_output=call.hook_rejection` |
| [box_agent/tools/engine/engine.py:334](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/engine/engine.py#L334) | `raw_output=_trace_safe_tool_raw_output(result.raw_output)` |
| [box_agent/tools/engine/results.py:256](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/engine/results.py#L256) | `raw_output=delivery.raw_output` |
| [box_agent/tools/engine/results.py:209](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/engine/results.py#L209) | `raw_output=_trace_safe_tool_raw_output(result.raw_output)` |
| [box_agent/tools/engine/results.py:236](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/engine/results.py#L236) | `raw_output=None` |
| [box_agent/tools/execution_result_tool.py:334](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/execution_result_tool.py#L334) | `raw_output=raw_output` |
| [box_agent/tools/file/email_tool.py:104](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file/email_tool.py#L104) | `raw_output=result` |
| [box_agent/tools/file/email_tool.py:193](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file/email_tool.py#L193) | `raw_output=result` |
| [box_agent/tools/file/jsonl_tool.py:498](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file/jsonl_tool.py#L498) | `raw_output={'source_size_bytes': file_size, 'returned_records': len(records), 'scanned_records': scanned_records, 'parse_error_count': parse_error_count, 'oversized_record_count': oversized_record_count, 'projection_truncated': bool(truncated_fields), 'truncated_fields': sorted(truncated_fields), 'has_more': has_more, 'next_cursor': next_cursor}` |
| [box_agent/tools/file/read_tool.py:345](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file/read_tool.py#L345) | `raw_output={'source_size_bytes': size, 'selected_char_count': len(text), 'start_byte': byte_offset, 'end_byte': end_byte, 'partial': byte_offset > 0 or next_byte_offset is not None, 'truncated': False, 'has_more': next_byte_offset is not None, 'next_byte_offset': next_byte_offset}` |
| [box_agent/tools/file/read_tool.py:430](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file/read_tool.py#L430) | `raw_output={'source_char_count': page.source_chars, 'selected_char_count': selected_char_count, 'selected_line_count': selected_line_count, 'total_lines': total_lines, 'truncated': False, 'has_more': has_more, 'next_offset': offset + selected_line_count if has_more else None, CONTEXT_RESOURCE_RAW_KEY: descriptor.as_raw_output()}` |
| [box_agent/tools/file/validate_table_tool.py:141](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file/validate_table_tool.py#L141) | `raw_output=payload` |
| [box_agent/tools/file/validate_table_tool.py:475](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file/validate_table_tool.py#L475) | `raw_output=payload` |
| [box_agent/tools/file_tools.py:992](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L992) | `raw_output=raw_output` |
| [box_agent/tools/file_tools.py:1169](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1169) | `raw_output={'type': 'intermediate_asset' if temporary else 'artifact', 'path': str(state.target), 'transaction_state': 'committed', 'created': created, 'temporary': temporary, 'size_bytes': state.size_bytes, 'sha256': digest, 'chunks': state.next_index, 'final': True, 'backup_path': str(backup_path) if backup_path is not None else None}` |
| [box_agent/tools/file_tools.py:656](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L656) | `raw_output={'target': target, 'path': str(search_path), 'total_matches': scan['matched_results'] if scan['exact_total'] else None, 'matched_through': scan['matched_results'], 'total_is_exact': scan['exact_total'], 'returned_matches': len(selected), 'truncated': truncated, 'next_offset': offset + len(selected) if truncated else None, 'scanned_files': scan['scanned_files'], 'timed_out': scan['timed_out'], 'output_limited': output_limited, 'output_chars': len(content), 'max_output_chars': MAX_SEARCH_OUTPUT_CHARS, 'limit_reason': limit_reason}` |
| [box_agent/tools/file_tools.py:1240](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1240) | `raw_output={'type': 'write_file_final_chunk_conflict', 'path': str(target), 'transaction_state': 'none', 'next_chunk_index': 0, 'restart_required': True, 'action': 'restart', 'final': True}` |
| [box_agent/tools/file_tools.py:1348](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1348) | `raw_output={'type': 'write_file_chunk', 'path': str(target), 'transaction_state': 'active', 'chunk_index': chunk_index, 'next_chunk_index': state.next_index, 'size_bytes': state.size_bytes, 'duplicate': False, 'final': False}` |
| [box_agent/tools/file_tools.py:1510](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1510) | `raw_output={'type': 'artifact', 'path': str(file_path)}` |
| [box_agent/tools/file_tools.py:542](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L542) | `raw_output={'code': 'PATH_NOT_FOUND', 'path': path, 'path_candidates': candidates}` |
| [box_agent/tools/file_tools.py:1033](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1033) | `raw_output={'type': 'write_file_chunk', 'path': str(state.target), 'transaction_state': 'active', 'chunk_index': chunk_index, 'next_chunk_index': state.next_index, 'size_bytes': state.size_bytes, **receipt, 'final': False}` |
| [box_agent/tools/file_tools.py:1073](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1073) | `raw_output={'action': 'continue', 'restart_required': False}` |
| [box_agent/tools/file_tools.py:1662](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1662) | `raw_output={'type': 'artifact', 'path': str(file_path)}` |
| [box_agent/tools/file_tools.py:1219](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1219) | `raw_output={'type': 'write_file_committed_state_changed', 'path': str(target), 'transaction_state': 'none', 'final': True}` |
| [box_agent/tools/file_tools.py:1305](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1305) | `raw_output={'type': 'write_file_transaction_missing', 'path': str(target), 'transaction_state': 'none', 'next_chunk_index': 0, 'restart_required': True, 'action': 'restart', 'final': final}` |
| [box_agent/tools/file_tools.py:1278](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1278) | `raw_output={'action': 'continue', 'restart_required': False}` |
| [box_agent/tools/image_generation_tool.py:708](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_generation_tool.py#L708) | `raw_output=info` |
| [box_agent/tools/image_inspection_tool.py:331](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_inspection_tool.py#L331) | `raw_output=raw_output` |
| [box_agent/tools/image_inspection_tool.py:627](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_inspection_tool.py#L627) | `raw_output={'code': code, 'tool': self.name}` |
| [box_agent/tools/image_inspection_tool.py:651](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_inspection_tool.py#L651) | `raw_output={'code': 'IMAGE_REQUEST_FAILED', 'tool': self.name, 'provider_error': safe}` |
| [box_agent/tools/image_inspection_tool.py:692](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_inspection_tool.py#L692) | `raw_output={'type': 'image_attachment_snapshot', 'schema_version': 1, 'images': self._image_metadata(images, detailed=True), **({} if eligible else {'not_delivered_reason': 'image_model_unavailable'})}` |
| [box_agent/tools/image_inspection_tool.py:234](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_inspection_tool.py#L234) | `raw_output={'type': 'image_inspection_native', 'schema_version': 1, 'images': image_metadata, 'instruction': normalized_instruction}` |
| [box_agent/tools/image_inspection_tool.py:201](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_inspection_tool.py#L201) | `raw_output={'code': 'IMAGE_PERMISSION_REQUIRED', 'tool': self.name}` |
| [box_agent/tools/image_inspection_tool.py:678](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_inspection_tool.py#L678) | `raw_output={'code': 'IMAGE_PERMISSION_REQUIRED', 'tool': self.name}` |
| [box_agent/tools/image_inspection_tool.py:293](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_inspection_tool.py#L293) | `raw_output={'tool': self.name, **exc.details()}` |
| [box_agent/tools/jupyter_tool.py:1785](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/jupyter_tool.py#L1785) | `raw_output={'phase': 'environment_prepare', 'timeout_seconds': _SANDBOX_PREPARE_TIMEOUT_SECONDS}` |
| [box_agent/tools/jupyter_tool.py:2049](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/jupyter_tool.py#L2049) | `raw_output={'phase': 'kernel_start'}` |
| [box_agent/tools/jupyter_tool.py:2063](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/jupyter_tool.py#L2063) | `raw_output={'phase': 'kernel_start'}` |
| [box_agent/tools/mcp_loader.py:956](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/mcp_loader.py#L956) | `raw_output={'code': 'INVALID_TOOL_ARGUMENTS', 'tool': self.name, 'issues': [issue], 'suggested_call': {'query': 'your search terms'}}` |
| [box_agent/tools/mcp_loader.py:1272](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/mcp_loader.py#L1272) | `raw_output=raw_output or None` |
| [box_agent/tools/mcp_loader.py:1254](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/mcp_loader.py#L1254) | `raw_output=raw_output or None` |
| [box_agent/tools/memory_tool.py:104](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L104) | `raw_output=data` |
| [box_agent/tools/memory_tool.py:141](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L141) | `raw_output={'type': 'memory_search', 'query': query, 'matched_memories': [{'id': str(index), 'source': self._backend.backend_type, 'category': 'history', 'text': line} for index, line in enumerate(lines)]}` |
| [box_agent/tools/memory_tool.py:405](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L405) | `raw_output={'type': 'memory_search', 'query': query, **({'topic': topic} if topic else {}), 'matched_memories': [{'id': f'context:{index}', 'source': 'context', 'category': 'context', 'text': line} for index, line in enumerate(results, start=1)]}` |
| [box_agent/tools/memory_tool.py:497](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L497) | `raw_output={'type': 'memory_list_corrections', 'corrections': [{'id': e.id, 'status': e.status, 'subject_kind': e.subject_kind, 'subject_name': e.subject_name, 'subject_version': e.subject_version, 'error_fingerprint': e.error_fingerprint, 'content': e.content} for e in entries]}` |
| [box_agent/tools/memory_tool.py:634](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L634) | `raw_output={'type': 'memory_write_correction', 'id': entry.id, 'status': entry.status}` |
| [box_agent/tools/memory_tool.py:686](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L686) | `raw_output={'type': 'memory_supersede_correction', 'id': entry.id, 'status': entry.status}` |
| [box_agent/tools/memory_tool.py:733](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L733) | `raw_output={'type': 'memory_delete_correction', 'id': entry.id, 'status': entry.status}` |
| [box_agent/tools/memory_tool.py:56](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L56) | `raw_output=data` |
| [box_agent/tools/memory_tool.py:394](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/memory_tool.py#L394) | `raw_output={'type': 'memory_search', 'query': query, **({'topic': topic} if topic else {}), 'matched_memories': []}` |
| [box_agent/tools/obsidian_tool.py:261](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/obsidian_tool.py#L261) | `raw_output={'argv': args}` |
| [box_agent/tools/plan_tool.py:335](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/plan_tool.py#L335) | `raw_output=_plan_snapshot(plan, action='set')` |
| [box_agent/tools/plan_tool.py:373](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/plan_tool.py#L373) | `raw_output=_plan_snapshot(plan)` |
| [box_agent/tools/plan_tool.py:306](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/plan_tool.py#L306) | `raw_output=_plan_snapshot(None, action='clear')` |
| [box_agent/tools/plan_tool.py:368](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/plan_tool.py#L368) | `raw_output=_plan_snapshot(None)` |
| [box_agent/tools/publish_artifact_tool.py:197](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/publish_artifact_tool.py#L197) | `raw_output={'type': 'artifact', 'abs_path': str(file), 'placement': 'primary'}` |
| [box_agent/tools/publish_artifact_tool.py:139](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/publish_artifact_tool.py#L139) | `raw_output={'write_id': transaction, 'staging_path': str(staging)}` |
| [box_agent/tools/publish_artifact_tool.py:185](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/publish_artifact_tool.py#L185) | `raw_output={'type': 'artifact', 'abs_path': str(target), 'placement': 'primary', 'sha256': digest.hexdigest()}` |
| [box_agent/tools/request_user_decision_tool.py:296](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/request_user_decision_tool.py#L296) | `raw_output=payload` |
| [box_agent/tools/request_user_input_tool.py:123](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/request_user_input_tool.py#L123) | `raw_output=payload` |
| [box_agent/tools/ripgrep_tool.py:730](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/ripgrep_tool.py#L730) | `raw_output={'path': str(search_path), 'returned_files': len(files), 'truncated': truncated or timed_out or partial or undecodable_paths, 'files': files}` |
| [box_agent/tools/ripgrep_tool.py:1159](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/ripgrep_tool.py#L1159) | `raw_output={'path': str(search_path), 'returned_matches': len(matches), 'truncated': truncated or timed_out or partial or undecodable_paths or oversized_record, 'matches': matches}` |
| [box_agent/tools/runtime_components.py:15](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/runtime_components.py#L15) | `raw_output={'type': 'runtime_component_required', 'schema_version': 1, 'component_id': component_id, 'code': code}` |
| [box_agent/tools/schedule_tool.py:251](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/schedule_tool.py#L251) | `raw_output={'kind': SCHEDULE_DRAFT_KIND, 'draft': draft}` |
| [box_agent/tools/skill_catalog_tool.py:152](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skill_catalog_tool.py#L152) | `raw_output=payload` |
| [box_agent/tools/skill_tool.py:131](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skill_tool.py#L131) | `raw_output={'code': 'SKILL_VALIDATION_TIMEOUT'}` |
| [box_agent/tools/skill_tool.py:140](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skill_tool.py#L140) | `raw_output={'code': exc.code}` |
| [box_agent/tools/skill_tool.py:91](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skill_tool.py#L91) | `raw_output={'code': exc.code}` |
| [box_agent/tools/skillhub_install_tool.py:277](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_install_tool.py#L277) | `raw_output={'type': SKILLHUB_INSTALLATION_TYPE, 'status': normalized_status, 'candidate': candidate, 'skillName': visible_name or installed_name, 'items': [candidate]}` |
| [box_agent/tools/skillhub_install_tool.py:305](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_install_tool.py#L305) | `raw_output={'type': SKILLHUB_INSTALLATION_TYPE, 'status': 'failed', 'code': code, 'skillId': skill_id}` |
| [box_agent/tools/skillhub_install_tool.py:152](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_install_tool.py#L152) | `raw_output={'type': SKILLHUB_INSTALLATION_TYPE, 'status': 'confirmation_required', 'candidate': normalized_candidate}` |
| [box_agent/tools/skillhub_search_tool.py:441](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_search_tool.py#L441) | `raw_output={'type': SKILLHUB_RECOMMENDATIONS_TYPE, 'status': normalized_status, 'requestKind': request_kind, 'query': searched_queries[0] if searched_queries else normalized_queries[0], 'queries': normalized_queries, 'searchedQueries': searched_queries, 'gapType': gap_type, 'missingCapability': missing_capability[:300], 'items': candidates}` |
| [box_agent/tools/skillhub_search_tool.py:460](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_search_tool.py#L460) | `raw_output={'type': SKILLHUB_RECOMMENDATIONS_TYPE, 'status': 'rejected', 'code': code}` |
| [box_agent/tools/staged_file_write_tool.py:221](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/staged_file_write_tool.py#L221) | `raw_output={'type': 'staged_file_write', 'action': 'begin', 'write_id': write_id, 'next_index': 0, 'chunk_limit': MAX_GENERATED_BODY_CHARS, 'expected_chunks': expected_chunks}` |
| [box_agent/tools/staged_file_write_tool.py:337](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/staged_file_write_tool.py#L337) | `raw_output={'type': 'staged_file_write', 'action': 'append', 'write_id': write_id, 'next_index': state.next_index, 'size_bytes': state.size_bytes}` |
| [box_agent/tools/staged_file_write_tool.py:393](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/staged_file_write_tool.py#L393) | `raw_output={'type': 'artifact', 'path': str(state.target), 'size_bytes': state.size_bytes, 'sha256': digest}` |
| [box_agent/tools/staged_file_write_tool.py:193](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/staged_file_write_tool.py#L193) | `raw_output={'type': 'staged_file_write', 'action': 'begin_rejected', 'write_id': active_write_id, 'next_index': active.next_index, 'size_bytes': active.size_bytes, 'expected_chunks': active.expected_chunks}` |
| [box_agent/tools/sub_agent_tool.py:665](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L665) | `raw_output=payload` |
| [box_agent/tools/sub_agent_tool.py:1042](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1042) | `raw_output=raw_output` |
| [box_agent/tools/sub_agent_tool.py:168](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L168) | `raw_output={'code': 'WRITE_SCOPE_VIOLATION', 'path': str(target), 'allowed_roots': [str(root) for root in self._roots]}` |
| [box_agent/tools/sub_agent_tool.py:1028](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1028) | `raw_output=raw_output` |
| [box_agent/tools/sub_agent_tool.py:1036](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1036) | `raw_output=raw_output` |
| [box_agent/tools/sub_agent_tool.py:1363](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1363) | `raw_output=raw_output` |
| [box_agent/tools/sub_agent_tool.py:994](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L994) | `raw_output={**diagnostic, 'model_calls': model_calls, 'tool_calls': tool_calls, 'usage': usage}` |
| [box_agent/tools/sub_agent_tool.py:1207](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1207) | `raw_output=payload` |
| [box_agent/tools/sub_agent_tool.py:1357](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1357) | `raw_output=raw_output` |
| [box_agent/tools/sub_agent_tool.py:1309](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1309) | `raw_output=payload` |
| [box_agent/tools/sub_agent_tool.py:1316](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/sub_agent_tool.py#L1316) | `raw_output={**diagnostic, 'model_calls': 1, 'tool_calls': len(files), 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}` |
| [box_agent/tools/todo_tool.py:559](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/todo_tool.py#L559) | `raw_output=raw_output` |
| [box_agent/tools/todo_tool.py:863](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/todo_tool.py#L863) | `raw_output=snapshot` |
| [box_agent/tools/todo_tool.py:846](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/todo_tool.py#L846) | `raw_output=snapshot` |
| [box_agent/tools/todo_tool.py:857](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/todo_tool.py#L857) | `raw_output=snapshot` |
| [box_agent/tools/video_tool.py:281](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/video_tool.py#L281) | `raw_output=result` |
| [box_agent/tools/video_tool.py:290](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/video_tool.py#L290) | `raw_output={'code': exc.code}` |
| [box_agent/tools/video_tool.py:292](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/video_tool.py#L292) | `raw_output={'code': 'VIDEO_READ_FAILED'}` |

## 5. 常量、函数默认值、条件表达式与活动载荷补充

这些构造不依赖 type 的字符串字面量搜索。结构示例仍使用运行时表达式占位；每项均为下行工具结果或嵌套业务数据。

### 'tool_result' — [box_agent/acp/__init__.py:965](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L965)

方向：下行。当前参数与结构示例：

```json
{
  "type": "tool_result",
  "text": "<runtime: result_text>",
  "policy_decision": "<runtime: policy_decision>"
}
```

### agent_activity_v1 — [box_agent/llm/anthropic_client.py:604](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/llm/anthropic_client.py#L604)

方向：下行。当前参数与结构示例：

```json
{
  "protocol": "agent_activity_v1",
  "phase": "provider_stream"
}
```

### agent_activity_v1 — [box_agent/llm/anthropic_client.py:649](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/llm/anthropic_client.py#L649)

方向：下行。当前参数与结构示例：

```json
{
  "protocol": "agent_activity_v1",
  "phase": "tool_arguments",
  "tool_name": "<runtime: current_tool_name or ''>",
  "argument_chars": "<runtime: arguments_len>"
}
```

### 'enabled' if thinking_enabled else 'disabled' — [box_agent/llm/openai_client.py:166](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/llm/openai_client.py#L166)

方向：下行。当前参数与结构示例：

```json
{
  "type": "<runtime: 'enabled' if thinking_enabled else 'disabled'>"
}
```

### agent_activity_v1 — [box_agent/llm/openai_client.py:1260](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/llm/openai_client.py#L1260)

方向：下行。当前参数与结构示例：

```json
{
  "protocol": "agent_activity_v1",
  "phase": "provider_stream"
}
```

### agent_activity_v1 — [box_agent/llm/openai_client.py:1341](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/llm/openai_client.py#L1341)

方向：下行。当前参数与结构示例：

```json
{
  "protocol": "agent_activity_v1",
  "phase": "tool_arguments",
  "tool_name": "<runtime: entry['name'] or ''>",
  "argument_chars": "<runtime: arguments_len>"
}
```

### agent_activity_v1 — [box_agent/tools/engine/engine.py:177](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/engine/engine.py#L177)

方向：下行。当前参数与结构示例：

```json
{
  "protocol": "agent_activity_v1",
  "phase": "tool_running",
  "tool_name": "<runtime: record.tool_name>"
}
```

### output_type — [box_agent/tools/file_tools.py:935](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L935)

方向：下行。当前参数与结构示例：

```json
{
  "type": "write_file_transaction_active",
  "path": "<runtime: str(state.target)>",
  "transaction_state": "active",
  "next_chunk_index": "<runtime: state.next_index>",
  "size_bytes": "<runtime: state.size_bytes>",
  "chunks": "<runtime: state.next_index>",
  "final": false
}
```

### 'intermediate_asset' if temporary else 'artifact' — [box_agent/tools/file_tools.py:1175](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_tools.py#L1175)

方向：下行。当前参数与结构示例：

```json
{
  "type": "<runtime: 'intermediate_asset' if temporary else 'artifact'>",
  "path": "<runtime: str(state.target)>",
  "transaction_state": "committed",
  "created": "<runtime: created>",
  "temporary": "<runtime: temporary>",
  "size_bytes": "<runtime: state.size_bytes>",
  "sha256": "<runtime: digest>",
  "chunks": "<runtime: state.next_index>",
  "final": true,
  "backup_path": "<runtime: str(backup_path) if backup_path is not None else None>"
}
```

### 'artifact' if publish_artifact else 'intermediate_asset' — [box_agent/tools/image_generation_tool.py:682](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_generation_tool.py#L682)

方向：下行。当前参数与结构示例：

```json
{
  "type": "<runtime: 'artifact' if publish_artifact else 'intermediate_asset'>",
  "kind": "image",
  "filename": "<runtime: target.name>",
  "rel_path": "<runtime: rel_path>",
  "artifact_rel_path": "<runtime: artifact_rel_path>",
  "abs_path": "<runtime: str(target)>",
  "uri": "<runtime: target.as_uri()>",
  "mime": "<runtime: mime_type>",
  "size_bytes": "<runtime: len(image_bytes)>",
  "path": "<runtime: artifact_rel_path>",
  "absolute_path": "<runtime: str(target)>",
  "mime_type": "<runtime: mime_type>",
  "bytes": "<runtime: len(image_bytes)>",
  "width": "<runtime: width>",
  "height": "<runtime: height>",
  "size": "<runtime: size>",
  "requested_width": "<runtime: requested_width>",
  "requested_height": "<runtime: requested_height>",
  "image_mode": "<runtime: 'image_to_image' if edit_requested else 'text_to_image'>",
  "reference_images": "<runtime: [self._artifact_display_path(path) for path in reference_paths]>",
  "alt_text": "<runtime: alt_text or ''>",
  "metadata": "<runtime: metadata or {}>",
  "watermark": "<runtime: watermark_status>",
  "aigc_metadata_applied": "<runtime: watermark_status.get('metadata_applied', False)>"
}
```

### 'artifact' if publish_artifact else 'intermediate_asset' — [box_agent/tools/image_generation_tool.py:672](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/image_generation_tool.py#L672)

方向：下行。当前参数与结构示例：

```json
{
  "type": "<runtime: 'artifact' if publish_artifact else 'intermediate_asset'>",
  "size_bytes": "<runtime: len(image_bytes)>",
  "sha256": "<runtime: hashlib.sha256(image_bytes).hexdigest()>"
}
```

### SKILLHUB_INSTALLATION_TYPE — [box_agent/tools/skillhub_install_tool.py:281](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_install_tool.py#L281)

方向：下行。当前参数与结构示例：

```json
{
  "type": "skillhub_installation",
  "status": "<runtime: normalized_status>",
  "candidate": "<runtime: candidate>",
  "skillName": "<runtime: visible_name or installed_name>",
  "items": [
    "<runtime: candidate>"
  ]
}
```

### SKILLHUB_INSTALLATION_TYPE — [box_agent/tools/skillhub_install_tool.py:308](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_install_tool.py#L308)

方向：下行。当前参数与结构示例：

```json
{
  "type": "skillhub_installation",
  "status": "failed",
  "code": "<runtime: code>",
  "skillId": "<runtime: skill_id>"
}
```

### SKILLHUB_INSTALLATION_TYPE — [box_agent/tools/skillhub_install_tool.py:170](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_install_tool.py#L170)

方向：下行。当前参数与结构示例：

```json
{
  "type": "skillhub_installation",
  "status": "confirmation_required",
  "candidate": "<runtime: normalized_candidate>"
}
```

### SKILLHUB_RECOMMENDATIONS_TYPE — [box_agent/tools/skillhub_search_tool.py:445](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_search_tool.py#L445)

方向：下行。当前参数与结构示例：

```json
{
  "type": "skillhub_recommendations",
  "status": "<runtime: normalized_status>",
  "requestKind": "<runtime: request_kind>",
  "query": "<runtime: searched_queries[0] if searched_queries else normalized_queries[0]>",
  "queries": "<runtime: normalized_queries>",
  "searchedQueries": "<runtime: searched_queries>",
  "gapType": "<runtime: gap_type>",
  "missingCapability": "<runtime: missing_capability[:300]>",
  "items": "<runtime: candidates>"
}
```

### SKILLHUB_RECOMMENDATIONS_TYPE — [box_agent/tools/skillhub_search_tool.py:463](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_search_tool.py#L463)

方向：下行。当前参数与结构示例：

```json
{
  "type": "skillhub_recommendations",
  "status": "rejected",
  "code": "<runtime: code>"
}
```

## 6. 反向RPC、业务结果和回执构造细则

主表给出调用方式；以下函数固定嵌套字段、条件增补和响应消费。函数内部的本地副作用不是新增协议方法。

### _normalize_workspace_layout — [box_agent/acp/__init__.py:873](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L873)

```python
def _normalize_workspace_layout(layout):
    if not isinstance(layout, dict):
        return None
    normalized = {}
    for canonical, alias in (
        ("selected_root_dir", "selectedRootDir"),
        ("session_workspace_dir", "sessionWorkspaceDir"),
        ("artifact_root_dir", "artifactRootDir"),
    ):
        for key in (canonical, alias):
            value = layout.get(key)
            if isinstance(value, str) and value.strip():
                normalized[canonical] = value.strip()
                break
    return normalized
```

### _tool_result_raw_output — [box_agent/acp/__init__.py:920](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L920)

```python
def _tool_result_raw_output(
    raw_output: Any,
    result_text: str,
    policy_decision: dict[str, Any] | None,
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    turn_id: str | None = None,
    workspace_dir: str | None = None,
) -> Any:
    if isinstance(raw_output, dict):
        payload = dict(raw_output)
        if payload.get("type") == "artifact":
            for key in ("abs_path", "absolute_path", "path"):
                value = payload.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                try:
                    candidate = Path(value).expanduser()
                    if not candidate.is_absolute():
                        if workspace_dir is None:
                            continue
                        candidate = Path(workspace_dir) / candidate
                    if is_intermediate_artifact(candidate.resolve()):
                        payload["type"] = "intermediate_asset"
                    break
                except (OSError, RuntimeError, ValueError):
                    continue
            if session_id:
                payload.setdefault("session_id", session_id)
                payload.setdefault("sessionId", session_id)
        if session_id and payload.get("file_changes_version") in {1, 2}:
            payload.setdefault("session_id", session_id)
            payload.setdefault("sessionId", session_id)
        if task_id:
            payload.setdefault("task_id", task_id)
            payload.setdefault("taskId", task_id)
        if turn_id:
            payload.setdefault("turn_id", turn_id)
            payload.setdefault("turnId", turn_id)
        if policy_decision is not None:
            payload["policy_decision"] = policy_decision
        return payload
    if policy_decision is None:
        return result_text
    return {
        "type": "tool_result",
        "text": result_text,
        "policy_decision": policy_decision,
    }
```

### _skills_meta — [box_agent/acp/__init__.py:1664](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1664)

```python
def _skills_meta(self) -> list[dict] | None:
    """Return current skills metadata for ACP _meta payload, reloading if changed.

    Returns ``None`` (rather than an empty list) while background
    discovery is still running so the initialize RPC never blocks on
    skill parsing. Hosts that need the catalog can read
    ``session/new._meta.skills`` — by newSession time the task has been
    awaited via ``_ensure_skills_loaded``.
    """
    if not self._skill_loader:
        return None
    if not self._skills_loaded:
        return None
    try:
        return self._skill_loader.list_skills_metadata(include_connector=False)
    except Exception as exc:
        log.warn("skills/meta_error", message=f"Failed to build skills metadata: {exc}")
        return None
```

### _apply_goal_action — [box_agent/acp/__init__.py:3675](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L3675)

```python
def _apply_goal_action(self, agent: Agent, params: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "get").strip().lower()
    if action == "status":
        action = "get"
    if action == "create":
        action = "set"
    evidence = params.get("evidence")
    progress = params.get("progress")
    blocked_reason = params.get("blocked_reason") or params.get("blockedReason")
    completed_by = params.get("completed_by") or params.get("completedBy")

    if action == "get":
        return {"ok": True, "goal": _goal_payload(agent)}

    if action == "set":
        objective = params.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            return {"error": "empty_objective"}
        try:
            agent.set_goal(
                objective,
                evidence=evidence,
                progress=progress,
                blocked_reason=blocked_reason,
                completed_by=completed_by,
            )
        except ValueError:
            return {"error": "empty_objective"}

        raw_status = params.get("status")
        status = raw_status.strip().lower() if isinstance(raw_status, str) else "active"
        if status == "paused":
            agent.pause_goal()
        elif status == "complete":
            agent.complete_goal(evidence=evidence, progress=progress, completed_by=completed_by)
        elif status == "blocked":
            reason = blocked_reason if isinstance(blocked_reason, str) else ""
            if not reason.strip():
                return {"error": "empty_blocked_reason"}
            agent.block_goal(reason, evidence=evidence, progress=progress)
        elif status not in ("", "active"):
            return {"error": f"invalid_status: {status}"}
        return {"ok": True, "goal": _goal_payload(agent)}

    if action == "pause":
        if agent.pause_goal() is None:
            return {"error": "goal_not_found"}
        return {"ok": True, "goal": _goal_payload(agent)}

    if action == "resume":
        if agent.resume_goal() is None:
            return {"error": "goal_not_found"}
        return {"ok": True, "goal": _goal_payload(agent)}

    if action == "complete":
        if agent.complete_goal(evidence=evidence, progress=progress, completed_by=completed_by) is None:
            return {"error": "goal_not_found"}
        return {"ok": True, "goal": _goal_payload(agent)}

    if action == "progress":
        if agent.update_goal_progress(progress, evidence=evidence) is None:
            return {"error": "goal_not_found"}
        return {"ok": True, "goal": _goal_payload(agent)}

    if action == "block":
        reason = blocked_reason if isinstance(blocked_reason, str) else ""
        if not reason.strip():
            return {"error": "empty_blocked_reason"}
        try:
            goal = agent.block_goal(reason, evidence=evidence, progress=progress)
        except ValueError:
            return {"error": "empty_blocked_reason"}
        if goal is None:
            return {"error": "goal_not_found"}
        return {"ok": True, "goal": _goal_payload(agent)}

    if action == "clear":
        agent.clear_goal()
        return {"ok": True, "goal": None}

    return {"error": f"unknown_action: {action}"}
```

### _memory_proposal_apply — [box_agent/acp/__init__.py:4436](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4436)

```python
async def _memory_proposal_apply(self, params: dict[str, Any]) -> dict[str, Any]:
    """Apply user decisions to promotion candidates.

    Two schemas are accepted:

    **Legacy (per-candidate)** —
    ``{sessionId, decisions: {id: "pin"|"skip"|"reject"}}``.
    Returns ``{pinned, rejected, skipped, core}``.

    **Plan-mode (delayed decision)** —
    ``{sessionId, plan: {currentCore, newCore, consumedEntryIds,
    rationale}, decision: "apply"|"reject"|"skip"}``.
    Returns ``{applied|rejected|skipped: int, consumed?: int, core}``.
    ``sessionId`` may be an empty string for orphan applies (after
    the originating session has closed) — the server-level memory
    manager is shared across sessions.
    """
    session_id = params.get("sessionId", "")
    if session_id and session_id not in self._sessions:
        return {"error": "session_not_found"}
    memory = (self._sessions[session_id].memory_manager or self._memory) if session_id else self._memory
    if not uses_local_memory(memory):
        return {"error": "memory_unavailable"}

    # ── Plan-mode branch ──────────────────────────────
    raw_plan = params.get("plan")
    if isinstance(raw_plan, dict):
        from ..events import MemoryPromotionPlan

        decision = str(params.get("decision", "")).lower()
        if decision not in ("apply", "reject", "skip"):
            return {"error": "invalid_decision"}

        raw_ids = raw_plan.get("consumedEntryIds") or []
        if not isinstance(raw_ids, list):
            return {"error": "invalid_plan"}
        new_core = str(raw_plan.get("newCore", ""))
        current_core = str(raw_plan.get("currentCore", ""))
        rationale = str(raw_plan.get("rationale", ""))
        consumed = tuple(str(x) for x in raw_ids)

        if decision in ("apply", "reject") and not consumed:
            return {"error": "invalid_plan"}
        if decision == "apply" and not new_core.strip():
            return {"error": "invalid_plan"}

        plan = MemoryPromotionPlan(
            current_core=current_core,
            new_core=new_core,
            consumed_entry_ids=consumed,
            rationale=rationale,
        )

        if decision == "apply":
            def _apply_plan() -> tuple[dict[str, int], str]:
                with memory.context_transaction():
                    counts = memory.apply_promotion_plan(plan)
                    return counts, memory.read_core()

            counts, core = await asyncio.to_thread(_apply_plan)
            log.info(
                "memory/plan_apply",
                session_id=session_id,
                consumed=counts.get("consumed", 0),
            )
            return {
                "applied": counts.get("applied", 1),
                "consumed": counts.get("consumed", 0),
                "core": core,
            }
        if decision == "reject":
            def _reject_plan() -> tuple[dict[str, int], str]:
                with memory.context_transaction():
                    counts = memory.reject_promotion_plan(plan)
                    return counts, memory.read_core()

            counts, core = await asyncio.to_thread(_reject_plan)
            log.info(
                "memory/plan_reject",
                session_id=session_id,
                rejected=counts.get("rejected", 0),
            )
            return {
                "rejected": counts.get("rejected", 0),
                "core": core,
            }
        # skip: host is just dropping its cache; nothing to do here.
        log.info("memory/plan_skip", session_id=session_id)
        core = await asyncio.to_thread(memory.read_core)
        return {"skipped": 1, "core": core}

    # ── Legacy per-candidate branch ───────────────────
    raw = params.get("decisions") or {}
    if not isinstance(raw, dict):
        return {"error": "invalid_decisions"}
    decisions: dict[str, str] = {
        str(entry_id): value
        for entry_id, value in raw.items()
        if isinstance(value, str) and value in ("pin", "skip", "reject")
    }
    def _consume_proposal() -> tuple[dict[str, int], str]:
        with memory.context_transaction():
            counts = memory.consume_core_proposal(decisions)
            return counts, memory.read_core()

    counts, core = await asyncio.to_thread(_consume_proposal)
    log.info(
        "memory/proposal_apply",
        session_id=session_id,
        **counts,
    )
    return {
        "pinned": counts["pinned"],
        "rejected": counts["rejected"],
        "skipped": counts["skipped"],
        "core": core,
    }
```

### _request_skillhub_search — [box_agent/acp/__init__.py:5649](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5649)

```python
async def _request_skillhub_search(
    self,
    session_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Search the host-owned Skill marketplace without exposing credentials."""

    send_ext = getattr(self._conn, "ext_method", None)
    if send_ext is None:
        return {"status": "unavailable"}
    request = {
        "sessionId": session_id,
        "query": payload.get("query"),
        "gapType": payload.get("gapType"),
        "limit": payload.get("limit", 3),
    }
    try:
        response = await send_ext(SKILLHUB_SEARCH_METHOD, request)
    except Exception as exc:
        log.warn(
            "skillhub/search_error",
            session_id=session_id,
            error=type(exc).__name__,
            message="Host Skill marketplace search failed",
        )
        return {"status": "unavailable"}
    return response if isinstance(response, dict) else {"status": "unavailable"}
```

### _request_skillhub_install — [box_agent/acp/__init__.py:5677](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5677)

```python
async def _request_skillhub_install(
    self,
    session_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Ask the authenticated host to install one confirmed market candidate."""

    send_ext = getattr(self._conn, "ext_method", None)
    if send_ext is None:
        return {"status": "unavailable"}
    request = {
        "sessionId": session_id,
        "skillId": payload.get("skillId"),
        "slug": payload.get("slug"),
        "displayName": payload.get("name"),
        "publisherDisplayName": payload.get("publisherDisplayName"),
        "version": payload.get("version"),
    }
    try:
        response = await send_ext(SKILLHUB_INSTALL_METHOD, request)
    except Exception as exc:
        log.warn(
            "skillhub/install_error",
            session_id=session_id,
            error=type(exc).__name__,
            message="Host Skill marketplace installation failed",
        )
        return {"status": "unavailable"}
    return response if isinstance(response, dict) else {"status": "unavailable"}
```

### _negotiate_once — [box_agent/acp/__init__.py:5797](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5797)

```python
async def _negotiate_once(self, permission_request: dict) -> bool:
    """Negotiate one permission request. Returns ``True`` if granted."""
    scope = permission_request.get("scope", "")
    requested_scope = permission_request.get("requested_scope", "")
    path_hint = permission_request.get("path", "")
    is_safety_request = scope == "safety"

    # Dedup: filesystem requests check the directory grant table; other
    # capabilities (memory) use the legacy (scope, requested_scope) key.
    # Safety requests are intentionally never cached: every dangerous
    # command needs an explicit one-shot decision.
    if scope == "filesystem" and path_hint:
        try:
            target = Path(path_hint).expanduser().resolve()
        except (OSError, RuntimeError):
            target = None
        if target is not None and self._store.has_filesystem_dir_grant(target):
            log.info(
                "permission/grant_hit",
                scope=scope,
                path=path_hint,
                message="Filesystem dir grant hit — skipping RPC",
            )
            return True
    elif not is_safety_request and self._store.has_grant(scope, requested_scope):
        log.info(
            "permission/grant_hit",
            scope=scope,
            requested_scope=requested_scope,
            message="Grant table hit — skipping RPC",
        )
        return True

    # Build ACP RequestPermissionRequest
    from acp.schema import (
        AllowedOutcome,
        PermissionOption,
        ToolCallUpdate,
    )

    reason = permission_request.get("reason", "")
    description = reason + (f": {path_hint}" if path_hint else "")
    temporary_supported = permission_request.get("temporary_supported", True) is not False
    persistent_supported = permission_request.get("persistent_supported", True) is not False
    tool_call = ToolCallUpdate(
        tool_call_id=f"perm-{scope}-{requested_scope}",
        raw_input=permission_request,
    )
    options = []
    if temporary_supported:
        options.append(PermissionOption(option_id="approve", name="仅本次允许", kind="allow_once"))
    if persistent_supported:
        persistent_name = permission_request.get("persistent_label") or "始终允许"
        options.append(
            PermissionOption(
                option_id="approve_session",
                name=str(persistent_name),
                kind="allow_always",
            )
        )
    options.append(PermissionOption(option_id="reject", name="拒绝", kind="reject_once"))

    log.info(
        "permission/request",
        scope=scope,
        requested_scope=requested_scope,
        description=description,
    )

    try:
        response = await asyncio.wait_for(
            self._conn.request_permission(
                session_id=self._session_id, tool_call=tool_call, options=options,
            ),
            timeout=120.0,
        )
    except asyncio.TimeoutError:
        log.warn(
            "permission/timeout",
            scope=scope,
            requested_scope=requested_scope,
            message="Timed out waiting for user decision — treating as denial",
        )
        return False
    except Exception as exc:
        log.warn(
            "permission/error",
            scope=scope,
            requested_scope=requested_scope,
            message=f"request_permission failed: {exc}",
        )
        return False

    if isinstance(response.outcome, AllowedOutcome):
        if response.outcome.option_id == "reject":
            log.info(
                "permission/denied",
                scope=scope,
                requested_scope=requested_scope,
            )
            return False
        grant_scope = self._OPTION_TO_SCOPE.get(response.outcome.option_id)
        if grant_scope is None:
            return False
        granted = self._store.apply_permission_grant(permission_request, grant_scope)
        log.info(
            "permission/granted" if granted else "permission/denied",
            scope=scope,
            requested_scope=requested_scope,
            grant_scope="one_shot" if is_safety_request else grant_scope,
        )
        return granted

    log.info(
        "permission/denied",
        scope=scope,
        requested_scope=requested_scope,
    )
    return False
```

### _record_skill_invocation — [box_agent/acp/__init__.py:4645](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4645)

```python
def _record_skill_invocation(
    skill_name: str,
    activation_source: str,
    *,
    usage_role: str = "primary",
    dependency_of: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    invocation_key = "\x1f".join(
        (
            billing_session_id or state.upstream_session_id or session_id,
            turn_id,
            skill_name,
        )
    )
    invocation_id = f"skill_{sha256(invocation_key.encode('utf-8')).hexdigest()[:32]}"
    if invocation_id in recorded_skill_invocation_ids:
        return None

    invocation: dict[str, Any] = {
        "invocationId": invocation_id,
        "skillName": skill_name,
        "activationSource": activation_source,
        "status": "succeeded",
        "usageRole": usage_role,
    }
    if usage_role == "dependency" and dependency_of:
        invocation["dependencyOf"] = dependency_of

    if metadata:
        for field, key in (("skillSource", "source"),
                           ("skillVersion", "skill_version"),
                           ("instructionDigest", "instruction_digest")):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                invocation[field] = value
    elif state.skill_loader is not None:
        skill = state.skill_loader.get_skill(
            skill_name,
            include_disabled=False,
        )
        # A broken SKILL.md returns a diagnostic from get_skill but does
        # not activate usable instructions, so it is not a billable fact.
        if skill is not None and skill.broken:
            return None
        if skill is not None:
            invocation["skillSource"] = skill.source
            raw_version = (
                skill.metadata.get("version")
                if isinstance(skill.metadata, dict)
                else None
            )
            if isinstance(raw_version, (str, int, float)) and not isinstance(
                raw_version, bool
            ):
                skill_version = str(raw_version).strip()
                if skill_version:
                    invocation["skillVersion"] = skill_version
            if skill.skill_path is not None:
                try:
                    invocation["instructionDigest"] = sha256(
                        skill.skill_path.read_bytes()
                    ).hexdigest()
                except OSError:
                    # Identity enrichment is best-effort. The successful
                    # runtime activation remains the source of truth.
                    pass

    expert_context = state.expert_context
    if expert_context is not None:
        if expert_context.expert is not None:
            invocation["contextExpertId"] = expert_context.expert.id
        if expert_context.team is not None:
            invocation["contextTeamId"] = expert_context.team.id

    recorded_skill_invocation_ids.add(invocation_id)
    skill_invocations.append(invocation)
    return invocation
```

### _turn_usage_payload — [box_agent/acp/__init__.py:4818](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L4818)

```python
def _turn_usage_payload(current: dict[str, Any] | None = None) -> dict[str, Any]:
    meter = get_token_meter()
    token_usage = (
        {
            "promptTokens": meter.prompt_tokens,
            "completionTokens": meter.completion_tokens,
            "totalTokens": meter.total_tokens,
            "calls": meter.calls,
        }
        if meter is not None and meter.total_tokens > 0
        else observer.usage.as_payload()
    )
    if meter is not None and meter.total_tokens > 0 and meter.cache_usage_reported_calls:
        token_usage["cachedTokens"] = meter.cached_tokens
    payload: dict[str, Any] = {
        "type": "turn_usage",
        "version": 3,
        "sessionId": billing_session_id or state.upstream_session_id or session_id,
        "session_id": billing_session_id or state.upstream_session_id or session_id,
        "acpSessionId": session_id,
        "taskId": task_context.task_id,
        "task_id": task_context.task_id,
        "turnId": turn_id,
        "turn_id": turn_id,
        "skills": list(used_skill_names),
        "skillInvocations": list(skill_invocations),
        "tools": [
            {"name": name, "count": count}
            for name, count in used_tool_counts.items()
        ],
        "mcp": [
            {
                "server": server_name,
                "tool": tool_name,
                "name": f"{server_name}.{tool_name}",
                "count": count,
            }
            for (server_name, tool_name), count in used_mcp_tool_counts.items()
        ],
        "tokenUsage": token_usage,
    }
    if current:
        payload["current"] = current
    return payload
```

### _request — [box_agent/tools/runtime_environment_tool.py:139](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/runtime_environment_tool.py#L139)

```python
async def _request(self, capability: str, action: str) -> dict[str, Any]:
    try:
        result = await asyncio.wait_for(
            self._requester({"capability": capability, "action": action}),
            timeout=600 if action == "ensure" else 30,
        )
    except asyncio.TimeoutError:
        return {
            "status": "timeout",
            "error": "Host response timed out; background installation may still be running. "
                     "Check status before retrying.",
        }
    except Exception as exc:
        if capability == "chromium":
            code = getattr(exc, "code", None)
            if type(code) is int:
                return {"status": "unavailable", "errorKind": "request_rejected",
                        "rpcCode": code, "error": "Host rejected the Chromium runtime request."}
            return {"status": "unavailable",
                    "errorKind": "transport_error" if isinstance(exc, (ConnectionError, OSError)) else "request_error",
                    "error": "Host Chromium runtime request failed; readiness is unknown."}
        return {"status": "unavailable", "error": "Host runtime service is unavailable."}
    if not isinstance(result, dict):
        return {"status": "failed", "error": "Invalid host runtime response.",
                **({"errorKind": "invalid_response"} if capability == "chromium" else {})}
    # Environment values stay internal, never become model-facing tool output.
    result = dict(result)
    env = result.pop("env", None)
    if isinstance(env, dict):
        self.runtime_env.update({
            k: v for k, v in env.items() if k in _ENV_KEYS and isinstance(v, str)
        })
    if result.get("status") == "ready" and isinstance(env, dict) and self.apply_environment:
        shell = self.apply_environment({
            k: v for k, v in env.items() if k in _ENV_KEYS and isinstance(v, str)
        })
        if shell is not None:
            result["shell"] = shell
    return result
```

### _normalize_candidate — [box_agent/tools/skillhub_search_tool.py:104](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_search_tool.py#L104)

```python
def _normalize_candidate(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    skill_id = _bounded_text(value.get("id"), limit=80)
    slug = _bounded_text(value.get("slug"), limit=80)
    name = _bounded_text(value.get("name"), limit=160)
    if not skill_id or not slug or not name:
        return None
    platforms = value.get("platforms")
    risk_labels = value.get("riskLabels")
    download_count = value.get("downloadCount")
    return {
        "id": skill_id,
        "slug": slug,
        "name": name,
        "description": _bounded_text(value.get("description"), limit=500),
        "publisherDisplayName": _bounded_text(
            value.get("publisherDisplayName"), limit=160
        ),
        "currentVersion": _bounded_text(value.get("currentVersion"), limit=40),
        "platforms": [
            item[:40]
            for item in platforms[:8]
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(platforms, list)
        else [],
        "riskLabels": [
            item[:80]
            for item in risk_labels[:8]
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(risk_labels, list)
        else [],
        "downloadCount": (
            download_count
            if isinstance(download_count, int) and not isinstance(download_count, bool)
            and download_count >= 0
            else 0
        ),
    }
```

### execute — [box_agent/tools/skillhub_search_tool.py:270](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_search_tool.py#L270)

```python
async def execute(
    self,
    requested_outcome: str,
    request_kind: str,
    missing_capability: str,
    gap_type: str,
    fallback_assessment: str,
    queries: list[str],
) -> ToolResult:
    requested_outcome = requested_outcome.strip()
    missing_capability = missing_capability.strip()
    fallback_assessment = fallback_assessment.strip()
    normalized_queries = []
    for query in queries if isinstance(queries, list) else []:
        if not isinstance(query, str):
            continue
        normalized = query.strip()
        if normalized and normalized not in normalized_queries:
            normalized_queries.append(normalized)

    if request_kind not in SEARCH_REQUEST_KINDS:
        return self._guard_error(
            "INVALID_SEARCH_REQUEST",
            "Unsupported Skill marketplace search request kind.",
        )
    if gap_type not in GAP_TYPES:
        return self._guard_error("NOT_CAPABILITY_GAP", "Unsupported capability-gap type.")
    if not all((requested_outcome, missing_capability, fallback_assessment)):
        return self._guard_error(
            "NOT_CAPABILITY_GAP",
            "The requested outcome, missing capability, and fallback assessment are required.",
        )
    if (
        not 2 <= len(normalized_queries) <= 5
        or any(
            not 2 <= len(query) <= 60 or not _query_is_safe(query)
            for query in normalized_queries
        )
    ):
        return self._guard_error(
            "UNSAFE_MARKET_QUERY",
            "Provide 2-5 short, independent, non-sensitive capability keywords and try once.",
        )
    if self._searched_this_turn:
        return self._guard_error(
            "SEARCH_ALREADY_PERFORMED",
            "Do not search the Skill marketplace again this turn. Continue with the "
            "existing result or best effort.",
        )

    snapshot = dict(self._snapshot_provider() or {})
    if (
        request_kind == "capability_gap"
        and snapshot.get("tool_search_available")
        and not snapshot.get("tool_search_used")
    ):
        return self._guard_error(
            "LOCAL_DISCOVERY_REQUIRED",
            "Use tool_search once to check deferred MCP capabilities before the "
            "Skill marketplace.",
        )

    self._searched_this_turn = True

    async def search_queries() -> tuple[list[dict[str, Any]], list[str], bool]:
        candidates: list[dict[str, Any]] = []
        candidate_ids: set[str] = set()
        searched_queries: list[str] = []
        unavailable = False
        for query in normalized_queries:
            searched_queries.append(query)
            payload = {
                "query": query,
                "gapType": gap_type,
                "limit": 3,
            }
            try:
                response = await self._searcher(payload)
            except Exception:
                unavailable = True
                continue
            if not isinstance(response, dict):
                unavailable = True
                continue
            status = response.get("status")
            if status == "unavailable":
                unavailable = True
                continue
            raw_items = response.get("items")
            if status != "found" or not isinstance(raw_items, list):
                continue
            for item in raw_items:
                candidate = _normalize_candidate(item)
                if candidate is None or candidate["id"] in candidate_ids:
                    continue
                candidate_ids.add(candidate["id"])
                candidates.append(candidate)
                if len(candidates) == 3:
                    return candidates, searched_queries, unavailable
        return candidates, searched_queries, unavailable

    try:
        candidates, searched_queries, unavailable = await asyncio.wait_for(
            search_queries(), timeout=self._timeout_seconds
        )
    except Exception:
        candidates = []
        searched_queries = []
        unavailable = True

    if candidates:
        self._candidates_by_id = {
            candidate["id"]: dict(candidate) for candidate in candidates
        }
        names = ", ".join(candidate["name"] for candidate in candidates)
        installation_guidance = (
            "Immediately call install_skillhub_skill with the selected candidate's exact "
            "skill_id. Do not ask for confirmation in prose and do not end the turn first: "
            "that tool owns the single mandatory host confirmation. Do not call get_skill "
            "before installation succeeds and do not present prose-only letter choices."
            if self._installation_available
            else (
                "Installation is not available in this host. Direct the user to the "
                "Skill marketplace recommendation card to review and confirm installation."
            )
        )
        content = (
            f"The Skill marketplace found {len(candidates)} candidate Skill(s): {names}. "
            f"They are not installed. {installation_guidance} You may also provide "
            "bounded interim help without claiming the missing capability is available."
        )
        candidate_context = "\n".join(
            f"- skill_id={candidate['id']!r}; slug={candidate['slug']!r}; "
            f"name={candidate['name']!r}"
            for candidate in candidates
        )
        model_context = (
            f"{content}\n\nExact Skill marketplace candidates returned for this session:\n"
            f"{candidate_context}\nUse the exact skill_id value above; never substitute "
            "the name, slug, translated text, or a guessed identifier."
        )
        normalized_status = "found"
    elif not unavailable:
        self._candidates_by_id = {}
        query_summary = ", ".join(repr(query) for query in searched_queries)
        content = (
            f"The Skill marketplace was searched for {query_summary} and returned no "
            "matching Skill. This result is scoped only to the Skill marketplace; it "
            "does not establish that a supplied "
            "repository or another external source has no matching Skill. Do not end the "
            "turn solely because of this result. "
            f"{SKILL_SOURCE_DISCOVERY_PROMPT}\nThe missing capability is: {missing_capability}. "
            "For high-risk work, provide informational guidance only and no professional "
            "verdict."
        )
        normalized_status = "empty"
        candidates = []
        model_context = content
    else:
        self._candidates_by_id = {}
        content = (
            "The Skill marketplace could not be searched right now. Do not say that no "
            "matching Skill exists. "
            f"{SKILL_SOURCE_DISCOVERY_PROMPT}\nState that the "
            f"missing capability is: {missing_capability}. For high-risk work, provide "
            "informational guidance only and no professional verdict."
        )
        normalized_status = "unavailable"
        candidates = []
        model_context = content

    return ToolResult(
        success=True,
        content=content,
        model_context=model_context,
        raw_output={
            "type": SKILLHUB_RECOMMENDATIONS_TYPE,
            "status": normalized_status,
            "requestKind": request_kind,
            "query": searched_queries[0] if searched_queries else normalized_queries[0],
            "queries": normalized_queries,
            "searchedQueries": searched_queries,
            "gapType": gap_type,
            "missingCapability": missing_capability[:300],
            "items": candidates,
        },
    )
```

### _guard_error — [box_agent/tools/skillhub_search_tool.py:459](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_search_tool.py#L459)

```python
def _guard_error(code: str, message: str) -> ToolResult:
    return ToolResult(
        success=False,
        error=f"{code}: {message}",
        raw_output={
            "type": SKILLHUB_RECOMMENDATIONS_TYPE,
            "status": "rejected",
            "code": code,
        },
    )
```

### execute — [box_agent/tools/skillhub_install_tool.py:92](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_install_tool.py#L92)

```python
async def execute(self, skill_id: str) -> ToolResult:
    normalized_id = skill_id.strip()
    candidate = self._candidate_provider(normalized_id)
    if candidate is None:
        known_candidates = self._candidate_list_provider()
        candidate_hint = ""
        if known_candidates:
            rendered = []
            for item in known_candidates[:3]:
                candidate_id = _text(item.get("id"), limit=80)
                slug = _text(item.get("slug"), limit=80)
                name = _text(item.get("name"), limit=160)
                if candidate_id:
                    rendered.append(
                        f"skill_id={candidate_id!r}, slug={slug!r}, name={name!r}"
                    )
            if rendered:
                candidate_hint = (
                    " Available candidates: "
                    + "; ".join(rendered)
                    + ". Use an exact skill_id; do not guess or bypass the Skill "
                    "marketplace with "
                    "another installer or online service."
                )
        return self._error(
            "SEARCH_REQUIRED",
            "Search the Skill marketplace first and use the exact ID of a candidate "
            f"returned in this session.{candidate_hint}",
            skill_id=normalized_id,
        )

    normalized_candidate = {
        "id": normalized_id,
        "slug": _text(candidate.get("slug"), limit=80),
        "name": _text(candidate.get("name"), limit=160),
        "publisherDisplayName": _text(
            candidate.get("publisherDisplayName"), limit=160
        ),
        "currentVersion": _text(candidate.get("currentVersion"), limit=40),
    }
    if not normalized_candidate["slug"] or not normalized_candidate["name"]:
        return self._error(
            "INVALID_CANDIDATE",
            "The selected Skill marketplace candidate is incomplete and cannot be installed.",
            skill_id=normalized_id,
        )

    installed_name = await asyncio.to_thread(self._installed_skill_name,
        normalized_id,
        normalized_candidate["slug"],
    )
    if installed_name:
        return self._successful_result(
            normalized_candidate,
            status="already_installed",
            visible_name=installed_name,
        )

    if normalized_id not in self._approved_skill_ids:
        publisher = normalized_candidate["publisherDisplayName"] or "unknown publisher"
        return ToolResult(
            success=False,
            error="USER_CONFIRMATION_REQUIRED: Skill marketplace installation needs approval.",
            permission_request={
                "type": "permission_request",
                "scope": "skillhub",
                "requested_scope": f"install:{normalized_id}",
                "reason": (
                    f"Install '{normalized_candidate['name']}' from the Skill marketplace, "
                    f"published by {publisher}"
                ),
                "temporary_supported": True,
                "persistent_supported": False,
                "skill_id": normalized_id,
                "slug": normalized_candidate["slug"],
                "name": normalized_candidate["name"],
                "version": normalized_candidate["currentVersion"],
            },
            raw_output={
                "type": SKILLHUB_INSTALLATION_TYPE,
                "status": "confirmation_required",
                "candidate": normalized_candidate,
            },
        )

    self._approved_skill_ids.discard(normalized_id)
    payload = {
        "skillId": normalized_id,
        "slug": normalized_candidate["slug"],
        "name": normalized_candidate["name"],
        "publisherDisplayName": normalized_candidate["publisherDisplayName"],
        "version": normalized_candidate["currentVersion"],
    }
    try:
        response = await asyncio.wait_for(
            self._installer(payload), timeout=self._timeout_seconds
        )
    except Exception:
        response = {"status": "unavailable"}

    status = response.get("status") if isinstance(response, dict) else None
    if status not in {"installed", "already_installed"}:
        if status == "unavailable":
            return self._error(
                "INSTALL_UNAVAILABLE",
                "The host could not install this Skill right now.",
                skill_id=normalized_id,
            )
        detail = _text(
            response.get("error") if isinstance(response, dict) else None,
            limit=500,
        )
        return self._error(
            "INSTALL_FAILED",
            detail or "The host reported that Skill marketplace installation failed.",
            skill_id=normalized_id,
        )

    skill_payload = response.get("skill") if isinstance(response, dict) else None
    installed_name = ""
    if isinstance(skill_payload, dict):
        installed_name = _text(skill_payload.get("name"), limit=80) or _text(
            skill_payload.get("slug"), limit=80
        )
    if not installed_name:
        installed_name = normalized_candidate["slug"]

    visible_name = await asyncio.to_thread(self._refresh_skill, installed_name, normalized_candidate["slug"])
    return self._successful_result(
        normalized_candidate,
        status=status,
        visible_name=visible_name,
        installed_name=installed_name,
    )
```

### _error — [box_agent/tools/skillhub_install_tool.py:304](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skillhub_install_tool.py#L304)

```python
def _error(code: str, message: str, *, skill_id: str) -> ToolResult:
    return ToolResult(
        success=False,
        error=f"{code}: {message}",
        raw_output={
            "type": SKILLHUB_INSTALLATION_TYPE,
            "status": "failed",
            "code": code,
            "skillId": skill_id,
        },
    )
```

### disconnect_mcp_server — [box_agent/tools/mcp_loader.py:2066](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/mcp_loader.py#L2066)

```python
async def disconnect_mcp_server(name: str) -> dict:
    """Disconnect a named MCP server and return its removed tool names."""
    global _mcp_connections
    epoch = _begin_mcp_connection(name)
    connections = [c for c in _mcp_connections if c.name == name]
    _mcp_connections = [c for c in _mcp_connections if c.name != name]
    get_mcp_tool_catalog().remove_server(name)
    removed_tools = [t.name for conn in connections for t in conn.tools]
    error = None
    for conn in connections:
        try:
            await conn.disconnect()
        except Exception as failure:
            error = str(failure)
            if _mcp_connection_epochs.get(name) is epoch:
                _mcp_connections.append(conn)
        finally:
            conn.tools = []
    if _mcp_connection_epochs.get(name) is epoch:
        _record_status(name, "failed" if error else "disabled", error=error)
    return {"success": error is None, "removedTools": removed_tools, **({"error": error} if error else {})}
```

### get_browser_isolation_status — [box_agent/tools/mcp_loader.py:2139](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/mcp_loader.py#L2139)

```python
def get_browser_isolation_status() -> dict | None:
    """Describe the managed-browser isolation mode for ``_mcp/status`` consumers."""
    conn = next((c for c in _mcp_connections if c.name == PLAYWRIGHT_SERVER_NAME), None)
    if conn is None:
        return None
    if conn.session_pool is not None:
        snapshot = conn.session_pool.snapshot()
        if conn.server_process is not None:
            snapshot["pid"] = conn.server_process.pid
            snapshot["url"] = conn.server_process.url
        return snapshot
    return {"mode": "shared_context"}
```

### get_mcp_status — [box_agent/tools/mcp_loader.py:2161](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/mcp_loader.py#L2161)

```python
def get_mcp_status() -> list[dict]:
    browser = get_browser_isolation_status()
    statuses = []
    for s in _mcp_status.values():
        entry = {
            "name": s.name,
            "owner": s.owner,
            "configId": s.config_id,
            "connectorId": s.connector_id,
            "connectorName": s.connector_name,
            "sourcePath": s.source_path,
            "state": s.state,
            "transport": s.transport,
            "toolCount": s.tool_count,
            "tools": s.tools,
            "error": s.error,
            "authStatus": s.auth_status,
        }
        if s.name == PLAYWRIGHT_SERVER_NAME and browser is not None:
            entry["browser"] = browser
        statuses.append(entry)
    return statuses
```

### reconnect_mcp_server — [box_agent/tools/mcp_loader.py:2707](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/mcp_loader.py#L2707)

```python
async def reconnect_mcp_server(name: str) -> dict:
    """Serialize hot reconnects for one server name."""
    global _mcp_server_definitions
    lock = _mcp_reconnect_locks.setdefault(name, asyncio.Lock())
    async with lock:
        if _mcp_config_path:
            try:
                # Reading another server's new credential version does not mean
                # its existing connection has used it. Update this server only.
                current = _resolve_registered_sources(_mcp_config_path)
                if name in current:
                    _mcp_server_definitions[name] = current[name]
                else:
                    _mcp_server_definitions.pop(name, None)
            except Exception as error:
                return {"success": False, "error": str(error)}
        return await _reconnect_mcp_server_locked(name)
```

### _reconcile_mcp_sources_locked — [box_agent/tools/mcp_loader.py:2813](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/mcp_loader.py#L2813)

```python
async def _reconcile_mcp_sources_locked(
    source: str | None = None, connector_ids: list[str] | None = None,
) -> dict:
    global _mcp_server_definitions
    if source is not None and source not in {"system", "connector", "user"}:
        return {"success": False, "error": f"Unknown MCP source: {source}"}
    if not _mcp_config_path:
        return {"success": False, "error": "MCP source paths are not initialized"}

    previous = dict(_mcp_server_definitions)
    try:
        current = _resolve_registered_sources(_mcp_config_path)
    except Exception as error:
        return {"success": False, "error": str(error)}

    async def retire(name: str, before: ResolvedMcpServer | None,
                     after: ResolvedMcpServer | None) -> dict | None:
        # An in-flight reconnect must finish publishing before revocation can
        # close it. Return only after that same server's critical section ends.
        lock = _mcp_reconnect_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if after is None:
                result = await disconnect_mcp_server(name)
                if not result.get("success"):
                    return {"name": name, "action": "removed", **result}
                if before is not None:
                    _record_status(name, "disabled", definition=before)
                _mcp_server_definitions.pop(name, None)
                return {"name": name, "action": "removed", **result}
            _mcp_server_definitions[name] = after
            if before is None or not before.config.get("disabled", False) or any(
                connection.name == name for connection in _mcp_connections
            ):
                result = await disconnect_mcp_server(name)
                if result.get("success"):
                    _record_status(name, "disabled", definition=after)
                return {"name": name, "action": "disabled", **result}
            _record_status(name, "disabled", definition=after)
            return None

    async def reconnect(name: str, action: str) -> dict:
        result = await reconnect_mcp_server(name)
        return {"name": name, "action": action, **result}

    def in_scope(definition: ResolvedMcpServer | None) -> bool:
        return definition is not None and (
            source is None or definition.owner == source
        ) and (
            connector_ids is None or definition.connector_id in connector_ids
        )

    operations = []
    all_names = sorted(set(previous) | set(current))
    for name in all_names:
        before = previous.get(name)
        after = current.get(name)
        before_in_scope, after_in_scope = in_scope(before), in_scope(after)
        if not before_in_scope and not after_in_scope:
            continue
        if not after_in_scope:
            # Revoke the old owner without applying a newly revealed source
            # outside this update. Its own reconciliation may activate it later.
            operations.append(retire(name, before, None))
            continue
        if after.config.get("disabled", False):
            operations.append(retire(name, before, after))
            continue
        if before is not None and before.fingerprint == after.fingerprint:
            status = _mcp_status.get(name)
            # An unchanged configuration is not proof of a successful connection.
            # Retry failures, but leave healthy or still-loading servers alone.
            if status is None or status.state != "failed":
                continue
        operations.append(reconnect(name, "added" if before is None else "modified"))

    # Reconnects and revocations share per-server locks, while unrelated names
    # start independently even if one removal waits for a slow connection.
    results = [result for result in await asyncio.gather(*operations) if result is not None]

    return {
        "success": all(result.get("success", False) for result in results),
        "source": source,
        "results": results,
        "configPaths": get_mcp_config_paths(),
    }
```

### to_metadata — [box_agent/experts.py:181](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L181)

```python
def to_metadata(self) -> dict[str, object]:
    meta: dict[str, object] = {"id": self.id, "name": self.name}
    if self.revision:
        meta["revision"] = self.revision
    skills = {
        "required": self.required_skills,
        "default": self.default_skills,
        "optional": self.optional_skills,
    }
    if any(skills.values()):
        meta["skills"] = skills
    return meta
```

### to_metadata — [box_agent/experts.py:244](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L244)

```python
def to_metadata(self) -> dict[str, object]:
    return {"id": self.id, "title": self.title, "owner": self.owner}
```

### to_metadata — [box_agent/experts.py:293](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L293)

```python
def to_metadata(self) -> dict[str, object]:
    return {
        "member_id": self.member_id,
        "title": self.title,
        "required": self.required,
    }
```

### to_metadata — [box_agent/experts.py:375](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L375)

```python
def to_metadata(self) -> dict[str, object]:
    return {
        "stage_count": len(self.stages),
        "workstream_count": len(self.workstreams),
        "required_workstreams": [
            stream.to_metadata() for stream in self.workstreams if stream.required
        ],
    }
```

### to_metadata — [box_agent/experts.py:437](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L437)

```python
def to_metadata(self) -> dict[str, object]:
    meta: dict[str, object] = {"id": self.id, "name": self.name}
    if self.source_expert_id:
        meta["source_expert_id"] = self.source_expert_id
    return meta
```

### to_metadata — [box_agent/experts.py:636](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L636)

```python
def to_metadata(self) -> dict[str, object]:
    meta: dict[str, object] = {
        "id": self.id,
        "name": self.name,
        "execution_mode": self.execution_mode,
        "members": [member.to_metadata() for member in self.members],
    }
    if self.revision:
        meta["revision"] = self.revision
    if self.leader:
        meta["leader"] = self.leader.to_metadata()
    if self.orchestration:
        meta["orchestration"] = self.orchestration.to_metadata()
    return meta
```

### to_metadata — [box_agent/experts.py:773](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/experts.py#L773)

```python
def to_metadata(self) -> dict[str, object]:
    meta: dict[str, object] = {}
    if self.expert:
        meta["expert"] = self.expert.to_metadata()
    if self.team:
        meta["expert_team"] = self.team.to_metadata()
    return meta
```

### to_dict — [box_agent/workspace_registry.py:33](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/workspace_registry.py#L33)

```python
def to_dict(self) -> dict[str, str]:
    return asdict(self)
```

## 7. 锁定 SDK 0.12.1 的线字段声明

静态读取本地已安装的该版本 schema.py；未安装或运行新依赖。只列字段声明；SDK支持字段与Box-Agent实际消费范围的差异以主表为准。field_meta序列化为_meta，alias为线上字段名。

### TextResourceContents

```python
mime_type: Annotated[Optional[str], Field(alias='mimeType')] = None
text: str
uri: str
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### BlobResourceContents

```python
blob: str
mime_type: Annotated[Optional[str], Field(alias='mimeType')] = None
uri: str
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### PromptCapabilities

```python
image: Optional[bool] = False
audio: Optional[bool] = False
embedded_context: Annotated[Optional[bool], Field(alias='embeddedContext')] = False
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### Implementation

```python
name: str
title: Optional[str] = None
version: str
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### DeniedOutcome

```python
outcome: Literal['cancelled']
```

### CancelNotification

```python
session_id: Annotated[str, Field(alias='sessionId')]
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### ResourceLink

```python
annotations: Optional[Annotations] = None
description: Optional[str] = None
mime_type: Annotated[Optional[str], Field(alias='mimeType')] = None
name: str
size: Optional[int] = None
title: Optional[str] = None
uri: str
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### PermissionOption

```python
option_id: Annotated[str, Field(alias='optionId')]
name: str
kind: PermissionOptionKind
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### PromptResponse

```python
stop_reason: Annotated[StopReason, Field(alias='stopReason')]
usage: Optional[Usage] = None
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### AllowedOutcome

```python
outcome: Literal['selected']
```

### TextContentBlock

```python
type: Literal['text']
```

### ImageContentBlock

```python
type: Literal['image']
```

### NewSessionRequest

```python
cwd: str
additional_directories: Annotated[Optional[List[str]], Field(alias='additionalDirectories')] = None
mcp_servers: Annotated[List[Union[HttpMcpServer, SseMcpServer, AcpMcpServer, McpServerStdio]], Field(alias='mcpServers')]
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### PromptRequest

```python
session_id: Annotated[str, Field(alias='sessionId')]
prompt: List[Annotated[Union[TextContentBlock, ImageContentBlock, AudioContentBlock, ResourceContentBlock, EmbeddedResourceContentBlock], Field(discriminator='type')]]
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### RequestPermissionResponse

```python
outcome: Annotated[Union[DeniedOutcome, AllowedOutcome], Field(discriminator='outcome')]
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### AgentMessageChunk

```python
session_update: Annotated[Literal['agent_message_chunk'], Field(alias='sessionUpdate')]
```

### AgentThoughtChunk

```python
session_update: Annotated[Literal['agent_thought_chunk'], Field(alias='sessionUpdate')]
```

### ToolCall

```python
tool_call_id: Annotated[str, Field(alias='toolCallId')]
title: str
kind: Optional[ToolKind] = None
status: Optional[ToolCallStatus] = None
content: Optional[List[Annotated[Union[ContentToolCallContent, FileEditToolCallContent, TerminalToolCallContent], Field(discriminator='type')]]] = None
locations: Optional[List[ToolCallLocation]] = None
raw_input: Annotated[Optional[Any], Field(alias='rawInput')] = None
raw_output: Annotated[Optional[Any], Field(alias='rawOutput')] = None
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### ClientCapabilities

```python
fs: Annotated[Optional[FileSystemCapabilities], Field(validate_default=True)] = FileSystemCapabilities()
terminal: Optional[bool] = False
session: Optional[ClientSessionCapabilities] = None
plan: Optional[PlanCapabilities] = None
auth: Annotated[Optional[AuthCapabilities], Field(validate_default=True)] = {'terminal': False}
elicitation: Optional[ElicitationCapabilities] = None
nes: Optional[ClientNesCapabilities] = None
position_encodings: Annotated[Optional[List[str]], Field(alias='positionEncodings')] = None
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### ToolCallUpdate

```python
tool_call_id: Annotated[str, Field(alias='toolCallId')]
kind: Optional[ToolKind] = None
status: Optional[ToolCallStatus] = None
title: Optional[str] = None
content: Optional[List[Annotated[Union[ContentToolCallContent, FileEditToolCallContent, TerminalToolCallContent], Field(discriminator='type')]]] = None
locations: Optional[List[ToolCallLocation]] = None
raw_input: Annotated[Optional[Any], Field(alias='rawInput')] = None
raw_output: Annotated[Optional[Any], Field(alias='rawOutput')] = None
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### NewSessionResponse

```python
session_id: Annotated[str, Field(alias='sessionId')]
modes: Optional[SessionModeState] = None
config_options: Annotated[Optional[List[Annotated[Union[SessionConfigOptionSelect, SessionConfigOptionBoolean], Field(discriminator='type')]]], Field(alias='configOptions')] = None
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### InitializeRequest

```python
protocol_version: Annotated[int, Field(alias='protocolVersion', ge=0, le=65535)]
client_capabilities: Annotated[Optional[ClientCapabilities], Field(alias='clientCapabilities', validate_default=True)] = ClientCapabilities()
client_info: Annotated[Optional[Implementation], Field(alias='clientInfo')] = None
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### RequestPermissionRequest

```python
session_id: Annotated[str, Field(alias='sessionId')]
tool_call: Annotated[ToolCallUpdate, Field(alias='toolCall')]
options: List[PermissionOption]
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### AgentCapabilities

```python
load_session: Annotated[Optional[bool], Field(alias='loadSession')] = False
prompt_capabilities: Annotated[Optional[PromptCapabilities], Field(alias='promptCapabilities', validate_default=True)] = PromptCapabilities()
mcp_capabilities: Annotated[Optional[McpCapabilities], Field(alias='mcpCapabilities', validate_default=True)] = McpCapabilities()
session_capabilities: Annotated[Optional[SessionCapabilities], Field(alias='sessionCapabilities', validate_default=True)] = SessionCapabilities()
auth: Annotated[Optional[AgentAuthCapabilities], Field(validate_default=True)] = {}
providers: Optional[ProvidersCapabilities] = None
nes: Optional[NesCapabilities] = None
position_encoding: Annotated[Optional[str], Field(alias='positionEncoding')] = None
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### SessionNotification

```python
session_id: Annotated[str, Field(alias='sessionId')]
update: Annotated[Union[UserMessageChunk, AgentMessageChunk, AgentThoughtChunk, ToolCallStart, ToolCallProgress, AgentPlanUpdate, AgentPlanContentUpdate, AgentPlanRemovedUpdate, AvailableCommandsUpdate, CurrentModeUpdate, ConfigOptionUpdate, SessionInfoUpdate, UsageUpdate], Field(discriminator='session_update')]
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

### InitializeResponse

```python
protocol_version: Annotated[int, Field(alias='protocolVersion', ge=0, le=65535)]
agent_capabilities: Annotated[Optional[AgentCapabilities], Field(alias='agentCapabilities', validate_default=True)] = AgentCapabilities()
auth_methods: Annotated[Optional[List[Union[EnvVarAuthMethod, TerminalAuthMethod, AuthMethodAgent]]], Field(alias='authMethods', validate_default=True)] = []
agent_info: Annotated[Optional[Implementation], Field(alias='agentInfo')] = None
field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```


## 8. 共享运行时、技能引用及内置网页工具的结果

方向：下行 tool_call_update.rawOutput；用户可见性仍由实际事件控制。补足工具目录以外生产的结果及无type引用回执。

### [box_agent/agent.py:284](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L284)

参数/结构示例：
```json
"<runtime: _goal_snapshot(self._agent)>"
```

### [box_agent/agent.py:279](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L279)

参数/结构示例：
```json
"<runtime: _goal_snapshot(self._agent)>"
```

### [box_agent/agent.py:371](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L371)

参数/结构示例：
```json
"<runtime: _goal_snapshot(self._agent, action='set')>"
```

### [box_agent/agent.py:380](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L380)

参数/结构示例：
```json
"<runtime: _goal_snapshot(self._agent, action='pause')>"
```

### [box_agent/agent.py:389](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L389)

参数/结构示例：
```json
"<runtime: _goal_snapshot(self._agent, action='resume')>"
```

### [box_agent/agent.py:407](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L407)

参数/结构示例：
```json
"<runtime: _goal_snapshot(self._agent, action='complete')>"
```

### [box_agent/agent.py:418](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L418)

参数/结构示例：
```json
"<runtime: _goal_snapshot(self._agent, action='progress')>"
```

### [box_agent/agent.py:430](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L430)

参数/结构示例：
```json
"<runtime: _goal_snapshot(self._agent, action='block')>"
```

### [box_agent/agent.py:438](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L438)

参数/结构示例：
```json
"<runtime: _goal_snapshot(self._agent, action='clear')>"
```

### [box_agent/kernel/context_engine.py:367](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/context_engine.py#L367)

参数/结构示例：
```json
{
  "code": "TRANSIENT_FOLLOWUP_NOT_ALLOWED"
}
```

### [box_agent/kernel/context_engine.py:377](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/context_engine.py#L377)

参数/结构示例：
```json
{
  "code": "IMAGE_NATIVE_UNSUPPORTED"
}
```

### [box_agent/kernel/context_engine.py:406](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/context_engine.py#L406)

参数/结构示例：
```json
{
  "code": "TRANSIENT_FOLLOWUP_TOO_LARGE",
  "estimated_tokens": "<runtime: pending_token_estimate + estimate>",
  "budget_tokens": "<runtime: budget>"
}
```

### [box_agent/kernel/context_engine.py:392](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/context_engine.py#L392)

参数/结构示例：
```json
{
  "code": "TRANSIENT_FOLLOWUP_INVALID"
}
```

### [box_agent/kernel/loop.py:496](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/kernel/loop.py#L496)

参数/结构示例：
```json
"<runtime: raw_output>"
```

### web_extract — [box_agent/mcp_servers/web_extract.py:203](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/mcp_servers/web_extract.py#L203)

```json
{
  "type": "web_extract",
  "url": "<runtime: normalized_url>",
  "final_url": "<runtime: result.final_url>",
  "summarized": "<runtime: summarized>",
  "content_truncated": false,
  "model": "<runtime: effective_model if summarized else None>",
  "original_chars": "<runtime: original_chars>",
  "returned_chars": "<runtime: len(content)>",
  "from_cache": "<runtime: result.from_cache>"
}
```

### [box_agent/mcp_servers/web_extract.py:215](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/mcp_servers/web_extract.py#L215)

参数/结构示例：
```json
"<runtime: metadata>"
```

### [box_agent/mcp_servers/web_extract.py:151](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/mcp_servers/web_extract.py#L151)

参数/结构示例：
```json
{
  "type": "web_extract",
  "status": "verification_required",
  "url": "<runtime: normalized_url>",
  "final_url": "<runtime: exc.final_url>",
  "next_action": "open_browser"
}
```

### web_extract — [box_agent/mcp_servers/web_extract.py:163](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/mcp_servers/web_extract.py#L163)

```json
{
  "type": "web_extract",
  "status": "verification_required",
  "url": "<runtime: normalized_url>",
  "final_url": "<runtime: exc.final_url>",
  "next_action": "open_browser"
}
```

### [box_agent/runtime.py:83](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/runtime.py#L83)

参数/结构示例：
```json
{
  "type": "tool_error",
  "code": "TOOL_EXECUTION_FAILED",
  "tool": "<runtime: tool.name>"
}
```

### [box_agent/skill_context.py:314](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_context.py#L314)

参数/结构示例：
```json
{
  "skill_reference": "<runtime: dict(metadata)>"
}
```

### [box_agent/skill_context.py:370](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_context.py#L370)

参数/结构示例：
```json
{
  "skill_reference": "<runtime: dict(metadata)>"
}
```

### [box_agent/skill_context.py:288](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_context.py#L288)

参数/结构示例：
```json
{
  "skill_reference": "<runtime: metadata>"
}
```

### [box_agent/skill_context.py:304](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_context.py#L304)

参数/结构示例：
```json
{
  "code": "SKILL_CONTEXT_BUDGET",
  "name": "<runtime: name>",
  "revision": "<runtime: current_revision>"
}
```

### [box_agent/skill_context.py:307](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_context.py#L307)

参数/结构示例：
```json
{
  "code": "SKILL_REQUIRES_PAGING",
  "name": "<runtime: name>",
  "revision": "<runtime: current_revision>"
}
```

### [box_agent/skill_context.py:361](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_context.py#L361)

参数/结构示例：
```json
{
  "code": "SKILL_CONTEXT_BUDGET",
  "name": "<runtime: name>"
}
```

### [box_agent/skill_context.py:268](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_context.py#L268)

参数/结构示例：
```json
{
  "code": "<runtime: exc.code>",
  "<dynamic>": "<runtime: exc.details>"
}
```

### [box_agent/skill_context.py:284](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_context.py#L284)

参数/结构示例：
```json
{
  "code": "SKILL_CONTEXT_BUDGET",
  "name": "<runtime: name>"
}
```

### [box_agent/skill_context.py:332](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_context.py#L332)

参数/结构示例：
```json
{
  "code": "<runtime: exc.code>",
  "<dynamic>": "<runtime: exc.details>"
}
```

### skill_reference — [box_agent/skill_state.py:50](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/skill_state.py#L50)

```python
def reference_metadata(self, *, offset: int, reason: str) -> dict[str, Any]:
    metadata = json.loads(self.metadata_json)
    metadata.update(offset=offset, end_offset=offset, reason=reason)
    return metadata
```


## 9. 模型候选、技能目录与文件变更回执字段

### normalize_auto_routing — [box_agent/llm/model_routing.py:168](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/llm/model_routing.py#L168)

```python
def normalize_auto_routing(value: Any) -> dict[str, list[dict[str, Any]]] | None:
    """Validate the host-owned automatic model pool carried by ACP metadata."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("llm_binding.autoRouting is invalid")
    raw_models = value.get("models")
    if not isinstance(raw_models, list) or not raw_models or len(raw_models) > 64:
        raise ValueError("llm_binding.autoRouting is invalid")

    seen: set[str] = set()
    models: list[dict[str, Any]] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            raise ValueError("llm_binding.autoRouting is invalid")
        model = _clean_model_name(raw.get("model"))
        tags = _clean_tags(raw.get("tags"))
        ability_level = _positive_int(raw.get("abilityLevel", raw.get("ability_level")))
        context_window = raw.get("contextWindow", raw.get("context_window"))
        max_tokens = raw.get("maxTokens", raw.get("max_tokens"))
        if (
            not model
            or model in seen
            or tags is None
            or ability_level is None
            or ability_level > 10
            or (context_window is not None and _positive_int(context_window) is None)
            or (max_tokens is not None and _positive_int(max_tokens) is None)
        ):
            raise ValueError("llm_binding.autoRouting is invalid")
        seen.add(model)
        models.append(
            {
                "model": model,
                "tags": tags,
                "abilityLevel": ability_level,
                **({"contextWindow": context_window} if context_window is not None else {}),
                **({"maxTokens": max_tokens} if max_tokens is not None else {}),
            }
        )
    return {"models": models}
```

### list_skills_metadata — [box_agent/tools/skill_loader.py:1353](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/skill_loader.py#L1353)

```python
def list_skills_metadata(
    self,
    *,
    include_disabled: bool = False,
    include_connector: bool = True,
) -> List[Dict[str, object]]:
    """Return structured metadata for every loaded skill.

    Intended for officev3 / ACP `_meta.skills` payloads.
    """
    return [
        skill.to_metadata_dict()
        for skill in self._skill_pool(include_disabled=include_disabled).values()
        if include_connector or skill.source != "connector"
    ]
```

### file_change_result — [box_agent/tools/file_change_receipts.py:49](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/tools/file_change_receipts.py#L49)

```python
def file_change_result(result: ToolResult, workspace_dir: str | Path, target: Path,
                       before: str | None, after: str, *, verify_current: bool = False) -> ToolResult:
    """Call only after committing the bytes whose digest is `after`."""
    if before is _UNKNOWN_BEFORE:
        return result
    root = Path(workspace_dir).resolve()
    try:
        target = target.resolve()
        relative = target.relative_to(root).as_posix()
        if verify_current and file_hash(target) != after:
            return result
    except (OSError, RuntimeError, ValueError):
        return result  # Full-access writes outside cwd retain their old behavior.
    if not result.success or before == after:
        return result
    raw = dict(result.raw_output or {})
    raw.update(file_changes_version=2, file_changes=[{
        'path': relative, 'before_sha256': before, 'after_sha256': after,
    }])
    return result.model_copy(update={'raw_output': raw})
```

## 10. 展示状态与工作区请求校验

补充两处关键消费/校验逻辑。源码固定到本附录的main提交；下面直接提取完整类或函数，缺少目标路径或定义时即失败，不静默跳过。

### MessageDisplay — [box_agent/acp/message_display.py:16](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/message_display.py#L16)

```python
class MessageDisplay:
    """Track one prompt, including multiple internal runs and text continuations.

    Chunks are deliberately unclassified while streaming: providers often decide
    to call a tool only after emitting text. Only the outer prompt response may
    publish a final receipt, after autopilot and producer cleanup have settled.
    """

    def __init__(self) -> None:
        self.begin_run()

    def begin_run(self) -> None:
        # Autopilot starts another run within the same prompt. Earlier answers
        # remain trace entries; UUIDs keep their identities distinct across runs.
        self.message_id: str | None = None
        # message_id 用于拼接同一回答；output_id 区分每次模型输出，补写也必须重新分配。
        # 这样客户端既能接回被截断的正文，也不会把补写快照误判成旧快照。
        self.output_id: str | None = None
        self.text_parts: list[str] = []
        self.last_text = ""
        self.answer_ids: list[str] = []
        self.final_ids: list[str] = []

    def observe(self, event: object) -> dict | None:
        if isinstance(event, ContentEvent) and not event._header and event.content:
            if self.message_id is None:
                self.message_id = uuid4().hex
            if self.output_id is None:
                self.output_id = uuid4().hex
            self.text_parts.append(event.content)
            return {"version": 1, "messageId": self.message_id, "outputId": self.output_id}
        if isinstance(event, LLMOutputEvent):
            identifier = self.message_id or uuid4().hex
            output_id = self.output_id or uuid4().hex
            # A no-tool output is only a candidate, never a final-answer signal.
            if event.tool_calls or not event.content:
                self.answer_ids = []
            else:
                self.answer_ids = [identifier]
            self.last_text = event.content
            self.message_id = None
            self.output_id = None
            self.text_parts = []
            return {"version": 1, "messageId": identifier, "outputId": output_id}
        if isinstance(event, InjectedMessageEvent):
            # Text continuation completes a truncated answer. Task continuation,
            # recovery and user steering produce a new answer instead.
            # Reuse the logical message ID for truncation: a continuation may
            # start in the middle of a word, table row or Markdown code fence.
            # Recovery keeps the failed partial text under a separate old ID.
            if event.kind == InjectionKind.TEXT_CONTINUATION.value:
                self.message_id = self.answer_ids[-1] if self.answer_ids else None
            elif event.kind not in {
                InjectionKind.BUDGET.value, InjectionKind.NO_PROGRESS.value,
            }:
                self.message_id = None
            # Advisory wrap-up nudges may arrive after the continuation request;
            # they do not revoke its identity or guarantee a rewritten answer.
            self.output_id = None
            self.text_parts = []
        if isinstance(event, DoneEvent) and event.stop_reason == StopReason.END_TURN:
            text = "".join(self.text_parts)
            if self.message_id and text == event.final_content and text.strip():
                self.final_ids = [self.message_id]
            elif self.last_text == event.final_content and self.last_text.strip():
                self.final_ids = self.answer_ids.copy()
        return None

    def receipt(self, *, completed: bool, duration_ms: int) -> dict:
        # Failure, cancellation, waiting and budget exhaustion must not hide the
        # partial reply. Clients only fold tagged text when final IDs are present.
        return {
            "version": 1,
            "finalMessageIds": self.final_ids if completed else [],
            "durationMs": max(0, duration_ms),
        }
```

### _normalize_task_type — [box_agent/workspace_registry.py:53](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/workspace_registry.py#L53)

参数：分类值；仅允许 `general` 或 `code`。缺失值和其他值抛出WorkspaceRegistryError，由ACP入口转为result.error。

```python
WorkspaceTaskType = Literal["general", "code"]
_SUPPORTED_TASK_TYPES = {"general", "code"}
```

```python
def _normalize_task_type(value: Any) -> WorkspaceTaskType:
    if value not in _SUPPORTED_TASK_TYPES:
        raise WorkspaceRegistryError("task_type must be 'general' or 'code'")
    return value
```

## 11. PR #250 输入、补充及恢复的精确实现

下列代码从固定提交直接提取。内部 input_facts、input/adopted 与 request/context 不是新增 ACP 请求参数。实时成功仍为文本 marker，取消/丢弃回执为 update._meta.inputStatus；图片补充取消后的 ImageContext 残留是已知未修复限制。第1节索引及既有函数片段已按新提交刷新；SDK版本仍为0.12.1。

### BoxACPAgent._new_session_with_binding

[box_agent/acp/__init__.py:1760](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L1760)

```python
async def _new_session_with_binding(self, params: NewSessionRequest, *, load_existing=False) -> NewSessionResponse:
    meta = getattr(params, "field_meta", None) or {}
    raw_upstream = meta.get("session_id") if isinstance(meta, dict) else None
    upstream_session_id = raw_upstream.strip() if isinstance(raw_upstream, str) else ""
    if not upstream_session_id:
        upstream_session_id = str(uuid4())

    reserved = self._session_bindings_in_progress.get(upstream_session_id)
    if reserved is not None:
        raise _session_busy_error(next(iter(reserved), None), rebinding=True)
    import base64
    stable_handle = "acp-v1-" + base64.urlsafe_b64encode(upstream_session_id.encode()).decode().rstrip("=")
    existing = tuple(
        handle for handle, state in self._sessions.items()
        if state.upstream_session_id == upstream_session_id or handle == stable_handle
    )
    for handle in existing:
        state = self._sessions[handle]
        if (state._prompt_in_progress or state.turn_active or state.run_handle.is_active
                or state._history_edit_in_progress):
            raise _session_busy_error(handle)
    # No await between checking and reserving. Keep retired handles reserved
    # through close/open awaits too, so prompt cannot auto-create a session
    # while the original handle is temporarily absent from _sessions.
    self._session_bindings_in_progress[upstream_session_id] = existing
    try:
        return await self._new_session(params, upstream_session_id, load_existing=load_existing)
    finally:
        del self._session_bindings_in_progress[upstream_session_id]
```

### BoxACPAgent.load_session

[box_agent/acp/__init__.py:2415](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2415)

```python
async def load_session(
    self, cwd: str, session_id: str, mcp_servers=None, additional_directories=None, **meta: Any,
) -> LoadSessionResponse:
    """Open and replay existing facts; never create a missing session or run tools."""
    self._require_accepting_requests()
    import base64
    try:
        if not session_id.startswith("acp-v1-") or len(session_id) > 4096:
            raise ValueError("UNKNOWN_SESSION_ID")
        encoded = session_id[len("acp-v1-"):]
        upstream = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode()
        canonical = "acp-v1-" + base64.urlsafe_b64encode(upstream.encode()).decode().rstrip("=")
        if not upstream or canonical != session_id:
            raise ValueError("UNKNOWN_SESSION_ID")
    except (ValueError, UnicodeError) as exc:
        raise RequestError(-32602, "UNKNOWN_SESSION_ID") from exc
    supplied = meta.get("session_id")
    if supplied is not None and supplied != upstream:
        raise RequestError(-32602, "SESSION_ID_MISMATCH")
    params = NewSessionRequest(cwd=cwd, mcp_servers=mcp_servers or [],
                               additional_directories=additional_directories,
                               field_meta={**meta, "session_id": upstream})
    from box_agent.session_log import SessionLogCorrupted, SessionLogInUseError, SessionLogWorkspaceMismatch
    try:
        response = await self._new_session_with_binding(params, load_existing=True)
    except (FileNotFoundError, SessionLogCorrupted, SessionLogInUseError, SessionLogWorkspaceMismatch) as exc:
        code = ("UNKNOWN_SESSION_ID" if isinstance(exc, FileNotFoundError) else
                "SESSION_WORKSPACE_MISMATCH" if isinstance(exc, SessionLogWorkspaceMismatch) else
                "SESSION_BUSY" if isinstance(exc, SessionLogInUseError) else "SESSION_LOG_CORRUPTED")
        raise RequestError(-32012, code, {"code": code}) from exc
    state = self._sessions[session_id]
    state._prompt_in_progress = True
    try:
        replay_status = {}
        bound_turn = None
        for event in state.agent.session_log._active_events():
            if event["type"] == "turn/binding":
                bound_turn = event["data"].get("turn_id")
            if event["type"] == "turn/end" and bound_turn:
                replay_status[bound_turn] = {"completed": "complete", "aborted": "cancelled",
                                             "error": "error"}.get(event["data"].get("reason", {}).get("kind"), "interrupted")
        replay_turn_id = None
        for seq, message, saved_data in state.agent.session_log.history_messages():
            if message.input_facts and message.input_facts.get("kind") != "user_supplement":
                replay_turn_id = message.input_facts["id"]
            updates = []
            if message.role == "user" and message.source == "user":
                record = message.input_facts
                blocks = record["blocks"] if record else [{"type": "text", "text": str(message.content)}]
                for block in blocks:
                    if block["type"] == "image":
                        # Reuse retained immutable bytes; no attachment tool or upload.
                        refs = (record or {}).get("images", [])
                        index = block.get("imageIndex", -1)
                        if 0 <= index < len(refs):
                            try:
                                image = state.agent.image_context.store.load_block(refs[index])
                                block = {"type": "image", "data": image["data"], "mimeType": image["media_type"]}
                            except (OSError, ValueError):
                                block = {"type": "text", "text": "[Image unavailable in saved history]"}
                        else:
                            block = {"type": "text", "text": "[Image unavailable in saved history]"}
                    updates.append(UserMessageChunk(session_update="user_message_chunk", content=block))
                # Attachment preparation ran before user/message was committed.
                # Replay its saved result alongside that atomic input, never rerun it.
                image_tool = (record or {}).get("image_tool")
                if image_tool:
                    updates.append(start_tool_call(image_tool["toolCallId"], image_tool["name"],
                                                   raw_input=image_tool["arguments"]))
                    updates.append(update_tool_call(image_tool["toolCallId"],
                        status="completed" if image_tool["success"] else "failed",
                        content=[tool_content(text_block(image_tool["content"]))]))
            elif message.role == "assistant":
                if message.content:
                    updates.append(update_agent_message(text_block(str(message.content))))
                for call in message.tool_calls or []:
                    updates.append(start_tool_call(call.id, call.function.name, raw_input=call.function.arguments))
            elif message.role == "tool":
                failed = bool(saved_data.get("error")) or saved_data.get("result", {}).get("success") is False
                updates.append(update_tool_call(message.tool_call_id, status="failed" if failed else "completed",
                                                content=[tool_content(text_block(str(message.content)))]))
            for index, update in enumerate(updates):
                update.field_meta = {
                    "replay": True, "eventId": f"{seq}:{index}", "turnId": replay_turn_id,
                    "turnStatus": replay_status.get(replay_turn_id, "interrupted"),
                }
                if message.input_facts and message.input_facts.get("kind") == "user_supplement":
                    update.field_meta.update(inputKind="user_supplement", inputId=message.input_facts["id"])
                await self._send(session_id, update)
    except BaseException:
        try:
            await state.aclose()
        finally:
            state.agent.session_log.close()
            self._sessions.pop(session_id, None)
        raise
    finally:
        state._prompt_in_progress = False
    return LoadSessionResponse(field_meta={**(response.field_meta or {}), "replayed": True})
```

### BoxACPAgent.prompt

[box_agent/acp/__init__.py:2579](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2579)

```python
async def prompt(
    self, session_id: str, prompt: list[Any], **meta: Any,
) -> PromptResponse:
    self._require_accepting_requests()
    params = PromptRequest(session_id=session_id, prompt=prompt, field_meta=meta or None)
    context = normalize_input_context(meta)
    if any(session_id in handles for handles in self._session_bindings_in_progress.values()):
        raise _session_busy_error(session_id, rebinding=True)
    state = self._sessions.get(session_id)
    if not state:
        # Auto-create session if not found (compatibility with clients that skip newSession)
        log.warn("session/prompt", session_id=session_id, message="Session not found, auto-creating")
        new_session = await self.new_session(
            cwd=self._config.agent.workspace_dir or ".", mcp_servers=[],
        )
        session_id = new_session.session_id  # use the NEW session id from here on
        state = self._sessions.get(session_id)
        if not state:
            log.error("session/prompt", session_id=session_id, message="Failed to auto-create session")
            return PromptResponse(stop_reason="refusal")

    meta = getattr(params, "field_meta", None) or {}
    continuation = meta.get("session_continuation") or meta.get("sessionContinuation")
    if isinstance(continuation, dict) and continuation.get("schema_version") == "officev3-session-continuation/v2":
        raise RequestError(-32011, "HISTORY_EDIT_UNSUPPORTED_CONTINUATION", {
            "code": "HISTORY_EDIT_UNSUPPORTED_CONTINUATION",
        })
    edit = meta.get("history_edit")
    if state._history_edit_in_progress:
        raise _session_busy_error(session_id)
    # Validate references before cancelling an edited turn, revising history,
    # binding its ID, or running any tool. This synchronous preflight does not
    # yield between session lookup and the existing request guards.
    input_value = UserInput(tuple(params.prompt), context,
                            _meta_string(meta, "turn_id", "turnId") or None)
    if input_value.input_id and state.agent.session_log is not None:
        previous = state.agent.session_log.turn_binding(input_value.input_id)
        if (previous is not None and "input_fingerprint" in previous
                and (previous["input_fingerprint"] != input_value.fingerprint()
                     or state.agent.session_log.has_committed_input(input_value.input_id))):
            error = ("TURN_ALREADY_ACCEPTED" if previous["input_fingerprint"] == input_value.fingerprint()
                     else "TURN_IDEMPOTENCY_CONFLICT")
            if edit is not None:
                if error == "TURN_IDEMPOTENCY_CONFLICT":
                    error = "HISTORY_REVISION_IDEMPOTENCY_CONFLICT"
                raise RequestError(-32011, error, {"code": error,
                    "history_revision_id": state.agent.session_log.current_revision()})
            raise ValueError(error)
    prepared_input, _ = prepare_user_input(input_value, state.agent.workspace_dir, state.permission_engine)
    context = prepared_input["context"]
    if edit is not None:
        try:
            await self._revise_session_history(params, state, edit, context)
        except Exception as exc:
            history = state.agent.session_log
            raise RequestError(-32011, str(exc), {
                "code": str(exc),
                "history_revision_id": history.current_revision() if history else "initial",
            }) from exc
    elif state._history_restore_failed:
        raise ValueError("HISTORY_RESTORE_REQUIRED")

    # Reserve the whole ACP request before preparation mutates shared state.
    # AgentService only guards the inner run; preparation and finalization
    # also own history, grants, cancellation and the injection queue.
    # There is no await between checking and claiming this event-loop flag.
    if state._prompt_in_progress or state.run_handle.is_active:
        raise _session_busy_error(session_id)
    state._prompt_in_progress = True
    finished = asyncio.get_running_loop().create_future()
    state._prompt_finished = finished
    try:
        log_store = state.agent.session_log
        turn_id = _meta_string(meta, "turn_id", "turnId")
        if log_store is not None and turn_id:
            binding = log_store.turn_binding(turn_id)
            if binding is not None:
                if binding.get("input_fingerprint", input_value.fingerprint()) != input_value.fingerprint():
                    raise ValueError("TURN_IDEMPOTENCY_CONFLICT")
                if "input_fingerprint" not in binding or log_store.has_committed_input(turn_id):
                    raise ValueError("TURN_ALREADY_ACCEPTED")
            else:
                log_store.append("turn/binding", {
                    "turn_id": turn_id,
                    "prompt_fingerprint": prompt_fingerprint(params.prompt, context),
                    "input_fingerprint": input_value.fingerprint(),
                })
                log_store.flush()
        if log_store is not None and context:
            log_store.append("input/context", {"turn_id": turn_id, **context}, ignorable=True)
        return await self._prompt_for_session(params, session_id, state, context, prepared_input)
    except Exception as exc:
        if edit is not None:
            history = state.agent.session_log
            raise RequestError(-32011, str(exc), {
                "code": str(exc),
                "history_revision_id": history.current_revision() if history else "initial",
            }) from exc
        raise
    finally:
        state._prompt_in_progress = False
        if not finished.done():
            finished.set_result(None)
```

### BoxACPAgent._run_image_attachment_tool

[box_agent/acp/__init__.py:2535](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L2535)

```python
async def _run_image_attachment_tool(
    self, *, state: SessionState, session_id: str, turn_id: str,
    user_text: str, prompt_meta: dict[str, Any],
) -> _ImageAttachmentContext | None:
    """Adapt the shared preparation's genuine tool results to ACP updates."""
    from box_agent.input_images import prepare_image_attachments
    paths = _meta_string_list(prompt_meta, "image_attachment_paths", "imageAttachmentPaths", limit=6)
    trace = getattr(state, "trace_writer", None)

    async def started(call_id, name, arguments):
        log.info("tool/start", session_id=session_id, tool_call_id=call_id,
                 tool_name=name, arguments=arguments, user_visible=True, source="structured_attachment")
        if trace is not None:
            trace.write("tool.request", turn_id=turn_id, step=0, tool_call_id=call_id,
                        data={"tool_name": name, "arguments": arguments, "allowed_to_execute": True,
                              "user_visible": True, "source": "structured_attachment"})
        await self._send(session_id, start_tool_call(call_id, f"🔧 {name}(本轮图片附件)",
                                                   kind="execute", raw_input=arguments))

    async def finished(call_id, name, result, decision):
        ok = bool(result.success)
        text = result.content if ok else result.error or "Image understanding failed"
        raw = _tool_result_raw_output(result.raw_output, text, decision,
                                      session_id=session_id, turn_id=turn_id)
        log.info("tool/end" if ok else "tool/fail", session_id=session_id,
                 tool_call_id=call_id, tool_name=name, result=text if ok else None,
                 error=None if ok else text, user_visible=True, source="structured_attachment")
        if trace is not None:
            trace.write("tool.response", turn_id=turn_id, step=0, tool_call_id=call_id,
                        data={"tool_name": name, "success": ok, "content": result.content if ok else "",
                              "error": None if ok else text, "raw_output": raw,
                              "source": "structured_attachment"})
        await self._send(session_id, update_tool_call(call_id, status="completed" if ok else "failed",
            content=[tool_content(text_block(("[OK] " if ok else "[ERROR] ") + text))],
            raw_output=raw or {"type": "structured_image_attachment_result", "success": ok,
                               "imageCount": len(paths), "content": text}))

    negotiator = (_PermissionNegotiator(conn=self._conn, session_id=session_id, grant_store=state.grant_store)
                  if getattr(state, "grant_store", None) is not None else None)
    return await prepare_image_attachments(state.agent, paths,
        _latest_user_request_for_plan_detection(user_text), permission_engine=getattr(state, "permission_engine", None),
        allow_full_access=getattr(state.agent, "_input_allow_full_access", False),
        permission_negotiator=negotiator, on_start=started, on_result=finished)
```

### UserInput

[box_agent/api/input.py:13](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/api/input.py#L13)

```python
class UserInput:
    """A user-authored input, before resource preparation or model projection."""

    blocks: tuple[dict[str, Any], ...]
    context: dict[str, Any] = field(default_factory=dict)
    input_id: str | None = None

    def __post_init__(self) -> None:
        from ..input_context import normalize_input_context

        blocks = []
        for item in self.blocks:
            block = (item.model_dump(by_alias=True, exclude_none=True)
                     if hasattr(item, "model_dump") else deepcopy(item))
            if not isinstance(block, dict) or block.get("type") not in {"text", "resource_link", "image"}:
                raise ValueError("INVALID_INPUT_BLOCK")
            if block["type"] == "text" and not isinstance(block.get("text"), str):
                raise ValueError("INVALID_INPUT_TEXT")
            if block["type"] == "image" and (
                not isinstance(block.get("data"), str)
                or not isinstance(block.get("mimeType"), str)
                or not block["mimeType"].startswith("image/")
            ):
                raise ValueError("INVALID_INPUT_IMAGE")
            blocks.append(block)
        if len(blocks) > 1040:
            raise ValueError("INVALID_INPUT_BLOCK_COUNT")
        if self.input_id is not None and (not isinstance(self.input_id, str) or not self.input_id.strip()):
            raise ValueError("INVALID_INPUT_ID")
        object.__setattr__(self, "blocks", tuple(blocks))
        object.__setattr__(self, "context", normalize_input_context(self.context))

    @classmethod
    def text(cls, text: str, *, input_id: str | None = None) -> UserInput:
        return cls(({"type": "text", "text": text},), input_id=input_id)

    @property
    def user_text(self) -> str:
        return "\n".join(block["text"] for block in self.blocks if block["type"] == "text")

    def fingerprint(self) -> str:
        # The identity is of the submitted request, not today's mutable filesystem.
        payload = json.dumps([self.blocks, self.context], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return deepcopy({"blocks": list(self.blocks), "context": self.context, "input_id": self.input_id})
```

### prepare_user_input

[box_agent/input_preparation.py:18](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_preparation.py#L18)

```python
def prepare_user_input(
    value: str | UserInput, workspace_dir: str | Path, permission_engine=None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = UserInput.text(value) if isinstance(value, str) else value
    if not isinstance(value, UserInput):
        raise TypeError("input must be a string or UserInput")
    files = normalize_file_inputs(list(value.blocks), workspace_dir, permission_engine)
    if sum(block["type"] == "image" for block in value.blocks) + sum(
        item.get("mimeType", "").startswith("image/") for item in files
    ) > 6:
        raise ValueError("INVALID_INPUT_IMAGE_COUNT")
    images = []
    blocks = []
    for block in value.blocks:
        copied = deepcopy(block)
        if block["type"] == "image":
            try:
                data = base64.b64decode(block["data"], validate=True)
            except ValueError as exc:
                raise ValueError("INVALID_INPUT_IMAGE") from exc
            if not data or len(data) > 20 * 1024 * 1024:
                raise ValueError("INVALID_INPUT_IMAGE_SIZE")
            from .image_store import SessionImageStore
            SessionImageStore._dimensions(data, {"media_type": block["mimeType"]})
            copied = {"type": "image", "mimeType": block["mimeType"],
                      "sha256": sha256(data).hexdigest(), "imageIndex": len(images)}
            images.append({"type": "input_image", "media_type": block["mimeType"], "data": block["data"]})
        blocks.append(copied)
    context = deepcopy(value.context)
    if files:
        context["files"] = files
    image_files = [item for item in files if item.get("mimeType", "").startswith("image/")]
    positions = {item["path"]: len(images) + index for index, item in enumerate(image_files)}
    resource_images = {block["uri"]: positions[str(Path(url2pathname(urlsplit(block["uri"]).path)).resolve())]
                       for block in blocks if block["type"] == "resource_link"
                       and str(Path(url2pathname(urlsplit(block["uri"]).path)).resolve()) in positions}
    return {"schemaVersion": 1, "id": value.input_id or uuid4().hex,
            "fingerprint": value.fingerprint(), "blocks": blocks, "context": context,
            "workspace": str(Path(workspace_dir).resolve()), "policy": {},
            "resourceImages": resource_images}, images
```

### resource_image_paths

[box_agent/input_preparation.py:60](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_preparation.py#L60)

```python
def resource_image_paths(record: dict[str, Any]) -> list[str]:
    return [item["path"] for item in record["context"].get("files", [])
            if item.get("mimeType", "").startswith("image/")]
```

### normalize_file_inputs

[box_agent/input_resources.py:14](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_resources.py#L14)

```python
def normalize_file_inputs(
    prompt: list[Any], workspace_dir: str | Path, permission_engine=None,
) -> list[dict[str, Any]]:
    """Validate all links before accepting input; tools recheck access when reading.

    Only files already inside the bound session workspace are references. A URI
    is neither an upload instruction nor a grant to another session's files.
    """
    files = []
    seen: set[Path] = set()
    links = [block.model_dump(by_alias=True) if hasattr(block, "model_dump") else block
             for block in prompt]
    links = [block for block in links if isinstance(block, dict) and block.get("type") == "resource_link"]
    # Existing host limits: 20 uploads plus 500 explicit source selections.
    if len(links) > 520:
        raise ValueError("INVALID_INPUT_RESOURCE:too_many_files")
    for block in links:
        uri, name = block.get("uri"), block.get("name")
        if (not isinstance(uri, str) or not uri or len(uri) > 16_384
                or not isinstance(name, str) or not name.strip() or len(name) > 1024
                or any(ord(char) < 32 for char in uri)
                or re.search(r"%(?![0-9a-fA-F]{2})", uri)):
            raise ValueError("INVALID_INPUT_RESOURCE:invalid_uri_or_name")
        try:
            parts = urlsplit(uri)
            if (parts.scheme != "file" or parts.netloc not in {"", "localhost"}
                    or parts.query or parts.fragment):
                raise ValueError("unsupported_uri")
            decoded = unquote(parts.path, encoding="utf-8", errors="strict")
            if "\x00" in decoded or "\\" in decoded:
                raise ValueError("invalid_path")
            if os.name == "nt" and re.match(r"^/[a-zA-Z]:/", decoded):
                decoded = decoded[1:]
            path = Path(decoded)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("invalid_path")
            if validate_path_in_workspace(path, Path(workspace_dir)):
                raise ValueError("outside_session_workspace")
            path = path.resolve(strict=True)
            info = path.stat()
            if not path.is_file() or info.st_nlink != 1:
                raise ValueError("not_a_regular_owned_file")
            if permission_engine is not None:
                decision = permission_engine.check(
                    capability="filesystem.read", resource={"path": str(path)}, tool_name="read_file",
                )
                if not decision.allowed:
                    raise ValueError("permission_denied")
            # Stream the version without loading file bodies into the prompt.
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino, before.st_nlink) != (info.st_dev, info.st_ino, 1):
                    raise ValueError("file_changed_during_preparation")
                digest = sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                current = path.stat()
                if (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError("file_changed_during_preparation")
                if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
                ):
                    raise ValueError("file_changed_during_preparation")
        except (OSError, ValueError, RuntimeError) as exc:
            raise ValueError(f"INVALID_INPUT_RESOURCE:{exc}") from exc
        if path in seen:
            continue
        seen.add(path)
        item = {"name": name, "path": str(path), "uri": path.as_uri(), "size": info.st_size}
        item["version"] = {"algorithm": "sha256", "digest": digest.hexdigest()}
        title = block.get("title")
        if title is not None:
            if not isinstance(title, str) or len(title) > 1024:
                raise ValueError("INVALID_INPUT_RESOURCE:invalid_title")
            item["title"] = title
        mime_type = block.get("mimeType")
        if mime_type is not None:
            if not isinstance(mime_type, str) or len(mime_type) > 256:
                raise ValueError("INVALID_INPUT_RESOURCE:invalid_mime_type")
            item["mimeType"] = mime_type
        files.append(item)
    return files
```

### verify_input_versions

[box_agent/input_resources.py:99](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_resources.py#L99)

```python
def verify_input_versions(files: list[dict[str, Any]], workspace_dir: str | Path, permission_engine=None) -> None:
    """Check a not-yet-adopted input without changing its accepted identity."""
    links = [{"type": "resource_link", **item} for item in files]
    try:
        current = normalize_file_inputs(links, workspace_dir, permission_engine)
    except ValueError as exc:
        raise ValueError("INPUT_RESOURCE_VERSION_UNAVAILABLE") from exc
    if len(current) != len(files) or any(a.get("version") != b.get("version") for a, b in zip(current, files)):
        raise ValueError("INPUT_RESOURCE_VERSION_CHANGED")
```

### InjectionStatus

[box_agent/injections.py:119](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/injections.py#L119)

```python
class InjectionStatus(Enum):
    PENDING = "pending"
    TAKEN = "taken"
    INJECTED = "injected"
    CANCELLED = "cancelled"
    DISCARDED = "discarded"
```

### submit_user_input

[box_agent/injections.py:127](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/injections.py#L127)

```python
def submit_user_input(value, *, agent, queue, active=True):
    """Prepare once, with receipt lookup before touching mutable resources."""
    from .api.input import UserInput
    from .input_preparation import prepare_user_input

    value = UserInput.text(value) if isinstance(value, str) else value
    if not isinstance(value, UserInput):
        raise TypeError("input must be a string or UserInput")
    identifier = value.input_id or str(uuid4())
    fingerprint = value.fingerprint()
    receipt = queue.receipt(identifier, fingerprint)
    if receipt is None:
        store = agent.session_log
        for _, message, _ in store.history_messages() if store is not None else ():
            record = message.input_facts
            if record and record.get("kind") == "user_supplement" and record["id"] == identifier:
                if record["fingerprint"] != fingerprint:
                    raise ValueError("INJECTION_IDEMPOTENCY_CONFLICT")
                receipt = InjectionStatus.INJECTED
                break
    if receipt is not None:
        return {"ok": True, "injectionId": identifier, "deduplicated": True, "status": receipt.value}
    if not active:
        raise ValueError("no_active_turn")
    if not value.user_text and not any(block["type"] != "text" for block in value.blocks):
        raise ValueError("empty_text")
    record, images = prepare_user_input(value, agent.workspace_dir,
                                       getattr(agent, "_input_permission_engine", None))
    record.update(id=identifier, kind="user_supplement")
    queue.submit(MessageInjection(InjectionKind.USER_SUPPLEMENT, value.user_text,
                                  identifier, input_facts=record, input_images=tuple(images)))
    return {"ok": True, "injectionId": identifier, "status": "pending"}
```

### InjectionManager

[box_agent/injections.py:161](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/injections.py#L161)

```python
class InjectionManager(asyncio.Queue[Any]):
    """Run-scoped injection lifecycle with the legacy asyncio queue interface.

    Access on the owning event loop. Session Log owns durable history; these
    receipts are transient and scoped to ``run_id``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.run_id = str(uuid4())
        self._statuses: dict[str, InjectionStatus] = {}
        self._inputs: dict[str, str] = {}
        self._run_finished = False
        self.prepare_input = None

    def status(self, injection_id: str) -> InjectionStatus | None:
        return self._statuses.get(injection_id)

    def terminal_receipts(self):
        return [(identifier, status.value) for identifier, status in self._statuses.items()
                if status in {InjectionStatus.CANCELLED, InjectionStatus.DISCARDED}]

    def receipt(self, injection_id: str, fingerprint: str) -> InjectionStatus | None:
        status = self.status(injection_id)
        if status is not None and self._inputs.get(injection_id) != fingerprint:
            raise ValueError("INJECTION_IDEMPOTENCY_CONFLICT")
        return status

    def submit(self, item: object) -> bool:
        """Return false for an empty message or a duplicate within this run."""
        injection = parse_queued_injection(item)
        if injection is None:
            return False
        if injection.kind not in _PROMPT_FORMATTERS:
            raise ValueError("unknown injection type")
        if self._run_finished:
            self.begin_run()
        identifier = injection.injection_id
        fingerprint = sha256(json.dumps([
            injection.kind.value, injection.content, injection.user_visible,
            injection.source, injection.input_context,
        ], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if injection.input_facts is not None:
            fingerprint = injection.input_facts["fingerprint"]
        elif (injection.kind is InjectionKind.USER_SUPPLEMENT and not injection.input_context
              and injection.user_visible is not False):
            from .api.input import UserInput
            fingerprint = UserInput.text(injection.content).fingerprint()
        if identifier is not None and self.status(identifier) is not None:
            if fingerprint != self._inputs[identifier]:
                raise ValueError("INJECTION_IDEMPOTENCY_CONFLICT")
            return False
        super().put_nowait(deepcopy(item))
        if identifier is not None:
            self._statuses[identifier] = InjectionStatus.PENDING
            self._inputs[identifier] = fingerprint
        return True

    def put_nowait(self, item: Any) -> None:
        self.submit(item)

    def get_nowait(self) -> Any:
        item = super().get_nowait()
        injection = parse_queued_injection(item)
        if injection is not None and injection.injection_id is not None:
            self._statuses[injection.injection_id] = InjectionStatus.TAKEN
        return item

    def cancel(self, injection_id: str) -> bool:
        """Cancel pending input without forgetting already consumed IDs."""
        if self.status(injection_id) is InjectionStatus.TAKEN:
            self._statuses[injection_id] = InjectionStatus.CANCELLED
            return True
        if self.status(injection_id) is not InjectionStatus.PENDING:
            return False
        kept = []
        removed = False
        while not self.empty():
            item = super().get_nowait()
            injection = parse_queued_injection(item)
            if injection is not None and injection.injection_id == injection_id:
                removed = True
                self.task_done()
            else:
                kept.append(item)
        for item in kept:
            super().put_nowait(item)
            self.task_done()
        if removed:
            self._statuses[injection_id] = InjectionStatus.CANCELLED
        return removed

    def discard_pending(self) -> list[Any]:
        discarded = []
        while not self.empty():
            item = self.get_nowait()
            self.task_done()
            injection = parse_queued_injection(item)
            if injection is not None and injection.injection_id is not None:
                self._statuses[injection.injection_id] = InjectionStatus.DISCARDED
            discarded.append(item)
        return discarded

    def begin_run(self, *, discard_pending: bool = False) -> list[Any]:
        """Start a new scope, optionally dropping stale input at a host boundary."""
        discarded = self.discard_pending() if discard_pending else []
        self._statuses = {
            identifier: status for identifier, status in self._statuses.items()
            if status is InjectionStatus.PENDING
        }
        self._inputs = {identifier: self._inputs[identifier] for identifier in self._statuses}
        self.run_id = str(uuid4())
        self._run_finished = False
        return discarded

    def end_run(self) -> None:
        """Retain final receipts until the next run or prequeued submission."""
        self.discard_pending()
        self._run_finished = True

    def apply_next(self, messages: list[Message], *, commit=None) -> tuple[MessageInjection, InjectedMessageEvent]:
        injection = parse_queued_injection(self.get_nowait())
        assert injection is not None
        original_length = len(messages)
        try:
            event = injection.apply(messages)
            if commit is not None:
                commit()
        except BaseException:
            del messages[original_length:]
            if injection.injection_id is not None:
                self._statuses[injection.injection_id] = InjectionStatus.DISCARDED
            raise
        finally:
            self.task_done()
        if injection.injection_id is not None:
            self._statuses[injection.injection_id] = InjectionStatus.INJECTED
        return injection, event

    async def aapply_next(self, messages, *, commit=None, cancelled=lambda: False):
        injection = parse_queued_injection(self.get_nowait())
        assert injection is not None
        temporary = []
        length = len(messages)
        try:
            event = injection.apply(temporary)
            if self.prepare_input is not None:
                await self.prepare_input(temporary[0], injection)
            if cancelled() or self.status(injection.injection_id) is InjectionStatus.CANCELLED:
                status = InjectionStatus.CANCELLED
            else:
                messages.extend(temporary)
                if commit is not None:
                    commit()
                status = InjectionStatus.INJECTED
        except BaseException:
            del messages[length:]
            if injection.injection_id is not None:
                self._statuses[injection.injection_id] = InjectionStatus.DISCARDED
            raise
        finally:
            self.task_done()
        if injection.injection_id is not None:
            self._statuses[injection.injection_id] = status
        if status is not InjectionStatus.INJECTED:
            from dataclasses import replace
            event = replace(event, status=status.value)
        return injection, event
```

### PreparedContext

[box_agent/context_input.py:30](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/context_input.py#L30)

```python
class PreparedContext:
    """One request projection; durable history and tool admission stay external."""

    messages: list[Message]
    context_messages: list[Message]
    references: tuple[dict[str, Any], ...] = ()
    input_tokens: int = 0
    request_only_input_tokens: int = 0
    blocked_reason: str | None = None
    on_committed: Callable[[], None] | None = None
    budget_blocked: bool = False
    on_response: Callable[[], None] | None = None
    reader_required: bool = False
    image_context: dict[str, Any] | None = None
    image_notices: tuple[str, ...] = ()
    required_reference_tokens: int = 0
    input_context: dict[str, Any] | None = None
```

### Agent.aadd_user_message

[box_agent/agent.py:890](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L890)

```python
async def aadd_user_message(self, content, *, image_content: list[dict[str, Any]] | None = None,
                            image_source: str = "user_upload", image_context_eligible: bool = True,
                            host_connector_status: str | None = None,
                            input_facts: dict[str, Any] | None = None) -> None:
    from .api.input import UserInput
    from .input_preparation import prepare_user_input
    value = UserInput.text(content) if isinstance(content, str) else content
    if input_facts is None:
        input_facts, images = prepare_user_input(value, self.workspace_dir,
                                                getattr(self, "_input_permission_engine", None))
        image_content = [*images, *(image_content or [])]
    image_content, eligible = await self._prepare_input_attachments(input_facts, image_content or [])
    image_context_eligible = image_context_eligible and eligible
    validation = await self.skill_runtime.avalidate_references(self.skill_runtime.selected_names)
    with self.skill_runtime.reference_scope(validation):
        self.add_user_message(content, image_content=image_content, image_source=image_source,
                              image_context_eligible=image_context_eligible,
                              host_connector_status=host_connector_status, input_facts=input_facts)
```

### Agent.prepare_injected_input

[box_agent/agent.py:930](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L930)

```python
async def prepare_injected_input(self, message, injection):
    if message.input_facts is None:
        return
    images, eligible = await self._prepare_input_attachments(message.input_facts, list(injection.input_images))
    if images:
        refs = self.image_context.register(message, images, source="user_upload", eligible=eligible)
        message.input_facts["images"] = refs
```

### Agent.add_user_message

[box_agent/agent.py:938](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/agent.py#L938)

```python
def add_user_message(self, content, *, image_content: list[dict[str, Any]] | None = None,
                     image_source: str = "user_upload", image_context_eligible: bool = True,
                     host_connector_status: str | None = None,
                     input_facts: dict[str, Any] | None = None) -> None:
    """Add a user message and materialize selected Skills beside it."""
    self._restore_session_surface_after_replay_failure()
    from .api.input import UserInput
    from .input_preparation import prepare_user_input
    value = UserInput.text(content) if isinstance(content, str) else content
    if input_facts is None:
        input_facts, inline_images = prepare_user_input(value, self.workspace_dir,
                                                       getattr(self, "_input_permission_engine", None))
        image_content = [*inline_images, *(image_content or [])]
    from .input_preparation import resource_image_paths
    if resource_image_paths(input_facts) and "image_result" not in input_facts:
        raise ValueError("resource images require aadd_user_input")
    input_facts = deepcopy(input_facts)
    content = "\n".join(block["text"] for block in input_facts["blocks"] if block["type"] == "text")
    if self.goal is not None and self.goal.status == "active":
        input_facts["policy"]["goal_objective"] = self.goal.objective
    # Explicit slash/host selection is resolved at the message boundary.
    # The resulting runtime message becomes ordinary durable history, so
    # ContextEngine does not need to rediscover Skill bodies per request.
    materialize = getattr(self.skill_runtime, "materialize_selected_messages", None)
    materialized = materialize(self.messages) if callable(materialize) else ()
    user_message = Message(role="user", content=content, host_connector_status=host_connector_status,
                           input_facts=input_facts)
    if image_content:
        refs = self.image_context.register(user_message, image_content, source=image_source,
                                           eligible=image_context_eligible)
        input_facts["images"] = refs
        user_message.input_facts = deepcopy(input_facts)
    original_messages = list(self.messages)
    self.messages.append(user_message)
    if self.session_log is not None:
        try:
            self._persist_unlogged_messages(turn=self._next_session_turn(), step=None)
            self.session_log.flush()
        except BaseException:
            self.messages[:] = original_messages
            self._session_surface_replay_failed = True
            raise
    if materialized:
        skill_messages = []
        for _name, skill_content in materialized:
            skill_messages.append(Message(role="user", source="runtime", content=skill_content))
        self.messages.extend(skill_messages)
        if self.session_log is not None:
            self._persist_unlogged_messages(
                turn=self._next_session_turn(), step=None,
            )
            self.session_log.flush()
        defer_ack = getattr(self.skill_runtime, "defer_materialized_acknowledgement", None)
        if callable(defer_ack):
            defer_ack(skill_messages)
```

### SessionLog.history_messages

[box_agent/session_log.py:964](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/session_log.py#L964)

```python
def history_messages(self) -> list[tuple[int, Message, dict[str, Any]]]:
    """Replay original effective history, excluding model compaction copies."""
    history = []
    compacting = False
    for event in self._active_events():
        kind, data = event["type"], event["data"]
        if kind == "surface/reset":
            history.clear()
        elif kind == "compaction/summary":
            compacting = True
        elif kind == "compaction/end":
            compacting = False
        elif (not compacting and not data.get("surfaceDerived")
              and kind in {"user/message", "assistant/message", "tool/result"}):
            payload = data if kind == "user/message" else data["message"]
            history.append((event["seq"], Message.model_validate(payload), data))
    return history
```

### project_user_message

[box_agent/input_context.py:173](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py#L173)

```python
def project_user_message(message):
    """Project a new-format input without mutating its durable representation."""
    record = message.input_facts
    if not record:
        return message
    from .turn_hints import turn_hints
    from .loop_guards import format_injected_message

    original = "\n".join(block["text"] for block in record["blocks"] if block["type"] == "text")
    marker = "\x00input:" + record["id"] + "\x00"
    text = marker
    policy = record.get("policy", {})
    decision = policy.get("user_decision_response")
    if decision:
        text = "[HOST_USER_DECISION_RESPONSE]\n" + json.dumps(decision, ensure_ascii=False) + "\n[/HOST_USER_DECISION_RESPONSE]\n\n" + text
        if decision.get("decision_kind") == "presentation_mode" and decision.get("selected_option_id") in {"fast", "design"}:
            text = (
                "[Host presentation progress guidance: The choice card already displays the "
                "selected mode. Continue the existing task with the next concrete action; "
                "do not announce or reconfirm the mode in progress text before or after loading "
                "a Skill. Report new work, results, blockers or required decisions, rather than "
                "rephrasing the previous update. Keep meaningful long-running progress updates. "
                "Explain the choice if the user asks; a timeout must not be described as a user click.]\n\n"
                + text
            )
    language = {"en": "English", "ja": "Japanese", "zh": "Chinese"}.get(policy.get("ui_language"))
    if language:
        text = (f"[Host UI language: {language}. Use this language for user-visible intermediate summaries, "
                "progress updates, and the final response unless the user explicitly requests another language.]\n\n" + text)
    if policy.get("turn_hints"):
        text = "\n\n".join([text, *turn_hints(original_request(original, record["context"]))])
    context = record["context"]
    if not original:
        context = {**context, "input_context": {**context.get("input_context", {}), "userRequest": ""}}
    text = project_input_context(text, context)
    if record.get("image_result"):
        text += "\n\n" + record["image_result"]
    if record.get("kind") == "user_supplement":
        text = format_injected_message(text)
    if policy.get("goal_objective"):
        text = (
            "## Active Goal\n"
            f"Objective: {policy['goal_objective']}\n\n"
            "Work toward this durable goal across turns. Treat completion as evidence-based: "
            "verify the objective against concrete files, tests, logs, command output, or artifacts "
            "before saying it is done. Keep changes scoped to the goal and the user's latest message. "
            "If the goal is satisfied, call `goal_write` with action `complete` and non-empty "
            "`evidence` before your final answer, then state the evidence that proves completion. "
            "Use `goal_write` action `progress` for verified partial progress and action `block` "
            "with `blocked_reason` when external input is required. Do not ask the user to run "
            "a slash command for this.\n\n"
            "## Latest User Message\n"
            f"{text}"
        )
    ordered = record.get("blocks", [])
    if not any(block["type"] != "text" for block in ordered):
        return message.model_copy(update={"content": text.replace(marker, original)})
    prefix, suffix = text.split(marker, 1)
    content = [{"type": "text", "text": prefix}] if prefix else []
    refs = record.get("images", [])
    for block in ordered:
        if block["type"] == "text":
            content.append({"type": "text", "text": block["text"]})
        elif block["type"] == "resource_link":
            content.append({"type": "text", "text": "[File reference] " + json.dumps(block, ensure_ascii=False)})
        image_index = block.get("imageIndex", -1)
        if block["type"] == "resource_link":
            image_index = record.get("resourceImages", {}).get(block["uri"], -1)
        if 0 <= image_index < len(refs):
            content.append({"type": "input_image_reference", "imageId": refs[image_index]["imageId"]})
    if suffix:
        content.append({"type": "text", "text": suffix})
    return message.model_copy(update={"content": content})
```

### 补充输入 wire 分支

[box_agent/acp/__init__.py:3793](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L3793)

```python
if method == "inject":
    session_id = params.get("sessionId", "")
    state = self._sessions.get(session_id)
    if not state:
        return {"error": "session_not_found"}
    structured = "prompt" in params
    if structured == ("text" in params):
        return {"error": "INVALID_INJECTION_INPUT:provide_exactly_one_of_text_and_prompt"}
    injection_id = params.get("injectionId")
    if structured and (not isinstance(injection_id, str) or not injection_id.strip()):
        return {"error": "INVALID_INJECTION_ID"}
    injection_id = injection_id or str(uuid4())
    try:
        blocks = params["prompt"] if structured else [
            {"type": "text", "text": params["text"]}, *params.get("resources", []),
        ]
        if not isinstance(blocks, list) or (structured and "resources" in params):
            raise ValueError("INVALID_INPUT_BLOCKS")
        value = UserInput(tuple(blocks), params.get("_meta") or {}, injection_id)
        from box_agent.injections import submit_user_input
        return submit_user_input(value, agent=state.agent, queue=state.inject_queue,
                                 active=state.turn_active)
    except (ValueError, TypeError) as exc:
        return {"error": str(exc), "injectionId": injection_id}
if method == "cancel_inject":
    session_id = params.get("sessionId", "")
    injection_id = params.get("injectionId", "")
    state = self._sessions.get(session_id)
    if not state:
        return {"error": "session_not_found"}
    if not injection_id:
        return {"error": "empty_injection_id"}
    removed = state.inject_queue.cancel(injection_id)
    log.info(
        "session/inject_cancel",
        session_id=session_id,
        injection_id=injection_id,
        removed=removed,
    )
    return {"ok": removed}
```

### 补充输入 wire 分支

[box_agent/acp/__init__.py:5414](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py#L5414)

```python
case InjectedMessageEvent(content=text, injection_id=injection_id, user_visible=user_visible, status=status):
    if status != "injected":
        update = update_agent_message(text_block(""))
        update.field_meta = {"inputStatus": {"injectionId": injection_id, "status": status}}
        await self._send(session_id, update)
        continue
    log.info(
        "session/injected",
        session_id=session_id,
        injection_id=injection_id,
        user_visible=user_visible,
        text=text[:80],
    )
    if not user_visible:
        continue
    await self._send(
        session_id,
        update_agent_message(text_block(_injected_marker(text, injection_id))),
    )
```

### 客户端消费依据（MR !1170 固定提交）

固定客户端提交 `d8b36a01235574cfda750e9209a8ddada3354fe3`。以下为实现链接，不把客户端派生状态冒充服务端新 wire 字段：

- [sessionReplay.ts](https://gitlab.bj.sensetime.com/sensecode/raccoon-lab/-/blob/d8b36a01235574cfda750e9209a8ddada3354fe3/apps/office-desktop/runtime/electron/main/client/conversation/sessionReplay.ts)：回放 eventId 去重，inputKind/inputId 转为客户端 injected 状态。
- [eventProcessor.ts](https://gitlab.bj.sensetime.com/sensecode/raccoon-lab/-/blob/d8b36a01235574cfda750e9209a8ddada3354fe3/apps/office-desktop/src/shared/agent/events/eventProcessor.ts)：先消费 inputStatus，再处理普通正文及旧注入标记。
- [boxAgentManager.ts](https://gitlab.bj.sensetime.com/sensecode/raccoon-lab/-/blob/d8b36a01235574cfda750e9209a8ddada3354fe3/apps/office-desktop/runtime/electron/main/agent/boxAgentManager.ts)：恢复请求、缓冲回放及会话绑定。
- [repository.ts](https://gitlab.bj.sensetime.com/sensecode/raccoon-lab/-/blob/d8b36a01235574cfda750e9209a8ddada3354fe3/apps/office-desktop/runtime/electron/main/localChatStorage/repository.ts)：历史与绑定的存储事务。

### load 与用户回放的 SDK 线字段补充

以下为同一锁定 SDK 0.12.1 的字段声明；省略说明字符串，不把 SDK 可选能力当作业务已实现。

#### LoadSessionRequest

```python
class LoadSessionRequest:
    mcp_servers: Annotated[List[Union[HttpMcpServer, SseMcpServer, AcpMcpServer, McpServerStdio]], Field(alias='mcpServers')]
    cwd: str
    additional_directories: Annotated[Optional[List[str]], Field(alias='additionalDirectories')] = None
    session_id: Annotated[str, Field(alias='sessionId')]
    field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

#### ContentChunk

```python
class ContentChunk:
    content: Annotated[Union[TextContentBlock, ImageContentBlock, AudioContentBlock, ResourceContentBlock, EmbeddedResourceContentBlock], Field(discriminator='type')]
    message_id: Annotated[Optional[str], Field(alias='messageId')] = None
    field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

#### LoadSessionResponse

```python
class LoadSessionResponse:
    modes: Optional[SessionModeState] = None
    config_options: Annotated[Optional[List[Annotated[Union[SessionConfigOptionSelect, SessionConfigOptionBoolean], Field(discriminator='type')]]], Field(alias='configOptions')] = None
    field_meta: Annotated[Optional[Dict[str, Any]], Field(alias='_meta')] = None
```

#### UserMessageChunk

```python
class UserMessageChunk:
    session_update: Annotated[Literal['user_message_chunk'], Field(alias='sessionUpdate')]
```
