---
name: yayoi-kusama-style
display_name: 草间弥生风格
description: "按草间弥生的年代与母题生成致敬向画面、AIGC 提示词、空间三幕与产品降级方案：先锁分期，再锁波点场/无限之网/南瓜/镜屋亚型/积累/永恒灵魂，用自我消融与无限重复覆盖主题。当用户提到草间弥生、Kusama、波点南瓜、无限镜屋、自我消融、永恒灵魂画风时使用。不要用于伪造原作或假联名，不要把稀疏圆点、红发表情包或 2010s 星空自拍舱当成全部草间。"
version: 1.2.0
author: shawn/raccoon-work
tags:
  - yayoi-kusama
  - visual-style
  - aigc-prompt
  - immersive-installation
  - polka-dots
  - self-obliteration
---

# 草间弥生风格

把「草间弥生」从打卡符号还原成可执行的视觉操作系统。

**波点不是装饰，是宇宙里的一个单点。**  
**没有一张标准草间图——先选年代，再选母题。**  
画面必须能成立：**无限重复、自我消融、灿烂底下的战栗**。  
模仿的是作品场，不是红发人设，也不是某一件原作。

心智模型：[references/visual-engine.md](references/visual-engine.md)。技法全书：[references/style-patterns.md](references/style-patterns.md)。不要加载仓库外的调研原文。

## 边界

做：画面 A、空间 B、产品致敬 C、诗性标题 D。  
不做：伪造原作/签名/工作室授权；1:1 直岛南瓜或具名镜屋施工；节日波点裙与角落三点；病理/住院猎奇；小说自传文本公案；人物扮演聊天；无视觉任务的代码与公文。

## 模式（先判定）

| 模式 | 信号 | 硬约束 |
|------|------|--------|
| **A 画面** | 出图、海报、静帧、AIGC 提示词 | 锁一期 + 一套母题；镜屋先选亚型 |
| **B 空间** | 展陈、镜屋、橱窗、沉浸 | 审视 → 纵情 → 消融；第三幕写亚型 |
| **C 产品** | 包装、潮玩、美陈、联名 | 语法致敬；禁止假授权视觉 |
| **D 标题** | 作品名、展览名 | 直白宇宙句；不是 slogan |

主题模糊时用一句话确认模式。用户没说年代 → **黄/黑波点场吞噬**，不自动加南瓜，不自动加 LED 星空舱。

## 原则（不可破）

1. 先选年代，再选母题。混期 = 纪念品店。
2. 单点即宇宙：主体被场吞没。
3. 没有视觉中心：拒 C 位英雄。
4. 正负同构：底与点、网与洞、灯与黑互相定义。
5. 重复是劳动：均匀印刷点是稀释态。
6. 灿烂底下要有战栗：可拍，但图案赢。

细则：[references/philosophy-rules.md](references/philosophy-rules.md)。

## 工作流

```text
草间进度：
- [ ] 1. 判定 A/B/C/D，扫描假联名 / 复刻 / 猎奇 / 装饰三点
- [ ] 2. 锁七期之一；未指定 → 黄黑波点场吞噬，不加南瓜、不加星空舱
- [ ] 3. 锁命题（消融 / 无限 / 安慰 / 过度生长 / 爱与和平）
- [ ] 4. 只选一套母题；镜屋先选亚型
- [ ] 5. 锁配色对 + 密度 + 一种主材质
- [ ] 6. 自检：草间逻辑，还是圆点墙纸 / 混片 / 人设
```

逐步说明：[references/workflow.md](references/workflow.md)。

- A → [playbooks/aigc-image.md](playbooks/aigc-image.md)
- B → [playbooks/spatial-immersive.md](playbooks/spatial-immersive.md)
- C → [playbooks/product-homage.md](playbooks/product-homage.md)
- D → [phrasebook/prompt-language.md](phrasebook/prompt-language.md)

### 命题 → 母题（仍服从年代）

| 命题 | 优先母题 |
|------|----------|
| 自我消融 | 波点场、消融之屋、对应年代的镜屋亚型 |
| 无限/宇宙 | 无限之网、LED 光点场（仅 2000s 亚型） |
| 安慰与生命 | 南瓜（期 5 为主） |
| 欲望/积累 | 软雕塑 / 茎铺地（期 3） |
| 晚年燃烧 | 永恒灵魂（期 6） |

输出前读 [qa/quality-checklist.md](qa/quality-checklist.md)。至少：年代与母题匹配；镜屋有亚型；有消融或无限；无假授权、无人设表情包、无病理猎奇；产品写清「致敬、非官方」。

## 输出

模板：[templates/output-template.md](templates/output-template.md)。

1. 模式 + 年代/期 + 命题 + 母题（及镜屋亚型）
2. 画面/空间方案
3. 可粘贴中英提示词
4. 一句「故意没做」
5. 产品模式：IP 降级

对照：[examples/style-examples.md](examples/style-examples.md)。红线：[references/boundary-rules.md](references/boundary-rules.md)。

## 禁止

- 声称原作、授权、可上拍。
- 具名镜屋 / 直岛南瓜的商业复刻施工图。
- 稀疏时尚波点、均匀矢量点、LV 老花当正确答案。
- 默认红发抱南瓜。
- 用 2010s 星空自拍舱冒充 1965 茎状镜屋。
- 幻觉/精神病院猎奇；无标记混搭村上/奈良。

## 异常

| 情况 | 处理 |
|------|------|
| 只要「加点圆点」 | 升级铺满或拒绝称草间风格 |
| 假冒原作/授权 | 拒绝，改致敬 |
| 缺主题、缺年代 | 消融 + 黄黑波点场吞噬 |
| 「镜屋同款」 | 选亚型 + 新单元；拒施工复刻 |
| 融合其他艺术家 | 标明融合与比例 |
| 追思/去世 | 尊严、永恒、继续创造；不写死因 |
| 要讨论小说文本争议 | 不展开；回到视觉规则 |

## 补充资源

- 心智模型：[references/visual-engine.md](references/visual-engine.md)
- 技法全书（七期、九母题、亚型、人设表）：[references/style-patterns.md](references/style-patterns.md)
- 生产流程：[references/workflow.md](references/workflow.md)
- 哲学判断：[references/philosophy-rules.md](references/philosophy-rules.md)
- 红线：[references/boundary-rules.md](references/boundary-rules.md)
- 提示词与标题 DNA：[phrasebook/prompt-language.md](phrasebook/prompt-language.md)
- 来源映射：[source-map.md](source-map.md)
- 三联蒸馏：[knowledge/source-article-notes.md](knowledge/source-article-notes.md)
