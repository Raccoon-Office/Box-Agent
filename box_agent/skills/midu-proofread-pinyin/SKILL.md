---
name: midu-proofread-pinyin
description: 使用蜜度接口检查汉字与拼音是否对应并输出拼音勘误结果。适用于拼音校对；不用于普通文本校对、自动注音或简繁转换。
license: JDT
---

# 蜜度拼音校对

执行前阅读 [蜜度共享认证流程](../_midu_shared/AUTH.md)。宿主已经注入有效凭证时直接复用；没有凭证时按该流程在对话中完成手机号和短信验证码认证。

输入为调用方提取后的纯文本；支持“拼音行 + 汉字行”交替格式：

```bash
"${BOX_AGENT_PYTHON:-python3}" "<skill-dir>/scripts/midu_proofread_pinyin.py" --text "<拼音与汉字文本>"
```

完整展示脚本返回的拼音校对状态和结果链接，不使用模型自行替代蜜度校对结果。脚本不会自动下载服务端返回的结果 URL。权益或余额不足时停止重试，不要重新登录。
