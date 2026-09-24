# Box-Agent 工具契约

本文件是 `sn-ppt-standard` 在 Box-Agent 上的薄适配层。只翻译 harness 接口，不降低根 `SKILL.md`、reference 或角色卡中的事实、设计、视觉验收与交付要求。

从零生成静态 PPT 时按根 `SKILL.md` 的“Box-Agent 静态新建的执行方式”执行；本文件的子任务规则只在实际委派时生效，不要求为制作、修复或 Review 新建子任务。已有编辑分工不变。

## 1. 路径与执行所有权

- `<SKILL_ROOT>` 只取 `get_skill(skill_name="sn-ppt-standard")` 返回的 `Skill Root Directory` 绝对路径；它是 Standard 的目录，不是 `pptx` 公共入口、Entry、Story 或最后加载的任意 Skill 目录。所有确定性命令使用 `python "<SKILL_ROOT>/scripts/<name>.py" ...`；不得依赖 `${SKILL_DIR:-skills/sn-ppt-standard}` 或当前目录中存在 `skills/`。批量渲染与正式待审成功回执中的 `skill_root`、`deck_dir`、`next_instructions` 和 `review_ledger` 分别给出本次实际方法、任务根与既有账本的绝对定位；确认任务根与当前 task pack 一致后，直接使用回执，原文已完整且未变则复用。压缩后回执也已丢失时，直接重新加载 `sn-ppt-standard` 恢复，不从其他 Skill 根目录猜测、扫描其他 workspace 或硬编码某台机器的安装路径。
- 父 Orchestrator 不在 Standard 阶段执行材料解析；Standard 只读取 Entry/Story 已交接的 `raw_documents.json`、`info_pack.json` 和 `outline.md`。父级使用 `bash` 执行 `deck.py`、`render.py`、`font_bundle.py`、`image_cutout.py` 和本契约的 `serper_images.py`。Box-Agent 子代理不请求 `bash`；父级负责转换、渲染、联系表、来源登记和 build。
- 所有 bash/脚本命令的任务根参数必须传入已验证的绝对 `"$DECK_DIR"`；禁止以当前工作目录、`.`或未解析的 `output/` 代替任务根。`write_scope` 由 Box-Agent 按文件工具的工作区解析，不经过 Skill 脚本；统一传入从同一个 `task_pack.deck_dir` 派生的绝对路径。只有 CLI 明确要求的资产键（例如 `assets/<file>`）使用 deck 内相对路径，由脚本解析。产物直接写 `plan/`、`assets/`、`slides/`、`renders/`、`speech.md`、`present.html`，并可由 Standard exporter 生成 `<DECK_ID>.pptx`；不要添加 `output/` 层。

## 2. 工具映射

只调用 Box-Agent 展示的 canonical 工具名，不依赖执行期旧别名。

| 旧 harness 写法 | Box-Agent 写法 |
| --- | --- |
| `read_file(path, offset, limit)` | `read_file(path, offset, limit)` |
| `write_file(path, content)` | `write_file(path, content)`；大文件按该工具返回的 chunk 合同继续 |
| `patch(path, old_string, new_string)` | `edit_file(path, old_str, new_str)`；旧串必须唯一，先读后改 |
| `search_files(...)` | `search_files(...)` |
| `terminal(command, ...)` | 父级 `bash(command, timeout)`；不用旧 `workdir/background` 参数 |
| `vision_analyze(image_url, question)` | `inspect_images(image_paths=[...], instruction=..., strategy="native")` |
| `image_generate(prompt, aspect_ratio)` | `generate_image(prompt, output_path, size, watermark=false)` |
| `delegate_task(...)` | `sub_agent(title, task, required_tools, files, write_scope, budget)` |

视觉模型优先使用 `strategy="native"`，让当前角色直接看新鲜像素。只有工具明确返回 `IMAGE_NATIVE_UNSUPPORTED` 时才改用 `proxy`，并在交接中标明这是代理视觉结论。HTML/CSS 修改后必须先重渲再看。

### 视觉检查分批（PPT Skill）

- 每次模型响应最多调用一次 `inspect_images`，每批最多 4 张图；工具的通用上限不改变本 Skill 的分批上限。不得在同一响应中并行发出多个 `inspect_images` 调用（包括多个 `native` 调用）来规避限制。
- 等待本批工具实际返回，并由模型看图形成检查结论后，先记录已覆盖页码或素材 ID、发现的问题和待检查清单，再在后续模型响应中检查下一批；工具调用成功或图片已附加都不等于已看图。
- 保留原有总览/联系表与逐页细查流程；总览不能替代逐页细查。按原验收要求覆盖全部页面和待检素材，遗漏、失败或修改后尚未复看的页面不能标为检查完成。
- `REQUEST_BODY_TOO_LARGE` 表示失败请求中的图片未被模型看到，不增加已覆盖清单，不得宣称本批或整册检查完成。保持原图质量，将失败批次缩小为 2 张、必要时 1 张后顺序重试；不得降低图片质量、跳过页面，也不得仅为绕过请求字节限制改用 `proxy`。单张仍超限时按待验收项读取相关高清局部，记录已检查区域及尚未检查区域；局部检查不能冒称整页已覆盖，只有原验收要求的区域与内容全部核验才标记页面完成。无法完成覆盖时保留未完成状态、错误与待检清单，如实报告阻塞。
- `IMAGE_REQUEST_FAILED` 的 `category=timeout` 是视觉服务请求超时，不等于图片或页面损坏。对同一批最多自动重试一次；重试前把每张图降到 1024px 长边，并将批次限制为 2 张，必要时 1 张。仍超时、服务不支持图片或请求参数无效时，立即把该批标记为 `visual_unverified`，停止重复视觉请求。
- 视觉降级的判定顺序固定为：先完成页面存在/非空、页数与顺序、HTML 自检、文本/占位符、必要资源与 PPTX 包结构等确定性检查；这些任一硬门失败仍阻塞交付。全部硬门通过后，视觉请求失败只记录 warning 和未检查页/区域清单，交付可用 HTML/PPTX，不把视觉结果写成“已通过”。
- 检查范围由 Slide / Review 方法确定：首次诊断覆盖完整要求，修复复验沿用原问题并查回归，最终全册像素覆盖不能省；不把每次复验重开为一轮全册诊断。同一批服务超时最多一次降采样重试。达到 3 次 Review 或任一页组 2 次返修后停止返工；需恢复已有验证版时按 Review 方法重新待审并检查最终像素，不在最终看图之后另跑 build。未看完的范围保留 visual_unverified，不写 PASS；禁止因低置信度审美建议继续重渲染。

## 3. Box-Agent 委派合同

- 一次 `sub_agent` 只委派一个完整工作单元；需要并行时，在同一模型回合发出多个互相独立的 `sub_agent` 调用，不使用旧 `tasks` 数组。
- `title` 对应旧 `label`；`task` 给出语言、所属组与页码、输入/输出绝对路径、写入边界和返回要求。静态新建时，父级已掌握且适合直接传入的短原文（本组合同、`boundary_handoff`、Style Lock、逐页计划）可随任务提供，标明来源路径与章节；不要改写事实、屏显文案或设计决策。区分“已提供原文”和“仍需读取”，后者给出精确定位，不再要求把前者读一遍。
- `required_tools` 只给完成任务所需的 canonical 工具。凡包含 `write_file`、`append_file` 或 `edit_file`，必须给精确且互不重叠的 `write_scope`。
- 普通页面生产委派省略 `budget`，使用本次运行时工具说明中的默认额度；只有主动限制小任务时才显式收紧。Skill 不保存额度数字，不读取配置或另查预算；父级以工具说明及实际回执为准，只读文件批处理保留其独立限制。
- 子代理不递归委派，不读运行轨迹。父级以子代理自然语言合同为交接，并用 `read_file` / `search_files` 验证声明的正式产物。
- Research 可用 `read_file/search_files/web_search/web_extract/write_file`；Material 只在父级 staging 后读取解析产物并写指定摘要；Image 优先用 `generate_image/inspect_images/read_file`；Slide/Review 用文件工具和 `inspect_images`，由父级在两次委派之间完成渲染。

Box-Agent 的 `sub_agent` 没有父子交错的暂停/续跑协议。实际委派制作时，一次任务仍负责一个完整 Production group，在本次任务内写完组内全部 HTML 首稿，一次返回全部待渲染页码；不在首张后结束任务等待父级。父级随后批量渲染并逐页看图；静态新建在所有写页子任务返回后由主 Agent 集中修复，不默认再按组重派。首次交回待渲染页面不算返修，实际像素问题修复计入根 Skill 的预算。简单编辑仍由唯一 Review 集中改文件后以 `pending_parent_verification` 交回待渲染页码，由父级完成渲染、build 和最终检查；子代理不能在父级执行前声称像素或交付已通过。

素材完成并回填计划后，直接使用当前原文件，不要求先生成 `group_input.py` 分片。所有 deck 内输入路径与 HTML 目标都从同一个已验证的 `task_pack.deck_dir` 派生为绝对路径；本角色说明、此契约和参考文档从已加载的 `<SKILL_ROOT>` 派生。`sub_agent.files` 只列已存在的输入文件，预计 HTML 输出只进入 `write_scope`，不能放进 `files`；两者使用同一组页码与任务根，不另猜目录或缩短成相对写域。含写工具的子任务中，`files` 只提供路径，不自动注入文件内容；父级读过也不表示子任务已读。

实际新建子任务必须完整掌握所属组合同、Style Lock、`base.css`、必要逐页计划与命中参考。随 `task` 已完整提供、未变化的原文满足对应读取要求，不能仅因是新子任务而回源重读；其余输入仍按路径读取。较大的 CSS、说明或参考保留精确路径并按需分段读完整，不为减少读取次数把全册或长 CSS 重复输出到每个 task。静态新建允许在容量合适时同回合读取多页输入，再连续提交多页 HTML；不强制逐页读写往返。选中文件或章节截断、修改或上下文压缩丢失时补读原文，不能用摘要补齐；工具明确拒绝输入或缺失原文时如实返回阻塞。

已有且仍与当前来源一致的完整原文分片可按顺序复用；与 task 中的完整原文一样，已含角色说明、契约或参考原文的不重复读取来源。它们只是输入视图，原计划、CSS 与素材账本仍是真相源。来源变化或原文缺失时读取当前源文件，不要求重新包装或迁移计划。新委派必须获得角色说明与本契约的完整内容，但不限定为额外一次读取；同一主 Agent 切换职责也不必重读仍完整有效的说明。修复前读取受影响页的当前 HTML、逐页计划，以及本次问题必要的当前 Style Lock、CSS、组合同和参考，不重新准备无关页面的输入或重做规划。路径与 task pack 不一致是错误，不搜索其他 workspace，也不做任务迁移。

首次准备基础 CSS 使用 `python "<SKILL_ROOT>/scripts/deck.py" init "$DECK_DIR"`，不拼接跨 Skill 的复制命令。它只在缺失时复制原模板、返回绝对路径，已有 CSS 不覆盖；不是 `prepare`、素材验收或页面验收。

## 4. 生图、搜图与来源账本

### 生图

调用 Box-Agent 原生 `generate_image`，必须给出位于 `assets/` 下的稳定 `output_path`。OpenAI 兼容服务使用 `1536x1024`、`1024x1536` 或 `1024x1024`；16:9 页面素材先选 `1536x1024`，再按计划的 crop contract 处理。PPT 素材调用显式传 `watermark=false`。

生成成功后，父级在素材阶段完成来源登记与分组，不等 `review-prep` 报缺项再补。保留工具实际返回的文件与原始 prompt；将文件落实到 `DECK_DIR/assets/` 下的正式路径，`ASSET_PATH` 使用 `assets/<真实文件名>`，不是绝对路径，也不是 `assets/generated/<file>`。`ASSET_ID` 与 `GROUP_ID` 使用素材计划中已经确定的真实 ID，不拿页码或临时编造的组名代替。原始 prompt 原样作为参数传入，不在收尾猜写。`GENERATOR_MODEL` 只使用本次执行明确暴露的实际模型名，不能由用户指定值、默认示例或自填 metadata 推断；服务未暴露时留空，命令将记录 `unknown (Box-Agent generate_image)`，诚实标记未知，不为此搜索配置或猜写模型名。

同一批现有命令可在一次 bash 中串行执行，保留失败退出码；每组所有图片登记/分配完成后，只生成一次素材联系表。例如已取到一张素材时：

```bash
python "<SKILL_ROOT>/scripts/deck.py" asset-register "$DECK_DIR" \
  --path "$ASSET_PATH" --origin generated \
  --generator-model "${GENERATOR_MODEL:-unknown (Box-Agent generate_image)}" --prompt "$ORIGINAL_PROMPT" &&
python "<SKILL_ROOT>/scripts/deck.py" asset-assign "$DECK_DIR" \
  --path "$ASSET_PATH" --asset-id "$ASSET_ID" --group-id "$GROUP_ID" &&
python "<SKILL_ROOT>/scripts/deck.py" asset-contact "$DECK_DIR" --group-id "$GROUP_ID"
```

看图通过后才用既有 `asset-review --group-id ... --ready ...` 写 ready，再回填计划的实际路径与 crop contract；登记/分配成功不是素材验收。下载素材沿用 fetch 返回的已登记路径，不重复登记；附件使用原路径的 `--source-path`，派生图保留 `--parent-asset`，不伪填 generated 来源。

### Serper 搜图和真实图落地

若会话已有 schema 明确支持 `search_type="images"` 的 `web_search`，可先用它检索；否则父级使用 skill 自带的确定性入口，密钥只从 `SERPER_API_KEY` 环境变量读取：

```bash
python "<SKILL_ROOT>/scripts/serper_images.py" search --query '<查询>' --limit 6
python "<SKILL_ROOT>/scripts/serper_images.py" fetch --url '<图片直链>' --root "$DECK_DIR"
```

`fetch` 校验响应确为可解析位图，写入 `assets/`，并通过现有 `deck.py asset-register` 自动记录 `origin=downloaded` 与原始 URL。不得把密钥写进计划、日志、HTML、讲稿或 `assets/catalog.json`。

## 5. 父级渲染闭环

静态新建仍先完成 `deck.py prepare`、素材验收、catalog ready 状态与实际路径/crop 合同回填；`prepare` 不代替素材验收。随后由主 Agent 完成闭环：

1. 主 Agent 连续制作一批页面；实际委派的 Slide 则写完自己 `write_scope` 内的全部首稿，一次返回全部待渲染页码。
2. 父级运行 `render.py --batch`，使用成功回执 `images` 中本批实际 PNG，按 `next_instructions`（Slide §3）的首次检查或修复复验范围调用 `inspect_images(image_paths=[...], instruction=..., strategy="native")`。一次响应只提交本契约允许的一批，模型实际形成页码/问题结论后才继续；回执不是看图通过。
3. 有硬伤时一次汇总新鲜像素证据和精确问题，所有写页子任务返回后由主 Agent 集中修复；子任务未返回前不改其页面或共享 CSS。遵守根 Skill 规定的修复预算，不因执行者变化重置次数。
4. 制作期重渲复看完成后进入根 Skill 阶段 5，先执行下方 `review-prep`，再按成功回执 `next_instructions` 指向的 `subagents/review.md` 执行正式 Review。正式 Review 与最终验收是同一轮，不先独立 Review 一遍；修复与复验顺序只按该方法。最终全册像素、播放器、原账本合同和 Standard exporter 均保留；命令不代替 Review 或 Entry 验收，不因此新增子任务。

已有编辑仍由原 Slide/Review 子任务集中改文件、父级渲染与看图；本文不改变编辑委派方式。

```bash
python "<SKILL_ROOT>/scripts/deck.py" review-prep "$DECK_DIR" --expected <总页数>
```

返回 `prepared` / `qa: not-run` 只代表待审产物就绪，不是 PASS。使用 `images` 和 `review_contact` 的最终图片：首次正式诊断中性看图，修复复验按原问题与受影响区域检查，具体范围以 Review 为准；之后按需读诊断，不追加 `contact` 或 `build`。`review_ledger` 是待模型填写的原 `_trace/review-issues.md`，按 Review §5 写唯一 `## Final review contract` 后才能导出，不另建根目录 review.md。正式待审固定全册，不传 `--pages`；局部诊断仍用下面的指定页命令。最终看图后若又改了 HTML、CSS、字体或资源，必须重新待审与验收。同页修复前读取当前 HTML、计划及必要的 CSS/参考，一次汇总修改；相邻修改合并为连续区块，不相邻的修改可用多个必要的 `edit_file`，大改可用已有 `write_file` 完整提交，不把一次合并修复等同于只准一次工具调用。不根据猜测旧串反复试 edit，同页只保留一个写者。

渲染命令应单独执行以保留非零退出码。失败先读 `_trace/render-issues.json` 并修对应 HTML，再运行已有的指定页入口，例如：

```bash
python "<SKILL_ROOT>/scripts/render.py" --batch "$DECK_DIR" --pages 2,7
```

不要用 `| tail` 或后接 `echo` 覆盖渲染退出码，也不要原样重复渲染未修复的质量失败页。导出成功后，按 stdout JSON 的精确 `output` 路径检查文件；不要执行 `ls "$DECK_DIR/*.pptx"`（星号被引用不会展开）并因此重导出。导出失败不能自行把用户要求的 PPTX 改称 HTML 已交付。字体 manifest 的源字体映射用于 PPTX 文本；接收机器仍需安装源字体，浏览器 WOFF2 并未嵌入 PPTX。

工具名或权限失败先按本契约修正一次；相同失败再次出现就保存原始错误、调用参数和产物状态，返回 `blocked`，不得搜索或修改 Box-Agent 源码。


## Artifact delivery boundary

`deck.py prepare` declares the task directory as a working-file scope. Images generated there default to intermediate, even when `publish_artifact` is omitted. Do not set it to true for PPT illustrations. `asset-contact` sheets are internal QA files. `deck.py build` registers only the finished `present.html` and whole-deck overview; the PPTX exporter registers the finished PPTX. For extra files explicitly requested by the user, run `deck.py publish "$DECK_DIR" --path <relative-file>` after generating them. Do not publish individual assets or review sheets as a routine final step.
