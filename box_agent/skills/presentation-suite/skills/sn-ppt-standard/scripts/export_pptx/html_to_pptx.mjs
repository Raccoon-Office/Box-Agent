#!/usr/bin/env node
// HTML → PPTX 转换器 CLI
// 用法: node html_to_pptx.mjs --deck-dir <path> [--output <filename>] [--force]

import { existsSync, statSync, mkdirSync, writeFileSync } from 'node:fs';
import { resolve, basename, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/**
 * 首次运行时自动安装依赖（npm install + playwright chromium）。
 * 后续运行检测到 node_modules 和 chromium 已存在则跳过。
 */
async function ensureDependencies() {
  const nodeModules = resolve(__dirname, 'node_modules');
  const pptxgenMarker = resolve(nodeModules, 'pptxgenjs');
  const playwrightMarker = resolve(nodeModules, 'playwright');
  const echartsMarker = resolve(nodeModules, 'echarts');

  if (!existsSync(pptxgenMarker) || !existsSync(playwrightMarker) || !existsSync(echartsMarker)) {
    console.error('[setup] 首次运行，正在安装 npm 依赖...');
    try {
      execSync('npm install --omit=dev', { cwd: __dirname, stdio: ['ignore', 2, 2] });
    } catch (e) {
      throw new Error(`npm install failed: ${e.message}. Headless browser environment unavailable.`);
    }
  }

  // 浏览器检查改为 pickBrowserExe()：本地已有任何可用浏览器（期望版本或
  // 本地扫描回退）就直接使用，不触发下载；只有完全没有任何可用路径时才安装。
  const { pickBrowserExe } = await import('./lib/browser_picker.mjs');
  const exe = pickBrowserExe();
  if (exe && existsSync(exe)) {
    return;
  }
  console.error('[setup] 本地无可用 Chromium，正在安装 Playwright Chromium...');
  try {
    execSync('npx playwright install chromium', { cwd: __dirname, stdio: ['ignore', 2, 2] });
  } catch (e) {
    throw new Error(`Chromium installation failed: ${e.message}. Cannot install headless browser in this environment.`);
  }
}

// These helpers only use Node built-ins; browser modules load after setup.
const { ensureDeckPreconditions } = await import('./lib/cli_guards.mjs');
const { downloadRemoteImages } = await import('./lib/image_downloader.mjs');

function parseArgs(args) {
  const result = { deckDir: null, pagesDir: null, output: null, outputDir: null, force: false, batch: false, debug: false };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--deck-dir' && args[i + 1]) {
      result.deckDir = resolve(args[i + 1]);
      i++;
    } else if (args[i] === '--pages-dir' && args[i + 1]) {
      result.pagesDir = resolve(args[i + 1]);
      i++;
    } else if (args[i] === '--output' && args[i + 1]) {
      result.output = args[i + 1];
      i++;
    } else if (args[i] === '--output-dir' && args[i + 1]) {
      result.outputDir = resolve(args[i + 1]);
      i++;
    } else if (args[i] === '--force') {
      result.force = true;
    } else if (args[i] === '--batch') {
      result.batch = true;
      result.force = true;
    } else if (args[i] === '--debug') {
      result.debug = true;
    }
  }
  return result;
}

async function main() {
  if (process.argv.slice(2).some(arg => arg === '--help' || arg === '-h')) {
    console.log('Usage: node html_to_pptx.mjs --deck-dir <path> [--pages-dir <path>] [--output <filename>] [--output-dir <path>] [--force] [--batch] [--debug]');
    console.log('Without --pages-dir, use the single populated pages/ or slides/ directory; if both contain pages, choose explicitly. Legacy root page_*.html files are also supported.');
    return;
  }
  const args = parseArgs(process.argv.slice(2));
  if (!args.deckDir || !existsSync(args.deckDir) || !statSync(args.deckDir).isDirectory()) {
    throw new Error('必须指定存在的 --deck-dir 目录');
  }

  // 确保依赖安装（npm + playwright chromium）。
  try {
    await ensureDependencies();
  } catch (e) {
    const result = {
      status: "failed",
      success: false,
      reason: "export_dependencies_unavailable",
      detail: e.message,
      converted: 0,
      pages: 0,
    };
    console.log(JSON.stringify(result));
    process.exitCode = 1;
    return;
  }

  const { htmlFiles } = ensureDeckPreconditions(args.deckDir, {
    force: args.force,
    batch: args.batch,
    pagesDir: args.pagesDir,
  });

  // 下载与导出使用同一组页面，资源路径相对于各自 HTML 所在目录。
  if (!args.batch) {
    await downloadRemoteImages(args.deckDir, htmlFiles);
  }

  const { extractPages } = await import('./lib/dom_extractor.mjs');
  const { buildPptx } = await import('./lib/pptx_builder.mjs');

  console.error(`正在处理 ${htmlFiles.length} 个 HTML 页面...`);

  // DOM 提取
  console.error('步骤 1/2: 提取 DOM...');
  const pages = await extractPages(htmlFiles);

  // --debug: dump IR 到 <deck_dir>/_debug/<page>.ir.json，便于诊断转换问题。
  // 不污染 deck 根目录；每页一个文件，避免单文件巨大。
  if (args.debug && args.deckDir) {
    const debugDir = resolve(args.deckDir, '_debug');
    mkdirSync(debugDir, { recursive: true });
    for (const p of pages) {
      const baseName = basename(p.path).replace(/\.html?$/i, '');
      const irPath = resolve(debugDir, `${baseName}.ir.json`);
      try {
        writeFileSync(irPath, JSON.stringify({ path: p.path, ir: p.ir, error: p.error }, null, 2));
      } catch (e) {
        console.error(`[debug] 写 ${irPath} 失败: ${e.message}`);
      }
    }
    console.error(`[debug] IR dump 完成 → ${debugDir}/`);
  }

  // PPTX 构建：默认文件名与 deck_dir 目录名一致
  const outputFilename = args.output || (basename(args.deckDir) + '.pptx');
  const outputBase = args.outputDir || args.deckDir;
  mkdirSync(outputBase, { recursive: true });
  const outputPath = resolve(outputBase, outputFilename);
  console.error('步骤 2/2: 生成 PPTX...');
  const result = await buildPptx(pages, args.deckDir, outputPath);

  // 输出验证
  if (!existsSync(outputPath)) {
    console.error('错误: PPTX 文件未生成');
    process.exit(1);
  }

  const fileSize = statSync(outputPath).size;
  if (fileSize === 0) {
    console.error('错误: PPTX 文件大小为 0');
    process.exit(1);
  }

  // 成功输出（stdout）
  const sizeKB = (fileSize / 1024).toFixed(1);
  console.log(JSON.stringify({
    success: result.failCount === 0,
    output: outputPath,
    pages: result.totalPages,
    converted: result.successCount,
    failed: result.failCount,
    fileSize: `${sizeKB} KB`,
  }));

  if (result.failCount > 0) {
    const details = (result.failures || [])
      .map(item => `${item.path}: ${item.message}`)
      .join('\n- ');
    console.error(`错误: ${result.failCount} 个页面转换失败\n- ${details}`);
    process.exit(1);
  }

  process.exit(0);
}

main().catch(err => {
  console.error(`错误: ${err.message}`);
  process.exit(1);
});
