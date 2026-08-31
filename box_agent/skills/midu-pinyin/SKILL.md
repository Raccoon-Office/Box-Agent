---
name: midu-pinyin
description: 使用蜜度接口为中文文本逐字注音，输出 ruby HTML、汉字拼音对照和纯拼音。适用于拼音注音；不用于拼音校对或普通文本校对。
license: JDT
---

# 蜜度拼音注音

执行前阅读 [蜜度共享认证流程](../_midu_shared/AUTH.md)。宿主已经注入有效凭证时直接复用；没有凭证时按该流程在对话中完成手机号和短信验证码认证。

```bash
"${BOX_AGENT_PYTHON:-python3}" "<skill-dir>/scripts/midu_pinyin.py" --text "<待注音文本>"
```

按脚本输出完整展示 ruby HTML、汉字拼音对照和纯拼音，不使用模型自行生成替代结果。本地预览只保留 `ruby`、`rt`、`rp` 标签和纯文本。权益或余额不足时停止重试，不要重新登录。
