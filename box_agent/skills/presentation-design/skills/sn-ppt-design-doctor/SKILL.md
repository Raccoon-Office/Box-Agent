---
name: sn-ppt-design-doctor
description: 按需诊断 SN PPT 的 XML 校验、PPTX 导出、LibreOffice 成品预览、中文字体、附件解析和可选搜索/生图配置。
metadata:
  user_visible: false
  allow_override: false
  project: SenseNova-Skills-local
  tier: aux
  category: diagnostic
triggers:
  - "sn-ppt-design-doctor"
  - "ppt 体检"
---

# SN PPT 本地体检

SN PPT 统一由 XML 生成 PPTX。本技能在用户要求或实际故障时运行，不作为每次生成的前置步骤。

```bash
python "<SKILL_ROOT>/ppt_doctor/check_environment.py"
```

默认不联网、不修改配置，也不使用浏览器。在临时目录调用 `sn-ppt-dsl/scripts/dsl.py` 实际生成一页中文 PPTX，再用同一个入口预览；运行时与字体选择均复用正式流程。退出后删除临时产物，不改用户演示文稿。

## 如何读取结果

- `workspace_writable`：工作目录可写性。
- `pypdf`、`python_docx`：附件解析依赖，缺失只影响对应文件类型。
- `xml_pptx.build`：`available` 表示真实 XML 校验及 PPTX 构建成功，涵盖 Python、Node、本地 npm 依赖和编译器。
- `xml_pptx.preview`：`available` 表示实际 PPTX 经 LibreOffice 转 PDF、Poppler 转 PNG，并通过中文字保真检查。预览依赖缺失时可以交付已有 PPTX，但必须说明未完成视觉验收。
- `visual_review=not_performed`：程序没有人工看图，不代表版式已经完成视觉检查。
- `bundled_media`：内置工具的配置事实。`configured` 不等于远端调用已验证。

不再检查其他生成流程的依赖。导出失败时修复 XML 编译链路，不改用其他生成器。

## 可选媒体配置

内置工具与 Doctor 自动读取用户级 `.env`：`SN_PPT_ENV_FILE` 明确指定时用该文件；OpenClaw 使用 `~/.openclaw/.env`，Hermes 使用 `~/.hermes/.env`。已有进程环境变量优先。只输出实际配置文件路径、变量名和缺失字段，不显示密钥，不自动改 `.env`。

已有通用配置可以直接复用：

- 搜索/搜图：`SN_PPT_SEARCH_API_KEY` 或 `SERPER_API_KEY`。
- 生图：`SN_IMAGE_GEN_BASE_URL`、`SN_IMAGE_GEN_API_KEY`、`SN_IMAGE_GEN_MODEL`；也可使用 `SN_PPT_IMAGE_GEN_URL`、`SN_PPT_IMAGE_GEN_API_KEY`、`SN_PPT_IMAGE_GEN_MODEL` 覆盖。
- 生图仅作为 XML 页面中的素材来源。无图版式不要求配置这些变量。

搜索探测只在用户要求测试远端接口时执行：

```bash
python "<SKILL_ROOT>/ppt_doctor/check_environment.py" --probe-search
```

真实生图会发送请求；用户明确要求测试生图时执行：

```bash
python "<SKILL_ROOT>/ppt_doctor/check_environment.py" \
  --probe-image-generation --probe-output-dir "/absolute/existing/directory"
```

脚本无法读取宿主工具注册表。运行此技能的 Agent 另行区分原生工具的 `absent`、`present_unverified`、`misconfigured`、`available` 和 `failed`；工具名称存在不能算可用。原生能力经过配置检查或真实调用验证后优先，否则按同级 `sn-ppt-design-tools/references/capability-policy.md` 使用内置能力或无图版式。
