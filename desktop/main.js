// Desktop shell: starts the bundled backend on a free local port, shows the web UI in a window
// and stops everything (including an Ollama server the backend started) on quit.
//
// Everything stays on 127.0.0.1. The only time anything reaches the internet is the first-run
// setup screen, when the user presses "Download and set up".

const { app, BrowserWindow, dialog, shell } = require("electron");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");

const DEFAULT_CONFIG = `# Local Form Filler settings. Edit and restart the app; the Model page changes llm.model for you.
llm:
  model: qwen2.5:7b-instruct
  keep_alive: 5m
ocr:
  engine: rapidocr
server:
  workers: 2
`;

let backend = null;
let win = null;
let port = 0;
let quitting = false;
let logPath = "";

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.whenReady().then(main).catch((e) => fail(`Could not start: ${e.message}`));
}

async function main() {
  const p = paths();
  for (const d of [p.data, p.logs]) fs.mkdirSync(d, { recursive: true });
  if (!fs.existsSync(p.config)) fs.writeFileSync(p.config, DEFAULT_CONFIG);
  logPath = path.join(p.logs, "backend.log");

  win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: "Local Form Filler",
    show: false,
    backgroundColor: "#f6f6f3",
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.once("ready-to-show", () => win.show());
  win.on("closed", () => { win = null; });
  await win.loadFile(path.join(__dirname, "loading.html"));

  // Links to other sites (there are none in the UI, but just in case) open in the browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(`http://127.0.0.1:${port}`)) shell.openExternal(url);
    return { action: "deny" };
  });

  port = await freePort();
  startBackend(p);
  const ok = await waitForBackend(60_000);
  if (!ok) return fail("The local backend did not start.");
  if (win) await win.loadURL(`http://127.0.0.1:${port}/`);

  // Smoke test hook (CI / development): save a screenshot of the first screen and quit.
  if (process.env.FF_DESKTOP_SCREENSHOT && win) {
    await new Promise((r) => setTimeout(r, 2500));
    const img = await win.webContents.capturePage();
    fs.writeFileSync(process.env.FF_DESKTOP_SCREENSHOT, img.toPNG());
    app.quit();
  }
}

function paths() {
  const userData = app.getPath("userData");
  const res = app.isPackaged ? process.resourcesPath : null;
  const exe = process.platform === "win32" ? "form-filler.exe" : "form-filler";
  return {
    userData,
    data: path.join(userData, "data"),
    config: path.join(userData, "config.yaml"),
    logs: path.join(userData, "logs"),
    backendExe: res ? path.join(res, "backend", exe) : null,
    frontend: res ? path.join(res, "frontend") : null,
    repo: path.resolve(__dirname, ".."),
  };
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.on("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

function startBackend(p) {
  const env = {
    ...process.env,
    FORMFILLER_CONFIG: p.config,
    FF__STORAGE__DATA_DIR: p.data,
    FF__SERVER__HOST: "127.0.0.1",
    FF__SERVER__PORT: String(port),
    PYTHONUNBUFFERED: "1",
  };
  if (p.frontend) env.FF_FRONTEND_DIST = p.frontend;
  const log = fs.openSync(logPath, "a");
  fs.writeSync(log, `\n---- ${new Date().toISOString()} starting on port ${port}\n`);
  let cmd, args, cwd;
  if (p.backendExe) {
    cmd = p.backendExe;
    args = ["serve", "--skip-setup"];
    cwd = path.dirname(p.backendExe);
  } else {
    // Development: `npm start` in desktop/ against the repo checkout.
    cmd = process.platform === "win32" ? "uv.exe" : "uv";
    args = ["run", "form-filler", "serve", "--skip-setup"];
    cwd = p.repo;
  }
  backend = spawn(cmd, args, { cwd, env, stdio: ["ignore", log, log], windowsHide: true });
  backend.on("error", (e) => fail(`Could not start the local backend: ${e.message}`));
  backend.on("exit", (code) => {
    backend = null;
    if (!quitting) fail(`The local backend stopped unexpectedly (exit code ${code}).`);
  });
}

function waitForBackend(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {
    const tick = () => {
      if (!backend || Date.now() > deadline) return resolve(false);
      const req = http.get({ host: "127.0.0.1", port, path: "/", timeout: 1500 }, (res) => {
        res.resume();
        res.statusCode === 200 ? resolve(true) : setTimeout(tick, 400);
      });
      req.on("error", () => setTimeout(tick, 400));
      req.on("timeout", () => { req.destroy(); setTimeout(tick, 400); });
    };
    tick();
  });
}

function stopBackend() {
  if (!backend) return;
  const proc = backend;
  backend = null;
  if (process.platform === "win32") {
    // No SIGTERM on Windows; kill the whole tree (backend, job workers, an Ollama it started).
    try { execFileSync("taskkill", ["/pid", String(proc.pid), "/T", "/F"], { stdio: "ignore" }); } catch { /* already gone */ }
  } else {
    proc.kill("SIGTERM"); // uvicorn shuts down cleanly and stops the Ollama it started
    setTimeout(() => { try { proc.kill("SIGKILL"); } catch { /* already gone */ } }, 8000).unref();
  }
}

function fail(message) {
  if (quitting) return;
  quitting = true;
  stopBackend();
  dialog.showErrorBox("Local Form Filler", `${message}\n\nLog: ${logPath}`);
  app.quit();
}

app.on("before-quit", () => { quitting = true; stopBackend(); });
app.on("window-all-closed", () => app.quit());
