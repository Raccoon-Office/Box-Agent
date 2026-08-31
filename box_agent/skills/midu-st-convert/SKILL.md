---
name: midu-st-convert
description: 使用蜜度接口进行中文简体和繁体字形互转。适用于简转繁、繁转简；不修改错别字、用词或拼音。
license: JDT
---

# 蜜度简繁转换

执行前阅读 [蜜度共享认证流程](../_midu_shared/AUTH.md)。宿主已经注入有效凭证时直接复用；没有凭证时按该流程在对话中完成手机号和短信验证码认证。

- 繁体转简体使用 `--convert-type 1`。
- 简体转繁体使用 `--convert-type 2`。

```bash
"${BOX_AGENT_PYTHON:-python3}" "<skill-dir>/scripts/midu_st_convert.py" --text "<待转换文本>" --convert-type <1或2>
```

完整展示脚本返回的转换全文和方向，不顺手校对、改写或注音。权益或余额不足时停止重试，不要重新登录。
