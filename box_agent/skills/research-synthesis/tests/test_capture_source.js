const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { validateUrl, capturePage, main } = require("../scripts/capture_source.js");

test("non-web URLs and embedded credentials are rejected", () => {
  for (const url of ["file:///private/data", "data:text/html,private", "https://user:secret@example.com/"]) {
    assert.throws(() => validateUrl(url), /HTTP\(S\).*credentials/);
  }
});

test("binds text to the document URL observed in the same DOM snapshot", async () => {
  const page = {
    goto: async () => ({ status: () => 200, url: () => "https://example.com/navigation" }),
    url: () => "https://example.com/later-document",
    locator: () => ({ waitFor: async () => {} }),
    waitForLoadState: async () => {},
    evaluate: async () => ({ document_url: "https://example.com/observed", text: "Observed text" }),
  };
  const observed = await capturePage(page, "https://example.com/requested");
  assert.equal(observed.final_url, "https://example.com/observed");
  assert.equal(observed.navigation_url, "https://example.com/navigation");
  assert.equal(observed.navigation_status, 200);
});

test("rejects a disallowed document URL reached after navigation", async () => {
  const page = {
    goto: async () => ({ status: () => 200, url: () => "https://example.com/navigation" }),
    url: () => "https://example.com/navigation",
    locator: () => ({ waitFor: async () => {} }),
    waitForLoadState: async () => {},
    evaluate: async () => ({ document_url: "file:///private/data", text: "Private text" }),
  };
  await assert.rejects(capturePage(page, "https://example.com/requested"), /HTTP\(S\)/);
});

const runtimeAvailable = Boolean(process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH
  && process.env.BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH);

test.describe("literal page observations", { skip: !runtimeAvailable }, () => {
  let server;
  let base;
  let browser;
  let temp;
  before(async () => {
    server = http.createServer((request, response) => {
      if (request.url === "/missing") {
        response.writeHead(404); response.end("Not found"); return;
      }
      if (request.url === "/redirect") {
        response.writeHead(302, { Location: "/source" }); response.end(); return;
      }
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(`<!doctype html><html lang="en"><title>Example source</title><body>
        <h1>Document options</h1><p>The detailed report includes 17 sections.</p>
        <label><input type="radio" name="mode">Compact</label>
        <label><input type="radio" name="mode" checked style="display:none">Detailed</label>
        <button role="tab" aria-selected="true">Overview</button>
        <select aria-label="Region"><option>North</option><option selected>South</option></select>
        <input type="password" value="canary-password">
        <a href="/terms">Source terms</a>
        <a href="https://user:link-secret@example.com/">Credential portal</a>
        <a href="http://">Malformed link</a>
      </body></html>`);
    });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    base = `http://127.0.0.1:${server.address().port}`;
    const { chromium } = require(process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH);
    browser = await chromium.launch({
      executablePath: process.env.BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
      headless: true,
    });
    temp = await fs.mkdtemp(path.join(os.tmpdir(), "research-capture-test-"));
  });
  after(async () => {
    if (browser) await browser.close();
    if (server) await new Promise(resolve => server.close(resolve));
    if (temp) await fs.rm(temp, { recursive: true, force: true });
  });

  test("captures actual text, selected state and source links without summarization", async () => {
    const page = await browser.newPage();
    try {
      const observed = await capturePage(page, base + "/source");
      assert.equal(observed.reader, "managed_browser_dom");
      assert.equal(observed.evidence_basis, "page_body");
      assert.equal(observed.title, "Example source");
      assert.match(observed.text, /The detailed report includes 17 sections\./);
      assert.equal(observed.controls.find(item => item.label === "Compact").checked, "false");
      assert.equal(observed.controls.find(item => item.label === "Detailed").checked, "true");
      assert.equal(observed.controls.find(item => item.label === "Overview").selected, "true");
      assert.deepEqual(observed.controls.find(item => item.label === "Region").selected_options, ["South"]);
      assert.ok(observed.links.some(item => item.url === base + "/terms"));
      assert.equal(JSON.stringify(observed).includes("canary-password"), false);
      assert.equal(JSON.stringify(observed).includes("link-secret"), false);
      assert.equal(observed.text_truncated, false);
      assert.ok(Number.isFinite(Date.parse(observed.captured_at)));
    } finally { await page.close(); }
  });

  test("records the final URL after an ordinary redirect", async () => {
    const page = await browser.newPage();
    try {
      const observed = await capturePage(page, base + "/redirect");
      assert.equal(observed.requested_url, base + "/redirect");
      assert.equal(observed.final_url, base + "/source");
    } finally { await page.close(); }
  });

  test("rejects an HTTP error instead of labeling it source evidence", async () => {
    const page = await browser.newPage();
    try {
      await assert.rejects(capturePage(page, base + "/missing"), /HTTP 404/);
    } finally { await page.close(); }
  });

  test("writes a capture once and preserves an existing observation", async () => {
    const output = path.join(temp, "source.json");
    await main([base + "/source", "--out", output]);
    const before = await fs.readFile(output, "utf8");
    assert.equal(JSON.parse(before).final_url, base + "/source");
    await assert.rejects(main([base + "/source", "--out", output]), /already exists/);
    assert.equal(await fs.readFile(output, "utf8"), before);
  });

  test("requires an available host runtime without installing a substitute", async () => {
    const saved = process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH;
    delete process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH;
    try {
      await assert.rejects(main([base + "/source", "--out", path.join(temp, "unavailable.json")]), /runtime is unavailable/);
      await assert.rejects(fs.access(path.join(temp, "unavailable.json")), { code: "ENOENT" });
    } finally { process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH = saved; }
  });
});
