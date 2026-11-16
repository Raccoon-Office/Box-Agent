# Understand Anything 代码图谱

Box-Agent 使用 Understand Anything 作为纳入版本管理的架构索引，用于代码导航、归属追踪、
依赖检查和新成员引导。图谱可以加快定位，但源码、聚焦测试、日志和运行探针仍然是
事实来源。

## 仓库范围

共享分析范围由
[`../.understand-anything/.understandignore`](../.understand-anything/.understandignore)
定义。当前配置包含核心运行时、ACP 适配层、工具、配置、示例、文档、测试，以及所有
仓库自有 Skill 的入口、实现、参考文档和配置。测试节点与 `tested_by` 关系帮助定位
已有验证；纳入图谱不表示测试已执行或通过。第三方依赖、vendor、字体、媒体、二进制、
workspace、虚拟环境和生成产物仍然排除，包括 PPTX 的 vendored runtime 与生成 bundle。

Git 中只应提交以下共享文件：

- `.understand-anything/.understandignore`
- `.understand-anything/config.json`
- [`.understand-anything/knowledge-graph.json`](../.understand-anything/knowledge-graph.json)
- `.understand-anything/meta.json`
- `.understand-anything/fingerprints.json`

纳入版本管理的 `knowledge-graph.json` 让贡献者在克隆仓库后即可浏览同一份架构快照。
`meta.json` 记录刷新时间、被分析的源码提交、格式版本和文件数量；
`fingerprints.json` 为 plugin 提供结构对比基线，使后续运行能够区分未变化、仅外观变化
和结构变化，避免每次重新分析整个仓库。

这三个生成文件应作为同一份刷新基线一起维护。不要手工编辑，也不要只更新
`meta.json`：plugin 需要配套 fingerprint 才能可靠执行增量更新。不要提交
`last-run-summary.json`、intermediate、trash、dashboard token 或缓存；这些仍属于
本地刷新状态，由 `.gitignore` 忽略。

## 浏览共享图谱

克隆仓库后，在已安装 Understand Anything plugin 的客户端中运行
`/understand-dashboard`。Dashboard 会直接读取仓库中的
`.understand-anything/knowledge-graph.json`，请打开 plugin 输出的带 token URL。
其他支持该图谱格式的工具也可以直接读取 JSON，无需先执行刷新。

快速检查新鲜度时，先比较 `meta.json.gitCommitHash` 与图谱中的
`project.gitCommitHash`，再检查该基线之后的源码变化。执行 `/understand` 时，plugin
会自动使用 `fingerprints.json`；它不用于回答架构问题。

## 刷新流程

1. 调整图谱范围前，先检查 `.understand-anything/.understandignore`。
2. 图谱缺失、明显过期或分析范围变化时，在已安装 Understand Anything plugin 的
   客户端中运行 `/understand --full --language zh`；常规增量刷新使用
   `/understand`。
3. 确认验证结果没有 critical issue，并且每个已分析的文件级节点都只属于一个
   架构层。
4. 验证通过后，在同一个可审查改动中一起更新 `knowledge-graph.json`、`meta.json`
   和 `fingerprints.json`，不要手工编辑这些生成文件。
5. 在本地保留 `scan-result.json`，让后续增量运行可以复用确定性的文件清单。
6. 启动 dashboard 后，应打开 plugin 输出的带 token URL；仅访问本地服务裸地址
   无法通过访问校验。

刷新读取当前工作树。若包含尚未提交的文件，提交 hash 只表示基础提交，不能单独证明
快照与该提交完全一致；共享前应核对工作树改动及配套 fingerprints。不要为了刷新自动
拉取、覆盖或提交他人的工作。

入口文件、子系统边界、共享工具、ACP 协议、运行时打包或需要进入引导路径的文档
发生变化后，应刷新图谱。不影响图结构或阅读路线的小型源码改动无需立即刷新图谱。

## 验证与 Review

先用图谱定位可能相关的文件和关系，再通过源码阅读、`rg`、聚焦的
`uv run pytest`、日志或运行探针验证重要结论。应比较图谱记录的分析基线与后续源码
变化；当这些变化影响架构或阅读路线时，应明确标记图谱已过期。

图谱中的 `project.gitCommitHash` 表示被分析的源码基线。独立图谱提交通常紧跟在该
基线提交之后，因此这个 hash 指向图谱提交的父提交是正常现象。更新图谱时，应在 PR
的 Task、Proof、Risk 中列出验证统计与剩余 warning；如果共享范围或语言配置发生
变化，还应包含相关配置 diff 并说明预期覆盖范围。
