const fs = require("fs");
const os = require("os");
const path = require("path");

function loadPlaywright() {
  // A hosted SDK is authoritative. Do not fall back to a stale user/workspace
  // package when that entry is missing or broken.
  return require(process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH || "playwright");
}

function officeRaccoonBrowserHostPath() {
  return path.join(os.homedir(), ".box-agent", "browsers");
}

function ensurePlaywrightBrowsersPath() {
  if (!process.env.PLAYWRIGHT_BROWSERS_PATH) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = officeRaccoonBrowserHostPath();
  }
}

function isUsableFile(filePath) {
  try {
    return fs.statSync(filePath).isFile();
  } catch {
    return false;
  }
}

function parseRevision(dirName) {
  const match = /-(\d+)$/.exec(dirName);
  return match ? Number(match[1]) : 0;
}

function configuredExecutablePath() {
  const candidates = [
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    process.env.BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    process.env.PLAYWRIGHT_EXECUTABLE_PATH,
  ];
  return candidates.find(candidate => candidate && isUsableFile(candidate)) || null;
}

function chromiumExecutableCandidates(browserDir) {
  if (process.platform === "darwin") {
    return [
      path.join(
        browserDir,
        "chrome-mac-arm64",
        "Google Chrome for Testing.app",
        "Contents",
        "MacOS",
        "Google Chrome for Testing"
      ),
      path.join(
        browserDir,
        "chrome-mac",
        "Google Chrome for Testing.app",
        "Contents",
        "MacOS",
        "Google Chrome for Testing"
      ),
      path.join(browserDir, "chrome-mac-arm64", "Chromium.app", "Contents", "MacOS", "Chromium"),
      path.join(browserDir, "chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
    ];
  }
  if (process.platform === "win32") {
    return [
      path.join(browserDir, "chrome-win64", "chrome.exe"),
      path.join(browserDir, "chrome-win", "chrome.exe"),
    ];
  }
  return [
    path.join(browserDir, "chrome-linux64", "chrome"),
    path.join(browserDir, "chrome-linux", "chrome"),
  ];
}

function headlessShellExecutableCandidates(browserDir) {
  if (process.platform === "darwin") {
    return [
      path.join(browserDir, "chrome-headless-shell-mac-arm64", "chrome-headless-shell"),
      path.join(browserDir, "chrome-headless-shell-mac", "chrome-headless-shell"),
    ];
  }
  if (process.platform === "win32") {
    return [
      path.join(browserDir, "chrome-headless-shell-win64", "chrome-headless-shell.exe"),
      path.join(browserDir, "chrome-headless-shell-win", "chrome-headless-shell.exe"),
    ];
  }
  return [
    path.join(browserDir, "chrome-headless-shell-linux64", "chrome-headless-shell"),
    path.join(browserDir, "chrome-headless-shell-linux", "chrome-headless-shell"),
  ];
}

function firstExistingFile(candidates) {
  return candidates.find(isUsableFile) || null;
}

function findManagedBrowserExecutable(browsersPath) {
  if (!browsersPath || !fs.existsSync(browsersPath)) return null;
  let dirs;
  try {
    dirs = fs.readdirSync(browsersPath);
  } catch {
    return null;
  }

  const headlessDirs = dirs
    .filter(dir => dir.startsWith("chromium_headless_shell-"))
    .sort((a, b) => parseRevision(b) - parseRevision(a));
  for (const dir of headlessDirs) {
    const executable = firstExistingFile(
      headlessShellExecutableCandidates(path.join(browsersPath, dir))
    );
    if (executable) return executable;
  }

  const chromiumDirs = dirs
    .filter(dir => dir.startsWith("chromium-"))
    .sort((a, b) => parseRevision(b) - parseRevision(a));
  for (const dir of chromiumDirs) {
    const executable = firstExistingFile(chromiumExecutableCandidates(path.join(browsersPath, dir)));
    if (executable) return executable;
  }
  return null;
}

function registryPathExists(expectedPath) {
  return expectedPath ? isUsableFile(expectedPath) : false;
}

function resolveChromiumExecutablePath(playwright) {
  ensurePlaywrightBrowsersPath();

  const hostPath = process.env.BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  if (hostPath) {
    return {
      ok: isUsableFile(hostPath),
      path: isUsableFile(hostPath) ? hostPath : null,
      source: "env",
      expectedPath: hostPath,
      browsersPath: process.env.PLAYWRIGHT_BROWSERS_PATH,
    };
  }

  const configured = configuredExecutablePath();
  if (configured) {
    return {
      ok: true,
      path: configured,
      source: "env",
      expectedPath: null,
      browsersPath: process.env.PLAYWRIGHT_BROWSERS_PATH,
    };
  }

  let expectedPath = null;
  try {
    expectedPath = playwright.chromium.executablePath();
    if (registryPathExists(expectedPath)) {
      return {
        ok: true,
        path: expectedPath,
        source: "playwright-registry",
        expectedPath,
        browsersPath: process.env.PLAYWRIGHT_BROWSERS_PATH,
      };
    }
  } catch {
    // Fall through to the managed browser scan below.
  }

  const browsersPath = process.env.PLAYWRIGHT_BROWSERS_PATH || officeRaccoonBrowserHostPath();
  if (expectedPath && browsersPath) {
    return {
      ok: false,
      path: null,
      source: null,
      expectedPath,
      browsersPath,
    };
  }

  const managed = findManagedBrowserExecutable(browsersPath);
  if (managed) {
    return {
      ok: true,
      path: managed,
      source: "managed-scan",
      expectedPath,
      browsersPath,
    };
  }

  return {
    ok: false,
    path: null,
    source: null,
    expectedPath,
    browsersPath,
  };
}

function chromiumLaunchOptions(playwright, options = {}) {
  const resolved = resolveChromiumExecutablePath(playwright);
  if (!resolved.ok && process.env.BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH) {
    throw new Error(`Host Playwright browser is unavailable: ${resolved.expectedPath}`);
  }
  return {
    resolved,
    options: resolved.path ? { ...options, executablePath: resolved.path } : options,
  };
}

module.exports = {
  loadPlaywright,
  chromiumLaunchOptions,
  ensurePlaywrightBrowsersPath,
  officeRaccoonBrowserHostPath,
  resolveChromiumExecutablePath,
};
