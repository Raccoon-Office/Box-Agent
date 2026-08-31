---
name: midu-writing
description: 使用蜜度完成公文、通知、讲话稿、新闻稿、总结、报告等中文写作与续写。当用户要求起草、改写或继续完善正式中文材料时使用。不要用于校对、热点查询、时政检索或视频处理。
license: MIT
---

# 蜜度写作

先按 [蜜度共用认证](../_midu_shared/AUTH.md) 检查状态；已有凭证时禁止重复登录。

首次写作：

```bash
"${BOX_AGENT_PYTHON:-python3}" "<skill-dir>/scripts/midu_write.py" --user_input "<完整写作要求>" --pretty
```

需要继续修改时，把上一次返回的 `thread_id` 原样传回：

```bash
"${BOX_AGENT_PYTHON:-python3}" "<skill-dir>/scripts/midu_write.py" --user_input "<补充要求>" --thread_id "<thread_id>" --pretty
```

不要把 `appSecret` 放入命令参数。业务返回 401/403 时重新检查认证；余额或权益不足时停止重试并说明原因。
