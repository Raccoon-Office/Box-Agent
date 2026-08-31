---
name: midu-proofread
description: 使用蜜度接口校对中文文本并输出勘误结果。适用于错别字、用词和中文文本校对；不用于拼音校对、拼音注音或简繁转换。
license: JDT
---

# 蜜度文本校对

执行前阅读 [蜜度共享认证流程](../_midu_shared/AUTH.md)。宿主已经注入有效凭证时直接复用；没有凭证时按该流程在对话中完成手机号和短信验证码认证。

调用现有校对脚本：

```bash
"${BOX_AGENT_PYTHON:-python3}" "<skill-dir>/scripts/midu_proofread.py" --text "<待校对文本>"
```

完整展示脚本返回的校对状态和结果链接，不使用模型自行替代蜜度校对结果。脚本不会自动下载服务端返回的结果 URL。权益或余额不足时按脚本提示停止重试，不要重新登录。
