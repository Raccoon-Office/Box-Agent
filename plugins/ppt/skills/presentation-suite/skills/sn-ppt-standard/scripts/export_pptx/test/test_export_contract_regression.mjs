import assert from 'node:assert/strict';
import test from 'node:test';
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import JSZip from 'jszip';
import { buildImageElement, buildPptx } from '../lib/pptx_builder.mjs';
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

function coldCli(t, args, withPlaywright = false) {
  const root = mkdtempSync(join(tmpdir(), 'pptx-cli-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  cpSync(fileURLToPath(new URL('..', import.meta.url)), root, {
    recursive: true, filter: path => !['node_modules', 'test'].includes(basename(path)),
  });
  if (withPlaywright) {
    mkdirSync(join(root, 'node_modules'));
    symlinkSync(fileURLToPath(new URL('../node_modules/playwright', import.meta.url)), join(root, 'node_modules/playwright'), 'dir');
  }
  mkdirSync(join(root, 'bin'));
  writeFileSync(join(root, 'bin/npm'), '#!/bin/sh\necho installer-stdout\necho controlled-install-failure >&2\nexit 23\n', { mode: 0o755 });
  return spawnSync(process.execPath, [join(root, 'html_to_pptx.mjs'), ...args], {
    encoding: 'utf8', env: { ...process.env, PATH: `${join(root, 'bin')}:${process.env.PATH}` },
  });
}

test('help works before dependencies are installed', t => {
  const result = coldCli(t, ['--help']);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /--deck-dir/);
  assert.doesNotMatch(result.stderr, /setup|controlled-install-failure/);
});

for (const withPlaywright of [false, true]) {
test(`dependency failure is nonzero (Playwright preinstalled: ${withPlaywright})`, t => {
  const result = coldCli(t, ['--deck-dir', tmpdir()], withPlaywright);
  assert.notEqual(result.status, 0);
  const report = JSON.parse(result.stdout);
  assert.equal(report.status, 'failed');
  assert.equal(report.success, false);
  assert.equal(report.converted, 0);
  assert.match(result.stderr, /controlled-install-failure/);
  assert.doesNotMatch(report.detail, /final deliverable/);
});
}

for (const sourceFamily of ['Noto Sans SC', 'Xiaolai', 'Caveat']) {
test(`PPTX restores ${sourceFamily} and leaves browser IR intact`, async t => {
  const root = mkdtempSync(join(tmpdir(), 'pptx-font-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, 'assets/fonts'), { recursive: true });
  writeFileSync(join(root, 'assets/fonts/manifest.json'), JSON.stringify({ faces: [
    { delivery_family: 'Deck-test-font', source_family: sourceFamily },
  ] }));
  const generic = sourceFamily === 'Noto Sans SC' ? 'sans-serif' : 'cursive';
  const styles = { color: 'rgb(0, 0, 0)', fontSize: '32px', fontFamily: `"Deck-test-font", ${generic}` };
  const ir = { canvasWidth: 1600, canvasHeight: 900, ct: {
    tag: 'DIV', bounds: { x: 80, y: 80, w: 900, h: 150 }, styles, text: '市场 Growth 2026',
    textRuns: [
      { text: '市场 Growth ', ...styles },
      { text: '2026', ...styles, fontFamily: 'Arial', fontWeight: '700' },
    ],
  } };
  const original = JSON.stringify(ir);
  const output = join(root, 'fonts.pptx');
  const result = await buildPptx([{ path: 'slide_01.html', ir }], root, output);
  assert.equal(result.successCount, 1);
  const zip = await JSZip.loadAsync(readFileSync(output));
  const xml = await zip.file('ppt/slides/slide1.xml').async('string');
  assert.ok(xml.includes(`typeface="${sourceFamily}"`));
  assert.match(xml, /typeface="Arial"/);
  assert.doesNotMatch(xml, /Deck-test/);
  assert.equal(JSON.stringify(ir), original);
});
}
