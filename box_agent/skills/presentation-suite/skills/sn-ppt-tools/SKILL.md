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

## 离线包执行边界

本分支专供离线包，保留快速、设计静态和设计动态模式。只有模型推理和本地文件/工具
可用；不调用网页搜索、图片搜索、生图、远程下载或依赖安装，不做连通性探测或重试。
恢复任务也直接将联网能力记为 unavailable，不继续历史待执行的联网步骤。
只用用户提供的本地材料、图片与随包字体/库。外部事实缺口标为“待补充（离线未核验）”，
不伪造引用、数据或真实对象图片。没有图片时重排为文字、图表、SVG/Canvas 与本地形状，
保留并披露明确未满足的图片需求，不留下破图或虚假完成状态。不得引入 CDN、远程字体、
脚本、样式或图片；引用链接只是文字证据，不抓取。此边界优先于下文历史联网操作说明，
并须原样交接给每个子任务；独立推理角色仍可使用同一宿主模型和本地工具。
视觉检查只能使用现有模型的图像输入能力；不可用时记录 visual_unverified，不调用代理
视觉服务、不配置其他模型、不重试。实际文件、页数、内容、渲染与资源检查照常执行。


本离线包禁用这套联网备用工具。不要调用 web_search.py、image_search.py、fetch_image.py、
image_generate.py，不读取或配置 API key。直接读取 `references/capability-policy.md`
并继续本地材料流程；不把能力缺失误认为需要重试的服务故障。
