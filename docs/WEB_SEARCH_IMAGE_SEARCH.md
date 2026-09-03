# `web_search` Text-to-Image Search Integration

This document defines how Box-Agent agents, Skills, and hosts call the hosted `web_search` MCP tool for text-to-image search and consume normalized image references.

## Supported modes

The hosted MCP tool currently exposes two `SearchType` values:

- `web`: text-to-web search and the default.
- `image`: text-to-image search.

The current MCP schema does not expose the upstream Global API's `visual` image-to-image mode or `ImageFilter`. Do not pass `DocCount`, `ImageFilter`, or `ImageQuery` to the hosted MCP tool.

## Call

```json
{
  "Query": "Shandong University campus architecture",
  "SearchType": "image",
  "Count": 5
}
```

`Query` is required and should describe one visual intent in 1–100 characters. Set `SearchType` to `image`; otherwise it defaults to `web`. Image search returns at most five results. `TimeRange` is intended for web results, and `AuthLevel` optionally filters authority (`0` default, `1` very high).

Call the registered `web_search` tool instead of addressing the internal MCP URL directly. A delegated sub-agent must explicitly request `required_tools: ["web_search"]`.

## Normalized event payload

Box-Agent normalizes common result shapes, including Custom `Result.ImageResults[]` and Global API `Result.Documents[].Snippet[]`, into `WebSearchEvent.payload`:

```json
{
  "type": "web_search",
  "refs": [
    {
      "reference_tag": "ref_1",
      "title": "Shandong University",
      "url": "https://example.com/shandong-university",
      "domain": "example.com",
      "passage": "Shandong University campus architecture",
      "images": ["https://cdn.example.com/campus.jpg"],
      "image_details": [
        {
          "url": "https://cdn.example.com/campus.jpg",
          "width": 1600,
          "height": 900,
          "alt": "Shandong University campus"
        }
      ],
      "date": "",
      "score": 0,
      "type": "web"
    }
  ]
}
```

Use `image_details` when available; `images` is the compatibility URL list. `image_details` can retain provider width, height, alt, shape, clarity, category, watermark, description, and style metadata. `url` prefers the source or landing page and falls back to the image URL when the provider supplies no landing page. Download from `image_details[].url`. Missing optional fields are omitted. Hosts consuming ACP updates should dispatch `rawOutput.type == "web_search"` and correlate the event with the original call by `toolCallId`.

Direct tool output may still contain provider JSON. Custom image entries are under `Result.ImageResults[]` and use `Image.Url`. In the Global API shape, image entries are under `Result.Documents[].Snippet[]`; select entries whose `Type` is `image` and read `Image.ImageUrl`.

## PPT asset rules

1. Use one query per visual intent.
2. Check width, height, and aspect ratio against the target layout; inspect the downloaded file when metadata is absent.
3. Download the image into the artifact directory before inserting it. Do not hot-link signed URLs, and do not rewrite their query strings.
4. Record the source page, image URL, query, download time, and selection reason. Search inclusion does not grant reuse rights; verify licensing and attribution separately.
5. Reject candidates that cannot be downloaded or decoded, are too small, or have unclear rights. Never substitute a favicon or source-page screenshot for the image result.

For empty refs, retry with a more specific single-intent query. Refs without `images` are source-only results. Keep existing shared concurrency handling for rate limits; do not duplicate the hosted MCP connection. Refresh product authentication for 401/403 responses rather than storing an API key in a Skill.

Upstream field references: [Volcengine Doubao Search Custom](https://docs.volcengine.com/docs/87772/2272953?lang=en) and [Doubao Search Global](https://docs.volcengine.com/docs/87772/2548026?lang=en). The runtime MCP tool schema and this normalized event contract are authoritative for Box-Agent consumers.
