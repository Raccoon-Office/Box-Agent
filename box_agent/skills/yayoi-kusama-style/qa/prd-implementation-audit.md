# PRD 落地审计

权威需求：`prd/equivalent-prd.md`（PRD-eq 1.1）+ 架构蓝图。用户冻结：视觉为主、少碰文本争议、专家单入口、不上架调研原文、默认黄/黑波点场。

| 需求条目 | 类型 | 应落入的包内位置 | 落地状态 | 证据路径 | 缺口与计划 |
|---|---|---|---|---|---|
| F1 可被草间/Kusama/波点南瓜/镜屋/消融/永恒灵魂触发 | 触发 | `SKILL.md` description | 已落地 | SKILL.md YAML | |
| F2 先选七期再选母题；默认波点场吞噬，不加南瓜/LED | P0 | `style-patterns.md`；`SKILL.md` 工作流 | 已落地 | 默认黄/黑 | |
| F3 镜屋必须选亚型 | P0 | `style-patterns.md` 第3节；spatial 第三幕表 | 已落地 | | |
| F4 输出含命题、年代、中英提示词、反例 | P0 | `templates/output-template.md` | 已落地 | | |
| F5 空间审视→纵情→消融；可拍但图案赢 | P0 | `playbooks/spatial-immersive.md` | 已落地 | | |
| F6 产品致敬非官方 + 风险表；拒 1:1 | P0 | `playbooks/product-homage.md` | 已落地 | 无律师意见 | |
| F7 人设≠作品；均匀印刷点=稀释 | P0 | `style-patterns.md` 第4节；boundary | 已落地 | | |
| F8 标题直白宇宙句 | P1 | `phrasebook/prompt-language.md` | 已落地 | 馆方句式 DNA | |
| F9 研究档案可查但不默认加载 01–06 | P1 | `AGENTS.md`；ZIP 不含 research/ | 已落地 | 维护者另存 | |
| F10 实模出图抽检 | P2 | `qa/test-prompts.md` | 未落地 | | 上架后抽检 T1/T6/T7 |
| 哲学：单点、消融、永恒 | 内核 | `visual-engine.md`；philosophy-rules | 已落地 | | |
| 禁止装饰圆点冒充 | 风险 | boundary + QA | 已落地 | | |
| 反猎奇病理 | 风险 | AGENTS、boundary | 已落地 | | |
| 不出人物扮演包 | 范围 | 未建 *-perspective | 已落地（排除） | 蓝图 | |
| 视觉为主少碰文本争议 | 范围 | visual-engine 诚实边界 | 已落地 | | |

## 汇总

- 需求条目总数：15
- 已落地：14
- 未落地：1（F10 实模抽检，P2）
- 是否允许声称专业完成：规则与知识储备可以；**不得声称出图效果已验证**
