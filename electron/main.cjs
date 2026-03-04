const { app, BrowserWindow, ipcMain, Menu, dialog } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn, spawnSync } = require("child_process");
const { autoUpdater } = require("electron-updater");

const DEFAULT_API_BASE = "http://127.0.0.1:2345";
const BACKEND_SCRIPT_NAME = "MakebotGUI - Copy.py";
const BACKEND_EXECUTABLE_NAME = "makebot-backend.exe";
let backendProcess = null;
let apiBase = DEFAULT_API_BASE;
let mainWindow = null;
let runtimeEngineDir = null;

function fileExists(p) {
  try {
    return !!p && fs.existsSync(p);
  } catch {
    return false;
  }
}

function ensureDir(p) {
  try {
    fs.mkdirSync(p, { recursive: true });
    return true;
  } catch {
    return false;
  }
}

function copyMissingTree(srcDir, dstDir) {
  try {
    if (!fileExists(srcDir)) return;
    ensureDir(dstDir);
    for (const entry of fs.readdirSync(srcDir, { withFileTypes: true })) {
      const src = path.join(srcDir, entry.name);
      const dst = path.join(dstDir, entry.name);
      if (entry.isDirectory()) {
        copyMissingTree(src, dst);
      } else if (!fileExists(dst)) {
        fs.copyFileSync(src, dst);
      }
    }
  } catch {
    // Ignore copy failures; app can still attempt to run from bundled paths.
  }
}

function resolveBackendScript() {
  const envPath = process.env.MAKEBOT_BACKEND_SCRIPT;
  if (fileExists(envPath)) return envPath;
  const runtimeDir = resolveRuntimeEngineDir();

  const exeDir = path.dirname(process.execPath || "");
  const packagedCandidates = [
    // Preferred packaged locations (real filesystem, not app.asar virtual paths)
    path.join(process.resourcesPath || "", "make-engine", BACKEND_SCRIPT_NAME),
    path.join(process.resourcesPath || "", "app.asar.unpacked", "make-engine", BACKEND_SCRIPT_NAME),
    path.join(exeDir, "make-engine", BACKEND_SCRIPT_NAME),
    path.join(process.resourcesPath || "", BACKEND_SCRIPT_NAME),
  ];
  const candidates = [
    ...packagedCandidates,
    path.join(runtimeDir, BACKEND_SCRIPT_NAME),

    // Dev/project fallbacks.
    path.resolve(process.cwd(), "make-engine", BACKEND_SCRIPT_NAME),
    path.resolve(__dirname, "..", "make-engine", BACKEND_SCRIPT_NAME),

    // Legacy / compatibility fallbacks
    path.resolve(__dirname, "..", "..", BACKEND_SCRIPT_NAME),
    path.resolve(process.cwd(), BACKEND_SCRIPT_NAME),
    path.join(process.resourcesPath || "", BACKEND_SCRIPT_NAME),
    path.join(exeDir, BACKEND_SCRIPT_NAME),
  ];

  const found = candidates.find(fileExists) || null;
  if (!found) return null;
  if (app.isPackaged && /app\.asar[\\/]/i.test(found)) {
    // Python cannot reliably execute scripts from virtual asar paths.
    return null;
  }
  return found;
}

function resolveBackendExecutable() {
  if (process.platform !== "win32") return null;
  const envPath = process.env.MAKEBOT_BACKEND_EXE;
  if (fileExists(envPath)) return envPath;
  const runtimeDir = resolveRuntimeEngineDir();

  const exeDir = path.dirname(process.execPath || "");
  const candidates = [
    path.join(runtimeDir, BACKEND_EXECUTABLE_NAME),
    path.join(process.resourcesPath || "", "make-engine", BACKEND_EXECUTABLE_NAME),
    path.join(process.resourcesPath || "", "app.asar.unpacked", "make-engine", BACKEND_EXECUTABLE_NAME),
    path.join(exeDir, "make-engine", BACKEND_EXECUTABLE_NAME),
    path.resolve(process.cwd(), "make-engine", BACKEND_EXECUTABLE_NAME),
    path.resolve(__dirname, "..", "make-engine", BACKEND_EXECUTABLE_NAME),
  ];
  const found = candidates.find(fileExists) || null;
  if (!found) return null;
  if (/app\.asar[\\/]/i.test(found)) {
    // Executables cannot be launched from app.asar.
    return null;
  }
  return found;
}

function configPath() {
  const portableDir = process.env.PORTABLE_EXECUTABLE_DIR;
  if (portableDir) {
    return path.join(portableDir, "makebot-config.json");
  }
  return path.join(app.getPath("userData"), "makebot-config.json");
}

function resolveRuntimeEngineDir() {
  if (runtimeEngineDir) return runtimeEngineDir;
  const cfg = loadConfig();
  const configured = typeof cfg.makeEngineDir === "string" ? cfg.makeEngineDir.trim() : "";
  if (configured) {
    runtimeEngineDir = configured;
    ensureDir(runtimeEngineDir);
    return runtimeEngineDir;
  }
  const portableDir = process.env.PORTABLE_EXECUTABLE_DIR;
  runtimeEngineDir = portableDir
    ? path.join(portableDir, "make-engine")
    : path.join(app.getPath("userData"), "make-engine");
  ensureDir(runtimeEngineDir);
  return runtimeEngineDir;
}

function loadConfig() {
  try {
    const raw = fs.readFileSync(configPath(), "utf8");
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

function saveConfig(next) {
  try {
    fs.writeFileSync(configPath(), JSON.stringify(next, null, 2), "utf8");
  } catch {
    // Ignore config write failures to avoid crashing desktop app.
  }
}

function writeStartupLog(message) {
  try {
    const line = `[${new Date().toISOString()}] ${message}\n`;
    fs.appendFileSync(path.join(app.getPath("userData"), "startup.log"), line, "utf8");
  } catch {
    // Ignore log write failures.
  }
}

const STARTUP_LOG_PATH = () => path.join(app.getPath("userData"), "startup.log");

function normalizeBaseUrl(input) {
  const value = String(input || "").trim().replace(/\/+$/, "");
  if (!value) return null;
  try {
    const u = new URL(value);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return `${u.protocol}//${u.host}`;
  } catch {
    return null;
  }
}

function isLocalBase(value) {
  try {
    const u = new URL(value);
    return u.hostname === "127.0.0.1" || u.hostname === "localhost";
  } catch {
    return false;
  }
}

function normalizeFsPath(input) {
  const v = String(input || "").trim();
  if (!v) return "";
  try {
    return path.resolve(v).replace(/[\\/]+$/, "").toLowerCase();
  } catch {
    return v.replace(/[\\/]+$/, "").toLowerCase();
  }
}

async function backendReachable() {
  try {
    const res = await fetch(`${apiBase}/settings`, { method: "GET" });
    return !!res;
  } catch {
    return false;
  }
}

async function backendMatchesRuntimeDir(runtimeDir) {
  try {
    const res = await fetch(`${apiBase}/settings`, { method: "GET" });
    if (!res.ok) return false;
    const data = await res.json();
    const backendPath = normalizeFsPath(data?.make_engine_path || "");
    const wantedPath = normalizeFsPath(runtimeDir || "");
    return !!backendPath && !!wantedPath && backendPath === wantedPath;
  } catch {
    return false;
  }
}

async function backendCompatible() {
  try {
    const [authRes, settingsRes] = await Promise.all([
      fetch(`${apiBase}/auth/status`, { method: "GET" }),
      fetch(`${apiBase}/settings`, { method: "GET" }),
    ]);
    if (!authRes.ok || !settingsRes.ok) return false;

    const authData = await authRes.json();
    const settingsData = await settingsRes.json();

    const authShapeOk = authData && typeof authData === "object" && typeof authData.authenticated === "boolean";
    const settingsShapeOk =
      settingsData &&
      typeof settingsData === "object" &&
      typeof settingsData.authenticated === "boolean" &&
      typeof settingsData.license_key === "string";

    return !!(authShapeOk && settingsShapeOk);
  } catch {
    return false;
  }
}

function pythonCandidates() {
  if (process.platform === "win32") {
    return ["py", "python", "python3"];
  }
  return ["python3", "python"];
}

function commandAvailable(cmd) {
  try {
    const out = spawnSync(cmd, ["--version"], { stdio: "ignore" });
    return !out.error;
  } catch {
    return false;
  }
}

function killProcessOnPort(port) {
  if (!port) return false;
  try {
    if (process.platform === "win32") {
      const net = spawnSync("cmd", ["/c", `netstat -ano | findstr :${port}`], { encoding: "utf8" });
      const text = `${net.stdout || ""}\n${net.stderr || ""}`;
      const pids = [...new Set(
        text
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean)
          .map((line) => line.split(/\s+/).pop())
          .filter((v) => /^\d+$/.test(v))
      )];
      for (const pid of pids) {
        spawnSync("taskkill", ["/PID", pid, "/T", "/F"], { stdio: "ignore" });
      }
      return pids.length > 0;
    }

    const out = spawnSync("lsof", ["-ti", `tcp:${port}`], { encoding: "utf8" });
    const pids = [...new Set(
      (out.stdout || "")
        .split(/\r?\n/)
        .map((s) => s.trim())
        .filter((v) => /^\d+$/.test(v))
    )];
    for (const pid of pids) {
      spawnSync("kill", ["-15", pid], { stdio: "ignore" });
      spawnSync("kill", ["-9", pid], { stdio: "ignore" });
    }
    return pids.length > 0;
  } catch {
    return false;
  }
}

async function ensureBackendStarted() {
  if (!isLocalBase(apiBase)) {
    writeStartupLog(`Skipping local backend auto-start for non-local base: ${apiBase}`);
    return;
  }
  if (await backendReachable()) {
    const runtimeDir = resolveRuntimeEngineDir();
    if (await backendCompatible()) {
      if (await backendMatchesRuntimeDir(runtimeDir)) {
        writeStartupLog(`Compatible backend already reachable at ${apiBase}`);
        return;
      }
      writeStartupLog(`Compatible backend reachable at ${apiBase}, but runtime dir changed; replacing it.`);
    } else {
      writeStartupLog(`Incompatible backend detected at ${apiBase}; attempting to replace it.`);
    }
    let port = "2345";
    try {
      const u = new URL(apiBase);
      port = u.port || port;
    } catch {}
    const killed = killProcessOnPort(port);
    writeStartupLog(killed ? `Stopped process(es) on port ${port}.` : `Could not stop process on port ${port}; continuing.`);
  }
  const runtimeDir = resolveRuntimeEngineDir();
  const backendExecutable = resolveBackendExecutable();
  if (backendExecutable) {
    const bundledEngineDir = path.dirname(backendExecutable);
    copyMissingTree(bundledEngineDir, runtimeDir);
    const runtimeExe = path.join(runtimeDir, BACKEND_EXECUTABLE_NAME);
    const execPath = fileExists(runtimeExe) ? runtimeExe : backendExecutable;
    writeStartupLog(`Backend executable resolved: ${execPath}`);
    writeStartupLog(`Backend runtime dir: ${runtimeDir}`);
    try {
      const child = spawn(execPath, [], {
        cwd: runtimeDir,
        env: {
          ...process.env,
          FLASK_DEBUG: "0",
          ELECTRON_RUN: "1",
          MAKEBOT_STARTUP_LOG: STARTUP_LOG_PATH(),
          MAKEBOT_BASE_DIR: runtimeDir,
        },
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });

      child.stdout?.on("data", (buf) => {
        const msg = String(buf || "").trim();
        if (msg) writeStartupLog(`[backend stdout] ${msg}`);
      });
      child.stderr?.on("data", (buf) => {
        const msg = String(buf || "").trim();
        if (msg) writeStartupLog(`[backend stderr] ${msg}`);
      });
      child.on("error", (err) => {
        writeStartupLog(`Backend spawn error via executable: ${String(err?.message || err)}`);
      });
      child.on("exit", (code, signal) => {
        writeStartupLog(`Backend executable exited: code=${code} signal=${signal || "none"}`);
      });

      backendProcess = child;
      writeStartupLog(`Backend spawned via executable (pid=${child.pid || "unknown"})`);
      return;
    } catch (err) {
      writeStartupLog(`Backend executable spawn threw: ${String(err?.message || err)}`);
    }
  }

  const backendScript = resolveBackendScript();
  if (!backendScript) {
    writeStartupLog(`Backend script not found. Set MAKEBOT_BACKEND_SCRIPT or place ${BACKEND_SCRIPT_NAME} near app/make-engine.`);
    return;
  }
  const scriptEngineDir = path.dirname(backendScript);
  copyMissingTree(scriptEngineDir, runtimeDir);
  const runtimeScript = path.join(runtimeDir, BACKEND_SCRIPT_NAME);
  const scriptPath = fileExists(runtimeScript) ? runtimeScript : backendScript;
  writeStartupLog(`Backend script resolved: ${scriptPath}`);
  writeStartupLog(`Backend runtime dir: ${runtimeDir}`);
  for (const cmd of pythonCandidates()) {
    if (!commandAvailable(cmd)) {
      writeStartupLog(`Python candidate unavailable: ${cmd}`);
      continue;
    }
    try {
      const child = spawn(cmd, [scriptPath], {
        cwd: runtimeDir,
        env: {
          ...process.env,
          FLASK_DEBUG: "0",
          ELECTRON_RUN: "1",
          MAKEBOT_STARTUP_LOG: STARTUP_LOG_PATH(),
          MAKEBOT_BASE_DIR: runtimeDir,
        },
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });

      child.stdout?.on("data", (buf) => {
        const msg = String(buf || "").trim();
        if (msg) writeStartupLog(`[backend stdout] ${msg}`);
      });
      child.stderr?.on("data", (buf) => {
        const msg = String(buf || "").trim();
        if (msg) writeStartupLog(`[backend stderr] ${msg}`);
      });

      child.on("error", (err) => {
        writeStartupLog(`Backend spawn error via ${cmd}: ${String(err?.message || err)}`);
      });
      child.on("exit", (code, signal) => {
        writeStartupLog(`Backend exited via ${cmd}: code=${code} signal=${signal || "none"}`);
      });

      backendProcess = child;
      writeStartupLog(`Backend spawned with ${cmd} (pid=${child.pid || "unknown"})`);
      return;
    } catch {
      // Try next python command.
      writeStartupLog(`Backend spawn threw with ${cmd}; trying next candidate.`);
    }
  }
  writeStartupLog("No python command could be used to start backend.");
}

function createWindow() {
  const windowOptions = {
    width: 1200,
    height: 800,
    backgroundColor: "#0b0f17",
    frame: false,
    ...(process.platform === "darwin" ? { titleBarStyle: "hidden" } : {}),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  };
  const win = new BrowserWindow(windowOptions);
  win.setMenuBarVisibility(false);
  win.removeMenu();

  const useDevServer = !app.isPackaged && process.env.MAKEBOT_USE_DEV_SERVER === "1";
  const rendererFile = path.join(__dirname, "../dist-renderer/index.html");
  let fallbackLoaded = false;
  const loadRendererFallback = () => {
    if (fallbackLoaded) return;
    fallbackLoaded = true;
    writeStartupLog("Loading fallback renderer file.");
    win.loadFile(rendererFile).catch(() => {});
  };

  if (useDevServer) {
    win.webContents.once("did-fail-load", (_event, code, desc, url) => {
      writeStartupLog(`Dev URL failed; code=${code} desc=${desc} url=${url}`);
      loadRendererFallback();
    });
  }

  if (useDevServer) {
    win.loadURL("http://127.0.0.1:43127").catch(() => {
      writeStartupLog("Dev server unavailable at http://127.0.0.1:43127; falling back to dist-renderer.");
      loadRendererFallback();
    });
  } else {
    win.loadFile(rendererFile);
  }

  win.webContents.on("did-fail-load", (_event, code, desc, url) => {
    writeStartupLog(`did-fail-load code=${code} desc=${desc} url=${url}`);
  });
  win.webContents.on("render-process-gone", (_event, details) => {
    writeStartupLog(`render-process-gone reason=${details?.reason || "unknown"}`);
  });
  win.webContents.on("console-message", (_event, details) => {
    const level = details?.level ?? "unknown";
    const message = details?.message ?? "";
    const line = details?.lineNumber ?? 0;
    const sourceId = details?.sourceId || "unknown";
    writeStartupLog(`console level=${level} source=${sourceId} line=${line} msg=${message}`);
  });
  mainWindow = win;
  win.on("closed", () => {
    if (mainWindow === win) mainWindow = null;
  });
}

function setupAutoUpdater() {
  if (!app.isPackaged) return;
  const updateConfig = path.join(process.resourcesPath || "", "app-update.yml");
  if (!fileExists(updateConfig)) {
    writeStartupLog(`Auto-updater disabled: missing ${updateConfig}`);
    return;
  }

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on("error", () => {
    // Keep updater silent for end users.
  });

  autoUpdater.on("update-downloaded", () => {
    setTimeout(() => {
      autoUpdater.quitAndInstall();
    }, 1200);
  });

}

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  const configuredBase = normalizeBaseUrl(process.env.MAKEBOT_API_BASE) || DEFAULT_API_BASE;
  if (!isLocalBase(configuredBase)) {
    writeStartupLog(`Ignoring non-local configured backend base: ${configuredBase}; using ${DEFAULT_API_BASE}`);
  }
  apiBase = isLocalBase(configuredBase) ? configuredBase : DEFAULT_API_BASE;
  setupAutoUpdater();
  ensureBackendStarted().finally(() => {
    createWindow();
  });
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill();
    backendProcess = null;
  }
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill();
    backendProcess = null;
  }
});

ipcMain.handle("makebot:request", async (_event, args) => {
  const { path: apiPath, method = "GET", headers = {}, body } = args || {};
  writeStartupLog(`[ipc request] method=${method} path=${apiPath || "missing"}`);
  if (!apiPath || typeof apiPath !== "string") {
    writeStartupLog(`[ipc request error] missing path`);
    return { ok: false, status: 400, error: "Missing path" };
  }

  const url = apiBase + apiPath;

  try {
    const res = await fetch(url, {
      method,
      headers: {
        "content-type": "application/json",
        "Ngrok-Skip-Browser-Warning": "1",
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });

    const text = await res.text();
    let data = text;
    try {
      data = JSON.parse(text);
    } catch {}

    const summary = typeof data === "object" ? JSON.stringify(data).slice(0, 300) : String(data).slice(0, 300);
    writeStartupLog(`[ipc response] method=${method} path=${apiPath} status=${res.status} ok=${res.ok} body=${summary}`);
    return { ok: res.ok, status: res.status, data };
  } catch (e) {
    writeStartupLog(`[ipc fetch error] method=${method} path=${apiPath} error=${String(e?.message || e)}`);
    return { ok: false, status: 0, error: String(e?.message || e) };
  }
});

ipcMain.handle("makebot:get-backend-base", async () => apiBase);

ipcMain.handle("makebot:get-engine-dir", async () => resolveRuntimeEngineDir());

ipcMain.handle("makebot:choose-engine-dir", async () => {
  const owner = mainWindow || BrowserWindow.getAllWindows()[0] || null;
  const current = resolveRuntimeEngineDir();
  const out = await dialog.showOpenDialog(owner, {
    title: "Select Make-Engine Directory",
    defaultPath: current,
    properties: ["openDirectory", "createDirectory"],
  });
  if (out.canceled || !out.filePaths?.length) return { ok: false, canceled: true };
  const picked = out.filePaths[0];
  if (!picked) return { ok: false, error: "No folder selected" };

  ensureDir(picked);
  runtimeEngineDir = picked;
  const cfg = loadConfig();
  cfg.makeEngineDir = picked;
  saveConfig(cfg);
  writeStartupLog(`make-engine directory set to: ${picked}`);

  app.relaunch();
  app.exit(0);
  return { ok: true, path: picked };
});

ipcMain.handle("makebot:window-control", (event, action) => {
  const win = BrowserWindow.fromWebContents(event.sender) || mainWindow;
  if (!win) return { ok: false, error: "No window" };
  try {
    switch (action) {
      case "minimize":
        win.minimize();
        writeStartupLog("window-control: minimize");
        break;
      case "maximize":
        if (win.isMaximized()) win.unmaximize();
        else win.maximize();
        writeStartupLog(`window-control: maximize -> ${win.isMaximized() ? "maximized" : "restored"}`);
        break;
      case "close":
        writeStartupLog("window-control: close");
        win.close();
        break;
      default:
        return { ok: false, error: "Unknown action" };
    }
    return { ok: true, maximized: win.isMaximized() };
  } catch (e) {
    return { ok: false, error: String(e?.message || e) };
  }
});

ipcMain.handle("makebot:set-backend-base", async (_event, nextBase) => {
  writeStartupLog(`Ignoring makebot:set-backend-base request (${String(nextBase || "")}) because backend switching is disabled.`);
  return { ok: false, error: "Backend switching is disabled." };
});

ipcMain.handle("makebot:check-for-updates", async () => {
  if (!app.isPackaged) return { ok: false, error: "Updates are only available in packaged builds." };
  const updateConfig = path.join(process.resourcesPath || "", "app-update.yml");
  if (!fileExists(updateConfig)) {
    const msg = "Updater metadata not found for this build. Download updates from GitHub Releases.";
    writeStartupLog(`check-for-updates skipped: missing ${updateConfig}`);
    return { ok: false, error: msg };
  }
  try {
    const result = await autoUpdater.checkForUpdates();
    return { ok: true, updateInfo: result?.updateInfo || null };
  } catch (e) {
    return { ok: false, error: String(e?.message || e) };
  }
});
