# 来源映射

代理执行时读本表与规则文件，不必加载调研原文。

## 主来源

| 源材料 | 路径或标识 | 应进入的位置 | 转化形式 | 覆盖状态 | 未覆盖原因 |
|---|---|---|---|---|---|
| 草间自述「草间哲学」：单点、无限、自我消融、回归永恒 | 三联专访问答 | `philosophy-rules.md`；`SKILL.md` | 判断规则：先命题后符号 | 已覆盖 | |
| 「好的艺术家要拥有自己的哲学」 | 专访 | `philosophy-rules.md`；`qa/quality-checklist.md` | 输出必须能一句话说清命题 | 已覆盖 | |
| 创作状态：面对画布手自动开始 | 专访 | `style-patterns.md` 密度 | 「停不下来的覆盖」 | 已覆盖 | |
| 2019 上海复星三空间：审视 / 纵情 / 消融 | 正文 | `playbooks/spatial-immersive.md` | 三幕空间剧本 | 已覆盖 | |
| 《隐匿的人生》凸面镜走廊 | 正文 | 空间第一幕 | 自我增殖 | 已覆盖 | |
| 无限镜屋史：1965 鼻祖、20 余间 | 正文 + 馆方 | `style-patterns.md` 亚型表 | 用逻辑不复刻某一间 | 已覆盖 | |
| 南瓜为入口符号 | 正文 + Benesse | `style-patterns.md` 卡3 | 安慰形体 + 黄黑对 | 已覆盖 | |
| 《无限的网》无中心 | 正文 + MoMA/Hirshhorn | `style-patterns.md` 卡1 | 小弧、figure=ground | 已覆盖 | |
| 幻觉记录为绘画原点 | 正文引述 | `boundary-rules.md` | 可作起源说明，禁止猎奇 | 已覆盖 | |
| 诗化标题与「永恒灵魂」 | 正文 + 馆方展签 | `phrasebook/prompt-language.md` | 全大写长句 + 口号 DNA | 已覆盖 | |
| 反战、赴美烧画 | 正文传记 | `knowledge/source-article-notes.md` | 背景，不作为默认画面 | 已覆盖 | 不做成传记生成器 |
| 对死亡：热情奔放去迎接 | 专访 | `SKILL.md` 异常处理；模式 D | 追思语气 | 已覆盖 | |
| 「我就过我自己的生活」 | 专访 | `boundary-rules.md` | 拒绝网红自拍腔 | 已覆盖 | |
| 视觉语法：波点/网/南瓜/镜屋/软雕塑/消融之屋/永恒灵魂 | 馆方墙文蒸馏 | `style-patterns.md` 卡1–9 | 母题卡、配色、失败冒充 | 已覆盖 | |
| 人设 ≠ 作品 | Tate / 批评 | `style-patterns.md` 第4节 | 默认画场不画红发 | 已覆盖 | |
| Accumulation ≠ Phalli’s Field | Beyeler / Tate | 卡5 vs 卡6 | 家具罩涂 vs 红白茎铺地 | 已覆盖 | |
| LV 均匀点 = 稀释 | 批评文献 | `boundary-rules.md`；产品 playbook | 传播态须标明 | 已覆盖 | |
| 原作与商标风险 | 基金会打假 | `boundary-rules.md` | 拒答与降级 | 已覆盖 | 无律师意见 |
| Phase 2 五个心智模型 | 全网蒸馏 | `visual-engine.md` | 执行启发式 | 已覆盖 | 文本公案不进执行层 |
| 七期年表 | 蒸馏 06 | `style-patterns.md` 第1节 | 先锁期 | 已覆盖 | |
| 等效 PRD F1–F9 | `prd/equivalent-prd.md` | 本包工作流与 QA | 见 `qa/prd-implementation-audit.md` | 已覆盖 | F10 实模抽检为 P2 |

## 覆盖率

- 相关条目 21，已覆盖 21（传记细节按设计降为背景）
- 覆盖率：100%（可执行信息）；F10 实模抽检不阻塞规则包
- 通过线：≥ 80%

## 本地原稿策略

- 不把《三联生活周刊》全文写入上传包。
- 蒸馏笔记见 `knowledge/source-article-notes.md`。
- 调研原文 `01`–`06` 不打进上架 ZIP。
- 公开原文入口（用户提供）：`https://mp.weixin.qq.com/s/zXRNZFIGL4MudnGDfjlT-A`
- 刊次：2019 年第 12 期；作者薛芃。

## 结论

- 结构合规：以 `scripts/validate-package.py` 为准
- 专业深度：v1.2.0 把九母题、七期、标题 DNA、心智模型写入执行层；不上架调研原文
- 下一步：F10 实模出图抽检（P2）。不得声称出图效果已验证
