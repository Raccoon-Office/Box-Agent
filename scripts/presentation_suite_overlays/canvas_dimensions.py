"""One explicit canvas contract for planning, rendering, player and PPTX."""


def replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"SN canvas overlay needs review: {old[:100]!r}")
    return text.replace(old, new, 1)


def apply(relative: str, data: bytes) -> bytes:
    if not relative.endswith((".py", ".mjs", ".md")):
        return data
    text = data.decode("utf-8")
    if relative == "skills/sn-ppt-standard/scripts/deck.py":
        text = replace(text, "def _init_workspace(root: Path) -> None:",
                       "def _init_workspace(root: Path, width=None, height=None) -> None:")
        text = replace(text, '    created = False\n    if not target.exists():',
                       '''    if (width is None) != (height is None) or (width is not None and (width <= 0 or height <= 0)):
        raise ValueError("--width and --height must both be positive integers")
    if target.exists() and width is not None and _detect_canvas(root) != (width, height):
        raise ValueError("existing base.css has different canvas dimensions; edit the workspace CSS explicitly")
    created = False
    if not target.exists():''')
        text = replace(text, '        data = template.read_bytes()', '''        data = template.read_bytes()
        if width is not None:
            css = data.decode("utf-8")
            for name, value in (("--canvas-w", width), ("--canvas-h", height)):
                css, count = re.subn(rf"({name}:\\s*)[0-9.]+px", rf"\\g<1>{value}px", css)
                if count != 1:
                    raise ValueError(f"base template must define {name} exactly once")
            data = css.encode("utf-8")''')
        text = replace(text, '        if name in {"sync", "prepare", "contact", "build", "audit"}:',
                       '''        if name == "init":
            command.add_argument("--width", type=int)
            command.add_argument("--height", type=int)
        if name in {"sync", "prepare", "contact", "build", "audit"}:''')
        text = replace(text, '_init_workspace(Path(args.root).resolve())',
                       '_init_workspace(Path(args.root).resolve(), args.width, args.height)')
        text = replace(text, '\n                      "created": created, "qa": "not-run"}',
                       '\n                      "created": created, "canvas": list(_detect_canvas(root)), "qa": "not-run"}')
    elif relative == "skills/sn-ppt-standard/scripts/render.py":
        text = text.replace("[宽=1600] [高=900]", "[宽] [高]（默认读取工作区 base.css）")
        text = replace(text, 'def render_batch(root, pages=None, width=1600, height=900):',
                       'def render_batch(root, pages=None, width=None, height=None):')
        text = replace(text, '    root = os.path.abspath(root)\n    wanted = None',
                       '''    root = os.path.abspath(root)
    from deck import _detect_canvas
    if (width is None) != (height is None):
        raise ValueError("--width and --height must be supplied together")
    width, height = _detect_canvas(root) if width is None else (width, height)
    if width <= 0 or height <= 0:
        raise ValueError("canvas dimensions must be positive")
    wanted = None''')
        text = replace(text, '    w = int(sys.argv[3]) if len(sys.argv) > 3 else 1600\n    h = int(sys.argv[4]) if len(sys.argv) > 4 else 900',
                       '''    from deck import _detect_canvas
    canvas_root = os.path.dirname(html)
    if os.path.basename(canvas_root) == "slides":
        canvas_root = os.path.dirname(canvas_root)
    w, h = _detect_canvas(canvas_root)
    if len(sys.argv) > 3:
        if len(sys.argv) != 5:
            raise ValueError("supply both width and height")
        w, h = int(sys.argv[3]), int(sys.argv[4])
    if w <= 0 or h <= 0:
        raise ValueError("canvas dimensions must be positive")''')
        for dimension, default in (("width", 1600), ("height", 900)):
            text = replace(text, f'parser.add_argument("--{dimension}", type=int, default={default})',
                           f'parser.add_argument("--{dimension}", type=int, help="override together; default reads base.css canvas")')
        text = replace(text, '"images": images, "deck_dir": os.path.realpath(root),',
                       '"images": images, "canvas": {"width": width, "height": height, "device_scale": 2}, "deck_dir": os.path.realpath(root),')
    elif relative == "skills/sn-ppt-standard/scripts/export_pptx/lib/pptx_builder.mjs":
        text = replace(text, "  pptx.defineLayout({ name: 'HTML_SLIDE', width: 10, height: 5.625 });",
                       '''  const canvas = exportPages.find(page => page.ir)?.ir;
  const canvasWidth = canvas?.canvasWidth || 1280;
  const canvasHeight = canvas?.canvasHeight || 720;
  if (!(canvasWidth > 0 && canvasHeight > 0) || exportPages.some(page => page.ir &&
      ((page.ir.canvasWidth || 1280) !== canvasWidth || (page.ir.canvasHeight || 720) !== canvasHeight))) {
    throw new Error('All pages must use the same positive canvas dimensions.');
  }
  pptx.defineLayout({ name: 'HTML_SLIDE', width: 10, height: 10 * canvasHeight / canvasWidth });''')
        text = text.replace('w: 10, h: 5.625', 'w: 10, h: 10 * _currentCanvasH / _currentCanvasW')
        text = replace(text, 'const SLIDE_W = 10, SLIDE_H = 5.625;',
                       'const SLIDE_W = 10, SLIDE_H = 10 * _currentCanvasH / _currentCanvasW;')
    elif relative in {"skills/sn-ppt-standard/SKILL.md", "skills/sn-ppt-standard/references/box-agent-tool-contract.md",
                      "skills/sn-ppt-standard/subagents/slide.md"}:
        if relative == "skills/sn-ppt-standard/SKILL.md":
            text = replace(text, "生成每页独立 HTML（1600×900）", "生成每页独立 HTML（默认 1600×900；用户指定尺寸优先）")
        text += '''

## Canvas dimensions / 画布尺寸执行约定

- 用户指定像素尺寸优先；1600×900 仅为未指定时的默认值。1920×1080 是精确 CSS 画布要求，不能用“同比例”代替。
- 在已有的绝对任务目录中执行一次 `python "<SKILL_ROOT>/scripts/deck.py" init "$DECK_DIR" --width 1920 --height 1080`（数值替换为用户要求）。不指定尺寸则省略两个参数。已有 CSS 不被 init 改写；变更尺寸须明确编辑工作区 `base.css`。
- `base.css` 中的 `--canvas-w` / `--canvas-h` 是本任务唯一尺寸来源；每页 `.slide` 使用它们，统一全册。`prepare`、`review-prep`、批量 `render.py --batch "$DECK_DIR"` 自动沿用尺寸，不需要反复查脚本源码，也不要覆盖 Skill 安装文件。
- 播放器沿用 CSS 画布并等比适配窗口；Standard PPTX exporter 从实际 HTML 画布映射到相同纵横比的英寸页面。CSS 像素、截图像素和 PPTX 英寸是不同单位：PNG 为 2 倍设备像素（1920×1080 画布截图为 3840×2160），PPTX 无固定像素分辨率。
- 先按此约定 init、规划并写页面，成功后进入既有渲染与验收流程；只有工具返回具体失败才读对应诊断，不为尺寸猜测反复搜索实现。
'''
    return text.encode("utf-8")
