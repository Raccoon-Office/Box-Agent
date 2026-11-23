# PPT 可选能力选择规则

普通搜索、图片搜索和图片生成都按同一顺序选择：

1. **Native**：查看当前 Agent 实际暴露的等价工具。工具注册只表示
   `present_unverified`；有 config/health 成功证据，或任务中的一次实际调用成功后，才算
   available 并优先使用。
2. **Bundled**：原生工具不存在，或一次实际调用已经确认不可用时，解析同级
   `sn-ppt-design-tools`，检查对应脚本和环境变量后调用。
3. **None**：内置工具未配置或调用失败时停止使用该能力，不伪造结果，不持续重试。

“原生可用”指当前 runtime 确实暴露了对应工具并能完成配置检查或实际调用，不根据工具
名称猜测。工具明确缺少配置时记为 `misconfigured`；没有检查手段时保持
`present_unverified`，实际任务需要该能力时可以尝试一次，失败后转 Bundled。
原生工具名称因 runtime 而异，例如普通/图片搜索可能是同一个带类型参数的工具。

## 路径

各 PPT Skill 从自己的 `SKILL_DIR` 取得同级 skills 目录：

```text
PPT_TOOLS_DIR = dirname(SKILL_DIR)/sn-ppt-design-tools
```

`sn-ppt-design-tools` 随 PPT 整包交付，不检查“是否安装”，也不扫描其他仓库位置。

## 状态记录

使用某项能力后，在已有 `task_pack.state.capabilities` 中更新对应小项：

```json
{
  "web_search": {
    "source": "native",
    "status": "available",
    "last_error": null,
    "updated_at": "ISO-8601"
  }
}
```

- `source` 只能是 `native`、`bundled` 或 `none`。
- `status` 使用 `available`、`unavailable` 或 `failed`。
- 只记录非敏感错误摘要；不得记录 key、Authorization header 或完整请求。
- 能力状态用于恢复和定位，不得反向改变 Story、输出出口或设计丰富度。

## 无能力路径

- 普通搜索不可用：PPT 流程跳过外部 Research，Story 基于用户材料继续，并明确事实覆盖
  边界。独立 Deep Research 仍按自己的前置条件处理。
- 需要准确说明真实对象时，图片搜索按 `native image search -> bundled image search -> none` 处理；两层都不可用或没有可靠结果，保留该图的缺口并说明，不以生图或程序绘图冒充真实证据。
- 场景插画需要生成时，图片生成按 `native generation -> bundled generation -> none` 处理；两层都不可用或失败时，说明缺口并调整页面，不能悄悄用程序绘制的场景 SVG 代替。只有输出 Skill 重新判断图片用途、核对来源与授权后，才能明确改用合适的现成图片。
- 用户提供的合适图片优先。原生形状和 SVG 适合流程、图标、结构与数据示意；不以对象名称决定来源，例如“荷花”既可能需要实拍，也可能需要氛围插画。

搜图和生图只提供 XML 中引用的局部素材。将宿主生成或附件提取的图片归入素材目录时，在同一次素材整理命令内运行同级 sn-ppt-dsl 的 `scripts/review_image.py DECK_DIR`，用返回的 inspect 路径看图。备用下载和生图脚本直接返回 inspect。实际图片及其来源要写入 `assets/sources.json`，搜索图片记录来源页与许可，生图记录模型与提示词文件，不记录密钥。所有设计风格统一由 XML 编译成 PPTX，不用整页图片打包替代页面源文件。

无图片时必须重新组织版式，不保留空图片框、破图、占位图或“待生成”文字。
