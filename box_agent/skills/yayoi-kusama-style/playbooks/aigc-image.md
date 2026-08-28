# Playbook：画面 / AIGC

## 场景名称
把主题转成可粘贴的草间向出图/出视频提示词与画面简报。

## 适用信号
- 出图、海报、封面、分镜静帧、文生图/图生图
- 「写成 prompt」「Midjourney / 即梦 / Flux 风格词」

## 不适用
- 展陈动线 → spatial-immersive
- 商品开模/包装量产 → product-homage
- 只要标题 → 模式 D + phrasebook

## 前置检查
1. 必填：主题（可以很短）。
2. 选填：年代/期、比例、是否保留人脸/产品、介质。
3. 缺失默认：波点场吞噬；无南瓜、无 LED 镜屋；方图；厚涂绘画感。

## 执行步骤
1. 锁年代（七期）。未指定则默认波点场，不混片。  
2. 写命题一行。  
3. 锁一套母题；镜屋必须写亚型。  
4. 一对配色 + 一级密度 + 一种材质。  
5. 五要素简报：场 / 物 / 覆盖规则 / 镜头 / 禁止项。  
6. 中英提示词。英文材料与构图为主，艺术家姓名最多一次。期 6 或镜屋可在末行加全大写作品名。negative 必须含：red wig selfie, evenly spaced vector dots, Instagram mirror selfie, era-mix, Halloween pumpkin face, Louis Vuitton print。  
7. 一句反例。视频加：缓慢推进、人被空间吞没。

## 判断标准
- 成功：提示词去掉艺术家名仍能读出无限或消融。
- 需降级：必须保留大面积干净 Logo → 只在非功能面做吞噬，并说明这是妥协。
- 应拒答：要假签名、要某张名作的像素级再造当商品图。

## 输出示例

见 `templates/output-template.md` 与 `examples/style-examples.md` 例 A。

## 失败与恢复

| 失败类型 | 处理 |
|---|---|
| 模型只出时尚波点裙 | 加强 all-over、no focal point、dots on skin/walls/floor |
| 模型画出草间本人 | negative: portrait of the artist, red wig selfie |
| 用星空舱冒充全部镜屋 | 改写亚型；1965 用红白茎铺地 |
| 出南瓜脸万圣节 | 强调 no carved face, sculptural pumpkin, ribbed volume |
| 用户主题与母题/年代冲突 | 拆成两版方案 |
