"""Environment-context injection for ACP sessions.

The Electron host knows things the runtime cannot easily probe — bundled
CLI paths (lark-cli, wecom-cli, dws), whether the browser-tools
shortcut has been completed, the user's platform. Hosts pass this through
``session/new._meta.env_context`` so the model stops claiming "you don't
have lark-cli installed" when in fact a bundled binary is sitting at a
known path.

The input is sanitized before it touches the system prompt:

* CLI paths must be absolute, free of control characters/backticks, and
  capped at ``_MAX_PATH_LEN`` chars. Violating entries are dropped.
* ``platform`` must match a conservative ``[A-Za-z0-9_-]`` charset and be
  ``<= _MAX_PLATFORM_LEN`` chars; violating values are discarded.
* Unknown top-level keys are still parsed into ``extras`` (so hosts can
  experiment without coordinating a backend bump) but are **not rendered
  into the system prompt**. They surface in logs only.

The trust model is "local stdio host" — we accept the host's facts but
defend against developer mistakes (accidental secrets in ``extras``,
malformed CLI paths) and accidental prompt-channel pollution (newlines
forging headings, backticks escaping fences).

This module deliberately does **not** consult ``action_hints``.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_KNOWN_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "cli",
        "platform",
        "browser_tools",
        "browser_connector",
        "hyperframes",
        "image_service",
        "memory_configured",
        "runtimes",
        "obsidian",
    }
)

_MAX_PATH_LEN = 512
_MAX_NAME_LEN = 64
_MAX_PLATFORM_LEN = 32
_PLATFORM_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _has_unsafe_chars(value: str) -> bool:
    """True if string contains control chars, backticks, or markdown fence chars."""
    if "`" in value:
        return True
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _is_absolute_path(value: str) -> bool:
    # Accept POSIX absolute paths; tolerate Windows ``C:\...`` style.
    return value.startswith("/") or (len(value) >= 3 and value[1:3] == ":\\")


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


def _sanitize_label(raw: Any, *, max_len: int = _MAX_NAME_LEN) -> str | None:
    if raw is None or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or len(raw) > max_len:
        return None
    if _has_unsafe_chars(raw):
        return None
    return raw


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


def _sanitize_hyperframes(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    cleaned: dict[str, Any] = {}
    for field_name in ("installed", "available", "template", "ffmpeg", "ffprobe", "setup_supported"):
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


class BrowserToolsState(BaseModel):
    """Whether host-side browser tooling is installed/enabled.

    ``installed`` and ``enabled`` are independent: the user may have
    installed Chromium but kept the MCP entry disabled.
    """

    model_config = ConfigDict(extra="allow")

    installed: bool | None = None
    enabled: bool | None = None
    available: bool | None = None


class BrowserConnectorState(BaseModel):
    """Whether the real-browser connector extension is usable.

    The connector can read and perform allowlisted actions in the user's real
    browser context. It is separate from Playwright automation: an enabled
    gateway is not enough if the extension is not connected or is paused.
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool | None = None
    connected: bool | None = None
    paused: bool | None = None
    available: bool | None = None


class ImageServiceState(BaseModel):
    """Legacy host advisory about image generation reachability.

    This field is retained for protocol compatibility but is not authoritative.
    The standard ``generate_image`` capability is owned by Box-Agent and reads
    its own ``image_generation`` config in both CLI and ACP modes.
    """

    model_config = ConfigDict(extra="allow")

    available: bool | None = None


class HyperFramesState(BaseModel):
    """Whether the host-side HyperFrames video runtime is usable."""

    model_config = ConfigDict(extra="ignore")

    installed: bool | None = None
    available: bool | None = None
    version: str | None = None
    browser: str | None = None
    template: bool | None = None
    ffmpeg: bool | None = None
    ffprobe: bool | None = None
    setup_supported: bool = False


class HostRuntime(BaseModel):
    """Sanitized host-provided runtime metadata."""

    model_config = ConfigDict(extra="ignore")

    path: str | None = None
    shell_path: str | None = None
    sandbox_path: str | None = None
    npm: str | None = None
    npx: str | None = None
    node_modules: str | None = None
    ready: bool | None = None
    provider: str | None = None


class ObsidianState(BaseModel):
    """Sanitized host-provided Obsidian integration metadata."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool | None = None
    vault_path: str | None = None
    vault_name: str | None = None
    cli_path: str | None = None
    app_path: str | None = None
    cli_available: bool | None = None
    app_running: bool | None = None


class EnvContext(BaseModel):
    """Parsed view of ``session/new._meta.env_context``.

    Unknown top-level keys land in ``extras`` (logged only — not rendered
    into the system prompt, see :func:`build_env_context_prompt`). Known
    fields are typed and sanitized; anything that fails validation is
    discarded with a warning rather than rejecting the whole session.
    """

    model_config = ConfigDict(extra="ignore")

    cli: dict[str, str | None] = Field(default_factory=dict)
    platform: str | None = None
    browser_tools: BrowserToolsState | None = None
    browser_connector: BrowserConnectorState | None = None
    hyperframes: HyperFramesState | None = None
    image_service: ImageServiceState | None = None
    memory_configured: bool | None = None
    runtimes: dict[str, HostRuntime] = Field(default_factory=dict)
    obsidian: ObsidianState | None = None
    extras: dict[str, Any] = Field(default_factory=dict)

    @classmethod
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

    def is_empty(self) -> bool:
        """Empty iff nothing the prompt cares about. ``extras`` doesn't count."""
        return not (
            self.cli
            or self.platform
            or self.browser_tools is not None
            or self.browser_connector is not None
            or self.hyperframes is not None
            or self.image_service is not None
            or self.memory_configured is not None
            or self.obsidian is not None
        )


def _format_cli_section(cli: dict[str, str | None]) -> list[str]:
    if not cli:
        return []
    available: list[str] = []
    missing: list[str] = []
    for name, path in sorted(cli.items()):
        if path:
            available.append(f"  - `{name}`: `{path}`")
        else:
            missing.append(f"`{name}`")
    lines: list[str] = []
    if available:
        lines.append("- 可用 CLI（机器上已安装，可以通过 bash 工具直接调用）：")
        lines.extend(available)
        if cli.get("lark-cli"):
            lines.append(
                "- 飞书/Lark CLI 策略：officev3 本地会话只允许用户身份；业务命令必须显式加 "
                "`--as user`，不要使用 `--as bot`、`config bind --identity bot-only` 或 "
                "`config strict-mode`。OAuth 命令不接受 `--as user`；需要用户授权且用户已同意时，"
                "使用 `lark-cli auth login --no-wait --json`（按需附加 `--recommend` 或 `--scope`）"
                "取得 CLI 返回的 `verification_url`，再调用可用的 `user_browser_open_tab_and_read` "
                "打开该地址。只有浏览器工具成功后才能声称已打开；工具不可用时必须把地址明确交给用户。"
            )
        if cli.get("dws"):
            lines.append(
                "- 钉钉 DWS 策略：当前 OAuth profile 由 officev3 管理；可读取文档、知识库和钉盘，使用 "
                "`doc create` / `doc update` 写入钉钉文档，或使用 `drive upload` / `drive mkdir` 上传文件、创建钉盘文件夹。"
                "不要执行 `drive upload-info` / `drive commit`、删除、移动、共享/权限、`auth login/reset`、profile/config、skills/plugins、升级或 raw API 命令。"
            )
    if missing:
        lines.append("- 未安装 CLI（不要假装能调用）：" + ", ".join(missing))
    return lines


def _format_browser_tools(state: BrowserToolsState) -> list[str]:
    if state.installed is None and state.enabled is None and state.available is None:
        return []
    parts = []
    if state.installed is not None:
        parts.append(f"installed={str(state.installed).lower()}")
    if state.enabled is not None:
        parts.append(f"enabled={str(state.enabled).lower()}")
    if state.available is not None:
        parts.append(f"available={str(state.available).lower()}")
    return [f"- 浏览器工具状态：{', '.join(parts)}"]


def _format_browser_connector(state: BrowserConnectorState) -> list[str]:
    if (
        state.enabled is None
        and state.connected is None
        and state.paused is None
        and state.available is None
    ):
        return []
    parts = []
    if state.enabled is not None:
        parts.append(f"enabled={str(state.enabled).lower()}")
    if state.connected is not None:
        parts.append(f"connected={str(state.connected).lower()}")
    if state.paused is not None:
        parts.append(f"paused={str(state.paused).lower()}")
    if state.available is not None:
        parts.append(f"available={str(state.available).lower()}")
    return [f"- 真实浏览器连接器状态：{', '.join(parts)}"]


def _format_image_service(state: ImageServiceState) -> list[str]:
    if state.available is None:
        return []
    label = "可用" if state.available else "不可用"
    return [
        f"- 生图服务状态（宿主兼容信息）：{label}；"
        "`generate_image` 标准能力以 Box-Agent 自身 `image_generation` 配置为准。"
    ]


def _format_hyperframes(state: HyperFramesState) -> list[str]:
    if (
        state.installed is None
        and state.available is None
        and state.version is None
        and state.browser is None
        and state.template is None
        and state.ffmpeg is None
        and state.ffprobe is None
    ):
        return []

    parts = []
    if state.installed is not None:
        parts.append(f"installed={str(state.installed).lower()}")
    if state.available is not None:
        parts.append(f"available={str(state.available).lower()}")
    if state.version is not None:
        parts.append(f"version={state.version}")
    if state.browser is not None:
        parts.append(f"browser={state.browser}")
    if state.template is not None:
        parts.append(f"template={str(state.template).lower()}")
    if state.ffmpeg is not None:
        parts.append(f"ffmpeg={str(state.ffmpeg).lower()}")
    if state.ffprobe is not None:
        parts.append(f"ffprobe={str(state.ffprobe).lower()}")

    lines = [f"- 视频生成（HyperFrames）状态：{', '.join(parts)}"]
    if state.available is True:
        lines.append(
            "  - 用户要求生成视频、动画、MP4/GIF、motion graphics、短片或可渲染 HTML 动画时，"
            "不要否认视频工具；优先加载 `hyperframes-video` skill。"
        )
        lines.append(
            "  - 用户要求把现有 HTML、网页、页面或站点转成视频时，优先保真录制原页面的真实渲染结果；"
            "不要重写文案、重做布局或生成相似页面。若 HyperFrames wrapper/iframe 抽帧为空白或内容不一致，"
            "改用托管 Chromium/Playwright 直接采集原 HTML 帧并用 ffmpeg 编码。"
        )
        lines.append(
            "  - 宿主已注入 `HYPERFRAMES_RUNNER_PATH`、`HYPERFRAMES_CLI_PATH`、`HYPERFRAMES_TEMPLATE_DIR`、"
            "`HYPERFRAMES_BROWSER_PATH`、`HYPERFRAMES_FFMPEG_PATH`、`HYPERFRAMES_FFPROBE_PATH` "
            "等环境变量。优先用 bash 通过 `$BOX_AGENT_NODE \"$HYPERFRAMES_RUNNER_PATH\" ...` "
            "执行 inspect/render；runner 会把 StaticGuard stderr 当作失败、解析实际 MP4 输出并用 ffprobe 验收。"
            "`HYPERFRAMES_TEMPLATE_DIR` 指向模板根目录本身，可能已经是 `templates/basic-composition`，"
            "复制时使用 `cp -R \"$HYPERFRAMES_TEMPLATE_DIR\"/. <project-dir>/`，不要再拼一层 `/basic-composition`。"
            "inspect/render 参数传项目目录 `.`，不要传 `index.html`；render 不要把 composition id 当作 "
            "`--composition` 文件路径；项目根只保留一个带 `data-composition-id` 的 HTML。不要使用 `npx --yes` 或临时安装。"
        )
        lines.append(
            "  - 生成 HyperFrames HTML 时，单次 `write_file`/`append_file` 的 content 控制在 5500 字符以内；"
            "首版优先写一个完整、紧凑、可严格渲染的小 composition，并保留根节点 "
            "`data-composition-id`、`data-start`、`data-duration`、`data-width`、`data-height` "
            "和匹配的 `window.__timelines[...]` 注册，再迭代丰富。"
            "使用本地图片/截图/视频/字体时，`window.hyperframesReady` 必须等待资源 load/decode；"
            "渲染后必须抽帧或生成 contact sheet 做视觉验收，不能只凭 ffprobe 通过就宣布完成。"
            "生成 contact sheet 时不要用 `rm -rf` 清理帧目录，改用新的子目录或覆盖固定帧文件，避免触发危险命令确认。"
            "不要复制 `[Full tool-call argument omitted from model history]` 这类历史占位文本。"
        )
    elif state.setup_supported:
        lines.append(
            "  - 视频插件尚未完整就绪。用户要求生成视频时，调用 get_skill('hyperframes-video') "
            "取得宿主的安装/修复入口；说明需要准备后结束本回合，等待用户操作。"
            "不要自行安装依赖、下载替代技能或承诺已完成渲染。"
        )
    elif state.installed is True:
        lines.append("  - HyperFrames 已安装但当前不可用：先检查模板、浏览器、ffmpeg/ffprobe 状态；不要承诺已完成视频渲染。")
    else:
        lines.append(
            "- HyperFrames 不可用：不要计划调用视频渲染链路，除非用户先启用/安装相关运行时。"
        )
    return lines


def _format_obsidian(state: ObsidianState) -> list[str]:
    parts: list[str] = []
    if state.enabled is not None:
        parts.append(f"enabled={str(state.enabled).lower()}")
    if state.cli_available is not None:
        parts.append(f"cli_available={str(state.cli_available).lower()}")
    if state.app_running is not None:
        parts.append(f"app_running={str(state.app_running).lower()}")

    lines: list[str] = []
    if parts:
        lines.append(f"- Obsidian 状态：{', '.join(parts)}")
    if state.vault_name:
        lines.append(f"  - Vault：{state.vault_name}")
    if state.vault_path:
        lines.append(f"  - Vault 路径：`{state.vault_path}`")
    if state.cli_path:
        lines.append(f"  - Obsidian CLI：`{state.cli_path}`")
    if state.app_path:
        lines.append(f"  - Obsidian App：`{state.app_path}`")
    if state.enabled is True:
        lines.append(
            "- Obsidian 写入/打开策略：当用户要求导出、写入、保存或追加到 Obsidian 时，"
            "必须优先使用 `obsidian_create_note`、`obsidian_update_note` 或 `obsidian_daily_note`；"
            "不要用 bash 直接调用 `obsidian create/append/prepend/open/daily`。"
            "如果 prompt 中存在 `obsidian_context`，且用户要求修改该引用笔记，"
            "必须使用 context 里的 Vault 相对 `path` 调用 `obsidian_update_note`；"
            "不要修改 workspace 副本或 `.data-sources` 文件。"
        )
    return lines


def build_env_context_prompt(ctx: EnvContext | None) -> str:
    """Render ``EnvContext`` into a markdown checklist for the system prompt.

    Only sanitized known fields go in. ``extras`` is intentionally
    excluded: passthrough values must not be promoted into the model's
    high-priority context without explicit backend support.
    """
    if ctx is None or ctx.is_empty():
        return ""

    lines: list[str] = ["## 当前用户环境", ""]
    if ctx.platform:
        lines.append(f"- 操作系统：`{ctx.platform}`")
    lines.extend(_format_cli_section(ctx.cli))
    if ctx.browser_tools is not None:
        lines.extend(_format_browser_tools(ctx.browser_tools))
    if ctx.browser_connector is not None:
        lines.extend(_format_browser_connector(ctx.browser_connector))
    if ctx.hyperframes is not None:
        lines.extend(_format_hyperframes(ctx.hyperframes))
    if ctx.image_service is not None:
        lines.extend(_format_image_service(ctx.image_service))
    if ctx.obsidian is not None:
        lines.extend(_format_obsidian(ctx.obsidian))
    if ctx.memory_configured is not None:
        state = "已完成" if ctx.memory_configured else "未完成"
        lines.append(f"- 个人记忆配置：{state}")

    lines.append("")
    lines.append(
        "请把以上信息当作事实依据：不要否认已列出可用的工具，也不要假装能调用未列出的工具。"
        "如果用户的需求需要某个未安装的工具，明确告知并建议安装途径。"
    )
    return "\n".join(lines)
