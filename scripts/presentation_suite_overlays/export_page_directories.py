"""Pinned six-file input patch for source-relative HTML-to-PPTX exports.

Line ranges refer to the generated bundle after existing host overlays. Exact
input/output hashes reject drift; this is not a general-purpose patch loader.
"""

import hashlib


_PATCHES = {
    'skills/sn-ppt-standard/scripts/export_pptx/html_to_pptx.mjs': {
        "before_sha256": '9b9b77355a12c6134be4950f90553da3e2a6bbba54fb204237756ff2faf7d880',
        "after_sha256": '4712a1a2c44ae74451b54c523d9bb7f9c3cf7e559dd1897397f370bf04de152a',
        "edits": (
            (80, 80, r'''    console.log('Without --pages-dir, use the single populated pages/ or slides/ directory; if both contain pages, choose explicitly. Legacy root page_*.html files are also supported.');
'''),
            (104, 109, ''),
            (114, 114, r'''
  // 下载与导出使用同一组页面，资源路径相对于各自 HTML 所在目录。
  if (!args.batch) {
    await downloadRemoteImages(args.deckDir, htmlFiles);
  }
'''),
        ),
    },
    'skills/sn-ppt-standard/scripts/export_pptx/lib/cli_guards.mjs': {
        "before_sha256": '742b6cc859b6949c8a048fef7d3134950ef969a681bfd1d58987b909439ba88d',
        "after_sha256": '520147c4f4476b117f69c4e0348cdf85c3b7d1292c620d19c7d50408108b3188',
        "edits": (
            (0, 1, r'''import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
'''),
            (425, 434, r'''function listPageFiles(pagesDir, pattern = /^(?:page|slide)_\d+\.html$/) {
  if (!existsSync(pagesDir) || !statSync(pagesDir).isDirectory()) return [];
  return readdirSync(pagesDir)
    .filter(name => pattern.test(name) && statSync(resolve(pagesDir, name)).isFile())
    .sort()
    .map(name => resolve(pagesDir, name));
}

function selectDeckPages(deckDir, explicitDir) {
  if (explicitDir) {
    const pagesDir = resolve(explicitDir);
    if (!existsSync(pagesDir) || !statSync(pagesDir).isDirectory()) {
      throw new Error(`页面目录不存在或不是目录: ${pagesDir}`);
    }
    const htmlFiles = listPageFiles(pagesDir);
    if (htmlFiles.length === 0) {
      throw new Error(`页面目录中没有 page_*.html 或 slide_*.html 文件: ${pagesDir}`);
    }
    return { pagesDir, htmlFiles };
'''),
            (436, 439, r'''  const candidates = ['pages', 'slides'].map(name => {
    const pagesDir = resolve(deckDir, name);
    return { pagesDir, htmlFiles: listPageFiles(pagesDir) };
  }).filter(candidate => candidate.htmlFiles.length > 0);
  if (candidates.length > 1) {
    throw new Error('pages/ 和 slides/ 均包含页面，请用 --pages-dir 明确选择一个目录');
'''),
            (440, 440, r'''  if (candidates.length === 1) return candidates[0];
'''),
            (441, 473, r'''  // 兼容旧版根目录 page_*.html；就地读取，避免复制页面后破坏相对资源路径。
  const htmlFiles = listPageFiles(deckDir, /^page_\d+\.html$/);
  if (htmlFiles.length > 0) return { pagesDir: resolve(deckDir), htmlFiles };
  throw new Error(`未找到页面: ${deckDir} 的 pages/、slides/ 中没有 page_*.html 或 slide_*.html，根目录也没有 page_*.html；可用 --pages-dir 指定页面目录`);
'''),
            (487, 507, r'''  const { pagesDir, htmlFiles } = selectDeckPages(deckDir, opts.pagesDir);
'''),
        ),
    },
    'skills/sn-ppt-standard/scripts/export_pptx/lib/dom_extractor.mjs': {
        "before_sha256": '6068aa230ab7fd3cc88df61e340f8354dbe5324241f45d9960045170bbb89408',
        "after_sha256": 'bafc831f0f1f854e6561bce0db4a67e34bb3596e953e638e5b1f600a0b71ceaf',
        "edits": (
            (571, 572, r'''      // 浏览器按当前 HTML 的位置解析资源地址，避免构建器猜测 pages/ 目录。
      node.src = el.getAttribute('src') ? el.src : undefined;
'''),
        ),
    },
    'skills/sn-ppt-standard/scripts/export_pptx/lib/image_downloader.mjs': {
        "before_sha256": '955cd8b7184bd95dc2b292b3d58d52809f69605c057a07bdffb4fb45fa7d1b0a',
        "after_sha256": '1f1be06dcc2d8d49e96fcc8f17d4eef931c5efbccb45f4dc3c853c52e1ddaa62',
        "edits": (
            (0, 2, r'''import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve, basename, dirname, extname, isAbsolute, relative, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
'''),
            (50, 55, r'''export async function downloadRemoteImages(deckDir, htmlFiles) {
'''),
            (60, 62, r'''  for (const htmlPath of htmlFiles) {
'''),
            (85, 86, r'''        const relativePath = relative(dirname(htmlPath), localPath);
        // Windows 跨盘路径不能写成相对 URL；同时转义 HTML 引号和 CSS url() 括号。
        const imageUrl = isAbsolute(relativePath)
          ? pathToFileURL(localPath).href
          : relativePath.split(sep).map(encodeURIComponent).join('/');
        const rel = imageUrl.replace(/['()]/g, char => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
'''),
        ),
    },
    'skills/sn-ppt-standard/scripts/export_pptx/lib/pptx_builder.mjs': {
        "before_sha256": 'd479692d768638934a26430cd5f6db044109443b0fb677f2db655f5f1a12dfbd',
        "after_sha256": '483a2e51af62525437b22392dad1b3d9a664921142d131c84fa6261212b51be5',
        "edits": (
            (10, 11, r'''import { isAbsolute, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
'''),
            (1276, 1277, r'''    imgPath = fileURLToPath(imgPath);
  } else {
    try { imgPath = decodeURIComponent(imgPath); } catch { /* keep as-is */ }
'''),
            (1278, 1280, r'''  if (!isAbsolute(imgPath)) {
'''),
        ),
    },
    'skills/sn-ppt-standard/scripts/export_pptx/test/test_export_contract_regression.mjs': {
        "before_sha256": '339eb644642fb7e66151b737f9cf79f12cca1615afffb2c04d84ae8aec575e0e',
        "after_sha256": '81bd2b1b9a4e037bd19b193a9315f2f873010b325ac831173a7decf5b50985a2',
        "edits": (
            (2, 3, r'''import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
'''),
            (5, 6, r'''import { fileURLToPath, pathToFileURL } from 'node:url';
'''),
            (7, 7, r'''import { createServer } from 'node:http';
'''),
            (8, 9, r'''import { buildImageElement, buildPptx } from '../lib/pptx_builder.mjs';
import { ensureDeckPreconditions } from '../lib/cli_guards.mjs';
import { downloadRemoteImages } from '../lib/image_downloader.mjs';
import { extractPages } from '../lib/dom_extractor.mjs';

function temporaryDeck(t) {
  const root = mkdtempSync(join(tmpdir(), 'pptx-pages-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  writeFileSync(join(root, 'review.md'), 'Review complete.');
  return root;
}

function writePage(root, relativePath, html = '<html><body>Page</body></html>') {
  const target = join(root, relativePath);
  mkdirSync(join(target, '..'), { recursive: true });
  writeFileSync(target, html);
  return target;
}

for (const directory of ['pages', 'slides']) {
test(`discovers ${directory} without a directory flag and preserves page order`, t => {
  const root = temporaryDeck(t);
  const last = writePage(root, `${directory}/slide_10.html`);
  const first = writePage(root, `${directory}/slide_02.html`);
  writePage(root, `${directory}/slide_02.bak.html`);
  writePage(root, `${directory}/nested/slide_03.html`);
  assert.deepEqual(ensureDeckPreconditions(root).htmlFiles, [first, last]);
});
}

test('empty pages directory does not hide slides; two populated directories require a choice', t => {
  const root = temporaryDeck(t);
  mkdirSync(join(root, 'pages'));
  const slide = writePage(root, 'slides/slide_01.html');
  assert.deepEqual(ensureDeckPreconditions(root).htmlFiles, [slide]);
  const page = writePage(root, 'pages/page_01.html');
  assert.throws(() => ensureDeckPreconditions(root), /--pages-dir/);
  assert.deepEqual(ensureDeckPreconditions(root, { pagesDir: join(root, 'pages') }).htmlFiles, [page]);
  assert.deepEqual(ensureDeckPreconditions(root, { pagesDir: join(root, 'slides') }).htmlFiles, [slide]);
});

test('explicit page directory is authoritative, including invalid or empty choices', t => {
  const root = temporaryDeck(t);
  writePage(root, 'pages/page_01.html');
  const custom = writePage(root, 'custom/nested/slide_01.html');
  assert.deepEqual(ensureDeckPreconditions(root, { pagesDir: join(root, 'custom/nested') }).htmlFiles, [custom]);
  assert.throws(() => ensureDeckPreconditions(root, { pagesDir: join(root, 'missing') }));
  mkdirSync(join(root, 'empty'));
  assert.throws(() => ensureDeckPreconditions(root, { pagesDir: join(root, 'empty') }));
});

test('legacy root pages export in place without copying or rewriting HTML', t => {
  const root = temporaryDeck(t);
  const html = '<img src="assets/photo.png">';
  const page = writePage(root, 'page_01.html', html);
  assert.deepEqual(ensureDeckPreconditions(root).htmlFiles, [page]);
  assert.equal(readFileSync(page, 'utf8'), html);
  assert.equal(existsSync(join(root, 'pages')), false);
});

test('missing pages and missing or blocked reviews remain errors', t => {
  const root = temporaryDeck(t);
  assert.throws(() => ensureDeckPreconditions(root));
  writePage(root, 'slides/slide_01.html');
  rmSync(join(root, 'review.md'));
  assert.throws(() => ensureDeckPreconditions(root), /缺少 review/);
  writeFileSync(join(root, 'review.md'), 'status: blocked');
  assert.throws(() => ensureDeckPreconditions(root), /review/);
});

test('remote images are downloaded only for selected pages with page-relative links', async t => {
  const root = temporaryDeck(t);
  const deckDir = join(root, "deck's(2026)");
  const requests = [];
  const server = createServer((req, res) => {
    requests.push(req.url);
    res.writeHead(200, { 'Content-Type': 'image/png' });
    res.end('image fixture');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const url = `http://127.0.0.1:${server.address().port}`;
  const selected = writePage(deckDir, 'custom/nested/slide_01.html',
    `<img src="${url}/photo.png"><div style="background-image:url('${url}/bg.png')"></div><script src="${url}/code.js"></script>`);
  const other = writePage(deckDir, 'pages/page_01.html', `<img src="${url}/other.png">`);
  const external = writePage(root, 'external/slide_01.html',
    `<img src='${url}/external.png'><div style="background-image:url(${url}/external.png)"></div>`);
  await downloadRemoteImages(deckDir, [selected, external]);
  assert.deepEqual(requests, ['/photo.png', '/bg.png', '/external.png']);
  const updated = readFileSync(selected, 'utf8');
  assert.ok(updated.includes('src="../../images/photo.png"'));
  assert.ok(updated.includes("url('../../images/bg.png')"));
  assert.ok(updated.includes(`src="${url}/code.js"`));
  assert.equal(readFileSync(other, 'utf8'), `<img src="${url}/other.png">`);
  const externalUrl = '../deck%27s%282026%29/images/external.png';
  assert.equal(readFileSync(external, 'utf8'),
    `<img src='${externalUrl}'><div style="background-image:url(${externalUrl})"></div>`);
});

test('DOM image sources resolve against the HTML location without switching to srcset', async t => {
  const root = temporaryDeck(t);
  const encodedPng = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42AAAAAASUVORK5CYII=';
  const page = writePage(root, 'custom/nested/slide_01.html',
    `<html><body><div class="wrapper" style="width:1280px;height:720px"><div id="ct"><img src="local%20图片.png" srcset="data:image/png;base64,${encodedPng} 1x" style="width:100px;height:100px"></div></div></body></html>`);
  const png = Buffer.from(encodedPng, 'base64');
  writeFileSync(join(root, 'custom/nested/local 图片.png'), png);
  const [result] = await extractPages([page]);
  assert.ok(result.ir, result.error);
  const images = [];
  function visit(value) {
    if (!value || typeof value !== 'object') return;
    if (value.tag === 'IMG') {
      images.push(value.src);
      assert.equal(value.naturalWidth, 1, 'fixture image must load in the browser');
      assert.equal(buildImageElement(value, root).path, join(root, 'custom/nested/local 图片.png'));
    }
    for (const child of Object.values(value)) visit(child);
  }
  visit(result.ir);
  assert.deepEqual(images, [pathToFileURL(join(root, 'custom/nested/local 图片.png')).href]);
});
'''),
        ),
    },
}


def apply(relative: str, data: bytes) -> bytes:
    """Apply only the pinned exporter changes; reject all unexpected inputs."""
    patch = _PATCHES.get(relative)
    if patch is None:
        return data
    if hashlib.sha256(data).hexdigest() != patch["before_sha256"]:
        raise ValueError(f"SN export page directory overlay needs review: {relative}")
    lines = data.decode("utf-8").splitlines(keepends=True)
    for start, end, replacement in reversed(patch["edits"]):
        lines[start:end] = replacement.splitlines(keepends=True)
    result = "".join(lines).encode("utf-8")
    if hashlib.sha256(result).hexdigest() != patch["after_sha256"]:
        raise ValueError(f"SN export page directory overlay output needs review: {relative}")
    return result
