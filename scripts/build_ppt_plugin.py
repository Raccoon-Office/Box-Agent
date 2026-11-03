"""Build the independent PPT resource plugin without reading runtime user data.

The package sources live in plugins/ppt. The explicit --import-builtin command
is a one-time migration helper: it reads tracked resources, never an installed
user Skill tree. Ordinary builds only use the independent plugin directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins/ppt"
RESOURCE_ROOTS = ("pptx", "document-skills/pptx", "presentation-suite")
SKILL_IDS = {
    "pptx": "pptx",
    "ppt-fast": "ppt_fast",
    **{f"sn-ppt-{name}": f"sn_ppt_{name}"
       for name in ("entry", "story", "standard", "dazzle", "tools", "doctor")},
}
EXCLUDED_PARTS = {"node_modules", "__pycache__", ".git", ".pytest_cache", ".DS_Store"}
INVENTORY = "package-files.json"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_files(package_root: Path) -> list[Path]:
    """Return the publishable files, refusing links and generated dependencies."""
    files = []
    for path in sorted(package_root.rglob("*")):
        relative = path.relative_to(package_root)
        if EXCLUDED_PARTS.intersection(relative.parts):
            raise ValueError(f"Local cache/dependency is not a plugin resource: {relative}")
        if path.is_symlink():
            raise ValueError(f"Plugin resources cannot be symlinks: {relative}")
        if path.is_file():
            if path.suffix in {".pyc", ".pyo"}:
                raise ValueError(f"Compiled cache is not a plugin resource: {relative}")
            files.append(path)
        elif not path.is_dir():
            raise ValueError(f"Unsupported plugin resource: {relative}")
    return files


def bind_skill_references(markdown: str) -> str:
    """Bind explicit Skill-name references, without rewriting resource paths.

    Frontmatter names remain upstream display/catalog metadata. Loader ownership
    and public aliases come from the Manifest and the deployment respectively.
    """
    if markdown.startswith("---\n"):
        frontmatter, separator, body = markdown[4:].partition("\n---\n")
        if not separator:
            raise ValueError("Missing closing Skill frontmatter")
        prefix = "---\n" + frontmatter + separator
    else:
        prefix, body = "", markdown
    for name, contribution_id in SKILL_IDS.items():
        target = "{{skill:" + contribution_id + "}}"
        # These are exact references, not substrings of file/directory names.
        if name != "pptx":
            body = body.replace(f"`{name}`", f"`{target}`")
        else:
            # `pptx` is also an output-format enum. Bind only the explicit
            # public Skill reference, leaving format/schema examples intact.
            body = body.replace("公共 `pptx` 入口", f"公共 `{target}` 入口")
        body = re.sub(
            r'(skill_name\s*[=:]\s*["\'])' + re.escape(name) + r'(["\'])',
            lambda match: match[1] + target + match[2], body,
        )
        body = body.replace(f'"skill_name": "{name}"', f'"skill_name": "{target}"')
    return prefix + body


def import_builtin_resources(repository: Path, package_root: Path) -> None:
    """Copy the complete tracked PPT closure once, then apply explicit overlays."""
    destination = package_root / "skills"
    if destination.exists():
        raise ValueError(f"Refusing to overwrite existing plugin sources: {destination}")
    skill_root = repository / "box_agent/skills"
    prefixes = [str((skill_root / root).relative_to(repository)) for root in RESOURCE_ROOTS]
    listing = subprocess.check_output(
        ["git", "ls-files", "-z", "--", *prefixes], cwd=repository,
    ).decode().split("\0")
    files = [Path(relative) for relative in listing if relative]
    if not files:
        raise ValueError("No tracked PPT resources found")
    for relative in files:
        source = repository / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Expected a regular tracked resource: {relative}")
        if EXCLUDED_PARTS.intersection(relative.parts) or source.suffix in {".pyc", ".pyo"}:
            raise ValueError(f"Unexpected tracked cache/dependency: {relative}")
        target = destination / source.relative_to(skill_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if source.stat().st_mode & stat.S_IXUSR else 0o644)

    for root in RESOURCE_ROOTS:
        for path in (destination / root).rglob("*.md"):
            original = path.read_text(encoding="utf-8")
            transformed = bind_skill_references(original)
            if transformed != original:
                path.write_text(transformed, encoding="utf-8")

    entry = destination / "pptx/SKILL.md"
    text = entry.read_text(encoding="utf-8")
    text = text.replace(
        "## 确定模式\n",
        "## 插件默认偏好\n\n"
        "宿主会随本 Skill 提供带配置修订的实例参数。`language` 为 `zh-CN` 或 `en-US` 时，"
        "在用户未指定语言时作为演示正文默认语言；`auto` 沿用任务语言。"
        "`preferred_mode` 为 `fast` 或 `design` 时，只在需求与交付约束允许时用作模式推荐偏好；"
        "`ask` 按下文原规则推荐。配置中的模式偏好不是用户对本轮的确认，不能跳过选择卡。"
        "用户本轮明确要求和当前任务已确认的选择优先，恢复已有任务时不覆盖 task_pack。"
        "这些参数不改变工具权限，也不能授权外部发布或删除。\n\n## 确定模式\n",
    )
    entry.write_text(text, encoding="utf-8")

    env = destination / "presentation-suite/skills/sn-ppt-tools/scripts/env_config.py"
    text = env.read_text(encoding="utf-8")
    start = text.index("def resolve_env_path()")
    end = text.index("\n\ndef _parse_value", start)
    text = text[:start] + '''def resolve_env_path() -> tuple[Path | None, str]:
    """Only an explicitly configured file may supplement the host environment."""
    explicit = os.environ.get("SN_PPT_ENV_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve(), "SN_PPT_ENV_FILE"
    return None, "host_process"
''' + text[end:]
    text = text.replace(
        "    path, source = resolve_env_path()\n    if not path.is_file():",
        "    path, source = resolve_env_path()\n"
        "    if path is None:\n"
        "        return EnvLoadResult(path=\"\", source=source, exists=False)\n"
        "    if not path.is_file():",
    )
    env.write_text(text, encoding="utf-8")
    tools_guide = destination / "presentation-suite/skills/sn-ppt-tools/SKILL.md"
    text = tools_guide.read_text(encoding="utf-8")
    text = text.replace(
        "1. 设置了 `SN_PPT_ENV_FILE` 时读取该文件；\n"
        "2. OpenClaw 读取 `~/.openclaw/.env`；\n"
        "3. 其他环境默认读取 `~/.hermes/.env`。",
        "仅在部署者显式设置 `SN_PPT_ENV_FILE` 时读取指定文件；否则只使用宿主进程"
        "已经提供的环境变量，不扫描其他应用的配置。优先使用宿主原生媒体工具。"
        "配置页不接收密钥，不把密钥写入 Skill 参数。",
    )
    tools_guide.write_text(text, encoding="utf-8")
    apply_host_runtime_overlays(package_root)
    refresh_suite_provenance(package_root)


def apply_host_runtime_overlays(package_root: Path) -> None:
    """Keep resource snapshots immutable and use only explicitly supplied runtime."""
    skills = package_root / "skills"
    suite = skills / "presentation-suite"
    for path in skills.rglob("*.md"):
        if path.name == "SKILL.md":
            continue
        text = path.read_text(encoding="utf-8")
        if "{{skill:" not in text:
            continue
        text = re.sub(r'(["\'])\{\{skill:([a-z0-9_]+)\}\}\1',
                      lambda match: "skill_names." + match[2], text)
        text = re.sub(r"\{\{skill:([a-z0-9_]+)\}\}",
                      lambda match: "skill_names." + match[1], text)
        text = ("本文件中的 `skill_names.<id>` 指当前 Skill 入口由宿主注入的实例名称映射；"
                "调用 `get_skill` 时传入该键对应的字符串值，不把映射表达式作为字面量发送。"
                "映射缺失时返回当前入口获取，不使用其他实例的同名 Skill。\n\n" + text)
        path.write_text(text, encoding="utf-8")
    guide = suite / "skills/sn-ppt-tools/SKILL.md"
    text = guide.read_text(encoding="utf-8").replace(
        "四个脚本每次启动都会自动加载一个用户级 `.env`：",
        "四个脚本使用宿主已提供的媒体配置：",
    )
    guide.write_text(text, encoding="utf-8")
    guide = suite / "skills/sn-ppt-doctor/SKILL.md"
    text = guide.read_text(encoding="utf-8").replace(
        "PPT 内置工具与 Doctor 自动读取：\n\n"
        "- Hermes：`~/.hermes/.env`\n"
        "- OpenClaw：`~/.openclaw/.env`\n"
        "- 自定义：先设置 `SN_PPT_ENV_FILE=/absolute/path/to/file`",
        "优先使用宿主原生媒体工具及其已有配置。PPT 附带脚本与 Doctor 仅使用宿主进程环境；"
        "部署者显式设置 `SN_PPT_ENV_FILE=/absolute/path/to/file` 时，才读取指定配置文件。"
        "不扫描其他应用目录，插件 HTML 配置页也不收集密钥。",
    ).replace(
        "用户只需编辑一次文件，不需要每次启动 Hermes 前执行 `export`。Doctor 输出实际读取的文件、",
        "部署配置由宿主管理。Doctor 输出显式读取的文件（未指定时为空）、",
    ).replace("显示应编辑的 `.env` 路径和缺失项", "显示缺失项及部署者显式指定的配置文件路径")
    text = text.replace("HTML -> PPTX 所需的 npm、`pptxgenjs`、`echarts`、Node Playwright 是否可解析",
                        "HTML -> PPTX 所需的 `pptxgenjs`、`jszip`、Node Playwright 是否可解析（ECharts 由包内资源提供）")
    guide.write_text(text, encoding="utf-8")
    guide = suite / "skills/sn-ppt-entry/SKILL.md"
    text = guide.read_text(encoding="utf-8").replace(
        "Hermes/OpenClaw 实际读取的用户级 `.env`、缺失项和配置模板。",
        "宿主进程已有配置、显式指定的 `SN_PPT_ENV_FILE`、缺失项和配置模板。",
    )
    guide.write_text(text, encoding="utf-8")
    guide = skills / "document-skills/pptx/references/presentation-system.md"
    text = guide.read_text(encoding="utf-8").replace(
        "node box_agent/skills/document-skills/pptx/scripts/",
        'node "$SKILL_ROOT/scripts/',
    )
    # Keep these maintenance commands relocatable as well as runtime scripts.
    text = text.replace("build_layout_manifest.js\n", 'build_layout_manifest.js"\n')
    text = text.replace("render_theme_matrix.js --out", 'render_theme_matrix.js" --out')
    guide.write_text(text, encoding="utf-8")

    exporter = suite / "skills/sn-ppt-standard/scripts/export_pptx"
    cli = exporter / "html_to_pptx.mjs"
    text = cli.read_text(encoding="utf-8").replace(
        "import { execSync } from 'node:child_process';",
        "import { createRequire } from 'node:module';",
    )
    start = text.index("/**\n * 首次运行时自动安装依赖") if "首次运行时自动安装依赖" in text else -1
    if start >= 0:
        end = text.index("// These helpers", start)
        text = text[:start] + '''// Dependencies belong to the host runtime, never to the immutable plugin package.
async function ensureDependencies() {
  const require = createRequire(import.meta.url);
  try {
    require.resolve('pptxgenjs');
    require.resolve('jszip');
    require.resolve(process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH || 'playwright');
  } catch (error) {
    throw new Error(`PPT host dependencies unavailable: ${error.message}. Prepare ppt-host-v1 in the host environment; the plugin does not install dependencies.`);
  }
  const { pickBrowserExe } = await import('./lib/browser_picker.mjs');
  if (!existsSync(new URL('../../assets/vendor/echarts.min.js', import.meta.url))) {
    throw new Error('PPT bundled ECharts resource is missing; reinstall the plugin package.');
  }
  const exe = pickBrowserExe();
  if (!exe || !existsSync(exe)) {
    throw new Error('PPT host Chromium unavailable. Prepare the host browser before enabling this plugin.');
  }
}

''' + text[end:]
    text = text.replace("require.resolve('echarts/dist/echarts.min.js');", "require.resolve('jszip');")
    text = text.replace("// 确保依赖安装（npm + playwright chromium）。", "// 仅检查已经准备的宿主依赖，不在插件目录安装。")
    if "PPT bundled ECharts resource is missing" not in text:
        text = text.replace("  const exe = pickBrowserExe();", "  if (!existsSync(new URL('../../assets/vendor/echarts.min.js', import.meta.url))) {\n"
                            "    throw new Error('PPT bundled ECharts resource is missing; reinstall the plugin package.');\n"
                            "  }\n  const exe = pickBrowserExe();")
    cli.write_text(text, encoding="utf-8")
    builder = exporter / "lib/pptx_builder.mjs"
    text = builder.read_text(encoding="utf-8").replace(
        "import PptxGenJS from 'pptxgenjs';",
        "import { createRequire } from 'node:module';\n"
        "const PptxGenJS = createRequire(import.meta.url)('pptxgenjs');",
    )
    builder.write_text(text, encoding="utf-8")
    postprocess = exporter / "lib/postprocess_pptx.mjs"
    text = postprocess.read_text(encoding="utf-8").replace(
        "import JSZip from 'jszip';",
        "import { createRequire } from 'node:module';\n"
        "const JSZip = createRequire(import.meta.url)('jszip');",
    )
    postprocess.write_text(text, encoding="utf-8")
    extractor = exporter / "lib/dom_extractor.mjs"
    text = extractor.read_text(encoding="utf-8").replace(
        "const bundledEchartsPath = require.resolve('echarts/dist/echarts.min.js');",
        "const bundledEchartsPath = new URL('../../../assets/vendor/echarts.min.js', import.meta.url);",
    ).replace("import { createRequire } from 'node:module';\n", "").replace(
        "const require = createRequire(import.meta.url);\n", "")
    extractor.write_text(text, encoding="utf-8")
    doctor = suite / "skills/sn-ppt-doctor/ppt_doctor/check_environment.py"
    text = doctor.read_text(encoding="utf-8").replace(
        "        f'npm --prefix \"{export_dir}\" install --omit=dev',\n"
        "        f'npm --prefix \"{export_dir}\" exec playwright install chromium',",
        "        'Prepare ppt-host-v1 dependencies in the host runtime, not the plugin package.',\n"
        "        'Provide NODE_PATH, BOX_AGENT_PLAYWRIGHT_MODULE_PATH and a prepared host Chromium.',",
    ).replace(
        "result.packages[packageName] = require.resolve(packageName, { paths: [process.cwd()] });",
        "const target = packageName === 'playwright' ? (process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH || packageName) : packageName;\n"
        "      result.packages[packageName] = require.resolve(target, { paths: [process.cwd()] });",
    ).replace(
        "  const { chromium } = require(result.packages.playwright);\n"
        "  const executable = chromium.executablePath();",
        "  const { pathToFileURL } = require('node:url');\n"
        "  const { resolve } = require('node:path');\n"
        "  const { chromium, pickBrowserExe } = await import(pathToFileURL(resolve('lib/browser_picker.mjs')).href);\n"
        "  const executable = pickBrowserExe();",
    ).replace(
        "result.chromium_present = fs.existsSync(executable);",
        "result.chromium_present = Boolean(executable && fs.existsSync(executable));",
    ).replace(
        "browser = await chromium.launch({\n      headless: true,",
        "browser = await chromium.launch({\n      executablePath: executable,\n      headless: true,",
    )
    text = text.replace('required_packages = ["pptxgenjs", "playwright", "echarts"]',
                        'required_packages = ["pptxgenjs", "playwright", "jszip"]')
    text = text.replace('required_packages = sorted(manifest.get("dependencies", {}).keys())',
                        'required_packages = sorted((set(manifest.get("dependencies", {})) - {"echarts"}) | {"jszip"})')
    doctor.write_text(text, encoding="utf-8")


def refresh_suite_provenance(package_root: Path) -> None:
    """Retain upstream provenance while recording the plugin integration overlay."""
    suite = package_root / "skills/presentation-suite"
    source_path = suite / "source.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    overlays = source.setdefault("overlays", [])
    for overlay in ("external-plugin-skill-references-v1", "explicit-ppt-env-only-v1", "immutable-plugin-host-runtime-v1"):
        if overlay not in overlays:
            overlays.append(overlay)
    for relative, record in source["files"].items():
        data = (suite / relative).read_bytes()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            data = data.replace(b"\r\n", b"\n")
        record["sha256"] = hashlib.sha256(data).hexdigest()
    source_path.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_inventory(package_root: Path) -> dict:
    manifest = yaml.safe_load((package_root / "plugin.yaml").read_text(encoding="utf-8"))
    records = {}
    for path in package_files(package_root):
        relative = path.relative_to(package_root).as_posix()
        if relative == INVENTORY:
            continue
        records[relative] = {"sha256": _file_digest(path), "bytes": path.stat().st_size}
    inventory = {"format_version": 1, "plugin_id": manifest["id"],
                 "plugin_version": manifest["version"], "files": records}
    (package_root / INVENTORY).write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return inventory


def build_archive(package_root: Path, output: Path) -> dict:
    inventory = write_inventory(package_root)
    output = output.resolve()
    if output.is_relative_to(package_root.resolve()):
        raise ValueError("Archive output must be outside the package sources")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in package_files(package_root):
            relative = path.relative_to(package_root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644)) << 16
            archive.writestr(info, path.read_bytes())
    return {"path": str(output), "sha256": _file_digest(output),
            "files": len(inventory["files"]) + 1, "bytes": output.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, default=PLUGIN)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--import-builtin", action="store_true")
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    if args.import_builtin:
        import_builtin_resources(REPO, args.package_root)
    if args.inventory_only:
        inventory = write_inventory(args.package_root)
        print(json.dumps({"files": len(inventory["files"]), "plugin_id": inventory["plugin_id"]}))
        return
    output = args.output or REPO / "workspace/plugin-dist/ppt-plugin-1.0.0.zip"
    print(json.dumps(build_archive(args.package_root, output), ensure_ascii=False))


if __name__ == "__main__":
    main()
