# `web_search` 文搜图接入说明

本文面向调用 Box-Agent 托管 `web_search` 的 Agent、Skill 和宿主，说明如何发起文搜图，以及如何消费 Box-Agent 发出的标准化图片引用。

## 能力边界

当前托管 MCP 工具支持两种 `SearchType`：

- `web`：文搜文，默认值。
- `image`：文搜图。

当前工具 schema 不支持火山 Global API 的 `visual` 图搜图，也没有暴露 `ImageFilter`。不要向托管 MCP 调用传入 `DocCount`、`ImageFilter` 或 `ImageQuery`；这些是上游 Global API 字段，不是当前 `web_search` MCP 契约。

## 调用

最小调用：

```json
{
  "Query": "山东大学校园建筑",
  "SearchType": "image",
  "Count": 5
}
```

参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `Query` | string | 是 | 搜索词，1～100 个字符。一次调用只传一个视觉意图，避免拼接多个主题。 |
| `SearchType` | string | 否 | 文搜图固定传 `image`；省略时为 `web`。 |
| `Count` | integer | 否 | 图片搜索最多返回 5 条。 |
| `TimeRange` | string | 否 | 仅文搜文使用；图片搜索不要依赖该字段。 |
| `AuthLevel` | integer | 否 | 权威等级过滤；`0` 为默认，`1` 为非常权威。图片素材检索通常使用默认值。 |

推荐让模型调用已注册的 `web_search` 工具，不要绕过 Box-Agent 直接请求内部 MCP URL。主 Agent 可直接调用；子 Agent 必须在委派时显式声明 `required_tools: ["web_search"]`。

## 标准化结果

Box-Agent 会将常见网页搜索结果、Custom `Result.ImageResults[]` 以及 Global `Result.Documents[].Snippet[]` 结构统一成 `WebSearchEvent.payload`：

```json
{
  "type": "web_search",
  "refs": [
    {
      "reference_tag": "ref_1",
      "title": "山东大学",
      "url": "https://example.com/shandong-university",
      "domain": "example.com",
      "passage": "山东大学校园建筑",
      "images": ["https://cdn.example.com/campus.jpg"],
      "image_details": [
        {
          "url": "https://cdn.example.com/campus.jpg",
          "width": 1600,
          "height": 900,
          "alt": "山东大学校园"
        }
      ],
      "date": "",
      "score": 0,
      "type": "web"
    }
  ]
}
```

字段使用规则：

- `images` 是兼容现有宿主的图片 URL 列表。
- `image_details` 是图片消费者的首选字段，包含原始 URL，以及上游可能提供的宽、高、替代文本、形状、清晰度、分类、水印和视觉描述。
- `url` 优先是图片来源或落地页；上游没有落地页时回退为图片 URL。实际图片下载始终使用 `image_details[].url`。
- `reference_tag` 用于把最终说明与来源卡片关联。
- 上游未提供的宽、高、替代文本会省略；调用方不得臆造。

宿主通过 ACP 消费时，监听 `WebSearchEvent` 对应的 `tool_call_update`，按 `rawOutput.type == "web_search"` 分发，并用 `toolCallId` 与原始工具调用关联。直接消费工具结果时仍可能看到上游原始 JSON：Custom 图片位于 `Result.ImageResults[].Image.Url`；Global 图片位于 `Result.Documents[].Snippet[]` 中，`Type == "image"` 的条目使用 `Image.ImageUrl`。

## PPT 素材接入建议

1. 每个版面视觉意图单独调用一次，例如“高铁穿越中国西部荒漠，纪实摄影，横版”，不要把人物、地图、图标等多个目标混在一个 Query 中。
2. 优先读取 `image_details`，按目标版式检查 `width / height` 和宽高比；可以把 `clarity`、`watermark`、`description` 和 `style_type` 作为候选排序信号，但必须在下载后验证实际图像。
3. 下载图片到当前产物目录后再插入 PPT，不要在 PPT 中热链远程 URL。带签名的 URL 可能过期，必须保留原始 URL，不要重新编码查询参数。
4. 同时记录 `url`、图片 URL、检索 Query、下载时间和选择原因。搜索结果不等于可复用许可；使用前仍需核对来源页的版权和署名要求。
5. 图片不可下载、解码失败、尺寸不足或许可不清楚时，丢弃该候选并换 Query；不要把站点 favicon 或来源页截图当作搜索图片。

## 失败与降级

- 没有 `refs`：本次没有可消费的结构化结果，换更具体的单一视觉 Query。
- 有 `refs` 但 `images` 为空：该结果只有来源页信息，不能作为图片素材。
- `700429` 或并发错误：按现有 `web_search` 限流策略重试，不要新增同地址 MCP 连接绕过共享并发限制。
- `401/403`：刷新产品登录态后重连 MCP；不要在 Skill 中保存或传递 API Key。

上游字段依据：[火山引擎豆包搜索 Custom 版](https://docs.volcengine.com/docs/87772/2272953?lang=zh)和[豆包搜索 Global 版](https://docs.volcengine.com/docs/87772/2548026?lang=zh)。Box-Agent 对外以运行时实际下发的 MCP tool schema 和本文标准化事件契约为准。
