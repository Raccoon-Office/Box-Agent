#!/usr/bin/env python3
"""Build the shared SMS-authentication hint used by all Midu skills."""

from __future__ import annotations

import os
import sys
from pathlib import Path


AUTH_SCRIPT = Path(__file__).with_name("midu_auth.py").resolve()


def build_auth_hint(
    python_executable: str | None = None,
    *,
    credential_source: str = "none",
) -> str:
    """Return the recovery flow for the credential source in use."""

    if credential_source == "environment":
        return (
            "宿主注入的蜜度凭证已失效。请回到 Officev3 的“蜜度能力”连接卡，"
            "重新获取短信验证码并登录；等待本地 Agent 刷新后，再重新执行原业务命令。\n"
            "不要运行独立 Box-Agent 的短信认证命令：本地凭证文件不能覆盖宿主注入的环境变量。"
        )

    executable = (
        python_executable
        or os.environ.get("BOX_AGENT_PYTHON")
        or sys.executable
        or "python3"
    ).strip().strip('"')
    command = f'"{executable}" "{AUTH_SCRIPT}"'
    return (
        "需要通过对话完成蜜度登录。请向用户索取手机号，执行：\n"
        f"  {command} --action send --mobile <手机号>\n"
        "再向用户索取收到的验证码，执行：\n"
        f"  {command} --action verify --mobile <手机号> --sms_code <验证码>\n"
        "登录成功后，重新执行原业务命令；后续蜜度 Skill 会复用同一凭证。"
    )
