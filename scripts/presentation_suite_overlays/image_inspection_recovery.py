"""Preserve bounded image-inspection recovery in generated PPT instructions."""


REPLACEMENTS = {
    'skills/sn-ppt-standard/SKILL.md': (
        '   Review 停滞收口时允许补齐或更新的正式产物只有 `_trace/review-issues.md` 与 `_trace/content-fidelity.md`；运行时不得禁止写入最终验收合同明确要求的这两份文件，也不得在收口阶段允许继续修改页面。',
        '   Review 停滞收口时允许补齐或更新的正式产物只有 `_trace/review-issues.md` 与 `_trace/content-fidelity.md`；运行时不得禁止写入最终验收合同明确要求的这两份文件，也不得在收口阶段允许继续修改页面。`inspect_images` 超时按工具契约只重试一次降采样批次；再次失败立即记为 `visual_unverified`，转入确定性 QA 和带 warning 的降级交付，不继续消耗整轮任务预算。',
    ),
    'skills/sn-ppt-standard/references/box-agent-tool-contract.md': (
        '- `REQUEST_BODY_TOO_LARGE` 表示失败请求中的图片未被模型看到，不增加已覆盖清单，不得宣称本批或整册检查完成。保持原图质量，将失败批次缩小为 2 张、必要时 1 张后顺序重试；不得降低图片质量、跳过页面，也不得仅为绕过请求字节限制改用 `proxy`。单张仍超限时按待验收项读取相关高清局部，记录已检查区域及尚未检查区域；局部检查不能冒称整页已覆盖，只有原验收要求的区域与内容全部核验才标记页面完成。无法完成覆盖时保留未完成状态、错误与待检清单，如实报告阻塞。',
        '- `REQUEST_BODY_TOO_LARGE` 表示失败请求中的图片未被模型看到，不增加已覆盖清单，不得宣称本批或整册检查完成。保持原图质量，将失败批次缩小为 2 张、必要时 1 张后顺序重试；不得降低图片质量、跳过页面，也不得仅为绕过请求字节限制改用 `proxy`。单张仍超限时按待验收项读取相关高清局部，记录已检查区域及尚未检查区域；局部检查不能冒称整页已覆盖，只有原验收要求的区域与内容全部核验才标记页面完成。无法完成覆盖时保留未完成状态、错误与待检清单，如实报告阻塞。\n- `IMAGE_REQUEST_FAILED` 的 `category=timeout` 是视觉服务请求超时，不等于图片或页面损坏。对同一批最多自动重试一次；重试前把每张图降到 1024px 长边，并将批次限制为 2 张，必要时 1 张。仍超时、服务不支持图片或请求参数无效时，立即把该批标记为 `visual_unverified`，停止重复视觉请求。\n- 视觉降级的判定顺序固定为：先完成页面存在/非空、页数与顺序、HTML 自检、文本/占位符、必要资源与 PPTX 包结构等确定性检查；这些任一硬门失败仍阻塞交付。全部硬门通过后，视觉请求失败只记录 warning 和未检查页/区域清单，交付可用 HTML/PPTX，不把视觉结果写成“已通过”。\n- 检查范围由 Slide / Review 方法确定：首次诊断覆盖完整要求，修复复验沿用原问题并查回归，最终全册像素覆盖不能省；不把每次复验重开为一轮全册诊断。同一批服务超时最多一次降采样重试。达到 3 次 Review 或任一页组 2 次返修后停止返工；需恢复已有验证版时按 Review 方法重新待审并检查最终像素，不在最终看图之后另跑 build。未看完的范围保留 visual_unverified，不写 PASS；禁止因低置信度审美建议继续重渲染。',
    ),
}


def apply(relative: str, data: bytes) -> bytes:
    if relative not in REPLACEMENTS:
        return data
    old, new = REPLACEMENTS[relative]
    text = data.decode("utf-8")
    if text.count(old) != 1 or new in text:
        raise ValueError(f"Image inspection recovery overlay needs review: {relative}")
    return text.replace(old, new, 1).encode("utf-8")
