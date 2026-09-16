---
name: sn-ppt-tools
description: Use when another PPT Skill needs web search, image search or download, or image generation and the host Agent's equivalent native capability is absent or has failed.
metadata:
  allow_override: false
  project: SenseNova-Skills
  tier: 0
  category: infrastructure
  user_visible: false
---

# sn-ppt-tools

这是 PPT Skill 整包自带的备用工具，不是独立内容生产流程。调用前先读
`references/capability-policy.md`。宿主 Agent 的等价原生工具始终优先；只有原生能力
不存在或一次实际调用确认不可用时，才调用这里的脚本。

## 持久配置

四个脚本每次启动都会自动加载一个用户级 `.env`：

1. 设置了 `SN_PPT_ENV_FILE` 时读取该文件；
2. OpenClaw 读取 `~/.openclaw/.env`；
3. 其他环境默认读取 `~/.hermes/.env`。

已有进程环境变量优先，`.env` 只补充缺失项。不要把 secret 写进 Skill 目录、workspace、
命令行、prompt 或 task pack。完整模板和缺失项检查通过 `sn-ppt-doctor` 查看。

## 普通搜索

```bash
python3 "$PPT_TOOLS_DIR/scripts/web_search.py" "QUERY" --num 10
```

配置：

- URL：`SN_PPT_WEB_SEARCH_URL`；否则
  `SN_PPT_SEARCH_BASE_URL` / `SERPER_BASE_URL` 加 `/search`。
- Key：`SN_PPT_WEB_SEARCH_API_KEY` -> `SN_PPT_SEARCH_API_KEY` ->
  `SERPER_API_KEY`。

## 图片搜索与下载

```bash
python3 "$PPT_TOOLS_DIR/scripts/image_search.py" "QUERY" --num 10

python3 "$PPT_TOOLS_DIR/scripts/fetch_image.py" \
  "https://example.com/image.png" \
  --deck-dir "$DECK_DIR" \
  --output "assets/image.png" \
  --referer "https://example.com/source-page"
```

图片搜索配置：

- URL：`SN_PPT_IMAGE_SEARCH_URL`；否则
  `SN_PPT_SEARCH_BASE_URL` / `SERPER_BASE_URL` 加 `/images`。
- Key：`SN_PPT_IMAGE_SEARCH_API_KEY` -> `SN_PPT_SEARCH_API_KEY` ->
  `SERPER_API_KEY`。

先根据搜索元数据选择候选，再下载选中的图片。不得把远程 URL 直接留在最终 deck。

## 图片生成

```bash
python3 "$PPT_TOOLS_DIR/scripts/image_generate.py" \
  --prompt-file "$DECK_DIR/pages/page_001.prompt.txt" \
  --deck-dir "$DECK_DIR" \
  --output "pages/page_001.png" \
  --size "2752x1536"
```

配置：

- URL：`SN_PPT_IMAGE_GEN_URL`；否则
  `SN_IMAGE_GEN_BASE_URL` / `SN_BASE_URL` 加 `/images/generations`。
- Key：`SN_PPT_IMAGE_GEN_API_KEY` -> `SN_IMAGE_GEN_API_KEY` ->
  `SN_API_KEY`。
- Model：`SN_PPT_IMAGE_GEN_MODEL` -> `SN_IMAGE_GEN_MODEL`。

只支持 OpenAI/SenseNova 兼容的同步 `POST /images/generations` 协议，响应可以是
`data[].url` 或 `data[].b64_json`。不在这里扩展其他 provider。

## 输出与失败

- 成功输出单行 JSON，`status` 为 `ok`。
- 缺少配置输出 `status=unavailable`，退出码为 2。
- 请求或落盘失败输出 `status=failed`，退出码为 1。
- 所有图片只能写入调用方给定的绝对 `DECK_DIR` 内。
- Key 不通过命令行传递，不写入输出、日志、task pack 或 deck 文件。
- 同一能力调用失败后不循环重试；由调用 Skill 按 policy 继续无工具路径。

## 方法采用与同任务恢复

公共 `pptx` 的需求与交付义务贯穿整个任务，保持它与当前后端同时采用。
下方阶段替换只退休已完成内部阶段，不退休公共 `pptx`；最终产物、回执及真实交付
完成后才可 `get_skill(skill_name="pptx", usage="release")`。

实际执行本方法用 `get_skill(..., usage="use")`（默认值）；仅查看其他出口、Tools/Doctor
文档用 `usage="reference"`，不改变当前采用方法或用户路线。完成阶段后读取下一阶段时
可传 `replace=["旧方法名"]`，仅新读取成功后退休旧方法；读取失败保留原方法并处理错误。
例如 Entry → Story 用 `get_skill(skill_name="sn-ppt-story", usage="use", replace=["sn-ppt-entry"])`；
Story → 已确认的 Standard/Dazzle 同样替换 Story。只退休方法用
`get_skill(skill_name="旧方法名", usage="release")`。不要以参考读取自动切换制作出口。
仅用户明确开始独立新任务才传 `new_task=True`，并在该新任务第一次方法读取时、澄清前声明；
不得在后续阶段交接时补传。选择卡回复、继续、补充材料、页面修改、
压缩后恢复和阶段交接均延续原任务，不能把短回复当成完整需求。
从可用对话历史、通用任务上下文及实际工作文件恢复原始目标、全部附件、用户明确选择、
后续更正、页数、格式、同一绝对目录与已完成阶段。task_pack 是工作数据，不能证明用户选择。
原始动态要求与 task_pack 静态字段冲突时先修正工作数据，保留 Research/Story 成果；
真实选择依据丢失或冲突未解时用 `request_user_decision` 澄清，不从默认字段推断静态。

按所选出口的收尾时序，父级从公共 `pptx` Skill 的实际目录运行
`scripts/finalize.py --workspace <工作空间> --deck-dir <同一目录> --requirements <需求文件> --task-pack <任务包>`。
先完成任务包阶段/产物字段更新，再执行正式收尾；调用后修改输入必须重跑。
检查 stdout 和 `_trace/finalize-receipt.json` 的产物与警告；技术回执不能替代视觉或内容检查。
