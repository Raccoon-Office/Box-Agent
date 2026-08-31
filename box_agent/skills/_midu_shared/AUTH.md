# 蜜度内置能力共用认证

所有蜜度 Skill 共用 `MIDU_APP_SECRET` 和 `MIDU_USER_ID`。每个会话第一次使用任一蜜度 Skill 时，先运行一次状态检查：

```bash
"${BOX_AGENT_PYTHON:-python3}" "<skill-dir>/../_midu_shared/midu_auth.py" --action status
```

`configured=true` 时直接执行业务能力，禁止再次索取手机号。宿主注入的环境变量优先；独立运行 Box-Agent 时才回退到兼容凭证文件。

只有 `configured=false` 时才依次执行：

```bash
"${BOX_AGENT_PYTHON:-python3}" "<skill-dir>/../_midu_shared/midu_auth.py" --action send --mobile <手机号>
"${BOX_AGENT_PYTHON:-python3}" "<skill-dir>/../_midu_shared/midu_auth.py" --action verify --mobile <手机号> --sms_code <验证码>
```

认证成功后继续用户原来的任务，不要求用户重复发起。凭证失效时必须按来源处理：

- `source=environment`：说明凭证由 Officev3 等宿主注入。引导用户回到宿主的「蜜度能力」连接卡重新获取验证码并登录，等待本地 Agent 刷新后重试。不要执行上述独立短信认证命令，因为 `~/.midu_keys` 不能覆盖宿主环境变量。
- `source=legacy_file`：说明当前为独立 Box-Agent，可在对话中重新执行上述发送验证码和验证命令，新凭证会覆盖兼容凭证文件。

只有 401/403 且明确是凭证无效时才重新认证；余额、次数或权益不足时停止重试并说明原因。

不得在回复、日志、命令参数或生成文件中展示 `appSecret`。五个业务脚本不提供 `--api-key` 或其他 Secret 命令行入口；认证脚本只输出配置状态、用户 ID 和脱敏错误，不输出 Secret。
