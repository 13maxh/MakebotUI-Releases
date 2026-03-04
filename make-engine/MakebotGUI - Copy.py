import os
import sys
import json
import csv
import time
import re
import threading
import subprocess
import webbrowser
import requests  # Used for Discord OAuth requests
from datetime import datetime, timedelta
from flask_cors import CORS
from flask import Flask, render_template_string, request, session, redirect, url_for, jsonify, abort
from apscheduler.schedulers.background import BackgroundScheduler
import logging
from flask import Flask, request, session, redirect, url_for, render_template_string


# Suppress APScheduler warnings and job execution logs
logging.getLogger("apscheduler").setLevel(logging.ERROR)
logging.getLogger("apscheduler.scheduler").setLevel(logging.ERROR)
logging.getLogger("apscheduler.executors.default").setLevel(logging.ERROR)
logging.getLogger("apscheduler.executors").setLevel(logging.ERROR)

active_automation_groups = set()
stop_automation_timers = {}
last_sent_product = {}
running_processes = {}
# For each group, we now store a simple boolean flag in monitoring_locked.
monitoring_locked = {}  
# Global lock to protect accesses to monitoring_locked and last_sent_product.
global_monitoring_lock = threading.Lock()
last_store_check = {}
# Global dictionaries for per-store and per-group monitoring
active_store_threads = {}
active_automation_monitors = {}
engine_process = None          # the single Popen for make-engine.exe
capture_group = None           # which group new lines currently belong to
processes = {}                 # map group_name -> engine_process
task_status = {}               # map group_name -> {global_status, TASK-xxxx: status}
engine_child = None


#################################
# Flask Setup
#################################
app = Flask(__name__)
CORS(app, supports_credentials=True, origins=["http://localhost:2345"])
app.secret_key = "supersecretkey"

#################################
# SETTINGS and default sites (stored in settings.json)
#################################
BASE_DIR = os.environ.get("MAKEBOT_BASE_DIR", "").strip()
if not BASE_DIR:
    if getattr(sys, 'frozen', False):
        BASE_DIR = os.path.dirname(sys.executable)  # For PyInstaller executable
    else:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # For normal .py run
os.makedirs(BASE_DIR, exist_ok=True)

# Define the settings file path
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

# Load settings.json if it exists; otherwise, create it with default values.
if os.path.exists(SETTINGS_PATH):
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        SETTINGS = json.load(f)
else:
    SETTINGS = {
        "make_engine_path": BASE_DIR,
        "webhook_url": "https://discord.com/api/webhooks/your_webhook_id/your_webhook_token",
        "discord_bot_token": "",
        "discord_channel_id": "",
        "custom_sizes": [],
        "device_authenticated": False,
        "sites": [
            {"name": "Kith", "url": "kith.com"},
            {"name": "Shoe Palace", "url": "shoepalace.com"},
            {"name": "Shop Nice Kicks", "url": "shopnicekicks.com"},
            {"name": "Sneaker Politics", "url": "sneakerpolitics.com"}
        ]
    }
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f, indent=2)
    except Exception as e:
        print("Error creating settings.json:", e)

# Ensure that "sites" key exists even if settings were loaded.
if "sites" not in SETTINGS:
    SETTINGS["sites"] = [
        {"name": "Kith", "url": "kith.com"},
        {"name": "Shoe Palace", "url": "shoepalace.com"},
        {"name": "Shop Nice Kicks", "url": "shopnicekicks.com"},
        {"name": "Sneaker Politics", "url": "sneakerpolitics.com"}
    ]

SETTINGS.setdefault("discord_bot_token", "")
SETTINGS.setdefault("discord_channel_id", "")
SETTINGS.setdefault("custom_sizes", [])
SETTINGS.setdefault("device_authenticated", False)

# Heal stale paths (common after packaged app updates/relaunches).
saved_engine_path = str(SETTINGS.get("make_engine_path", "") or "").strip()
if not saved_engine_path or not os.path.isdir(saved_engine_path):
    SETTINGS["make_engine_path"] = BASE_DIR
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f, indent=2)
    except Exception:
        pass

def save_settings():
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f, indent=2)
    except Exception as e:
        print("Error saving settings:", e)


def startup_log(message):
    try:
        log_path = os.environ.get("MAKEBOT_STARTUP_LOG", os.path.join(BASE_DIR, "startup.log"))
        ts = datetime.utcnow().isoformat() + "Z"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [backend] {message}\n")
    except Exception:
        pass


def _safe_int_count(path, suffix):
    try:
        return len([f for f in os.listdir(path) if f.endswith(suffix)])
    except Exception:
        return 0


def _count_total_tasks():
    total = 0
    try:
        for fname in os.listdir(TASK_DIR):
            if not fname.endswith(".csv"):
                continue
            fpath = os.path.join(TASK_DIR, fname)
            with open(fpath, newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
                total += max(0, len(rows) - 1)
    except Exception:
        pass
    return total


def parse_link_input(input_val, selected_sizes=None):
    if not input_val.startswith("http"):
        return None, input_val

    try:
        pattern = r"https?://([^/]+)/products/([^/?#]+)"
        match = re.search(pattern, input_val)
        if not match:
            return None, input_val

        site = match.group(1)
        handle = match.group(2)
        keywords = ' '.join(word.capitalize() for word in handle.split('-'))

        product_json_url = f"https://{site}/products/{handle}.json"
        resp = requests.get(product_json_url, timeout=5)
        if resp.status_code != 200:
            return site, keywords

        product_data = resp.json()
        variants = product_data["product"]["variants"]
        matching_variants = []

        if selected_sizes:
            selected_sizes = selected_sizes.lower().split("&")

            for variant in variants:
                size = str(variant.get("option1", "")).lower()
                if "random" in selected_sizes or size in selected_sizes:
                    matching_variants.append(str(variant["id"]))

        # If no sizes matched, return all variants and indicate to frontend
        if not matching_variants:
            all_variants = [str(v["id"]) for v in variants]
            return site, {
                "fallback": True,
                "input": " ".join(all_variants),
                "keywords": keywords
            }

        return site, " ".join(matching_variants)

    except Exception as e:
        print("Error parsing link input:", e)
        return None, input_val

#################################
# HTML Templates
#################################
HTML_LOGIN = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Makebot+ GUI - Unlock</title>
  <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
  <style>
    body {
      background: linear-gradient(180deg, hsla(160, 48%, 46%, 1) 0%, hsla(0, 0%, 0%, 1) 64%);
      min-height: 100vh;
      display: flex;
      justify-content: center;
      align-items: center;
    }
    .login-card {
      background-color: rgba(0, 0, 0, 0.85);
      padding: 2rem;
      border-radius: 0.75rem;
      width: 350px;
      text-align: center;
      box-shadow: 0 4px 10px rgba(0, 0, 0, 0.3);
    }
    .key-input {
      background-color: #374151;
      color: white;
      padding: 0.75rem;
      border-radius: 0.375rem;
      border: none;
      width: 100%;
      margin-bottom: 1rem;
    }
    .submit-btn {
      background-color: #25AEE1;
      color: white;
      padding: 0.75rem;
      border-radius: 0.375rem;
      font-weight: bold;
      width: 100%;
    }
    .submit-btn:hover {
      background-color: #1d94c4;
    }
    .logo {
      position: absolute;
      top: 20px;
      right: 20px;
      width: 50px;
    }
  </style>
</head>
<body>
  <img src="https://media.discordapp.net/attachments/939310352351522836/1357112029424521216/download_3.png?ex=67ef0491&is=67edb311&hm=0d9aa65e2abb4f65a0de4fb31b62c62cf637d4b1bf3b3dca778fbdb0e7be7ea9&=&format=webp&quality=lossless&width=1515&height=1515" 
       alt="Bot Logo" class="logo">
  <form method="POST" class="login-card">
    <h1 class="text-2xl font-bold text-white mb-4">Welcome to Makebot+ GUI</h1>
    {% if error %}
      <p class="text-red-400 text-sm mb-4">{{ error }}</p>
    {% endif %}
    <input type="text" name="license_key" class="key-input" placeholder="Enter License Key" required />
    <button type="submit" class="submit-btn">Unlock</button>
    <p class="text-gray-500 text-sm mt-4">Developed by 13maxh</p>
  </form>
</body>
</html>
"""


HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Makebot+ GUI</title>
    <!-- Tailwind and Font Awesome CSS -->
    <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet" />
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" />
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet" />
    <!-- Apple meta tags -->
    <meta name="apple-mobile-web-app-capable" content="yes" />
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
    <meta name="apple-mobile-web-app-title" content="Makebot+ GUI" />
    <link rel="apple-touch-icon" href="https://pbs.twimg.com/profile_images/1562315428126666755/vwwptAHp_400x400.jpg" />
    <!-- Additional Styles -->
    <style>
      :root {
        --bg-1: #09090b;
        --bg-2: #18181b;
        --card: rgba(24, 24, 27, 0.92);
        --card-border: rgba(63, 63, 70, 0.85);
        --text-main: #e4e4e7;
        --text-muted: #a1a1aa;
        --accent: #22d3ee;
        --accent-2: #34d399;
      }

      body {
        font-family: "JetBrains Mono", monospace;
        background:
          radial-gradient(circle at 8% 8%, rgba(34, 211, 238, 0.20), transparent 38%),
          radial-gradient(circle at 92% 10%, rgba(52, 211, 153, 0.12), transparent 40%),
          linear-gradient(145deg, var(--bg-1), var(--bg-2));
        color: var(--text-main);
      }

      .panel {
        background: var(--card);
        border: 1px solid var(--card-border);
        border-radius: 14px;
        box-shadow: 0 14px 40px rgba(0, 0, 0, 0.2);
      }

      .panel-title {
        letter-spacing: 0.02em;
      }

      .btn {
        border-radius: 0.75rem;
        padding: 0.58rem 0.9rem;
        font-weight: 600;
        transition: transform 0.05s, opacity 0.15s, background 0.2s;
      }
      .btn:active { transform: translateY(1px); }
      .btn-primary { background: rgba(34, 211, 238, 0.15); border: 1px solid rgba(34, 211, 238, 0.35); color: #22d3ee; }
      .btn-primary:hover { background: rgba(34, 211, 238, 0.24); }
      .btn-muted {
        background: rgba(39, 39, 42, 0.95);
        border: 1px solid rgba(63, 63, 70, 0.9);
      }
      .btn-muted:hover { background: rgba(63, 63, 70, 0.95); }
      .btn-danger { background: #b91c1c; }
      .btn-danger:hover { background: #991b1b; }

      .field-label {
        display: block;
        font-size: 0.7rem;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: #9aa4b2;
        margin-bottom: 0.3rem;
      }
      .field-input, .field-select {
        width: 100%;
        border-radius: 0.7rem;
        background: rgba(39, 39, 42, 0.9);
        color: #fff;
        border: 1px solid rgba(82, 82, 91, 0.9);
        padding: 0.58rem 0.75rem;
      }
      .field-input:focus, .field-select:focus {
        outline: none;
        box-shadow: 0 0 0 3px rgba(34,211,238,.18);
        border-color: rgba(34,211,238,.55);
      }

      .modal-overlay {
        position: fixed;
        inset: 0;
        z-index: 50;
        display: none;
        align-items: center;
        justify-content: center;
        padding: 1rem;
        background: rgba(0, 0, 0, 0.72);
        backdrop-filter: blur(6px);
      }
      .modal-overlay:not(.hidden) { display: flex; }
      .modal-card {
        width: 100%;
        max-width: 58rem;
        background: rgba(24, 24, 27, 0.98);
        border: 1px solid rgba(82, 82, 91, 0.85);
        border-radius: 1rem;
        box-shadow: 0 30px 90px rgba(0, 0, 0, 0.65);
      }
      .modal-header {
        padding: 1rem 1.2rem;
        border-bottom: 1px solid rgba(148, 163, 184, 0.22);
      }
      .modal-body {
        padding: 1rem 1.2rem;
        max-height: 68vh;
        overflow-y: auto;
      }
      .modal-actions {
        display: flex;
        justify-content: flex-end;
        gap: 0.5rem;
        padding: 0.9rem 1.2rem 1.1rem;
        border-top: 1px solid rgba(148, 163, 184, 0.22);
      }

      .control-btn {
        background: #1f2937;
        border: 1px solid rgba(148, 163, 184, 0.25);
        transition: all 0.15s ease;
      }

      .control-btn:hover {
        background: #334155;
        transform: translateY(-1px);
      }

      .primary-btn {
        background: linear-gradient(135deg, #0284c7, #0369a1);
      }

      .success-btn {
        background: linear-gradient(135deg, #16a34a, #15803d);
      }

      .danger-btn {
        background: linear-gradient(135deg, #dc2626, #b91c1c);
      }

      .item-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.65rem;
        padding: 0.75rem 0.85rem;
        border-radius: 0.75rem;
        background: rgba(23, 32, 52, 0.7);
        border: 1px solid var(--card-border);
        transition: all 0.15s ease;
      }

      .item-row:hover {
        background: rgba(30, 41, 59, 0.95);
      }

      .nav-btn {
        color: #a1a1aa;
        background: rgba(24, 24, 27, 0.45);
        border: 1px solid rgba(63, 63, 70, 0.5);
      }
      .nav-btn:hover {
        color: #e4e4e7;
        background: rgba(39, 39, 42, 0.95);
      }
      .nav-btn.active {
        color: #fff;
        background: rgba(39, 39, 42, 0.95);
        border-color: rgba(82, 82, 91, 0.95);
      }
      .nav-btn.active i:first-child {
        color: #22d3ee;
      }

      table.table-fixed th:nth-child(7),
      table.table-fixed td:nth-child(7),
      .status-cell {
        background-color: #1f2937;
        color: #fbbf24;
        overflow: visible !important;
        white-space: nowrap;
        position: sticky;
        right: 0;
        z-index: 2;
        padding-left: 4px;
        padding-right: 4px;
      }

      .status-text {
        display: inline-block;
        background-color: inherit;
        white-space: nowrap;
        padding-left: 2px;
        padding-right: 2px;
      }

      table.table-fixed th, table.table-fixed td {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .size-dropdown {
        background: rgba(39, 39, 42, 0.92);
        border: 1px solid rgba(82, 82, 91, 0.85);
        padding: 0.5rem;
        border-radius: 0.75rem;
        margin-bottom: 0.75rem;
      }
      .size-dropdown summary {
        cursor: pointer;
        padding: 0.6rem 0.75rem;
        background: rgba(63, 63, 70, 0.95);
        border: 1px solid rgba(113, 113, 122, 0.8);
        border-radius: 0.6rem;
        margin-bottom: 0.5rem;
        font-size: 12px;
        font-weight: 600;
      }
      .size-dropdown .checkbox-container {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 0.45rem;
        padding: 0.5rem;
        max-height: 220px;
        overflow-y: auto;
      }
      .size-dropdown .checkbox-container label {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.4rem 0.5rem;
        background: rgba(24, 24, 27, 0.95);
        border: 1px solid rgba(63, 63, 70, 0.85);
        border-radius: 0.5rem;
        font-size: 12px;
      }
      .size-dropdown .checkbox-container label:hover {
        background: rgba(39, 39, 42, 0.95);
      }
      .size-dropdown .checkbox-container input[type="checkbox"] {
        margin: 0;
      }
      .size-dropdown .checkbox-container .font-bold {
        grid-column: 1 / -1;
        margin-top: 0.25rem;
        color: #67e8f9;
        font-size: 11px;
        letter-spacing: 0.06em;
        text-transform: uppercase;
      }
      .schedule-info {
        font-size: 0.875rem;
        color: #9CA3AF;
        margin-top: 0.25rem;
      }
      .soft-card {
        background: rgba(24, 24, 27, 0.9);
        border: 1px solid rgba(82, 82, 91, 0.7);
        border-radius: 12px;
      }
      .mini-muted {
        font-size: 11px;
        color: #a1a1aa;
        text-transform: uppercase;
        letter-spacing: .08em;
      }
      .profile-group-item {
        cursor: pointer;
        padding: .75rem;
        border-radius: .7rem;
        border: 1px solid rgba(63,63,70,.8);
        background: rgba(24,24,27,.85);
      }
      .profile-group-item:hover { background: rgba(39,39,42,.92); }
      .profile-group-item.active {
        border-color: rgba(34,211,238,.6);
        background: rgba(34,211,238,.12);
      }
      .pro-table th { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color:#a1a1aa; }
      .pro-table td { font-size: 13px; color:#e4e4e7; }
      .pro-table tr { border-top: 1px solid rgba(63,63,70,.5); }
      .pill {
        border-radius: 999px;
        padding: .2rem .55rem;
        font-size: 11px;
        border: 1px solid rgba(82,82,91,.9);
        background: rgba(39,39,42,.95);
        color: #cbd5e1;
      }
    </style>
  </head>
  <body class="bg-gray-900 text-white">
    <!-- Main Container -->
    <div class="flex flex-col md:flex-row h-screen">
      <!-- Sidebar -->
      <aside id="sidebar" class="hidden md:flex flex-col w-64 p-3 transition-all duration-300 ease-in-out border-r border-zinc-800/70">
        <div class="p-3 mb-2 flex items-center gap-2 border-b border-zinc-800/80">
          <div class="w-7 h-7 rounded-lg bg-gradient-to-br from-cyan-500 to-violet-500 flex items-center justify-center shadow-lg shadow-cyan-500/20 shrink-0">
            <i class="fa-solid fa-robot text-[12px] text-white"></i>
          </div>
          <h1 class="text-sm font-bold sidebar-text tracking-tight">MakeBot+</h1>
        </div>
        <nav class="p-1 flex-grow">
          <ul class="grid gap-2">
            <li>
              <button id="nav-tasks" class="nav-btn item-row w-full" onclick="window.showTasks()">
                <span class="flex items-center gap-3"><i class="fas fa-list text-lg"></i><span class="sidebar-text">Tasks</span></span>
                <i class="fa-solid fa-chevron-right text-xs opacity-50"></i>
              </button>
            </li>
            <li>
              <button id="nav-proxies" class="nav-btn item-row w-full" onclick="window.showProxies()">
                <span class="flex items-center gap-3"><i class="fas fa-network-wired text-lg"></i><span class="sidebar-text">Proxies</span></span>
                <i class="fa-solid fa-chevron-right text-xs opacity-50"></i>
              </button>
            </li>
            <li>
              <button id="nav-settings" class="nav-btn item-row w-full" onclick="window.showSettings()">
                <span class="flex items-center gap-3"><i class="fas fa-cog text-lg"></i><span class="sidebar-text">Settings</span></span>
                <i class="fa-solid fa-chevron-right text-xs opacity-50"></i>
              </button>
            </li>
            <li>
              <button id="nav-automations" class="nav-btn item-row w-full" onclick="window.showAutomations()">
                <span class="flex items-center gap-3"><i class="fas fa-robot text-lg"></i><span class="sidebar-text">Automations</span></span>
                <i class="fa-solid fa-chevron-right text-xs opacity-50"></i>
              </button>
            </li>
            <li>
              <button id="nav-profiles" class="nav-btn item-row w-full" onclick="window.showProfiles()">
                <span class="flex items-center gap-3"><i class="fas fa-id-badge text-lg"></i><span class="sidebar-text">Profiles</span></span>
                <i class="fa-solid fa-chevron-right text-xs opacity-50"></i>
              </button>
            </li>
          </ul>
        </nav>
        <button onclick="toggleSidebar()" class="mt-2 p-2 bg-zinc-800 rounded hover:bg-zinc-700 flex items-center justify-center">
          <i class="fas fa-angle-double-left" id="sidebarToggleIcon"></i>
        </button>
      </aside>

      <!-- Mobile Menu -->
      <div class="md:hidden bg-gray-800 text-white p-4 flex justify-between items-center border-b border-gray-700">
        <h1 class="text-xl font-bold">Make Engine</h1>
        <button onclick="toggleMobileMenu()">
          <i class="fas fa-bars text-xl"></i>
        </button>
      </div>
      <div id="mobileMenu" class="hidden md:hidden bg-gray-800 text-white px-4 pb-4 transition-all duration-300 ease-in-out">
        <ul>
          <li class="mb-2 p-2 bg-gray-700 rounded cursor-pointer" onclick="window.showTasks(); toggleMobileMenu()">Tasks</li>
          <li class="mb-2 p-2 bg-gray-700 rounded cursor-pointer" onclick="window.showProxies(); toggleMobileMenu()">Proxies</li>
          <li class="mb-2 p-2 bg-gray-700 rounded cursor-pointer" onclick="window.showSettings(); toggleMobileMenu()">Settings</li>
          <li class="mb-2 p-2 bg-gray-700 rounded cursor-pointer" onclick="window.showAutomations(); toggleMobileMenu()">Automations</li>
          <li class="mb-2 p-2 bg-gray-700 rounded cursor-pointer" onclick="window.showProfiles(); toggleMobileMenu()">Profiles</li>
        </ul>
      </div>

      <!-- Main Content -->
      <main class="flex-1 flex flex-col min-w-0 overflow-hidden" id="content">
        <header class="flex items-center justify-between h-12 px-5 border-b border-zinc-800/60 shrink-0 bg-zinc-950/90 backdrop-blur-sm">
          <h1 id="mainPageTitle" class="text-sm font-semibold text-white tracking-wide">Tasks</h1>
          <div class="flex items-center gap-4">
            <span id="mainConnectionStatus" class="text-[11px] text-emerald-400">● Online</span>
            <div class="w-6 h-6 rounded-full bg-gradient-to-br from-cyan-500 to-violet-500 flex items-center justify-center text-[10px] font-bold text-white">M</div>
          </div>
        </header>
        <div class="flex-1 overflow-y-auto p-4 md:p-6">
        <!-- Tasks View -->
        <div id="taskView" class="panel p-4 md:p-6">
          <div class="flex flex-wrap gap-2 justify-between items-center mb-4">
            <h2 class="text-2xl font-bold panel-title">Task Groups</h2>
            <div class="flex flex-wrap gap-2 items-center">
              <div id="liveClock" class="text-sm text-gray-300 px-3 py-2 rounded bg-gray-800 border border-gray-700"></div>
              <button onclick="window.showCreateTaskPopup()" class="btn btn-primary">+ Create Task Group</button>
            </div>
          </div>
          <div id="taskGroups" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4"></div>
          <div id="taskTable" class="hidden mt-4">
            <h2 id="taskGroupTitle" class="text-2xl font-bold">Tasks</h2>
            <input id="taskSearch" type="text" placeholder="Search tasks..." class="field-input mb-4">
            <div class="overflow-x-auto panel p-0">
              <table class="w-full table-fixed text-sm">
                <thead class="bg-white/5 uppercase text-xs">
                  <tr class="text-center">
                    <th class="p-2">Site</th>
                    <th class="p-2">Mode</th>
                    <th class="p-2">Input</th>
                    <th class="p-2">Size</th>
                    <th class="p-2">Profile</th>
                    <th class="p-2">Proxy</th>
                    <th class="p-2">Status</th>
                  </tr>
                </thead>
                <tbody id="taskTableBody" class="text-center divide-y divide-white/5"></tbody>
              </table>
            </div>
          </div>
        </div>

      <script>
        const groupOffsets = {};
        function updateClock() {
          const now = new Date();
          const hours = now.getHours().toString().padStart(2, '0');
          const minutes = now.getMinutes().toString().padStart(2, '0');
          const seconds = now.getSeconds().toString().padStart(2, '0');
          document.getElementById('liveClock').textContent = `${hours}:${minutes}:${seconds}`;
        }
        setInterval(updateClock, 1000);
        updateClock();
      </script>

        <!-- Proxies View -->
        <div id="proxyView" class="hidden panel p-4 md:p-6">
          <div class="flex flex-wrap gap-2 justify-between items-center mb-4">
            <h2 class="text-2xl font-bold panel-title">Proxy Groups</h2>
            <div class="flex flex-wrap gap-2">
            </div>
          </div>
          <input id="proxySearch" type="text" placeholder="Search proxies..." class="field-input mb-4">
          <div id="proxyGroups" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"></div>
          <textarea id="proxyContent" class="w-full h-40 field-input hidden" spellcheck="false"></textarea>
          <button onclick="saveProxy()" class="btn btn-primary mt-2" id="saveProxyButton" style="display:none;">Save Proxies</button>
        </div>

        <!-- Settings View -->
        <div id="settingsView" class="hidden panel p-4 md:p-6">
          <h2 class="text-2xl font-bold mb-4">Settings</h2>
          <div class="bg-gray-800 p-4 rounded mb-4">
            <h3 class="text-xl font-bold mb-3 text-white">Integrations</h3>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div class="md:col-span-2">
                <label class="field-label">Discord Webhook URL</label>
                <input id="settingsWebhookUrl" class="field-select" placeholder="https://discord.com/api/webhooks/...">
              </div>
              <div>
                <label class="field-label">Discord Bot Token</label>
                <input id="settingsDiscordBotToken" class="field-select" placeholder="Bot token">
              </div>
              <div>
                <label class="field-label">Discord Channel ID</label>
                <input id="settingsDiscordChannelId" class="field-select" placeholder="Channel id">
              </div>
            </div>
            <div class="flex flex-wrap gap-2 mt-3">
              <button onclick="loadAppSettings()" class="btn btn-muted">Reload</button>
              <button onclick="saveAppSettings()" class="btn btn-primary">Save Integration Settings</button>
            </div>
          </div>

          <div class="bg-gray-800 p-4 rounded mb-4">
            <h3 class="text-xl font-bold mb-2 text-white">Manage Sites</h3>
            <div class="flex flex-wrap gap-2 mb-2">
            <button onclick="showAddSiteModal()" class="btn btn-primary">Add Site</button>
            </div>
            <table class="w-full table-fixed text-sm">
              <thead class="bg-white/5 uppercase text-xs">
                <tr class="text-center">
                  <th class="p-2">Name</th>
                  <th class="p-2">URL</th>
                  <th class="p-2">Actions</th>
                </tr>
              </thead>
              <tbody id="sitesTableBody" class="text-center divide-y divide-white/5"></tbody>
            </table>
          </div>
        </div>

        <!-- NEW: Automations View -->
        <div id="automationView" class="hidden panel p-4 md:p-6">
          <div class="flex justify-between items-center mb-4">
            <h2 class="text-2xl font-bold">Automations</h2>
            <button onclick="window.showCreateAutomationPopup()" class="btn btn-primary">+ Create Automation Group</button>
          </div>
          <div id="automationGroups" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4"></div>
        </div>

      <!-- Profiles View -->
      <div id="profilesView" class="hidden panel p-4 md:p-6">
        <div class="flex flex-wrap gap-2 justify-between items-center mb-4">
          <h2 class="text-2xl font-bold panel-title">Profiles</h2>
          <div class="flex flex-wrap gap-2">
            <button onclick="window.openProfileCreateModal()" class="btn btn-primary">+ New Profile</button>
          </div>
        </div>
        <div class="grid grid-cols-1 xl:grid-cols-12 gap-4">
          <div class="xl:col-span-4 soft-card p-3">
            <div class="flex items-center justify-between mb-2">
              <div class="mini-muted">Profile Groups</div>
              <button class="btn btn-muted text-xs" onclick="loadProfileFiles()">Refresh</button>
            </div>
            <div id="profileGroupList" class="space-y-2 max-h-[520px] overflow-y-auto"></div>
          </div>
          <div class="xl:col-span-8 soft-card p-3">
            <div class="flex flex-wrap gap-2 justify-between items-center mb-3">
              <div>
                <div class="mini-muted">Current Group</div>
                <div id="profileCurrentGroup" class="text-lg font-semibold">—</div>
              </div>
              <div id="profileMeta" class="flex gap-2"></div>
            </div>
            <input id="profileSearch" type="text" placeholder="Search profiles (name, email, city, country...)" class="field-input mb-3">
            <div class="overflow-x-auto">
              <table class="w-full pro-table">
                <thead>
                  <tr>
                    <th class="text-left p-2">Profile</th>
                    <th class="text-left p-2">Contact</th>
                    <th class="text-left p-2">Location</th>
                    <th class="text-right p-2">Actions</th>
                  </tr>
                </thead>
                <tbody id="profileTableBody"></tbody>
              </table>
              <div id="profileEmptyState" class="text-sm text-zinc-500 py-8 text-center">Select a profile group to view entries.</div>
            </div>
          </div>
        </div>
      </div>

      </div>
      </main>
    </div>




    <div id="profileModal" class="modal-overlay hidden">
      <div class="modal-card w-full max-w-3xl p-6 max-h-[88vh] overflow-y-auto">
        <div class="flex items-center justify-between mb-4">
          <h2 id="profileModalTitle" class="text-xl font-bold">Edit Profile</h2>
          <button onclick="closeProfileModal()" class="control-btn px-3 py-1 rounded">Close</button>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <label class="field-label">Profile Group</label>
            <input id="profileModalGroup" class="field-select" placeholder="Group file (without .csv)">
          </div>
          <div>
            <label class="field-label">Profile Name</label>
            <input id="profileModal_profileName" class="field-select">
          </div>
          <div>
            <label class="field-label">First Name</label>
            <input id="profileModal_firstName" class="field-select">
          </div>
          <div>
            <label class="field-label">Last Name</label>
            <input id="profileModal_lastName" class="field-select">
          </div>
          <div>
            <label class="field-label">Email</label>
            <input id="profileModal_email" class="field-select">
          </div>
          <div>
            <label class="field-label">Phone Number</label>
            <input id="profileModal_phoneNumber" class="field-select">
          </div>
          <div class="md:col-span-2">
            <label class="field-label">Address 1</label>
            <input id="profileModal_address1" class="field-select">
          </div>
          <div class="md:col-span-2">
            <label class="field-label">Address 2</label>
            <input id="profileModal_address2" class="field-select">
          </div>
          <div>
            <label class="field-label">City</label>
            <input id="profileModal_city" class="field-select">
          </div>
          <div>
            <label class="field-label">State</label>
            <input id="profileModal_state" class="field-select">
          </div>
          <div>
            <label class="field-label">Zipcode</label>
            <input id="profileModal_zipcode" class="field-select">
          </div>
          <div>
            <label class="field-label">Country</label>
            <input id="profileModal_country" class="field-select">
          </div>
          <div>
            <label class="field-label">Card Number</label>
            <input id="profileModal_ccNumber" class="field-select">
          </div>
          <div class="grid grid-cols-3 gap-2">
            <div>
              <label class="field-label">Month</label>
              <input id="profileModal_ccMonth" class="field-select">
            </div>
            <div>
              <label class="field-label">Year</label>
              <input id="profileModal_ccYear" class="field-select">
            </div>
            <div>
              <label class="field-label">CVV</label>
              <input id="profileModal_cvv" class="field-select">
            </div>
          </div>
        </div>
        <div class="modal-actions">
          <button id="profileDeleteBtn" onclick="window.deleteProfileFromModal()" class="btn btn-danger hidden">Delete</button>
          <button onclick="window.saveProfileFromModal()" class="btn btn-primary">Save Profile</button>
        </div>
      </div>
    </div>

    <!-- NEW: Create Automation Group Popup -->
    <div id="createAutomationPopup" class="modal-overlay hidden">
      <div class="modal-card p-6 text-left w-full max-w-2xl max-h-[80vh] overflow-y-auto">
        <h2 id="automationPopupHeader" class="text-xl font-bold mb-4 text-center">
          Create Automation Group
        </h2>
        <label for="automationGroupName" class="field-label">Group Name</label>
        <input id="automationGroupName" class="field-select" placeholder="Enter group name">
        
        <label class="field-label">Select Store(s)</label>
        <details id="storeSelection" class="size-dropdown">
          <summary class="bg-gray-700 text-white p-2 rounded">Select Stores</summary>
          <div class="checkbox-container">
            <label class="text-white"><input type="checkbox" id="selectAllStores" value="all"> All Stores</label>
            <!-- Options will be populated via JS from /sites -->
          </div>
        </details>
        
        <label for="skuInput" class="field-label">SKU(s) (comma separated)</label>
        <input id="skuInput" class="field-select" placeholder="e.g. DB4612-100">

        <label for="automationProfileGroupSelect" class="field-label">Profile Group</label>
        <select id="automationProfileGroupSelect" class="field-select">
          <option value="" disabled selected>Select Profile Group</option>
        </select>
        
        <label for="profileInput" class="field-label">Profile</label>
        <input id="profileInput" class="field-select" placeholder="Enter profile name">
        
        <label for="proxyInput" class="field-label">Proxy Group</label>
        <input id="proxyInput" class="field-select" placeholder="Enter proxy group">
        
        <label for="taskCountInput" class="field-label">Task Count</label>
        <input id="taskCountInput" type="number" class="field-select" placeholder="e.g. 250">
        
        <label for="modeSelect" class="field-label">Mode</label>
        <select id="modeSelect" class="field-select">
          <option value="preload">Preload</option>
          <option value="preloadwait">PreloadWait</option>
          <option value="preloadstuck">PreloadStuck</option>
          <option value="direct">Direct</option>
          <option value="fast">Fast</option>
        </select>
        
        <label class="field-label">Select Sizes</label>
        <details id="automationSizes" class="size-dropdown">
          <summary class="bg-gray-700 text-white p-2 rounded">Select Sizes</summary>
          <div class="checkbox-container">
            <div class="font-bold text-white">Preset Options</div>
            <label class="text-white"><input type="checkbox" value="random"> Random</label>
            <label class="text-white"><input type="checkbox" value="Mens Sizing (9-13)"> Mens Sizing (9-13)</label>
            <label class="text-white"><input type="checkbox" value="GS Sizing (5-7)"> GS Sizing (5-7)</label>
            <label class="text-white"><input type="checkbox" value="Big Mens Sizing (10.5-14)"> Big Mens Sizing (10.5-14)</label>
            <div class="font-bold text-white">Sneaker Sizes</div>
            <label class="text-white"><input type="checkbox" value="3.5"> 3.5</label>
            <label class="text-white"><input type="checkbox" value="4"> 4</label>
            <label class="text-white"><input type="checkbox" value="4.5"> 4.5</label>
            <label class="text-white"><input type="checkbox" value="5"> 5</label>
            <label class="text-white"><input type="checkbox" value="5.5"> 5.5</label>
            <label class="text-white"><input type="checkbox" value="6"> 6</label>
            <label class="text-white"><input type="checkbox" value="6.5"> 6.5</label>
            <label class="text-white"><input type="checkbox" value="7"> 7</label>
            <label class="text-white"><input type="checkbox" value="7.5"> 7.5</label>
            <label class="text-white"><input type="checkbox" value="8"> 8</label>
            <label class="text-white"><input type="checkbox" value="8.5"> 8.5</label>
            <label class="text-white"><input type="checkbox" value="9"> 9</label>
            <label class="text-white"><input type="checkbox" value="9.5"> 9.5</label>
            <label class="text-white"><input type="checkbox" value="10"> 10</label>
            <label class="text-white"><input type="checkbox" value="10.5"> 10.5</label>
            <label class="text-white"><input type="checkbox" value="11"> 11</label>
            <label class="text-white"><input type="checkbox" value="11.5"> 11.5</label>
            <label class="text-white"><input type="checkbox" value="12"> 12</label>
            <label class="text-white"><input type="checkbox" value="12.5"> 12.5</label>
            <label class="text-white"><input type="checkbox" value="13"> 13</label>
            <label class="text-white"><input type="checkbox" value="13.5"> 13.5</label>
            <label class="text-white"><input type="checkbox" value="14"> 14</label>
            <label class="text-white"><input type="checkbox" value="14.5"> 14.5</label>
            <label class="text-white"><input type="checkbox" value="15"> 15</label>
            <label class="text-white"><input type="checkbox" value="16"> 16</label>
            <label class="text-white"><input type="checkbox" value="17"> 17</label>
            <label class="text-white"><input type="checkbox" value="18"> 18</label>
            <div class="font-bold text-white">Clothing Sizes</div>
            <label class="text-white"><input type="checkbox" value="XSmall"> XSmall</label>
            <label class="text-white"><input type="checkbox" value="Small"> Small</label>
            <label class="text-white"><input type="checkbox" value="Medium"> Medium</label>
            <label class="text-white"><input type="checkbox" value="Large"> Large</label>
            <label class="text-white"><input type="checkbox" value="XLarge"> XLarge</label>
            <label class="text-white"><input type="checkbox" value="XXLarge"> XXLarge</label>
          </div>
        </details>
        
        <label for="runningTimeInput" class="field-label">Time Running (minutes)</label>
        <input id="runningTimeInput" type="number" class="field-select" placeholder="e.g. 10">
        
        <div class="modal-actions">
          <button onclick="hideCreateAutomationPopup()" class="btn btn-muted">Cancel</button>
          <button onclick="submitCreateAutomation()" class="btn btn-primary">Create</button>
        </div>
      </div>
    </div>
    <!-- Add Site Modal -->
    <div id="addSiteModal" class="modal-overlay hidden">
  <div class="modal-card p-6 w-full max-w-md">
    <h2 class="text-xl font-bold mb-4 text-white">Add Site</h2>
    <label for="newSiteName" class="field-label">Site Name</label>
    <input id="newSiteName" class="field-select" placeholder="Enter site name">
    <label for="newSiteURL" class="field-label">Site URL</label>
    <input id="newSiteURL" class="field-select" placeholder="Enter site URL">
    <div class="modal-actions">
      <button onclick="closeAddSiteModal()" class="btn btn-muted">Cancel</button>
      <button onclick="submitNewSite()" class="btn btn-primary">Save</button>
    </div>
  </div>
</div>

<!-- Edit Site Modal -->
<div id="editSiteModal" class="modal-overlay hidden">
  <div class="modal-card p-6 w-full max-w-md">
    <h2 class="text-xl font-bold mb-4 text-white">Edit Site</h2>
    <!-- Hidden field to store original name -->
    <input type="hidden" id="editSiteOriginalName">
    <label for="editSiteName" class="field-label">Site Name</label>
    <input id="editSiteName" class="field-select" placeholder="Enter new site name">
    <label for="editSiteURL" class="field-label">Site URL</label>
    <input id="editSiteURL" class="field-select" placeholder="Enter new site URL">
    <div class="modal-actions">
      <button onclick="closeEditSiteModal()" class="btn btn-muted">Cancel</button>
      <button onclick="submitEditSite()" class="btn btn-primary">Save</button>
    </div>
  </div>
</div>

    <!-- Create Task Group Popup -->
    <div id="createTaskPopup" class="modal-overlay hidden">
      <div class="modal-card p-6 text-left w-full max-w-2xl max-h-[80vh] overflow-y-auto">
        <h2 class="text-xl font-bold mb-4 text-center">Create Task Group</h2>
        <label for="taskGroupName" class="field-label">Task Group Name</label>
        <input id="taskGroupName" class="field-select">
        <label for="createSiteDropdown" class="field-label">Site</label>
        <div class="mb-2" id="dropdownContainerCreate">
          <select id="createSiteDropdown" onchange="handleChange(this)" class="field-select">
            <option value="" disabled selected>Select Site</option>
            <option value="custom">Custom URL...</option>
          </select>
        </div>
        <label for="inputValue" class="field-label">Input</label>
        <input id="inputValue" class="field-select">
        <label class="field-label">Size</label>
        <details id="createSizeDropdown" class="size-dropdown">
          <summary class="bg-gray-700 text-white p-2 rounded">Select Sizes</summary>
          <div class="checkbox-container">
            <div class="font-bold text-white">Preset Options</div>
            <label class="text-white"><input type="checkbox" value="random"> Random</label>
            <label class="text-white"><input type="checkbox" value="Mens Sizing (9-13)"> Mens Sizing (9-13)</label>
            <label class="text-white"><input type="checkbox" value="GS Sizing (5-7)"> GS Sizing (5-7)</label>
            <label class="text-white"><input type="checkbox" value="Big Mens Sizing (10.5-14)"> Big Mens Sizing (10.5-14)</label>
            <div class="font-bold text-white">Sneaker Sizes</div>
            <label class="text-white"><input type="checkbox" value="3.5"> 3.5</label>
            <label class="text-white"><input type="checkbox" value="4"> 4</label>
            <label class="text-white"><input type="checkbox" value="4.5"> 4.5</label>
            <label class="text-white"><input type="checkbox" value="5"> 5</label>
            <label class="text-white"><input type="checkbox" value="5.5"> 5.5</label>
            <label class="text-white"><input type="checkbox" value="6"> 6</label>
            <label class="text-white"><input type="checkbox" value="6.5"> 6.5</label>
            <label class="text-white"><input type="checkbox" value="7"> 7</label>
            <label class="text-white"><input type="checkbox" value="7.5"> 7.5</label>
            <label class="text-white"><input type="checkbox" value="8"> 8</label>
            <label class="text-white"><input type="checkbox" value="8.5"> 8.5</label>
            <label class="text-white"><input type="checkbox" value="9"> 9</label>
            <label class="text-white"><input type="checkbox" value="9.5"> 9.5</label>
            <label class="text-white"><input type="checkbox" value="10"> 10</label>
            <label class="text-white"><input type="checkbox" value="10.5"> 10.5</label>
            <label class="text-white"><input type="checkbox" value="11"> 11</label>
            <label class="text-white"><input type="checkbox" value="11.5"> 11.5</label>
            <label class="text-white"><input type="checkbox" value="12"> 12</label>
            <label class="text-white"><input type="checkbox" value="12.5"> 12.5</label>
            <label class="text-white"><input type="checkbox" value="13"> 13</label>
            <label class="text-white"><input type="checkbox" value="13.5"> 13.5</label>
            <label class="text-white"><input type="checkbox" value="14"> 14</label>
            <label class="text-white"><input type="checkbox" value="14.5"> 14.5</label>
            <label class="text-white"><input type="checkbox" value="15"> 15</label>
            <label class="text-white"><input type="checkbox" value="16"> 16</label>
            <label class="text-white"><input type="checkbox" value="17"> 17</label>
            <label class="text-white"><input type="checkbox" value="18"> 18</label>
            <div class="font-bold text-white">Clothing Sizes</div>
            <label class="text-white"><input type="checkbox" value="XSmall"> XSmall</label>
            <label class="text-white"><input type="checkbox" value="Small"> Small</label>
            <label class="text-white"><input type="checkbox" value="Medium"> Medium</label>
            <label class="text-white"><input type="checkbox" value="Large"> Large</label>
            <label class="text-white"><input type="checkbox" value="XLarge"> XLarge</label>
            <label class="text-white"><input type="checkbox" value="XXLarge"> XXLarge</label>
          </div>
        </details>
        <label for="profileGroupSelect" class="field-label">Profile Group</label>
        <select id="profileGroupSelect" class="field-select">
          <option value="" disabled selected>Select Profile Group</option>
        </select>

        <label class="field-label">Select Profiles</label>
        <details id="profileSelection" class="size-dropdown">
          <summary class="bg-gray-700 text-white p-2 rounded">Loading profiles…</summary>
          <div class="checkbox-container" id="profileCheckboxes">
            <!-- JS will populate one checkbox per profileName, plus an “All Profiles” option -->
          </div>
        </details>
        <label for="proxyGroup" class="field-label">Proxy Group</label>
        <select id="proxyGroup" class="field-select">
          <option value="" disabled selected>Select Proxy</option>
        </select>
        <label for="colorValue" class="field-label">Color</label>
        <input id="colorValue" value="random" class="field-select">
        <label for="modeValue" class="field-label">Mode</label>
        <select id="modeValue" class="field-select">
          <option value="preload">Preload</option>
          <option value="preloadwait">PreloadWait</option>
          <option value="preloadstuck">PreloadStuck</option>
          <option value="direct">Direct</option>
          <option value="fast">Fast</option>
        </select>
        <label for="cartQuantity" class="field-label">Cart Quantity</label>
        <input id="cartQuantity" value="1" class="field-select">
        <label for="delayValue" class="field-label">Delay</label>
        <input id="delayValue" value="3333" class="field-select">
        <label for="taskQuantity" class="field-label">Task Quantity</label>
        <input id="taskQuantity" type="number" value="250" class="field-select">
        <div class="modal-actions">
          <button onclick="hideCreateTaskPopup()" class="btn btn-muted">Cancel</button>
          <button onclick="submitTaskGroup()" class="btn btn-primary">Create</button>
        </div>
      </div>
    </div>
    <!-- Add Task Popup -->
    <div id="addTaskPopup" class="modal-overlay hidden">
      <div class="modal-card p-6 text-left w-full max-w-2xl max-h-[80vh] overflow-y-auto">
        <h2 class="text-xl font-bold mb-4 text-center">Add Task</h2>
        <input type="hidden" id="addTaskGroupName">
        <label for="addSiteDropdown" class="field-label">Site</label>
        <div class="mb-2" id="dropdownContainerAdd">
          <select id="addSiteDropdown" onchange="handleChange(this)" class="field-select">
            <option value="" disabled selected>Select Site</option>
            <option value="custom">Custom URL...</option>
          </select>
        </div>
        <label for="addMode" class="field-label">Mode</label>
        <select id="addMode" class="field-select">
          <option value="preload">Preload</option>
          <option value="preloadwait">PreloadWait</option>
          <option value="preloadstuck">PreloadStuck</option>
          <option value="direct">Direct</option>
          <option value="fast">Fast</option>
        </select>
        <label for="addInput" class="field-label">Input</label>
        <input id="addInput" class="field-select">
        <label class="field-label">Size</label>
        <details id="addSizeDropdown" class="size-dropdown">
          <summary class="bg-gray-700 text-white p-2 rounded">Select Sizes</summary>
          <div class="checkbox-container">
            <div class="font-bold text-white">Preset Options</div>
            <label class="text-white"><input type="checkbox" value="random"> Random</label>
            <label class="text-white"><input type="checkbox" value="Mens Sizing (9-13)"> Mens Sizing (9-13)</label>
            <label class="text-white"><input type="checkbox" value="GS Sizing (5-7)"> GS Sizing (5-7)</label>
            <label class="text-white"><input type="checkbox" value="Big Mens Sizing (10.5-14)"> Big Mens Sizing (10.5-14)</label>
            <div class="font-bold text-white">Sneaker Sizes</div>
            <label class="text-white"><input type="checkbox" value="3.5"> 3.5</label>
            <label class="text-white"><input type="checkbox" value="4"> 4</label>
            <label class="text-white"><input type="checkbox" value="4.5"> 4.5</label>
            <label class="text-white"><input type="checkbox" value="5"> 5</label>
            <label class="text-white"><input type="checkbox" value="5.5"> 5.5</label>
            <label class="text-white"><input type="checkbox" value="6"> 6</label>
            <label class="text-white"><input type="checkbox" value="6.5"> 6.5</label>
            <label class="text-white"><input type="checkbox" value="7"> 7</label>
            <label class="text-white"><input type="checkbox" value="7.5"> 7.5</label>
            <label class="text-white"><input type="checkbox" value="8"> 8</label>
            <label class="text-white"><input type="checkbox" value="8.5"> 8.5</label>
            <label class="text-white"><input type="checkbox" value="9"> 9</label>
            <label class="text-white"><input type="checkbox" value="9.5"> 9.5</label>
            <label class="text-white"><input type="checkbox" value="10"> 10</label>
            <label class="text-white"><input type="checkbox" value="10.5"> 10.5</label>
            <label class="text-white"><input type="checkbox" value="11"> 11</label>
            <label class="text-white"><input type="checkbox" value="11.5"> 11.5</label>
            <label class="text-white"><input type="checkbox" value="12"> 12</label>
            <label class="text-white"><input type="checkbox" value="12.5"> 12.5</label>
            <label class="text-white"><input type="checkbox" value="13"> 13</label>
            <label class="text-white"><input type="checkbox" value="13.5"> 13.5</label>
            <label class="text-white"><input type="checkbox" value="14"> 14</label>
            <label class="text-white"><input type="checkbox" value="14.5"> 14.5</label>
            <label class="text-white"><input type="checkbox" value="15"> 15</label>
            <label class="text-white"><input type="checkbox" value="16"> 16</label>
            <label class="text-white"><input type="checkbox" value="17"> 17</label>
            <label class="text-white"><input type="checkbox" value="18"> 18</label>
            <div class="font-bold text-white">Clothing Sizes</div>
            <label class="text-white"><input type="checkbox" value="XSmall"> XSmall</label>
            <label class="text-white"><input type="checkbox" value="Small"> Small</label>
            <label class="text-white"><input type="checkbox" value="Medium"> Medium</label>
            <label class="text-white"><input type="checkbox" value="Large"> Large</label>
            <label class="text-white"><input type="checkbox" value="XLarge"> XLarge</label>
            <label class="text-white"><input type="checkbox" value="XXLarge"> XXLarge</label>
          </div>
        </details>
        <!-- inside your Add Task Popup -->
        <label for="addProfileGroupSelect" class="field-label">Profile Group</label>
        <select id="addProfileGroupSelect" class="field-select">
          <option value="" disabled selected>Select Profile Group</option>
        </select>

        <label class="field-label">Select Profiles</label>
        <details id="addProfileSelection" class="size-dropdown">
          <summary class="bg-gray-700 text-white p-2 rounded">Loading profiles…</summary>
          <div id="addProfileCheckboxes" class="checkbox-container">
            <!-- JS will populate one checkbox per profileName, plus “All Profiles” -->
          </div>
        </details>
        <label for="addProxy" class="field-label">Proxy Group</label>
        <select id="addProxy" class="field-select">
          <option value="" disabled selected>Select Proxy</option>
        </select>
        <label for="addTaskQuantity" class="field-label">Task Quantity</label>
        <input id="addTaskQuantity" type="number" value="1" class="field-select">
        <div class="modal-actions">
          <button onclick="window.hideAddTaskPopup()" class="btn btn-muted">Cancel</button>
          <button onclick="window.submitAddTask()" class="btn btn-primary">Add Task</button>
        </div>
      </div>
    </div>
    <!-- Edit Task Group Popup (Mass Edit) -->
    <div id="editTaskGroupPopup" class="modal-overlay hidden">
      <div class="modal-card p-6 text-left w-full max-w-2xl max-h-[80vh] overflow-y-auto">
        <h2 class="text-xl font-bold mb-4 text-center">Edit Task Group</h2>
        <input type="hidden" id="editTaskGroupFileName">
        <label for="editSiteDropdown" class="field-label">Site</label>
        <div class="mb-2" id="dropdownContainerEdit">
          <select id="editSiteDropdown" onchange="handleChange(this)" class="field-select">
            <option value="" disabled selected>Select Site</option>
            <option value="custom">Custom URL...</option>
          </select>
        </div>
        <label for="editInputValue" class="field-label">Input</label>
        <input id="editInputValue" class="field-select">
        <label for="editModeValue" class="field-label">Mode</label>
        <select id="editModeValue" class="field-select">
          <option value="" disabled selected>Select Mode</option>
          <option value="preload">Preload</option>
          <option value="preloadwait">PreloadWait</option>
          <option value="preloadstuck">PreloadStuck</option>
          <option value="direct">Direct</option>
          <option value="fast">Fast</option>
        </select>
        <label class="field-label">Size</label>
        <details id="editSizeDropdown" class="size-dropdown">
          <summary class="bg-gray-700 text-white p-2 rounded">Select Sizes</summary>
          <div class="checkbox-container">
            <div class="font-bold text-white">Preset Options</div>
            <label class="text-white"><input type="checkbox" value="random"> Random</label>
            <label class="text-white"><input type="checkbox" value="Mens Sizing (9-13)"> Mens Sizing (9-13)</label>
            <label class="text-white"><input type="checkbox" value="GS Sizing (5-7)"> GS Sizing (5-7)</label>
            <label class="text-white"><input type="checkbox" value="Big Mens Sizing (10.5-14)"> Big Mens Sizing (10.5-14)</label>
            <div class="font-bold text-white">Sneaker Sizes</div>
            <label class="text-white"><input type="checkbox" value="3.5"> 3.5</label>
            <label class="text-white"><input type="checkbox" value="4"> 4</label>
            <label class="text-white"><input type="checkbox" value="4.5"> 4.5</label>
            <label class="text-white"><input type="checkbox" value="5"> 5</label>
            <label class="text-white"><input type="checkbox" value="5.5"> 5.5</label>
            <label class="text-white"><input type="checkbox" value="6"> 6</label>
            <label class="text-white"><input type="checkbox" value="6.5"> 6.5</label>
            <label class="text-white"><input type="checkbox" value="7"> 7</label>
            <label class="text-white"><input type="checkbox" value="7.5"> 7.5</label>
            <label class="text-white"><input type="checkbox" value="8"> 8</label>
            <label class="text-white"><input type="checkbox" value="8.5"> 8.5</label>
            <label class="text-white"><input type="checkbox" value="9"> 9</label>
            <label class="text-white"><input type="checkbox" value="9.5"> 9.5</label>
            <label class="text-white"><input type="checkbox" value="10"> 10</label>
            <label class="text-white"><input type="checkbox" value="10.5"> 10.5</label>
            <label class="text-white"><input type="checkbox" value="11"> 11</label>
            <label class="text-white"><input type="checkbox" value="11.5"> 11.5</label>
            <label class="text-white"><input type="checkbox" value="12"> 12</label>
            <label class="text-white"><input type="checkbox" value="12.5"> 12.5</label>
            <label class="text-white"><input type="checkbox" value="13"> 13</label>
            <label class="text-white"><input type="checkbox" value="13.5"> 13.5</label>
            <label class="text-white"><input type="checkbox" value="14"> 14</label>
            <label class="text-white"><input type="checkbox" value="14.5"> 14.5</label>
            <label class="text-white"><input type="checkbox" value="15"> 15</label>
            <label class="text-white"><input type="checkbox" value="16"> 16</label>
            <label class="text-white"><input type="checkbox" value="17"> 17</label>
            <label class="text-white"><input type="checkbox" value="18"> 18</label>
            <div class="font-bold text-white">Clothing Sizes</div>
            <label class="text-white"><input type="checkbox" value="XSmall"> XSmall</label>
            <label class="text-white"><input type="checkbox" value="Small"> Small</label>
            <label class="text-white"><input type="checkbox" value="Medium"> Medium</label>
            <label class="text-white"><input type="checkbox" value="Large"> Large</label>
            <label class="text-white"><input type="checkbox" value="XLarge"> XLarge</label>
            <label class="text-white"><input type="checkbox" value="XXLarge"> XXLarge</label>
          </div>
        </details>
        <label for="editProfileGroupSelect" class="field-label">Profile Group</label>
        <select id="editProfileGroupSelect" class="field-select">
          <option value="" disabled selected>Select Profile Group</option>
        </select>
        <label for="editProfileName" class="field-label">Profile Name</label>
        <input id="editProfileName" class="field-select">
        <label for="editProxyGroup" class="field-label">Proxy Group</label>
        <select id="editProxyGroup" class="field-select">
          <option value="" disabled selected>Select Proxy Group</option>
        </select>
        <label for="editColorValue" class="field-label">Color</label>
        <input id="editColorValue" class="field-select">
        <label for="editCartQuantity" class="field-label">Cart Quantity</label>
        <input id="editCartQuantity" class="field-select">
        <label for="editDelayValue" class="field-label">Delay</label>
        <input id="editDelayValue" class="field-select">
        <div class="modal-actions">
          <button onclick="window.hideEditTaskGroupPopup()" class="btn btn-muted">Cancel</button>
          <button onclick="window.submitEditTaskGroup()" class="btn btn-primary">Update</button>
        </div>
      </div>
    </div>
    <!-- Schedule Popup for Task Group Settings -->
    <div id="taskGroupSettingsPopup" class="modal-overlay hidden">
      <div class="modal-card p-6 text-left w-full max-w-xl">
        <h2 class="text-xl font-bold mb-4 text-center">Task Group Settings</h2>
        <input type="hidden" id="settingsTaskGroupFileName">
        <label for="settingsGroupName" class="field-label">Task Group Name</label>
        <input id="settingsGroupName" class="field-select">
        <label for="settingsStartTime" class="field-label">Schedule Start Time</label>
        <input id="settingsStartTime" type="datetime-local" class="field-select">
        <label for="settingsRunDuration" class="field-label">Run Duration (minutes)</label>
        <input id="settingsRunDuration" type="number" min="1" class="field-select">
        <label for="settingsTaskQuantity" class="field-label">Task Quantity</label>
        <input id="settingsTaskQuantity" type="number" min="1" class="field-select">
        <div class="modal-actions">
          <button onclick="clearSchedule()" class="btn btn-danger">Cancel</button>
          <button onclick="saveSchedule()" class="btn btn-primary">Save</button>
        </div>
      </div>
    </div>
    <!-- Delete Confirmation Popup -->
    <div id="deletePopup" class="modal-overlay hidden">
      <div class="modal-card p-6 text-center w-full max-w-md">
        <h2 class="text-xl font-bold text-white">Are you sure?</h2>
        <p id="deleteMessage" class="my-2 text-white"></p>
        <div class="modal-actions">
          <button onclick="hideDeletePopup()" class="btn btn-muted">Cancel</button>
          <button id="confirmDelete" class="btn btn-danger">Delete</button>
        </div>
      </div>
    </div>
    <!-- Global JavaScript -->
    <script>
      const baseUrl = "http://localhost:2345";
      const skipHeader = { "ngrok-skip-browser-warning": "true" };
      let deleteFileName = "";
      let scheduleTimers = {};
      window.currentGroup = "";
      let pollingInterval = null;
      let isPolling = false;
      const presetMapping = {
        "GS Sizing (5-7)": "5&5.5&6&6.5&7",
        "Mens Sizing (9-13)": "9&9.5&10&10.5&11&11.5&12&12.5&13",
        "Big Mens Sizing (10.5-14)": "10.5&11&11.5&12&12.5&13&13.5&14"
      };

      // Utility functions for dropdowns
      function updateSizeDropdown(dropdownId) {
        const dropdown = document.getElementById(dropdownId);
        const summary = dropdown.querySelector('summary');
        const checkboxes = dropdown.querySelectorAll('input[type="checkbox"]');
        let selectedValues = [];
        let presetSelected = false;
        checkboxes.forEach(cb => {
          if (cb.checked) {
            selectedValues.push(cb.value);
            if (presetMapping[cb.value]) {
              presetSelected = true;
            }
          }
        });
        if (presetSelected) {
          checkboxes.forEach(cb => {
            if (!presetMapping[cb.value]) {
              cb.disabled = true;
            }
          });
        } else {
          let nonPresetSelected = false;
          checkboxes.forEach(cb => {
            if (cb.checked && !presetMapping[cb.value]) {
              nonPresetSelected = true;
            }
          });
          if (nonPresetSelected) {
            checkboxes.forEach(cb => {
              if (presetMapping[cb.value]) {
                cb.disabled = true;
              }
            });
          } else {
            checkboxes.forEach(cb => cb.disabled = false);
          }
        }
        summary.innerText = selectedValues.length > 0 ? "Selected: " + selectedValues.join(", ") : "Select Sizes";
      }
      function setupSizeDropdown(dropdownId) {
        const dropdown = document.getElementById(dropdownId);
        const checkboxes = dropdown.querySelectorAll('input[type="checkbox"]');
        checkboxes.forEach(cb => {
          cb.addEventListener('change', () => updateSizeDropdown(dropdownId));
        });
        updateSizeDropdown(dropdownId);
      }
      function getSelectedSizes(dropdownId) {
        const container = document.getElementById(dropdownId);
        const checkboxes = container.querySelectorAll('input[type="checkbox"]:checked');
        let selected = [];
        checkboxes.forEach(cb => selected.push(cb.value));
        let result = [];
        selected.forEach(val => {
          if (presetMapping[val]) {
            result.push(...presetMapping[val].split("&"));
          } else {
            result.push(val);
          }
        });
        return result.join("&");
      }
      function populateProxyDropdown(dropdownId) {
        return new Promise((resolve, reject) => {
          fetch(baseUrl + "/proxy-files", { headers: skipHeader })
            .then(response => response.json())
            .then(proxyFiles => {
              const dropdown = document.getElementById(dropdownId);
              dropdown.innerHTML = "";
              const defaultOption = document.createElement("option");
              defaultOption.disabled = true;
              defaultOption.selected = true;
              defaultOption.value = "";
              defaultOption.innerText = "Select Proxy Group";
              dropdown.appendChild(defaultOption);
              proxyFiles.forEach(file => {
                const option = document.createElement("option");
                option.value = file.replace('.txt', '');
                option.text = file.replace('.txt', '');
                dropdown.appendChild(option);
              });
              resolve();
            })
            .catch(err => {
              console.error("Error populating proxy dropdown:", err);
              reject(err);
            });
        });
      }

      function populateProfileGroupDropdown(dropdownId) {
            return fetch(baseUrl + "/profile-groups")
                  .then(function(response) { return response.json(); })
                  .then(function(groups) {
                        var dropdown = document.getElementById(dropdownId);
                        if (dropdown) {
                              dropdown.innerHTML = '<option value="" disabled selected>Select Profile Group</option>';
                              groups.forEach(function(group) {
                                    var option = document.createElement("option");
                                    option.value = group;
                                    option.text = group;
                                    dropdown.appendChild(option);
                              });
                        }
                  })
                  .catch(function(err) {
                        console.error("Error fetching profile groups", err);
                  });
      }


      function populateSiteDropdown(dropdownId) {
        fetch(baseUrl + "/sites")
          .then(response => response.json())
          .then(sites => {
            window.sites = sites;
            const dropdown = document.getElementById(dropdownId);
            if (!dropdown) return;
            dropdown.innerHTML = '<option value="" disabled selected>Select Site</option>';
            sites.forEach(site => {
              const option = document.createElement("option");
              option.value = site.url;
              option.text = site.name;
              dropdown.appendChild(option);
            });
            const customOption = document.createElement("option");
            customOption.value = "custom";
            customOption.text = "Custom URL...";
            dropdown.appendChild(customOption);
          })
          .catch(err => console.error("Error fetching sites:", err));
      }
      function handleChange(selectEl) {
        if (selectEl.value === "custom") {
          const id = selectEl.id;
          const parent = selectEl.parentElement;
          const inputWrapper = document.createElement("div");
          const input = document.createElement("input");
          input.type = "text";
          input.placeholder = "Enter custom URL...";
          input.id = id;
          input.className = selectEl.className + " mb-2";
          inputWrapper.appendChild(input);
          const backBtn = document.createElement("button");
          backBtn.innerText = "← Back to dropdown";
          backBtn.className = "text-sm text-blue-500 hover:underline";
          backBtn.onclick = () => {
            const dropdown = document.createElement("select");
            dropdown.id = id;
            dropdown.className = selectEl.className;
            dropdown.onchange = () => handleChange(dropdown);
            dropdown.innerHTML = `
              <option value="" disabled selected>Select Site</option>
              ${getSiteOptionsHTML()}
              <option value="custom">Custom URL...</option>
            `;
            parent.replaceChild(dropdown, inputWrapper);
          };
          inputWrapper.appendChild(backBtn);
          parent.replaceChild(inputWrapper, selectEl);
        }
      }
      function getSiteOptionsHTML() {
        let html = "";
        if (window.sites && Array.isArray(window.sites) && window.sites.length > 0) {
          window.sites.forEach(site => {
            html += `<option value="${site.url}">${site.name}</option>`;
          });
        } else {
          html = `
          <option value="kith.com">Kith</option>
          <option value="shoepalace.com">Shoe Palace</option>
          <option value="https://shopnicekicks.com">Shop Nice Kicks</option>
          <option value="https://sneakerpolitics.com">Sneaker Politics</option>
          `;
        }
        return html;
      }

      /***************************
       * View Switching Functions
       ***************************/
      function hideAllViews() {
        ['taskView', 'proxyView', 'settingsView', 'automationView', 'profilesView']
          .forEach((id) => document.getElementById(id).classList.add('hidden'));
      }
      function setActiveNav(navId) {
        document.querySelectorAll('.nav-btn').forEach((el) => el.classList.remove('active'));
        const target = document.getElementById(navId);
        if (target) target.classList.add('active');
      }
      function setMainTitle(label) {
        const el = document.getElementById('mainPageTitle');
        if (el) el.textContent = label;
      }

      window.showTasks = function() {
        hideAllViews();
        setActiveNav('nav-tasks');
        setMainTitle('Tasks');
        document.getElementById('taskView').classList.remove('hidden');
        fetchTaskGroups();
      };
      window.showProxies = function() {
        hideAllViews();
        setActiveNav('nav-proxies');
        setMainTitle('Proxies');
        document.getElementById('proxyView').classList.remove('hidden');
        fetchProxies();
      };
      window.showSettings = function() {
        hideAllViews();
        setActiveNav('nav-settings');
        setMainTitle('Settings');
        document.getElementById('settingsView').classList.remove('hidden');
        loadAppSettings();
        loadSitesTable();
      };
      window.showAutomations = function() {
        hideAllViews();
        setActiveNav('nav-automations');
        setMainTitle('Automations');
        document.getElementById('automationView').classList.remove('hidden');
        loadAutomationGroups();
      };

      window.showProfiles = function() {
        hideAllViews();
        setActiveNav('nav-profiles');
        setMainTitle('Profiles');
        document.getElementById('profilesView').classList.remove('hidden');
        loadProfileFiles();
      };

      // Profile management: card list + edit popup per profile
      const profileFieldsOrder = [
        'profileName','firstName','lastName','email',
        'address1','address2','city','state','zipcode',
        'country','phoneNumber','ccNumber','ccMonth','ccYear','cvv'
      ];
      window.currentProfileGroup = "";
      window.currentProfileRows = [];
      window.currentProfileIndex = -1;
      window.profileGroupFiles = [];

      function getProfilePayloadFromModal() {
        const payload = {};
        profileFieldsOrder.forEach((field) => {
          const input = document.getElementById(`profileModal_${field}`);
          payload[field] = input ? input.value.trim() : "";
        });
        return payload;
      }

      function fillProfileModalFromPayload(payload = {}) {
        profileFieldsOrder.forEach((field) => {
          const input = document.getElementById(`profileModal_${field}`);
          if (input) input.value = payload[field] || "";
        });
      }

      window.closeProfileModal = function() {
        document.getElementById("profileModal").classList.add("hidden");
      };

      window.openProfileCreateModal = function() {
        if (!window.currentProfileGroup) {
          alert("Select a profile group first.");
          return;
        }
        window.currentProfileIndex = -1;
        document.getElementById("profileModalTitle").innerText = "Create Profile";
        document.getElementById("profileDeleteBtn").classList.add("hidden");
        document.getElementById("profileModalGroup").value = window.currentProfileGroup.replace(".csv", "");
        fillProfileModalFromPayload({});
        document.getElementById("profileModal").classList.remove("hidden");
      };

      window.openProfileEditModal = function(groupFile, index) {
        window.currentProfileGroup = groupFile;
        window.currentProfileIndex = index;
        const row = window.currentProfileRows[index] || {};
        document.getElementById("profileModalTitle").innerText = `Edit Profile: ${row.profileName || "Untitled"}`;
        document.getElementById("profileDeleteBtn").classList.remove("hidden");
        document.getElementById("profileModalGroup").value = groupFile.replace(".csv", "");
        fillProfileModalFromPayload(row);
        document.getElementById("profileModal").classList.remove("hidden");
      };

      window.deleteProfileFromModal = async function() {
        if (window.currentProfileIndex < 0) return;
        if (!confirm("Delete this profile?")) return;
        window.currentProfileRows.splice(window.currentProfileIndex, 1);
        await saveProfile(window.currentProfileGroup, window.currentProfileRows);
        closeProfileModal();
        await fetchProfileContent(window.currentProfileGroup);
      };

      window.saveProfileFromModal = async function() {
        let groupFile = document.getElementById("profileModalGroup").value.trim();
        if (!groupFile) {
          alert("Profile group is required.");
          return;
        }
        if (!groupFile.endsWith(".csv")) groupFile += ".csv";

        if (groupFile !== window.currentProfileGroup) {
          window.currentProfileGroup = groupFile;
          window.currentProfileRows = [];
        }

        const payload = getProfilePayloadFromModal();
        if (!payload.profileName) {
          alert("Profile name is required.");
          return;
        }

        if (window.currentProfileIndex >= 0) {
          window.currentProfileRows[window.currentProfileIndex] = payload;
        } else {
          window.currentProfileRows.push(payload);
        }

        await saveProfile(window.currentProfileGroup, window.currentProfileRows);
        closeProfileModal();
        await loadProfileFiles();
        await fetchProfileContent(window.currentProfileGroup);
      };

      function renderProfileGroupList(files) {
        const host = document.getElementById('profileGroupList');
        host.innerHTML = '';
        if (!files.length) {
          host.innerHTML = `<div class="text-sm text-zinc-500">No profile groups found in <code>profile/</code>.</div>`;
          return;
        }
        files.forEach((file) => {
          const active = file === window.currentProfileGroup ? "active" : "";
          const item = document.createElement('div');
          item.className = `profile-group-item ${active}`;
          item.innerHTML = `
            <div class="flex items-center justify-between gap-2">
              <div>
                <div class="mini-muted">Group</div>
                <div class="font-semibold">${file.replace('.csv', '')}</div>
              </div>
              <button class="btn btn-primary text-xs px-2 py-1" data-add-group="${file}">Add</button>
            </div>
          `;
          item.onclick = () => fetchProfileContent(file);
          item.querySelector('[data-add-group]')?.addEventListener('click', (e) => {
            e.stopPropagation();
            window.currentProfileGroup = file;
            openProfileCreateModal();
          });
          host.appendChild(item);
        });
      }

      function renderProfileRows() {
        const tbody = document.getElementById('profileTableBody');
        const empty = document.getElementById('profileEmptyState');
        const current = document.getElementById('profileCurrentGroup');
        const meta = document.getElementById('profileMeta');
        const query = (document.getElementById('profileSearch')?.value || '').trim().toLowerCase();

        current.textContent = window.currentProfileGroup ? window.currentProfileGroup.replace('.csv', '') : '—';

        let rows = window.currentProfileRows || [];
        if (query) {
          rows = rows.filter((r) =>
            [r.profileName, r.email, r.city, r.country, r.phoneNumber]
              .map((x) => (x || '').toLowerCase())
              .some((x) => x.includes(query))
          );
        }

        meta.innerHTML = `
          <span class="pill">${rows.length} shown</span>
          <span class="pill">${(window.currentProfileRows || []).length} total</span>
        `;

        tbody.innerHTML = '';
        if (!rows.length) {
          empty.classList.remove('hidden');
          empty.textContent = window.currentProfileGroup
            ? 'No profiles match this search.'
            : 'Select a profile group to view entries.';
          return;
        }
        empty.classList.add('hidden');

        rows.forEach((row) => {
          const originalIdx = (window.currentProfileRows || []).indexOf(row);
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td class="p-2">
              <div class="font-semibold">${row.profileName || 'Unnamed Profile'}</div>
              <div class="text-xs text-zinc-500">${row.ccNumber ? 'Card on file' : 'No card'}</div>
            </td>
            <td class="p-2">
              <div>${row.email || '—'}</div>
              <div class="text-xs text-zinc-500">${row.phoneNumber || 'No phone'}</div>
            </td>
            <td class="p-2">
              <div>${row.city || '—'}, ${row.state || '—'}</div>
              <div class="text-xs text-zinc-500">${row.country || '—'}</div>
            </td>
            <td class="p-2 text-right">
              <button class="btn btn-muted text-xs px-2 py-1 mr-1" data-edit-idx="${originalIdx}">Edit</button>
              <button class="btn btn-danger text-xs px-2 py-1" data-del-idx="${originalIdx}">Delete</button>
            </td>
          `;
          tr.querySelector('[data-edit-idx]')?.addEventListener('click', () => openProfileEditModal(window.currentProfileGroup, originalIdx));
          tr.querySelector('[data-del-idx]')?.addEventListener('click', async () => {
            if (!confirm('Delete this profile?')) return;
            window.currentProfileRows.splice(originalIdx, 1);
            await saveProfile(window.currentProfileGroup, window.currentProfileRows);
            renderProfileRows();
          });
          tbody.appendChild(tr);
        });
      }

      async function loadProfileFiles() {
        const searchEl = document.getElementById('profileSearch');
        if (searchEl && !searchEl.dataset.bound) {
          searchEl.addEventListener('input', renderProfileRows);
          searchEl.dataset.bound = '1';
        }
        const resp = await fetch(baseUrl + '/profile-files', { headers: skipHeader });
        const files = await resp.json();
        window.profileGroupFiles = Array.isArray(files) ? files : [];
        renderProfileGroupList(window.profileGroupFiles);
        if (!window.currentProfileGroup && files.length) {
          await fetchProfileContent(files[0]);
        } else {
          renderProfileRows();
        }
      }

      async function fetchProfileContent(file) {
        window.currentProfileGroup = file;
        renderProfileGroupList(window.profileGroupFiles || []);
        const resp = await fetch(`${baseUrl}/profile-content?file=${encodeURIComponent(file)}`, { headers: skipHeader });
        const data = await resp.json();
        window.currentProfileRows = Array.isArray(data) ? data : [];
        renderProfileRows();
      }

      async function saveProfile(file, data) {
        const resp = await fetch(`${baseUrl}/save-profile?file=${encodeURIComponent(file)}`, {
          method: 'POST',
          headers: Object.assign({'Content-Type':'application/json'}, skipHeader),
          body: JSON.stringify({rows:data})
        });
        if (!resp.ok) {
          const err = await resp.json();
          alert('Save failed: ' + (err.error || "unknown error"));
        }
      }


      function showCreateAutomationPopup() {
            // 1) Clear any editing flag
            window.currentAutomationEditGroup = null;

            const popup = document.getElementById("createAutomationPopup");
            // 2) Reset header
            const headerElem = popup.querySelector("#automationPopupHeader") || popup.querySelector("h2");
            if (headerElem) {
                  headerElem.innerText = "Create Automation Group";
            }

            // 3) Find or assign the submit button
            let submitBtn = document.getElementById("automationSubmitButton");
            if (!submitBtn && popup) {
                  // fallback: first button inside popup
                  submitBtn = popup.querySelector("button");
                  if (submitBtn) {
                        submitBtn.id = "automationSubmitButton";
                  }
            }
            // 4) Reset its text & handler
            if (submitBtn) {
                  submitBtn.innerText = "Create";
                  submitBtn.onclick   = submitCreateAutomation;
            }

            // 5) Clear text fields and allow renaming
            const groupNameField = document.getElementById("automationGroupName");
            if (groupNameField) {
                  groupNameField.value = "";
                  groupNameField.removeAttribute("readonly");
            }
            const skuField = document.getElementById("skuInput");
            if (skuField) skuField.value = "";

            // 6) Reset profile-group dropdown
            const profileGroupSelect = document.getElementById("automationProfileGroupSelect");
            if (profileGroupSelect) {
                  profileGroupSelect.innerHTML = '<option value="" disabled selected>Select Profile Group</option>';
                  populateProfileGroupDropdown("automationProfileGroupSelect");
            }

            // 7) Clear profile & proxy fields
            const profileField = document.getElementById("profileInput");
            if (profileField) profileField.value = "";
            const proxyField = document.getElementById("proxyInput");
            if (proxyField) proxyField.value = "";

            // 8) Clear task-count, mode, and running-time
            const taskCountField   = document.getElementById("taskCountInput");
            const modeSelect       = document.getElementById("modeSelect");
            const runningTimeField = document.getElementById("runningTimeInput");
            if (taskCountField)   taskCountField.value = "";
            if (modeSelect)       modeSelect.selectedIndex = 0;
            if (runningTimeField) runningTimeField.value = "";

            // 9) Populate & reset store checkboxes + summary
            fetch(baseUrl + "/sites")
              .then(res => res.json())
              .then(sites => {
                const container = popup.querySelector("#storeSelection .checkbox-container");
                if (container) {
                  container.innerHTML = '<label class="text-white"><input type="checkbox" id="selectAllStores" value="all"> All Stores</label>';
                  sites.forEach(site => {
                    container.innerHTML += `<label class="text-white">
                      <input type="checkbox" value="${site.url}"> ${site.name}
                    </label>`;
                  });
                  const storeSummary = popup.querySelector("#storeSelection summary");
                  if (storeSummary) storeSummary.innerText = "Select Stores";
                }
              })
              .catch(err => console.error("Error fetching sites", err));

            // 10) Reset size checkboxes + summary
            const sizeCbs = popup.querySelectorAll("#automationSizes .checkbox-container input[type='checkbox']");
            sizeCbs.forEach(cb => {
                  cb.checked  = false;
                  cb.disabled = false;
            });
            const sizeSummary = popup.querySelector("#automationSizes summary");
            if (sizeSummary) sizeSummary.innerText = "Select Sizes";

            // 11) Finally, show the popup
            if (popup) popup.classList.remove("hidden");
      }

      function hideCreateAutomationPopup() {
            document.getElementById("createAutomationPopup").classList.add("hidden");
            // Optionally, clear any editing flag.
            window.currentAutomationEditGroup = null;
      }

      function getSelectedStores() {
          const checkboxes = document.querySelectorAll("#storeSelection .checkbox-container input[type='checkbox']");
          let selected = [];
          let selectAll = false;
          checkboxes.forEach(cb => {
              if (cb.checked) {
                  if (cb.value === "all") {
                      selectAll = true;
                  } else {
                      selected.push(cb.value);
                  }
              }
          });
          return selectAll ? "all" : selected;
      }

      function submitCreateAutomation() {
        const groupName = document.getElementById("automationGroupName").value.trim();
        const stores = getSelectedStores();
        const skuInput = document.getElementById("skuInput").value.trim();
        const skus = skuInput.split(",").map(s => s.trim()).filter(s => s !== "");
        const automationProfileGroup = document.getElementById("automationProfileGroupSelect").value;
        const profile = document.getElementById("profileInput").value.trim();
        const proxy = document.getElementById("proxyInput").value.trim();
        const taskCount = parseInt(document.getElementById("taskCountInput").value.trim() || "0", 10);
        const mode = document.getElementById("modeSelect").value;
        const sizes = getSelectedSizes("automationSizes");
        const runningTime = parseInt(document.getElementById("runningTimeInput").value.trim() || "0", 10);

        if (
          !groupName ||
          !stores ||
          skus.length === 0 ||
          !automationProfileGroup ||
          !profile ||
          !proxy ||
          taskCount <= 0 ||
          !mode ||
          sizes.length === 0 ||
          runningTime <= 0
        ) {
          alert("Please complete all fields correctly.");
          return;
        }

        const data = {
          groupName,
          stores,
          skus,
          automationProfileGroup,
          profile,
          proxy,
          taskCount,
          mode,
          sizes,
          runningTime,
          active: true
        };

        fetch(baseUrl + "/create-automation-group", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data)
        })
          .then(response => {
            if (response.ok) {
              alert("Automation Group Created!");
              hideCreateAutomationPopup();
              loadAutomationGroups();
            } else {
              response.text().then(text => alert("Error: " + text));
            }
          })
          .catch(err => {
            console.error("Failed to create automation group:", err);
            alert("An error occurred. Check the console for details.");
          });
      }


      function loadAutomationGroups() {
        fetch(baseUrl + "/automation-groups")
          .then(resp => resp.json())
          .then(groups => {
            const container = document.getElementById("automationGroups");
            container.innerHTML = "";
            groups.forEach(group => {
              const card = document.createElement("div");
              card.className = "panel p-4 relative";
              card.id = "automationCard-" + group.groupName;

              // Compute storesDisplay robustly
              let storesDisplay;
              if (group.stores === "all") {
                storesDisplay = "All Stores";
              } else if (Array.isArray(group.stores)) {
                storesDisplay = group.stores.join(", ");
              } else {
                storesDisplay = group.stores;
              }
              const statusText = group.active ? "Active" : "Inactive";
              const statusColor = group.active ? "bg-green-600/20 text-green-300 border border-green-400/30" : "bg-red-600/20 text-red-300 border border-red-400/30";

              card.innerHTML = `
                <div class="flex justify-between items-center mb-2">
                  <h3 class="text-lg font-bold">${group.groupName}</h3>
                  <span class="text-xs ${statusColor} px-2 py-1 rounded">${statusText}</span>
                </div>
                <div class="text-sm text-zinc-300 leading-6">
                  <div><span class="mini-muted">Stores</span> ${storesDisplay}</div>
                  <div><span class="mini-muted">SKUs</span> ${group.skus.join(", ")}</div>
                  <div><span class="mini-muted">Profile</span> ${group.profile}</div>
                  <div><span class="mini-muted">Proxy</span> ${group.proxy}</div>
                  <div><span class="mini-muted">Task Count</span> ${group.taskCount}</div>
                  <div><span class="mini-muted">Mode</span> ${group.mode}</div>
                  <div><span class="mini-muted">Run Time</span> ${group.runningTime} min</div>
                </div>
                <div class="flex mt-2 space-x-2">
                  <button onclick="startAutomationGroup('${group.groupName}')" class="btn btn-primary text-sm px-3 py-1">
                    <i class="fa-solid fa-play"></i>
                  </button>
                  <button onclick="stopAutomationGroup('${group.groupName}')" class="btn btn-danger text-sm px-3 py-1">
                    <i class="fa-solid fa-stop"></i>
                  </button>
                  <button onclick="editAutomationGroup('${group.groupName}')" class="btn btn-muted text-sm px-3 py-1">
                    <i class="fa-solid fa-pencil"></i>
                  </button>
                  <button onclick="copyAutomationGroup('${group.groupName}')" class="btn btn-muted text-sm px-3 py-1">
                    <i class="fa-solid fa-copy"></i>
                  </button>
                  <button onclick="deleteAutomationGroup('${group.groupName}')" class="btn btn-danger text-sm px-3 py-1">
                    <i class="fa-solid fa-trash"></i>
                  </button>
                </div>
              `;
              container.appendChild(card);
            });
          });
      }


      function editAutomationGroup(groupName) {
            // 1) Show and reset the popup (resets fields to “create” defaults)
            showCreateAutomationPopup();
            // 2) Restore the edit flag now that we’re in edit mode
            window.currentAutomationEditGroup = groupName;

            const popup     = document.getElementById("createAutomationPopup");
            const header    = popup.querySelector("#automationPopupHeader") || popup.querySelector("h2");
            const submitBtn = document.getElementById("automationSubmitButton");

            // 3) Switch header & button into “Edit” mode
            if (header) {
                  header.innerText = "Edit Automation Group";
            }
            if (submitBtn) {
                  submitBtn.innerText = "Edit";
                  submitBtn.onclick   = submitEditAutomationGroup;
            }

            // 4) Fetch the latest groups, then populate fields
            fetch(baseUrl + "/automation-groups")
              .then(res => res.json())
              .then(groups => {
                    const group = groups.find(g => g.groupName === groupName);
                    if (!group) {
                          alert("Automation group not found");
                          return;
                    }

                    // 5) Prefill inputs
                    document.getElementById("automationGroupName").value    = group.groupName;
                    document.getElementById("automationGroupName").readOnly = true;
                    document.getElementById("skuInput").value               = Array.isArray(group.skus)
                                                                              ? group.skus.join(", ")
                                                                              : group.skus;
                    document.getElementById("profileInput").value           = group.profile;
                    document.getElementById("proxyInput").value             = group.proxy;
                    document.getElementById("taskCountInput").value         = group.taskCount;
                    document.getElementById("modeSelect").value             = group.mode;
                    document.getElementById("runningTimeInput").value       = group.runningTime;

                    // 6) Profile-group dropdown
                    const pg = document.getElementById("automationProfileGroupSelect");
                    pg.innerHTML = '<option value="" disabled>Select Profile Group</option>';
                    populateProfileGroupDropdown("automationProfileGroupSelect")
                      .then(() => { pg.value = group.automationProfileGroup; });

                    // 7) Stores checkboxes + summary
                    const storeContainer = popup.querySelector("#storeSelection .checkbox-container");
                    storeContainer.innerHTML = '<label><input type="checkbox" id="selectAllStores" value="all"> All Stores</label>';
                    fetch(baseUrl + "/sites")
                      .then(r => r.json())
                      .then(sites => {
                            sites.forEach(s => {
                                  storeContainer.innerHTML += `
                                        <label><input type="checkbox" value="${s.url}"> ${s.name}</label>
                                  `;
                            });
                            const allCb = storeContainer.querySelector("#selectAllStores");
                            const cbs   = storeContainer.querySelectorAll("input[type=checkbox]");
                            cbs.forEach(cb => {
                                  cb.checked  = (group.stores === "all" && cb.value === "all")
                                             || (Array.isArray(group.stores) && group.stores.includes(cb.value));
                                  cb.disabled = false;
                            });
                            const summary = popup.querySelector("#storeSelection summary");
                            if (allCb.checked) {
                                  summary.innerText = "All Stores";
                            } else {
                                  const picked = Array.from(cbs)
                                    .filter(cb => cb.checked && cb.value !== "all")
                                    .map(cb => cb.value);
                                  summary.innerText = picked.length
                                    ? "Selected: " + picked.join(", ")
                                    : "Select Stores";
                            }
                      })
                      .catch(console.error);

                    // 8) Sizes checkboxes + summary
                    const sizeCbs = popup.querySelectorAll("#automationSizes .checkbox-container input[type=checkbox]");
                    const sizes   = group.sizes === "random" ? ["random"] : group.sizes.split("&");
                    sizeCbs.forEach(cb => {
                          cb.checked  = sizes.includes(cb.value);
                          cb.disabled = false;
                    });
                    const sizeSummary = popup.querySelector("#automationSizes summary");
                    if (sizes.includes("random")) {
                          sizeSummary.innerText = "Random";
                    } else if (sizes.length) {
                          sizeSummary.innerText = "Selected: " + sizes.join(", ");
                    } else {
                          sizeSummary.innerText = "Select Sizes";
                    }
              })
              .catch(err => {
                    console.error("Failed to load automation groups:", err);
                    alert("Could not load automation groups. See console.");
              });
      }


      function submitEditAutomationGroup() {
        const originalGroupName = window.currentAutomationEditGroup;
        if (!originalGroupName) {
          alert("No automation group selected for editing.");
          return;
        }

        // Read all fields
        const groupName = document.getElementById("automationGroupName").value.trim();

        // Stores
        const storeCbs = document.querySelectorAll("#storeSelection .checkbox-container input[type='checkbox']");
        let stores = [];
        storeCbs.forEach(cb => {
          if (cb.checked) stores.push(cb.value);
        });
        if (stores.includes("all")) stores = "all";

        // SKUs
        const rawSkus = document.getElementById("skuInput").value.trim();
        const skus = rawSkus.split(",").map(s => s.trim()).filter(s => s !== "");

        // Profile group & profile
        const automationProfileGroup = document.getElementById("automationProfileGroupSelect").value;
        const profile = document.getElementById("profileInput").value.trim();

        // Proxy, count, mode, sizes, running time
        const proxy = document.getElementById("proxyInput").value.trim();
        const taskCount = parseInt(document.getElementById("taskCountInput").value.trim(), 10) || 0;
        const mode = document.getElementById("modeSelect").value;
        const sizes = getSelectedSizes("automationSizes");
        const runningTime = parseInt(document.getElementById("runningTimeInput").value.trim(), 10) || 0;

        // Validate
        if (
          !groupName ||
          !stores ||
          skus.length === 0 ||
          !automationProfileGroup ||
          !profile ||
          !proxy ||
          taskCount <= 0 ||
          !mode ||
          !sizes ||
          runningTime <= 0
        ) {
          alert("Please complete all fields correctly.");
          return;
        }

        const updates = {
          groupName,
          stores,
          skus,
          automationProfileGroup,
          profile,
          proxy,
          taskCount,
          mode,
          sizes,
          runningTime
        };

        fetch(baseUrl + "/edit-automation-group", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ originalGroupName, updates })
        })
          .then(response => {
            if (response.ok) {
              alert("Automation group updated!");
              document.getElementById("createAutomationPopup").classList.add("hidden");
              loadAutomationGroups();
            } else {
              return response.text().then(text => { throw new Error(text); });
            }
          })
          .catch(err => {
            console.error("Edit failed:", err);
            alert("Error editing automation group: " + err.message);
          });
      }

      function copyAutomationGroup(groupName) {
            if (!confirm("Are you sure you want to duplicate " + groupName + "?")) return;
            fetch(baseUrl + "/duplicate-automation-group", {
                  method: "POST",
                  headers: {"Content-Type": "application/json"},
                  body: JSON.stringify({ groupName: groupName })
            }).then(response => {
                  if (response.ok) {
                        response.json().then(data => {
                              alert("Automation Group duplicated as: " + data.newGroupName);
                              loadAutomationGroups();
                        });
                  } else {
                        response.text().then(text => alert("Error: " + text));
                  }
            });
      }



      function startAutomationGroup(groupName) {
          fetch(baseUrl + "/start-automation-group", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({groupName: groupName})
          }).then(response => {
              if (response.ok) {
                  alert("Automation started");
                  loadAutomationGroups();
              } else {
                  response.text().then(text => alert("Error: " + text));
              }
          });
      }

      function stopAutomationGroup(groupName) {
          fetch(baseUrl + "/stop-automation-group", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({groupName: groupName})
          }).then(response => {
              if (response.ok) {
                  alert("Automation stopped");
                  loadAutomationGroups();
              } else {
                  response.text().then(text => alert("Error: " + text));
              }
          });
      }

      function deleteAutomationGroup(groupName) {
          if (!confirm("Are you sure you want to delete " + groupName + "?")) return;
          fetch(baseUrl + "/delete-automation-group?groupName=" + encodeURIComponent(groupName), {
              method: "DELETE"
          }).then(response => {
              if (response.ok) {
                  alert("Automation deleted");
                  loadAutomationGroups();
              } else {
                  response.text().then(text => alert("Error: " + text));
              }
          });
      }
      window.showCreateTaskPopup = function() {
        document.getElementById("createTaskPopup").classList.remove("hidden");
        populateSiteDropdown("createSiteDropdown");
      };
      window.hideCreateTaskPopup = function() {
        document.getElementById("createTaskPopup").classList.add("hidden");
      };
      window.showAddTaskPopup = function(groupName) {
        document.getElementById('addTaskGroupName').value = groupName;
        populateSiteDropdown("addSiteDropdown");
        document.getElementById('addMode').value = "preload";
        document.getElementById('addInput').value = "";
        document.getElementById('addTaskQuantity').value = 1;
        const addSizeDropdown = document.getElementById('addSizeDropdown');
        addSizeDropdown.querySelectorAll('input[type="checkbox"]').forEach(cb => { cb.checked = false; cb.disabled = false; });
        updateSizeDropdown("addSizeDropdown");
        const profileGroupSelect = document.getElementById('addProfileGroupSelect');
        if (profileGroupSelect) profileGroupSelect.selectedIndex = 0;
        const profileContainer = document.getElementById('addProfileCheckboxes');
        if (profileContainer) {
          profileContainer.innerHTML = `
            <label class="text-white">
              <input type="checkbox" value="__ALL__" id="addSelectAllProfiles"> All Profiles
            </label>
          `;
          document.querySelector('#addProfileSelection summary').innerText = 'Select Profiles';
        }
        document.getElementById('addProxy').selectedIndex = 0;
        document.getElementById('addTaskPopup').classList.remove('hidden');
      };

      window.hideAddTaskPopup = function() {
        document.getElementById('addTaskPopup').classList.add('hidden');
      };
      window.showEditTaskGroupPopup = function(fileName) {
        document.getElementById('editTaskGroupFileName').value = fileName;
        fetch(baseUrl + `/tasks?file=${encodeURIComponent(fileName)}`, { headers: skipHeader })
          .then(res => res.json())
          .then(tasks => {
            const firstTask = tasks[0] || {};
            const currentSite = firstTask.site || "";
            populateSiteDropdown("editSiteDropdown");
            populateProxyDropdown("editProxyGroup");
            const dropdown = document.getElementById('editSiteDropdown');
            if (dropdown && currentSite) dropdown.value = currentSite;
            const modeDropdown = document.getElementById("editModeValue");
            if (modeDropdown) modeDropdown.selectedIndex = 0;
            const proxyDropdown = document.getElementById("editProxyGroup");
            if (proxyDropdown) proxyDropdown.selectedIndex = 0;
            const editSizeDropdown = document.getElementById('editSizeDropdown');
            editSizeDropdown.querySelectorAll('input[type="checkbox"]').forEach(cb => {
              cb.checked = false;
              cb.disabled = false;
            });
            updateSizeDropdown("editSizeDropdown");
            document.getElementById('editInputValue').value = "";
            document.getElementById('editProfileName').value = "";
            document.getElementById('editColorValue').value = "";
            document.getElementById('editCartQuantity').value = "";
            document.getElementById('editDelayValue').value = "";
            document.getElementById('editTaskGroupPopup').classList.remove('hidden');
          });
      };
      window.hideEditTaskGroupPopup = function() {
        document.getElementById('editTaskGroupPopup').classList.add('hidden');
      };
      window.showTaskGroupSchedulePopup = function(fileName) {
        document.getElementById('settingsTaskGroupFileName').value = fileName;
        let groupName = fileName.replace('.csv', '');
        document.getElementById('settingsGroupName').value = groupName;
        document.getElementById('settingsStartTime').value = "";
        document.getElementById('settingsRunDuration').value = "";
        fetch(baseUrl + `/tasks?file=${encodeURIComponent(fileName)}`, { headers: skipHeader })
          .then(res => res.json())
          .then(tasks => {
            document.getElementById('settingsTaskQuantity').value = tasks.length;
          });
        document.getElementById('taskGroupSettingsPopup').classList.remove('hidden');
      };
      window.hideTaskGroupSettingsPopup = function() {
        document.getElementById('taskGroupSettingsPopup').classList.add('hidden');
      };
      function clearSchedule() {
        const fileName = document.getElementById('settingsTaskGroupFileName').value;
        const groupName = fileName.replace('.csv', '');
        if (scheduleTimers[groupName]) {
          clearTimeout(scheduleTimers[groupName].start);
          clearTimeout(scheduleTimers[groupName].stop);
          delete scheduleTimers[groupName];
        }
        const groupCard = document.getElementById("groupCard-" + groupName);
        if (groupCard) {
          const info = groupCard.querySelector(".schedule-info");
          if(info) info.innerText = "";
        }
        hideTaskGroupSettingsPopup();
      }
      function saveSchedule() {
        const fileName = document.getElementById('settingsTaskGroupFileName').value;
        const newGroupName = document.getElementById('settingsGroupName').value.trim();
        const startTimeStr = document.getElementById('settingsStartTime').value;
        const runDuration = parseInt(document.getElementById('settingsRunDuration').value);
        const taskQuantity = parseInt(document.getElementById('settingsTaskQuantity').value);
        if (!newGroupName) {
          alert("Please enter a task group name.");
          return;
        }
        const groupName = fileName.replace('.csv', '');
        const groupCard = document.getElementById("groupCard-" + groupName);
        fetch(baseUrl + "/edit-task-group", {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, skipHeader),
          body: JSON.stringify({
            fileName: groupName + ".csv",
            taskGroupData: {
              taskGroupName: newGroupName,
              taskQuantity: taskQuantity
            }
          })
        }).then(async res => {
          if (res.ok) {
            groupCard.querySelector("h3").innerText = newGroupName;
            groupCard.id = "groupCard-" + newGroupName;
            await fetchTaskGroups();
            const card = document.getElementById("groupCard-" + newGroupName);
            if (card) card.click();
          } else {
            alert("Failed to update task group");
          }
        });
        if (startTimeStr && runDuration) {
          const startTime = new Date(startTimeStr);
          const now = new Date();
          let delay = startTime.getTime() - now.getTime();
          if (delay < 0) delay = 0;
          const formatted = "Scheduled for " + startTime.toLocaleString(undefined, { month: "numeric", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
          if (groupCard) {
            let info = groupCard.querySelector(".schedule-info");
            if (!info) {
              info = document.createElement("div");
              info.className = "schedule-info";
              groupCard.appendChild(info);
            }
            info.innerText = formatted;
          }
          scheduleTimers[groupName] = {};
          scheduleTimers[groupName].start = setTimeout(() => { startTaskGroup(fileName); }, delay);
          scheduleTimers[groupName].stop = setTimeout(() => { stopTaskGroup(fileName); }, delay + runDuration * 60000);
        }
        hideTaskGroupSettingsPopup();
      }
      async function submitTaskGroup() {
        const taskGroupName = document.getElementById("taskGroupName").value;
        const profileGroup = document.getElementById("profileGroupSelect").value;
        // gather checked profiles
        const boxes = Array.from(document.querySelectorAll('#profileCheckboxes input[type=checkbox]'));
        let profileNames;
        if (boxes.find(cb => cb.value === '__ALL__').checked) {
          profileNames = boxes
            .filter(cb => cb.value !== '__ALL__')
            .map(cb => cb.value);
        } else {
          profileNames = boxes
            .filter(cb => cb.checked && cb.value !== '__ALL__')
            .map(cb => cb.value);
        }
        if (profileNames.length === 0) {
          alert("Please select at least one profile.");
          return;
        }
        const proxyGroup   = document.getElementById("proxyGroup").value;
        const inputValue   = document.getElementById("inputValue").value;
        const sizeValue    = getSelectedSizes("createSizeDropdown");
        const colorValue   = document.getElementById("colorValue").value || "Random";
        const siteValue    = document.getElementById("createSiteDropdown").value;
        const modeValue    = document.getElementById("modeValue").value;
        const cartQuantity = document.getElementById("cartQuantity").value;
        const delayValue   = document.getElementById("delayValue").value || "3333";
        const taskQuantity = parseInt(document.getElementById("taskQuantity").value) || 1;
        if (!taskGroupName || !inputValue || !siteValue || !modeValue || !cartQuantity) {
          alert("Please fill in all required fields.");
          return;
        }
        const taskData = {
          fileName:      taskGroupName + ".csv",
          profileGroup:  profileGroup,
          profileNames:  profileNames,
          proxyGroup:    proxyGroup,
          accountGroup:  "example",
          input:         inputValue,
          size:          sizeValue,
          color:         colorValue,
          site:          siteValue,
          mode:          modeValue,
          cartQuantity:  cartQuantity,
          delay:         delayValue,
          taskQuantity:  taskQuantity
        };
        await fetch(baseUrl + "/create-task-group", {
          method:  "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, skipHeader),
          body:    JSON.stringify(taskData)
        });
        hideCreateTaskPopup();
        fetchTaskGroups();
      }
      async function submitAddTask() {
        const groupName       = document.getElementById('addTaskGroupName').value;
        const site            = document.getElementById('addSiteDropdown').value;
        const mode            = document.getElementById('addMode').value;
        const inputVal        = document.getElementById('addInput').value;
        const sizeVal         = getSelectedSizes("addSizeDropdown");
        const proxy           = document.getElementById('addProxy').value;
        const quantity        = parseInt(document.getElementById('addTaskQuantity').value, 10) || 1;
        const profileGroup    = document.getElementById("addProfileGroupSelect").value;

        // gather checked profiles
        const checkboxes = Array.from(document.querySelectorAll('#addProfileCheckboxes input[type=checkbox]'));
        const allCb      = checkboxes.find(cb => cb.value === '__ALL__');
        let profileNames;
        if (allCb && allCb.checked) {
          profileNames = checkboxes
            .filter(cb => cb.value !== '__ALL__')
            .map(cb => cb.value);
        } else {
          profileNames = checkboxes
            .filter(cb => cb.checked && cb.value !== '__ALL__')
            .map(cb => cb.value);
        }
        if (profileNames.length === 0) {
          alert("Please select at least one profile.");
          return;
        }

        const task = {
          profileGroup: profileGroup,
          site:         site,
          mode:         mode,
          input:        inputVal,
          size:         sizeVal,
          proxyGroup:   proxy
        };

        await fetch(baseUrl + "/add-task", {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, skipHeader),
          body: JSON.stringify({
            groupName:     groupName,
            task:          task,
            profileNames:  profileNames,
            taskQuantity:  quantity
          })
        });

        hideAddTaskPopup();
        await fetchTaskGroups();
        await fetchTasks(groupName);
      }

      async function submitEditTaskGroup() {
        const fileName = document.getElementById('editTaskGroupFileName').value;
        const profileGroup = document.getElementById("editProfileGroupSelect").value;
        let data = {};

        if (profileGroup) {
            data.profileGroup = profileGroup;
          }

        const fieldMapping = {
          "editInputValue": "input",
          "editProfileGroup": "profileGroup",
          "editProfileName": "profileName",
          "editProxyGroup": "proxyGroup",
          "editColorValue": "color",
          "editModeValue": "mode",
          "editCartQuantity": "cartQuantity",
          "editDelayValue": "delay"
        };
        Object.keys(fieldMapping).forEach(id => {
          const el = document.getElementById(id);
          if (el) {
            const val = el.value.trim();
            if (val !== "") data[fieldMapping[id]] = val;
          }
        });
        const sizeStr = getSelectedSizes("editSizeDropdown");
        if (sizeStr) data.size = sizeStr;
        const selectedSite = document.getElementById("editSiteDropdown").value.trim();
        if (selectedSite) {
          data.site = selectedSite;
        }
        const taskQuantity = document.getElementById("settingsRunDuration")?.value ||
                             document.getElementById("editTaskQuantity")?.value;
        if (taskQuantity) {
          data.taskQuantity = parseInt(taskQuantity);
        }
        const response = await fetch(baseUrl + "/edit-task-group", {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, skipHeader),
          body: JSON.stringify({ fileName: fileName, taskGroupData: data })
        });
        hideEditTaskGroupPopup();
        await fetchTaskGroups();
        if (window.currentGroup === fileName) {
          await fetchTasks(fileName);
        }
      }
      async function fetchTaskGroups() {
        const response = await fetch(baseUrl + "/task-groups", { headers: skipHeader });
        const taskGroups = await response.json();
        const taskContainer = document.getElementById('taskGroups');
        taskContainer.innerHTML = '';
        taskGroups.forEach(group => {
          const groupName = group.replace('.csv', '');
          const groupDiv = document.createElement('div');
          groupDiv.classList.add('panel', 'p-4', 'relative', 'cursor-pointer', 'transition', 'hover:border-cyan-400/40');
          groupDiv.id = "groupCard-" + groupName;
          groupDiv.onclick = () => {
            document.querySelectorAll("#taskGroups > div")
                    .forEach(div => div.classList.remove("ring-2", "ring-cyan-500/40"));
            groupDiv.classList.add("ring-2", "ring-cyan-500/40");
            window.showTasks();
            window.fetchTasks(group);
          };
          groupDiv.innerHTML = `
            <h3 class="text-lg font-bold flex items-center justify-between">
              <span>${groupName}</span>
              <span id="liveBadge-${groupName}" class="ml-2 text-emerald-300 text-xs hidden px-2 py-1 rounded bg-emerald-500/20 border border-emerald-400/30">Running</span>
            </h3>
            <div id="taskCount-${groupName}" class="text-xs mt-1 text-zinc-400"></div>
            <div class="flex mt-3 space-x-2">
              <button title="Add Task" class="btn btn-primary text-sm px-3 py-1" onclick="event.stopPropagation(); window.showAddTaskPopup('${group}')">
                <i class="fa-solid fa-plus"></i>
              </button>
              <button title="Edit Task Group" class="btn btn-muted text-sm px-3 py-1" onclick="event.stopPropagation(); window.showEditTaskGroupPopup('${group}')">
                <i class="fa-solid fa-pencil"></i>
              </button>
              <button title="Schedule Tasks" class="btn btn-muted text-sm px-3 py-1" onclick="event.stopPropagation(); window.showTaskGroupSchedulePopup('${group}')">
                <i class="fa-solid fa-gear"></i>
              </button>
              <button title="Start Tasks" class="btn text-sm px-3 py-1 bg-emerald-600/80 rounded border border-emerald-400/30" onclick="event.stopPropagation(); startTaskGroup('${group}')">
                <i class="fa-solid fa-play"></i>
              </button>
              <button title="Stop Tasks" class="btn btn-danger text-sm px-3 py-1" onclick="event.stopPropagation(); stopTaskGroup('${group}')">
                <i class="fa-solid fa-stop"></i>
              </button>
            </div>
            <span class="absolute bottom-2 right-2 cursor-pointer text-red-500" onclick="event.stopPropagation(); confirmDelete('${group}')">
              <i class="fa-solid fa-trash"></i>
            </span>
          `;
          taskContainer.appendChild(groupDiv);
          fetch(baseUrl + `/tasks?file=${encodeURIComponent(group)}`, { headers: skipHeader })
            .then(resp => resp.json())
            .then(tasks => {
              const total = tasks.length;
              const countElement = document.getElementById(`taskCount-${groupName}`);
              if (countElement) {
                countElement.innerHTML = `<span class="text-sm">
                  <i class="fa-solid fa-tasks"></i> ${total} |
                  <span class="text-green-500"><i class="fa-solid fa-check"></i> 0</span> | 
                  <span class="text-red-500"><i class="fa-solid fa-xmark"></i> 0</span>
                </span>`;
              }
            })
            .catch(err => console.error("Error fetching tasks for group", group, err));
        });
        fetch(baseUrl + "/running-groups", { headers: skipHeader })
          .then(res => res.json())
          .then(runningGroups => {
            runningGroups.forEach(groupName => {
              const liveBadge = document.getElementById("liveBadge-" + groupName);
              if (liveBadge) liveBadge.classList.remove("hidden");
            });
          });
      }
      // clear any existing polling when loading a new (or the same) group
      function stopPolling() {
        if (pollingInterval) {
          clearInterval(pollingInterval);
          pollingInterval = null;
          isPolling = false;
        }
      }

      async function fetchTasks(groupFile) {
        const groupName = groupFile.replace('.csv', '');

        // stop any old poll loop
        stopPolling();

        window.currentGroup = groupFile;
        document.getElementById('taskGroupTitle').innerText = groupName;

        // 1) fetch the tasks
        const respTasks = await fetch(`${baseUrl}/tasks?file=${encodeURIComponent(groupFile)}`, { headers: skipHeader });
        const tasks = await respTasks.json();

        // 2) fetch the last-known statuses
        const respStatus = await fetch(`${baseUrl}/task-status?group=${encodeURIComponent(groupName)}`, { headers: skipHeader });
        const { statuses } = await respStatus.json();

        // 3) extract just the TASK-xxxx keys and sort
        const tids = Object.keys(statuses)
          .filter(k => k !== 'global_status')
          .sort();

        // 4) compute offset = (smallest index) - 1
        const offset = tids.length
          ? parseInt(tids[0].slice(5), 10) - 1
          : 0;
        groupOffsets[groupFile] = offset;

        // 5) build the table
        const tbody = document.getElementById('taskTableBody');
        tbody.innerHTML = '';
        tasks.forEach((task, i) => {
          const idx = offset + i + 1;
          const tid = `TASK-${String(idx).padStart(4, '0')}`;
          const statusText = statuses[tid] || 'Idle';

          const row = document.createElement('tr');
          row.innerHTML = `
            <td class="p-2">${task.site}</td>
            <td class="p-2">${task.mode}</td>
            <td class="p-2">${task.input}</td>
            <td class="p-2">${task.size}</td>
            <td class="p-2">${task.profileName}</td>
            <td class="p-2">${task.proxyGroup}</td>
            <td class="status-cell bg-gray-800 text-yellow-500" id="status-${tid}">${statusText}</td>
          `;
          tbody.appendChild(row);
        });

        document.getElementById('taskTable').classList.remove('hidden');
        enableTaskSearch();

        // only show badge & start polling if this group is actually running
        const liveBadge = document.getElementById(`liveBadge-${groupName}`);
        if (statuses.global_status && statuses.global_status.toLowerCase() === 'running') {
          if (liveBadge) liveBadge.classList.remove('hidden');
          startPolling(groupName);
        } else {
          if (liveBadge) liveBadge.classList.add('hidden');
        }
      }


      function enableTaskSearch() {
        const searchInput = document.getElementById("taskSearch");
        const newInput = searchInput.cloneNode(true);
        searchInput.parentNode.replaceChild(newInput, searchInput);
        newInput.addEventListener("input", function () {
          const filter = this.value.toLowerCase();
          const rows = document.querySelectorAll("#taskTableBody tr");
          rows.forEach(row => {
            row.style.display = row.textContent.toLowerCase().includes(filter) ? "" : "none";
          });
        });
      }
      function confirmDelete(fileName) {
        deleteFileName = fileName;
        document.getElementById('deleteMessage').textContent = `Are you sure you want to delete ${fileName}?`;
        document.getElementById('deletePopup').classList.remove('hidden');
      }
      function hideDeletePopup() {
        document.getElementById('deletePopup').classList.add('hidden');
      }
      async function deleteTaskGroup() {
        await fetch(baseUrl + `/delete-task-group?file=${encodeURIComponent(deleteFileName)}`, {
          method: "DELETE",
          headers: skipHeader
        });
        hideDeletePopup();
        fetchTaskGroups();
      }
      document.getElementById('confirmDelete').addEventListener('click', deleteTaskGroup);
      async function fetchProxyContent(fileName) {
        const response = await fetch(baseUrl + `/proxy-content?file=${encodeURIComponent(fileName)}`, { headers: skipHeader });
        const content = await response.text();
        document.getElementById('proxyContent').value = content;
        document.getElementById('proxyContent').classList.remove('hidden');
        document.getElementById('saveProxyButton').style.display = "block";
        document.getElementById('saveProxyButton').setAttribute("data-file", fileName);
      }
      async function fetchProxies() {
        const response = await fetch(baseUrl + "/proxy-files", { headers: skipHeader });
        const proxyFiles = await response.json();
        const proxyContainer = document.getElementById('proxyGroups');
        proxyContainer.innerHTML = '';
        proxyFiles.forEach(file => {
          const proxyDiv = document.createElement('div');
          proxyDiv.classList.add('panel', 'p-4', 'rounded', 'cursor-pointer', 'border', 'hover:border-cyan-400/40');
          proxyDiv.innerHTML = `<div class="mini-muted">Proxy Group</div><h3 class="text-lg font-bold">${file.replace('.txt', '')}</h3>`;
          proxyDiv.onclick = () => fetchProxyContent(file);
          proxyContainer.appendChild(proxyDiv);
        });
        enableProxySearch();
      }

      function enableProxySearch() {
        const searchInput = document.getElementById("proxySearch");
        const newInput = searchInput.cloneNode(true);
        searchInput.parentNode.replaceChild(newInput, searchInput);
        newInput.addEventListener("input", function () {
          const filter = this.value.toLowerCase();
          const cards = document.querySelectorAll("#proxyGroups > div");
          cards.forEach(card => {
            card.style.display = card.textContent.toLowerCase().includes(filter) ? "" : "none";
          });
        });
      }
      async function saveProxy() {
        const fileName = document.getElementById('saveProxyButton').getAttribute("data-file");
        const content = document.getElementById('proxyContent').value;
        await fetch(baseUrl + `/save-proxy?file=${encodeURIComponent(fileName)}`, {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, skipHeader),
          body: JSON.stringify({ content })
        });
        alert("Proxy file saved successfully!");
      }
      function startPolling(groupName) {
        window.currentGroup = groupName + ".csv";
        const liveBadge = document.getElementById("liveBadge-" + groupName);
        if (liveBadge) liveBadge.classList.remove("hidden");
        if (isPolling) return;
        isPolling = true;
        pollingInterval = setInterval(async () => {
          if (window.currentGroup.replace(".csv", "") !== groupName) {
            clearInterval(pollingInterval);
            pollingInterval = null;
            isPolling = false;
            return;
          }
          try {
            const response = await fetch(baseUrl + `/task-status?group=${encodeURIComponent(groupName)}`, { headers: skipHeader });
            const data = await response.json();
            const statuses = data.statuses || {};
            let successCount = 0;
            let declineCount = 0;
            let stillRunning = false;
            Object.entries(statuses).forEach(([taskId, status]) => {
              if (taskId === "global_status") return;
              const statusCell = document.getElementById("status-" + taskId);
              if (statusCell) statusCell.innerText = status;
              const lowerStatus = status.toLowerCase();
              if (lowerStatus === "success") {
                successCount++;
              } else if (lowerStatus.includes("decline")) {
                declineCount++;
              }
              if (lowerStatus !== "idle") {
                stillRunning = true;
              }
            });
            const groupCard = document.getElementById("groupCard-" + groupName);
            if (groupCard) {
              const total = Object.keys(statuses).filter(key => key !== "global_status").length;
              const countElement = groupCard.querySelector(`#taskCount-${groupName}`);
              if (countElement) {
                countElement.innerHTML = `<span class="text-sm">
                  <i class="fa-solid fa-tasks"></i> ${total} |
                  <span class="text-green-500"><i class="fa-solid fa-check"></i> ${successCount}</span> | 
                  <span class="text-red-500"><i class="fa-solid fa-xmark"></i> ${declineCount}</span>
                </span>`;
              }
            }
            if (!stillRunning && statuses.global_status && statuses.global_status.toLowerCase() === "idle") {
              clearInterval(pollingInterval);
              pollingInterval = null;
              isPolling = false;
              if (liveBadge) liveBadge.classList.add("hidden");
            }
          } catch (err) {
            console.error("Error polling task statuses", err);
            clearInterval(pollingInterval);
            pollingInterval = null;
            isPolling = false;
          }
        }, 500);
      }
      // == Polling functions ==
      function restartPolling(groupName) {
            // clear any existing polling loop
            if (pollingInterval) {
                  clearInterval(pollingInterval);
            }
            // start a fresh one every 500ms
            pollingInterval = setInterval(() => {
                  if (window.currentGroup === groupName + ".csv") {
                        console.log(`[ POLL ] - fetching status for ${groupName}`);
                        fetchTaskStatus(groupName);
                  }
            }, 500);
      }

      async function onGroupTabClick(groupName) {
            window.currentGroup = groupName + ".csv";
            await fetchTasks(window.currentGroup);
            restartPolling(groupName);
      }

      // == Revised startTaskGroup ==
      async function startTaskGroup(groupName) {
        try {
          const response = await fetch(baseUrl + "/start-task", {
            method: "POST",
            headers: Object.assign({ "Content-Type": "application/json" }, skipHeader),
            body: JSON.stringify({ groupName: groupName })
          });
          const result = await response.json();

          if (response.ok) {
            alert("Task started successfully.");

            // 1) refresh badges
            fetchTaskGroups();

            const fileName = groupName;

            // 2) if we're not already in the Tasks view or not viewing this group, switch to it
            if (document.getElementById('taskView').classList.contains('hidden') ||
                window.currentGroup !== fileName) {
              showTasks();
            }

            // 3) load that group's tasks (this will start polling only if it's running)
            await fetchTasks(fileName);
          } else {
            alert("Error starting task: " + result.error);
          }
        } catch (error) {
          alert("Error: " + error);
        }
      }


      async function stopTaskGroup(groupName) {
        try {
          const response = await fetch(baseUrl + "/stop-task", {
            method: "POST",
            headers: Object.assign({ "Content-Type": "application/json" }, skipHeader),
            body: JSON.stringify({ groupName: groupName })
          });
          const result = await response.json();
          if (response.ok) {
            alert("Task stopped successfully.");
            document
              .querySelectorAll('[id^="liveBadge-"]')
              .forEach(badge => badge.classList.add('hidden'));
            document
              .querySelectorAll('[id^="status-"]')
              .forEach(cell => cell.textContent = 'Idle');
            if (pollingInterval) {
              clearInterval(pollingInterval);
              pollingInterval = null;
              isPolling = false;
            }
          } else {
            alert("Error stopping task: " + result.error);
          }
        } catch (error) {
          alert("Error: " + error);
        }
      }

      // --- Settings Page Functions ---
      async function loadAppSettings() {
        try {
          const res = await fetch(baseUrl + "/settings", { headers: skipHeader });
          const s = await res.json();
          document.getElementById("settingsWebhookUrl").value = s.webhook_url || "";
          document.getElementById("settingsDiscordBotToken").value = s.discord_bot_token || "";
          document.getElementById("settingsDiscordChannelId").value = s.discord_channel_id || "";
        } catch (e) {
          alert("Failed to load settings: " + e.message);
        }
      }

      async function saveAppSettings() {
        try {
          const payload = {
            webhook_url: document.getElementById("settingsWebhookUrl").value.trim(),
            discord_bot_token: document.getElementById("settingsDiscordBotToken").value.trim(),
            discord_channel_id: document.getElementById("settingsDiscordChannelId").value.trim()
          };
          const res = await fetch(baseUrl + "/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json", ...skipHeader },
            body: JSON.stringify(payload)
          });
          const out = await res.json();
          if (!res.ok || out.error) {
            alert("Failed to save settings: " + (out.error || `HTTP ${res.status}`));
            return;
          }
          alert("Settings saved.");
        } catch (e) {
          alert("Failed to save settings: " + e.message);
        }
      }

      function loadSitesTable() {
        fetch(baseUrl + "/sites")
          .then(res => res.json())
          .then(sites => {
            const tbody = document.getElementById("sitesTableBody");
            tbody.innerHTML = "";
            sites.forEach(site => {
              const tr = document.createElement("tr");
              tr.innerHTML = `<td class="p-2">${site.name}</td>
                              <td class="p-2">${site.url}</td>
                              <td class="p-2">
                                <button onclick="showEditSiteModal('${site.name}')" class="bg-yellow-600 px-2 py-1 rounded text-white">Edit</button>
                                <button onclick="deleteSite('${site.name}')" class="bg-red-600 px-2 py-1 rounded text-white ml-2">Delete</button>
                              </td>`;
              tbody.appendChild(tr);
            });
          })
          .catch(err => console.error("Error fetching sites:", err));
      }
      function showAddSiteModal() {
        document.getElementById("newSiteName").value = "";
        document.getElementById("newSiteURL").value = "";
        document.getElementById("addSiteModal").classList.remove("hidden");
      }
      function closeAddSiteModal() {
        document.getElementById("addSiteModal").classList.add("hidden");
      }
      async function submitNewSite() {
        const name = document.getElementById("newSiteName").value.trim();
        const url = document.getElementById("newSiteURL").value.trim();
        if (!name || !url) { alert("Please provide both site name and URL."); return; }
        const res = await fetch(baseUrl + "/sites", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name, url})
        });
        if (res.ok) { closeAddSiteModal(); loadSitesTable(); }
        else { alert("Error adding site"); }
      }
      function showEditSiteModal(name) {
        fetch(baseUrl + "/sites")
          .then(res => res.json())
          .then(sites => {
            const site = sites.find(s => s.name === name);
            if (!site) return;
            document.getElementById("editSiteOriginalName").value = site.name;
            document.getElementById("editSiteName").value = site.name;
            document.getElementById("editSiteURL").value = site.url;
            document.getElementById("editSiteModal").classList.remove("hidden");
          });
      }
      function closeEditSiteModal() {
        document.getElementById("editSiteModal").classList.add("hidden");
      }
      async function submitEditSite() {
        const originalName = document.getElementById("editSiteOriginalName").value;
        const name = document.getElementById("editSiteName").value.trim();
        const url = document.getElementById("editSiteURL").value.trim();
        if (!name || !url) { alert("Please provide both site name and URL."); return; }
        const res = await fetch(baseUrl + "/sites", {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({originalName, name, url})
        });
        if (res.ok) { closeEditSiteModal(); loadSitesTable(); }
        else { alert("Error editing site"); }
      }
      async function deleteSite(name) {
        if (!confirm("Are you sure you want to delete this site?")) return;
        const res = await fetch(baseUrl + "/sites?name=" + encodeURIComponent(name), { method: "DELETE" });
        if (res.ok) { loadSitesTable(); }
        else { alert("Error deleting site"); }
      }
document.addEventListener('DOMContentLoaded', function() {
  const inputFieldCreate = document.getElementById("inputValue");
  const siteDropdownParentCreate = document.getElementById("dropdownContainerCreate");

  inputFieldCreate?.addEventListener("input", () => {
    const val = inputFieldCreate.value.trim();
    const match = val.match(/^https?:\/\/([^/]+)\/products\//);
    if (match) {
      const domain = match[1];
      const inputWrapper = document.createElement("div");
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "Enter custom URL...";
      input.id = "createSiteDropdown";
      input.value = domain;
      input.className = "w-full p-2 border rounded bg-gray-700 text-white mb-2";
      inputWrapper.appendChild(input);

      const backBtn = document.createElement("button");
      backBtn.innerText = "← Back to dropdown";
      backBtn.className = "text-sm text-blue-500 hover:underline";
      backBtn.onclick = () => {
        const dropdown = document.createElement("select");
        dropdown.id = "createSiteDropdown";
        dropdown.className = input.className;
        dropdown.onchange = () => handleChange(dropdown);
        dropdown.innerHTML = `
          <option value="" disabled selected>Select Site</option>
          ${getSiteOptionsHTML()}
          <option value="custom">Custom URL...</option>
        `;
        siteDropdownParentCreate.replaceChild(dropdown, inputWrapper);
      };

      inputWrapper.appendChild(backBtn);
      siteDropdownParentCreate.replaceChild(inputWrapper, document.getElementById("createSiteDropdown"));
    }
  });

  const inputFieldEdit = document.getElementById("editInputValue");
  const siteDropdownParentEdit = document.getElementById("dropdownContainerEdit");

  inputFieldEdit?.addEventListener("input", () => {
    const val = inputFieldEdit.value.trim();
    const match = val.match(/^https?:\/\/([^/]+)\/products\//);
    if (match) {
      const domain = match[1];
      const inputWrapper = document.createElement("div");
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "Enter custom URL...";
      input.id = "editSiteDropdown";
      input.value = domain;
      input.className = "w-full p-2 border rounded bg-gray-700 text-white mb-2";
      inputWrapper.appendChild(input);

      const backBtn = document.createElement("button");
      backBtn.innerText = "← Back to dropdown";
      backBtn.className = "text-sm text-blue-500 hover:underline";
      backBtn.onclick = () => {
        const dropdown = document.createElement("select");
        dropdown.id = "editSiteDropdown";
        dropdown.className = input.className;
        dropdown.onchange = () => handleChange(dropdown);
        dropdown.innerHTML = `
          <option value="" disabled selected>Select Site</option>
          ${getSiteOptionsHTML()}
          <option value="custom">Custom URL...</option>
        `;
        siteDropdownParentEdit.replaceChild(dropdown, inputWrapper);
      };

      inputWrapper.appendChild(backBtn);
      siteDropdownParentEdit.replaceChild(inputWrapper, document.getElementById("editSiteDropdown"));
    }
  });

  const inputFieldAdd = document.getElementById("addInput");
  const siteDropdownParentAdd = document.getElementById("dropdownContainerAdd");

  inputFieldAdd?.addEventListener("input", () => {
    const val = inputFieldAdd.value.trim();
    const match = val.match(/^https?:\/\/([^/]+)\/products\//);
    if (match) {
      const domain = match[1];
      const inputWrapper = document.createElement("div");
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "Enter custom URL...";
      input.id = "addSiteDropdown";
      input.value = domain;
      input.className = "w-full p-2 border rounded bg-gray-700 text-white mb-2";
      inputWrapper.appendChild(input);

      const backBtn = document.createElement("button");
      backBtn.innerText = "← Back to dropdown";
      backBtn.className = "text-sm text-blue-500 hover:underline";
      backBtn.onclick = () => {
        const dropdown = document.createElement("select");
        dropdown.id = "addSiteDropdown";
        dropdown.className = input.className;
        dropdown.onchange = () => handleChange(dropdown);
        dropdown.innerHTML = `
          <option value="" disabled selected>Select Site</option>
          ${getSiteOptionsHTML()}
          <option value="custom">Custom URL...</option>
        `;
        siteDropdownParentAdd.replaceChild(dropdown, inputWrapper);
      };

      inputWrapper.appendChild(backBtn);
      siteDropdownParentAdd.replaceChild(inputWrapper, document.getElementById("addSiteDropdown"));
    }
  });

  // ───── Create-Task: dynamic profile checkboxes ─────
  document.getElementById('profileGroupSelect')
    .addEventListener('change', async (e) => {
      const group = e.target.value;
      if (!group) return;
      const file = `${group}.csv`;
      const resp = await fetch(`${baseUrl}/profile-content?file=${encodeURIComponent(file)}`, { headers: skipHeader });
      const rows = await resp.json();
      const container = document.getElementById('profileCheckboxes');
      container.innerHTML = `
        <label class="text-white">
          <input type="checkbox" value="__ALL__" id="selectAllProfiles"> All Profiles
        </label>`;
      rows.forEach(r => {
        container.innerHTML += `
          <label class="text-white">
            <input type="checkbox" value="${r.profileName}"> ${r.profileName}
          </label>`;
      });
      const summary = document.getElementById('profileSelection').querySelector('summary');
      summary.innerText = 'Select Profiles';
      setupProfileSelection();
    });

  function updateProfileSelection() {
    const cbs = Array.from(document.querySelectorAll('#profileCheckboxes input[type=checkbox]'));
    const all = cbs.find(cb => cb.value === '__ALL__');
    const selected = cbs
      .filter(cb => cb.checked && cb.value !== '__ALL__')
      .map(cb => cb.value);
    let summaryText;
    if (all.checked) {
      cbs.forEach(cb => { if (cb !== all) cb.disabled = true; });
      summaryText = 'All Profiles';
    } else {
      cbs.forEach(cb => cb.disabled = false);
      summaryText = selected.length
        ? `Selected: ${selected.join(', ')}`
        : 'Select Profiles';
    }
    document.getElementById('profileSelection')
      .querySelector('summary').innerText = summaryText;
  }

  function setupProfileSelection() {
    document.querySelectorAll('#profileCheckboxes input[type=checkbox]')
      .forEach(cb => cb.addEventListener('change', updateProfileSelection));
  }

  // ───── Add-Task: dynamic profile checkboxes ─────
  document.getElementById('addProfileGroupSelect')
    .addEventListener('change', async (e) => {
      const group = e.target.value;
      if (!group) return;
      const file = `${group}.csv`;
      const resp = await fetch(`${baseUrl}/profile-content?file=${encodeURIComponent(file)}`, { headers: skipHeader });
      const rows = await resp.json();
      const container = document.getElementById('addProfileCheckboxes');
      container.innerHTML = `
        <label class="text-white">
          <input type="checkbox" value="__ALL__" id="addSelectAllProfiles"> All Profiles
        </label>`;
      rows.forEach(r => {
        container.innerHTML += `
          <label class="text-white">
            <input type="checkbox" value="${r.profileName}"> ${r.profileName}
          </label>`;
      });
      document.querySelector('#addProfileSelection summary').innerText = 'Select Profiles';
      setupAddProfileSelection();
    });

  function updateAddProfileSelection() {
    const cbs = Array.from(document.querySelectorAll('#addProfileCheckboxes input[type=checkbox]'));
    const all = cbs.find(cb => cb.value === '__ALL__');
    const sel = cbs.filter(cb => cb.checked && cb.value !== '__ALL__').map(cb => cb.value);
    let text;
    if (all.checked) {
      cbs.forEach(cb => { if (cb !== all) cb.disabled = true; });
      text = 'All Profiles';
    } else {
      cbs.forEach(cb => cb.disabled = false);
      text = sel.length
        ? `Selected: ${sel.join(', ')}`
        : 'Select Profiles';
    }
    document.querySelector('#addProfileSelection summary').innerText = text;
  }

  function setupAddProfileSelection() {
    document.querySelectorAll('#addProfileCheckboxes input[type=checkbox]')
      .forEach(cb => cb.addEventListener('change', updateAddProfileSelection));
  }

  // ────────────────────────────────────────────────
  setupSizeDropdown('createSizeDropdown');
  setupSizeDropdown('addSizeDropdown');
  setupSizeDropdown('editSizeDropdown');

  // Global popup UX polish: consistent backdrop close, ESC close, and focus behavior.
  function initModalUX() {
    const overlays = Array.from(document.querySelectorAll('.modal-overlay'));
    overlays.forEach((overlay) => {
      const card = overlay.querySelector('.modal-card');
      if (card) {
        card.addEventListener('click', (e) => e.stopPropagation());
        card.addEventListener('mousedown', (e) => e.stopPropagation());
      }
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.classList.add('hidden');
      });
    });

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      const visible = overlays.filter((el) => !el.classList.contains('hidden'));
      if (visible.length) visible[visible.length - 1].classList.add('hidden');
    });

    const observer = new MutationObserver(() => {
      overlays.forEach((overlay) => {
        if (overlay.classList.contains('hidden')) return;
        const firstInput = overlay.querySelector('input, select, textarea, button');
        if (firstInput) firstInput.focus({ preventScroll: true });
      });
    });
    overlays.forEach((overlay) => observer.observe(overlay, { attributes: true, attributeFilter: ['class'] }));
  }

  // Better size dropdown summaries for all popup dropdowns.
  function initSizeDropdownSummaries() {
    const managedIds = new Set(['createSizeDropdown', 'addSizeDropdown', 'editSizeDropdown']);
    document.querySelectorAll('.size-dropdown').forEach((dropdown) => {
      if (managedIds.has(dropdown.id)) return;
      const summary = dropdown.querySelector('summary');
      if (!summary) return;
      if (!summary.dataset.baseLabel) {
        summary.dataset.baseLabel = summary.textContent.trim();
      }
      const update = () => {
        const selected = dropdown.querySelectorAll('input[type="checkbox"]:checked').length;
        summary.textContent = selected > 0
          ? `${summary.dataset.baseLabel} (${selected} selected)`
          : summary.dataset.baseLabel;
      };
      dropdown.querySelectorAll('input[type="checkbox"]').forEach((cb) => cb.addEventListener('change', update));
      update();
    });
  }

  initModalUX();
  initSizeDropdownSummaries();

  populateProxyDropdown('proxyGroup');
  populateProxyDropdown('addProxy');
  populateProxyDropdown('editProxyGroup');
  populateProfileGroupDropdown("profileGroupSelect");
  populateProfileGroupDropdown("addProfileGroupSelect");
  populateProfileGroupDropdown("automationProfileGroupSelect");
  populateProfileGroupDropdown("editProfileGroupSelect");

  async function refreshConnectionStatus() {
    const el = document.getElementById('mainConnectionStatus');
    if (!el) return;
    try {
      const resp = await fetch(baseUrl + '/task-groups', { headers: skipHeader });
      if (!resp.ok) throw new Error('offline');
      el.textContent = '● Online';
      el.classList.remove('text-red-400');
      el.classList.add('text-emerald-400');
    } catch {
      el.textContent = '● Offline';
      el.classList.remove('text-emerald-400');
      el.classList.add('text-red-400');
    }
  }
  refreshConnectionStatus();
  setInterval(refreshConnectionStatus, 10000);

  window.showTasks();
});



    </script>
    <script>
      let sidebarExpanded = true;

      function toggleSidebar() {
        const sidebar = document.getElementById('sidebar');
        const icon = document.getElementById('sidebarToggleIcon');
        const textElements = document.querySelectorAll('.sidebar-text');

        if (sidebarExpanded) {
          sidebar.classList.remove('w-64');
          sidebar.classList.add('w-16');
          icon.classList.remove('fa-angle-double-left');
          icon.classList.add('fa-angle-double-right');
          textElements.forEach(el => el.classList.add('hidden'));
        } else {
          sidebar.classList.remove('w-16');
          sidebar.classList.add('w-64');
          icon.classList.remove('fa-angle-double-right');
          icon.classList.add('fa-angle-double-left');
          textElements.forEach(el => el.classList.remove('hidden'));
        }

        sidebarExpanded = !sidebarExpanded;
      }
    </script>
    <script>
      function toggleMobileMenu() {
        const menu = document.getElementById("mobileMenu");
        if (menu.classList.contains("hidden")) { menu.classList.remove("hidden"); }
        else { menu.classList.add("hidden"); }
      }
    </script>
  </body>
</html>
"""
#################################
# Directories for tasks, proxies, and profiles.
# Support both singular and plural folder names for compatibility
# with older/newer runtime layouts.
def _resolve_data_dir(singular_name: str, plural_name: str) -> str:
    singular = os.path.join(BASE_DIR, singular_name)
    plural = os.path.join(BASE_DIR, plural_name)

    singular_exists = os.path.isdir(singular)
    plural_exists = os.path.isdir(plural)

    # Prefer whichever existing folder already has files.
    if singular_exists:
        try:
            if os.listdir(singular):
                return singular
        except Exception:
            return singular
    if plural_exists:
        try:
            if os.listdir(plural):
                return plural
        except Exception:
            return plural

    # Otherwise prefer an existing folder, then create singular as default.
    if singular_exists:
        return singular
    if plural_exists:
        return plural

    os.makedirs(singular, exist_ok=True)
    return singular


TASK_DIR = _resolve_data_dir("task", "tasks")
PROXY_DIR = _resolve_data_dir("proxy", "proxies")
PROFILE_DIR = _resolve_data_dir("profile", "profiles")
os.makedirs(TASK_DIR, exist_ok=True)
os.makedirs(PROXY_DIR, exist_ok=True)
os.makedirs(PROFILE_DIR, exist_ok=True)

PROFILE_FIELDS = [
    "profileName", "firstName", "lastName", "email",
    "address1", "address2", "city", "state", "zipcode",
    "country", "phoneNumber", "ccNumber", "ccMonth", "ccYear", "cvv"
]


def _safe_data_filename(raw_name: str, allowed_ext: str) -> str:
    name = os.path.basename((raw_name or "").strip())
    if not name:
        raise ValueError("Missing file name")
    if not name.endswith(allowed_ext):
        name += allowed_ext
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("Invalid file name")
    return name


task_status = {}
processes = {}
scheduler = BackgroundScheduler()
scheduler.start()

def clear_make_engine_log():
    try:
        # truncate the log
        with open(make_engine_log, 'w', encoding='utf-8'):
            pass
        print(f"[{datetime.now().isoformat()}] Cleared make-engine.txt")
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Failed to clear make-engine.txt: {e}")

# schedule it to run every 36 hours
scheduler.add_job(
    clear_make_engine_log,
    trigger='interval',
    hours=36,
    next_run_time=datetime.now() + timedelta(hours=36),
    id='clear_make_engine_log',
    replace_existing=True
)

last_task_idx = 0
tid_to_group = {}
_engine_reader_started = False
_pattern = re.compile(r".*\[(TASK-\d{4})\]\|\s*>\s*(.*)")
make_engine_log = os.path.join(BASE_DIR, "make-engine.txt")
running_group_keys  = set()

#################################
# Helper Functions for Starting/Stopping Tasks
#################################
def _engine_reader():
    """Continuously read engine_child.stdout, log it, and update task_status."""
    with open(make_engine_log, "a", encoding="utf-8") as logf:
        while True:
            proc = engine_child
            if proc is None:
                break
            line = proc.stdout.readline()
            if not line:
                break
            logf.write(line); logf.flush()
            m = _pattern.search(line)
            if m:
                tid, msg = m.groups()
                g = tid_to_group.get(tid)
                if g and tid in task_status.get(g, {}):
                    task_status[g][tid] = msg.strip()

def start_task_process(group_name):
    """
    1) Reject with 409 if this group is already running and make-engine.exe is alive.
    2) Spawn (or reuse) the engine and begin running.
    """
    global engine_child, last_task_idx, tid_to_group, task_status, running_group_keys

    # Normalize key (no “.csv”)
    group_key = group_name.replace(".csv", "")

    # 1) guard against double-start, but only if engine is truly up
    if group_key in running_group_keys:
        # if make-engine.exe is still running, return JSON 409
        if engine_child is not None and engine_child.poll() is None:
            return jsonify({"error": f"Group {group_key} is already running"}), 409
        # otherwise clear the stale lock and proceed
        running_group_keys.discard(group_key)

    # mark as running
    running_group_keys.add(group_key)

    # count tasks in CSV
    file_path = os.path.join(TASK_DIR, group_name)
    try:
        with open(file_path, newline='', encoding='utf-8') as f:
            row_count = sum(1 for _ in csv.reader(f)) - 1
    except:
        row_count = 0

    # allocate global TASK-xxxx IDs
    start_id = last_task_idx + 1
    statuses = {}
    for i in range(row_count):
        tid = f"TASK-{start_id + i:04d}"
        statuses[tid] = "Idle"
        tid_to_group[tid] = group_key
    statuses["global_status"] = "Running"
    task_status[group_key] = statuses
    last_task_idx += max(0, row_count)

    # spawn engine if needed
    if engine_child is None or engine_child.poll() is not None:
        engine_child = subprocess.Popen(
            "make-engine.exe",
            cwd=SETTINGS["make_engine_path"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        threading.Thread(target=_engine_reader, daemon=True).start()

    # drive the menu to run that group
    engine_child.stdin.write("n\n");    engine_child.stdin.flush(); time.sleep(0.2)
    engine_child.stdin.write("2\n");    engine_child.stdin.flush(); time.sleep(0.2)
    engine_child.stdin.write(f"{group_key}\n"); engine_child.stdin.flush()

    # log invocation
    with open(make_engine_log, "a", encoding="utf-8") as logf:
        logf.write(f"[{datetime.now().isoformat()}] Started group {group_key}\n")

    # return a successful JSON response
    return jsonify({"message": f"Task group {group_key} started"}), 200

def stop_task_process(_):
    """
    Stops the engine and clears the 'running' flag so future runs can start.
    """
    global engine_child, task_status, last_task_idx, tid_to_group, running_group_keys

    # attempt a clean shutdown
    if engine_child and engine_child.poll() is None:
        try:
            engine_child.stdin.write("9\n")
            engine_child.stdin.flush()
        except:
            pass
        try:
            engine_child.terminate()
        except:
            engine_child.kill()

    # clear the handle so next start respawns
    engine_child = None

    # reset all task_status
    for g in list(task_status):
        task_status[g] = {"global_status": "Idle"}

    # reset counters & mappings
    last_task_idx = 0
    tid_to_group.clear()

    # clear any running flag
    running_group_keys.clear()



#################################
# NEW: AUTOMATIONS CODE BEGIN
#################################

# File to store automation groups
AUTOMATION_FILE = os.path.join(BASE_DIR, "automations.json")

def load_automation_groups():
    if os.path.exists(AUTOMATION_FILE):
        with open(AUTOMATION_FILE, "r") as f:
            return json.load(f)
    return []

def save_automation_groups(groups):
    with open(AUTOMATION_FILE, "w") as f:
        json.dump(groups, f, indent=2)

# Automation Endpoints
@app.route("/create-automation-group", methods=["POST"])
def create_automation_group():
    data = request.get_json()
    groupName = data.get("groupName")
    stores = data.get("stores")
    skus = data.get("skus")
    automationProfileGroup = data.get("automationProfileGroup")    # ← grab it
    profile = data.get("profile")
    proxy = data.get("proxy")
    taskCount = data.get("taskCount")
    mode = data.get("mode")
    sizes = data.get("sizes")
    runningTime = data.get("runningTime")
    active = data.get("active", True)
    
    # include it in your required‑fields check:
    if not (groupName and stores and skus and automationProfileGroup
            and profile and proxy and taskCount and mode and sizes and runningTime):
        return jsonify({"error": "Missing required fields"}), 400

    groups = load_automation_groups()
    if any(g["groupName"] == groupName for g in groups):
        return jsonify({"error": "Automation group already exists"}), 400

    group = {
        "groupName": groupName,
        "stores": stores,
        "skus": skus,
        "automationProfileGroup": automationProfileGroup,  # ← store it
        "profile": profile,
        "proxy": proxy,
        "taskCount": taskCount,
        "mode": mode,
        "sizes": sizes,
        "runningTime": runningTime,
        "active": active
    }
    groups.append(group)
    save_automation_groups(groups)
    schedule_automation_job(group)
    return jsonify({"message": "Automation group created"}), 201


@app.route("/automation-groups", methods=["GET"])
def get_automation_groups():
    groups = load_automation_groups()
    return jsonify(groups), 200

# New endpoint for editing automation groups
@app.route("/edit-automation-group", methods=["PUT"])
def edit_automation_group():
    data = request.get_json()
    # accept either field name from the client
    original_group_name = data.get("originalGroupName") or data.get("groupName")
    if not original_group_name:
        return jsonify({"error": "groupName required"}), 400

    updates = data.get("updates")
    if not updates or not isinstance(updates, dict):
        return jsonify({"error": "updates field required and must be a dictionary"}), 400

    groups = load_automation_groups()
    group_found = None
    for group in groups:
        if group["groupName"] == original_group_name:
            group_found = group
            break

    if not group_found:
        return jsonify({"error": "Automation group not found"}), 404

    old_name = group_found["groupName"]
    # apply updates
    for key, value in updates.items():
        group_found[key] = value

    new_name = group_found.get("groupName", old_name)
    # if renamed, cancel old job
    if new_name != old_name:
        cancel_automation_job(old_name)

    save_automation_groups(groups)

    # reschedule if still active
    if group_found.get("active", True):
        schedule_automation_job(group_found)

    return jsonify({"message": "Automation group edited", "groupName": new_name}), 200


# New endpoint for duplicating automation groups
@app.route("/duplicate-automation-group", methods=["POST"])
def duplicate_automation_group():
    data = request.get_json()
    original_group_name = data.get("groupName")
    if not original_group_name:
        return jsonify({"error": "groupName required"}), 400

    groups = load_automation_groups()
    original_group = None
    for group in groups:
        if group["groupName"] == original_group_name:
            original_group = group
            break

    if not original_group:
        return jsonify({"error": "Automation group not found"}), 404

    # Create a deep copy of the original group.
    import copy
    new_group = copy.deepcopy(original_group)

    # Generate a unique new group name by appending " Copy" (or " Copy X" if needed)
    base_name = original_group_name + " Copy"
    new_group_name = base_name
    count = 2
    existing_names = {group["groupName"] for group in groups}
    while new_group_name in existing_names:
        new_group_name = f"{base_name} {count}"
        count += 1

    new_group["groupName"] = new_group_name
    groups.append(new_group)
    save_automation_groups(groups)

    if new_group.get("active", True):
        schedule_automation_job(new_group)
    return jsonify({"message": "Automation group duplicated", "newGroupName": new_group_name}), 201


@app.route("/delete-automation-group", methods=["DELETE"])
def delete_automation_group():
    groupName = request.args.get("groupName")
    if not groupName:
        return jsonify({"error": "groupName parameter required"}), 400
    groups = load_automation_groups()
    new_groups = [g for g in groups if g["groupName"] != groupName]
    if len(new_groups) == len(groups):
        return jsonify({"error": "Automation group not found"}), 404
    save_automation_groups(new_groups)
    cancel_automation_job(groupName)
    return jsonify({"message": "Automation group deleted"}), 200

@app.route("/start-automation-group", methods=["POST"])
def start_automation_group():
    data = request.get_json()
    groupName = data.get("groupName")
    groups = load_automation_groups()
    for group in groups:
        if group["groupName"] == groupName:
            group["active"] = True
            save_automation_groups(groups)
            schedule_automation_job(group)
            return jsonify({"message": "Automation group started"}), 200
    return jsonify({"error": "Group not found"}), 404

@app.route("/stop-automation-group", methods=["POST"])
def stop_automation_group():
    data = request.get_json()
    groupName = data.get("groupName")
    groups = load_automation_groups()
    for group in groups:
        if group["groupName"] == groupName:
            group["active"] = False
            save_automation_groups(groups)
            cancel_automation_job(groupName)
            return jsonify({"message": "Automation group stopped"}), 200
    return jsonify({"error": "Group not found"}), 404

# Scheduler globals for automation jobs.
automation_jobs = {}         # { groupName: scheduler.Job, ... }
stop_automation_timers = {}  # { groupName: threading.Timer, ... }

def write_to_quicktask_csv(task_data_list):
    file_path = os.path.join("task", "Automations.csv")
    with open(file_path, mode="w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=[
            "profileGroup", "profileName", "proxyGroup", "accountGroup",
            "input", "size", "color", "site", "mode", "cartQuantity", "delay"
        ])
        writer.writeheader()
        for task_data in task_data_list:
            for _ in range(int(task_data["taskQuantity"])):
                writer.writerow({
                    "profileGroup": task_data["profileGroup"],
                    "profileName": task_data["profileName"],
                    "proxyGroup": task_data["proxyGroup"],
                    "accountGroup": task_data["accountGroup"],
                    "input": task_data["input"],
                    "size": task_data["size"],
                    "color": task_data["color"],
                    "site": task_data["site"],
                    "mode": task_data["mode"],
                    "cartQuantity": task_data["cartQuantity"],
                    "delay": task_data["delay"]
                })


def expand_size_presets(size_input):
    """
    Accepts either a preset string or an ampersand-delimited string of sizes.
    If it contains a known preset, it expands it. Otherwise, if it appears to
    already be in numeric format, it normalizes it.
    """
    if isinstance(size_input, str):
        size_input = size_input.strip()
        if not size_input:
            return ""
        # Remove any empty strings after splitting on "&".
        parts = [p for p in size_input.split("&") if p.strip()]
        try:
            # Try converting every part to float.
            [float(part) for part in parts]
            # If successful, sort and rejoin the numbers.
            sorted_parts = sorted(parts, key=lambda x: float(x))
            return "&".join(sorted_parts)
        except ValueError:
            # Not all parts are numeric; fall back to processing.
            size_list = parts
    elif isinstance(size_input, list):
        size_list = size_input
    else:
        return ""
        
    output = set()
    for s in size_list:
        s = s.strip()
        if s == "Mens Sizing (9-13)":
            sizes = [i / 2 for i in range(18, 27)]  # 9 to 13 with half sizes.
            output.update([str(int(x)) if x.is_integer() else str(x) for x in sizes])
        elif s == "GS Sizing (5-7)":
            sizes = [i / 2 for i in range(10, 15)]
            output.update([str(int(x)) if x.is_integer() else str(x) for x in sizes])
        elif s == "Big Mens Sizing (10.5-14)":
            sizes = [i / 2 for i in range(21, 29)]
            output.update([str(int(x)) if x.is_integer() else str(x) for x in sizes])
        elif s.lower() == "random":
            return "random"
        else:
            # Only add non-empty strings.
            if s:
                output.add(s)
    try:
        sorted_output = sorted(list(output), key=lambda x: float(x))
    except Exception:
        sorted_output = list(output)
    return "&".join(sorted_output)


def normalize_skus(skus):
    # If list of actual SKUs (alphanumeric without spaces), keep as list
    if isinstance(skus, list):
        if all(sku.replace("-", "").isalnum() and not any(c.isspace() for c in sku) for sku in skus):
            return skus
        else:
            return " | ".join(skus)  # Convert keyword-style list to string
    return skus  # Already a string or valid input

def parse_keywords(keyword_str):
    groups = keyword_str.split('|')
    parsed = []
    for group in groups:
        words = group.strip().split()
        positives = [w.lower() for w in words if not w.startswith('-')]
        negatives = [w[1:].lower() for w in words if w.startswith('-')]
        parsed.append((positives, negatives))
    return parsed

def is_keyword_match(group, text):
    positives, negatives = group
    return all(p in text for p in positives) and all(n not in text for n in negatives)



import threading
import requests
import json
from datetime import datetime

# Global locks and state
global_monitoring_lock = threading.Lock()
monitoring_locked = {}
last_sent_product = {}
stop_automation_timers = {}

from werkzeug.exceptions import Conflict

def check_store(prod: dict, group: dict):
    """
    Trigger exactly one automation run per product handle.
    Uses last_sent_product to ensure idempotency.
    """
    global global_monitoring_lock, last_sent_product

    group_name = group["groupName"]

    # ─── NEW: re-load and bail if group is inactive ─────────────────────
    latest = next((g for g in load_automation_groups() if g["groupName"] == group_name), None)
    if not latest or not latest.get("active", False):
        return
    # ─────────────────────────────────────────────────────────────────────

    site       = prod.get("site", "").strip().lower()
    handle     = prod.get("handle", "").strip()
    sku        = prod.get("sku", "").strip()
    csv_name   = "Automations.csv"

    # 1) Only fire on your configured settings.json sites
    allowed = [s["url"].lower() for s in SETTINGS["sites"]]
    if not any(site.endswith(a) for a in allowed):
        return

    # 2) Dedupe by handle: skip if we've already triggered for this handle & group
    with global_monitoring_lock:
        if last_sent_product.get(group_name) == handle:
            return
        last_sent_product[group_name] = handle

    # 3) Log the trigger
    with open("automation-logs.txt", "a") as logf:
        logf.write(f"[TRIGGER] {site} – handle={handle} sku={sku} group={group_name}\n")

    # 4) Build and write the quick-task CSV
    raw_sizes  = group.get("sizes", [])
    size_field = "random" if raw_sizes == "random" else raw_sizes
    task_data = {
        "profileGroup": group["automationProfileGroup"],
        "profileName":  group["profile"],
        "proxyGroup":   group["proxy"],
        "accountGroup": "example",
        "input":        f"{handle} -MakebotGUI",
        "size":         size_field,
        "color":        "Random",
        "site":         site,
        "mode":         group["mode"],
        "cartQuantity": "1",
        "delay":        "3333",
        "taskQuantity": group["taskCount"],
    }
    write_to_quicktask_csv([task_data])


    # 5) Start the engine and send a single webhook
    start_task_process(csv_name)
    send_webhook_notification(
        site=site,
        sku=sku,
        taskCount=group["taskCount"],
        group=group,
        product=prod,
        stopped=False
    )

    # 6) Schedule stop & cleanup (which will clear last_sent_product)
    def stop_and_resume():
        stop_task_process(csv_name)

        # Log the stop
        with open("automation-logs.txt", "a") as logf:
            logf.write(f"[STOPPED] {site} – handle={handle} group={group_name}\n")

        send_webhook_notification(
            site=site,
            sku=sku,
            taskCount=0,
            group=group,
            product=prod,
            stopped=True
        )

        # Clear the dedupe key so future matches for this handle can re-trigger
        with global_monitoring_lock:
            last_sent_product.pop(group_name, None)

        # Reactivate monitoring after 3s
        def reactivate():
            groups = load_automation_groups()
            for g in groups:
                if g["groupName"] == group_name:
                    g["active"] = True
                    save_automation_groups(groups)
                    schedule_automation_job(g)
                    break

        threading.Timer(3, reactivate).start()

    delay = int(group.get("runningTime", 10)) * 60
    t     = threading.Timer(delay, stop_and_resume)
    t.start()
    stop_automation_timers[group_name] = t



def automation_monitor(group: dict):
    """
    Every 3 seconds, fetch both EU and US monitor endpoints,
    merge and dedupe their product lists, then fire exactly one
    start‑webhook + task launch for the first site+SKU/handle match.
    Logs a single “[NO MATCH]” if none matched.
    """
    global global_monitoring_lock, active_automation_monitors

    group_name = group["groupName"]

    # Ensure only one monitor runs per group at a time
    with global_monitoring_lock:
        if active_automation_monitors.get(group_name):
            return
        active_automation_monitors[group_name] = True

    try:
        # Reload latest automation config
        all_groups = load_automation_groups()
        cfg = next((g for g in all_groups if g["groupName"] == group_name), group)
        if not cfg.get("active", True):
            return

        # Prepare SKU and store filters
        desired_skus = {s.lower() for s in cfg.get("skus", [])}
        stores = cfg.get("stores")
        if stores == "all":
            stores = [s["url"].lower() for s in SETTINGS["sites"]]
        else:
            stores = [s.lower() for s in set(stores)]

        # Fetch and merge both endpoints
        products = []
        for endpoint in (
            "https://automations.accountcodes.xyz/eumonitor",
            "https://automations.accountcodes.xyz/usmonitor"
        ):
            try:
                resp = requests.get(endpoint, timeout=5)
                if resp.status_code == 200:
                    products.extend(resp.json())
            except Exception:
                continue

        # Deduplicate by (site, handle)
        seen = set()
        unique = []
        for p in products:
            key = (p.get("site", "").lower(), p.get("handle", "").lower())
            if key and key not in seen:
                seen.add(key)
                unique.append(p)

        # Scan for first match
        matched = False
        for prod in unique:
            site = prod.get("site", "").lower()
            if not any(site.endswith(s) for s in stores):
                continue

            sku    = prod.get("sku", "").lower()
            handle = prod.get("handle", "").lower()
            if sku in desired_skus or handle in desired_skus:
                # Log match
                with open("automation-logs.txt", "a") as logf:
                    logf.write(f"[MATCH] {prod['site']} – handle={prod['handle']} sku={prod['sku']} group={group_name}\n")
                # Trigger task
                check_store(prod, cfg)
                matched = True
                break

        # If nothing matched, log once
        if not matched:
            with open("automation-logs.txt", "a") as logf:
                logf.write(f"[NO MATCH] group={group_name} checked {len(unique)} unique products\n")

    finally:
        with global_monitoring_lock:
            active_automation_monitors[group_name] = False



def schedule_automation_job(group):
    if group.get("active"):
        # → schedule each group’s monitor every 3 seconds instead of 2
        job = scheduler.add_job(
            lambda: automation_monitor(group),
            trigger='interval',
            seconds=2,
            id=group["groupName"],
            replace_existing=True,
            max_instances=1
        )
        automation_jobs[group["groupName"]] = job



def cancel_automation_job(groupName):
    job = automation_jobs.get(groupName)
    if job:
        job.remove()
        del automation_jobs[groupName]
    if groupName in stop_automation_timers:
        stop_automation_timers[groupName].cancel()
        del stop_automation_timers[groupName]

def schedule_all_automation_jobs():
    groups = load_automation_groups()
    for group in groups:
        if group.get("active"):
            schedule_automation_job(group)

schedule_all_automation_jobs()

def send_discord_bot_message(embed):
    token = SETTINGS.get("discord_bot_token", "").strip()
    channel_id = SETTINGS.get("discord_channel_id", "").strip()
    if not token or not channel_id:
        return
    try:
        r = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json"
            },
            json={"embeds": [embed]},
            timeout=10
        )
        if not r.ok:
            print(f"[{datetime.now()}] Discord bot message failed: {r.text}")
    except Exception as e:
        print(f"[{datetime.now()}] Discord bot message error: {e}")


def send_webhook_notification(site, sku, taskCount, group, product, stopped=False):
    # Retrieve the webhook URL from the settings dictionary.
    webhook_url = SETTINGS.get("webhook_url")

    title      = "Stopped Shopify Automation Tasks :red_circle:" if stopped else "Started Shopify Automation Tasks :green_circle:"
    time_label = "Time Ran" if stopped else "Time Running"
    color_hex  = 0xFF0000 if stopped else 0x00FF00

    # Use handle if sku is missing or "N/A"
    display_sku = sku if sku and sku.upper() != "N/A" else product.get("handle", "N/A")

    product_title = str(product.get("title", "No Title"))
    if not product_title.strip() or product_title.strip() == "0":
        product_title = "No Title"

    embed = {
        "title": title,
        "description": product_title,
        "color": color_hex,
        "fields": [
            {"name": "Site",       "value": str(site) if site else "N/A",                 "inline": True},
            {"name": "SKU",  "value": display_sku,                                   "inline": True},
            {"name": "Task Count",  "value": str(taskCount) if taskCount is not None else "N/A", "inline": True},
            {"name": time_label,    "value": f"{group.get('runningTime')} Minutes" if group.get("runningTime") is not None else "N/A", "inline": True},
            {"name": "Proxy Group", "value": str(group.get("proxy")) if group.get("proxy") else "N/A", "inline": True}
        ],
        "footer":   {"text": "Makebot+ Automation"},
        "timestamp": datetime.utcnow().isoformat(),
    }

    # Always put product["image"] into the thumbnail
    image_url = product.get("image")
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    else:
        # fallback to the first images list entry if available
        imgs = product.get("images")
        if isinstance(imgs, list) and imgs:
            src = imgs[0].get("src")
            if src:
                embed["thumbnail"] = {"url": src}

    payload = {"content": None, "embeds": [embed], "attachments": []}

    if webhook_url:
        try:
            r = requests.post(webhook_url, json=payload, timeout=10)
            if r.ok:
                print(f"[{datetime.now()}] Webhook sent: {'Stopped' if stopped else 'Started'}")
            else:
                print(f"[{datetime.now()}] Webhook failed: {r.text}")
        except Exception as e:
            print(f"[{datetime.now()}] Error sending webhook:", e)
    else:
        print("Webhook URL is not configured in settings.json")

    send_discord_bot_message(embed)


#################################
# NEW: AUTOMATIONS CODE END
#################################

#################################
# Discord OAuth2 Endpoints
#################################
@app.route("/discord/login")
def discord_login():
    client_id = SETTINGS.get("discord_client_id")
    redirect_uri = SETTINGS.get("discord_redirect_uri")
    discord_oauth_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={client_id}"
        f"&redirect_uri={redirect_uri}&response_type=code&scope=identify"
    )
    return redirect(discord_oauth_url)

@app.route("/discord/callback")
def discord_callback():
    code = request.args.get("code")
    if not code:
        return redirect(url_for("login"))
    data = {
        "client_id": SETTINGS.get("discord_client_id"),
        "client_secret": SETTINGS.get("discord_client_secret"),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SETTINGS.get("discord_redirect_uri"),
        "scope": "identify"
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token_response = requests.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
    token_json = token_response.json()
    access_token = token_json.get("access_token")
    if not access_token:
        return "OAuth failed", 400
    user_response = requests.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"})
    user_data = user_response.json()
    discord_id = user_data.get("id")
    allowed_ids = ["698272764636823572", "878292047616954398"]
    if discord_id not in allowed_ids:
        return "Access denied", 403
    session["authenticated"] = f"discord_{discord_id}"
    return redirect(url_for("home"))

#################################
# Authentication Middleware (Discord only)
#################################
@app.before_request
def require_auth():
    allowed_endpoints = [
        'login', 'discord_login', 'discord_callback', 'static',
        'get_app_settings', 'update_app_settings',
        'get_sites', 'add_site', 'update_site', 'delete_site',
        'validate_license_endpoint', 'create_license_key',
        'list_license_keys', 'revoke_license_key',
        'auth_status', 'logout'
    ]
    if request.endpoint in allowed_endpoints:
        startup_log(f"auth allow endpoint={request.endpoint}")
        return None

    # Electron app routes requests through IPC/fetch and does not keep Flask session cookies.
    # For API requests, rely on persisted license key validation instead of session cookie.
    if request.headers.get("Ngrok-Skip-Browser-Warning") == "1":
        key = (SETTINGS.get("license_key", "") or "").strip()
        if key and bool(SETTINGS.get("device_authenticated")):
            startup_log(f"auth allow endpoint={request.endpoint} via_device_auth")
            return None
        if not key:
            startup_log(f"auth deny endpoint={request.endpoint} reason=no_license_key")
            return jsonify({"error": "Not authenticated"}), 401
        valid, message = validate_license_key(key)
        if not valid:
            startup_log(f"auth deny endpoint={request.endpoint} reason=license_invalid detail={message}")
            return jsonify({"error": message or "License validation failed"}), 401
        startup_log(f"auth allow endpoint={request.endpoint} via_saved_license")
        return None

    if "authenticated" not in session:
        startup_log(f"auth redirect endpoint={request.endpoint} reason=no_session")
        return redirect(url_for('login'))

#################################
# Site Management Endpoints
#################################
@app.route('/settings', methods=['GET'])
def get_app_settings():
    startup_log("route /settings GET")
    key = (SETTINGS.get("license_key", "") or "").strip()
    device_auth = bool(SETTINGS.get("device_authenticated"))
    if key and device_auth:
        authenticated = True
    else:
        authenticated = False
        if key:
            valid, _ = validate_license_key(key)
            authenticated = bool(valid)
            if authenticated:
                SETTINGS["device_authenticated"] = True
                save_settings()
    return jsonify({
        "authenticated": authenticated,
        "license_key": key,
        "make_engine_path": SETTINGS.get("make_engine_path", ""),
        "webhook_url": SETTINGS.get("webhook_url", ""),
        "discord_bot_token": SETTINGS.get("discord_bot_token", ""),
        "discord_channel_id": SETTINGS.get("discord_channel_id", "")
        ,"custom_sizes": SETTINGS.get("custom_sizes", []),
        "device_authenticated": bool(SETTINGS.get("device_authenticated")),
    }), 200


@app.route('/settings', methods=['POST'])
def update_app_settings():
    data = request.get_json(silent=True) or {}
    startup_log(f"route /settings POST keys={','.join(sorted(data.keys())) if isinstance(data, dict) else 'none'}")
    allowed_keys = [
        "webhook_url", "discord_bot_token", "discord_channel_id", "license_key", "custom_sizes",
    ]
    for key in allowed_keys:
        if key in data:
            if key == "custom_sizes":
                raw = data.get(key, [])
                if isinstance(raw, list):
                    val = [str(x).strip() for x in raw if str(x).strip()]
                else:
                    val = []
                SETTINGS[key] = val
                continue
            val = str(data.get(key, "")).strip()
            if key == "license_key" and val:
                valid, message = validate_license_key(val)
                if not valid:
                    startup_log(f"route /settings POST license invalid detail={message}")
                    return jsonify({"error": message or "License validation failed"}), 400
                SETTINGS["device_authenticated"] = True
            SETTINGS[key] = val
    if "license_key" in data and not str(data.get("license_key", "")).strip():
        SETTINGS["device_authenticated"] = False
    save_settings()
    startup_log("route /settings POST success")
    return jsonify({"message": "Settings updated"}), 200


@app.route('/auth/status', methods=['GET'])
def auth_status():
    startup_log("route /auth/status GET")
    key = (SETTINGS.get("license_key", "") or "").strip()
    if not key:
        startup_log("route /auth/status no_key")
        return jsonify({"authenticated": False}), 200
    if bool(SETTINGS.get("device_authenticated")):
        startup_log("route /auth/status authenticated=true via_device_auth")
        return jsonify({"authenticated": True}), 200
    valid, message = validate_license_key(key)
    if not valid:
        startup_log(f"route /auth/status invalid detail={message}")
        return jsonify({"authenticated": False, "error": message}), 200
    SETTINGS["device_authenticated"] = True
    save_settings()
    startup_log("route /auth/status authenticated=true")
    return jsonify({"authenticated": True}), 200


@app.route('/logout', methods=['POST'])
def logout():
    startup_log("route /logout POST")
    SETTINGS["license_key"] = ""
    SETTINGS["device_authenticated"] = False
    save_settings()
    session.pop("authenticated", None)
    return jsonify({"ok": True}), 200


@app.route('/sites', methods=['GET'])
def get_sites():
    return jsonify(SETTINGS.get("sites", [])), 200

@app.route('/sites', methods=['POST'])
def add_site():
    data = request.get_json()
    name = data.get("name")
    url = data.get("url")
    if not name or not url:
        return jsonify({"error": "Name and URL required"}), 400
    SETTINGS["sites"].append({"name": name, "url": url})
    save_settings()
    return jsonify({"message": "Site added successfully"}), 201

@app.route('/sites', methods=['PUT'])
def update_site():
    data = request.get_json()
    original_name = data.get("originalName")
    name = data.get("name")
    url = data.get("url")
    if not original_name or not name or not url:
        return jsonify({"error": "Original name, name and URL required"}), 400
    updated = False
    for site in SETTINGS["sites"]:
        if site["name"] == original_name:
            site["name"] = name
            site["url"] = url
            updated = True
            break
    if updated:
        save_settings()
        return jsonify({"message": "Site updated successfully"}), 200
    else:
        return jsonify({"error": "Site not found"}), 404

@app.route('/sites', methods=['DELETE'])
def delete_site():
    name = request.args.get("name")
    if not name:
        return jsonify({"error": "Site name required"}), 400
    initial_count = len(SETTINGS["sites"])
    SETTINGS["sites"] = [site for site in SETTINGS["sites"] if site["name"] != name]
    if len(SETTINGS["sites"]) < initial_count:
        save_settings()
        return jsonify({"message": "Site deleted successfully"}), 200
    else:
        return jsonify({"error": "Site not found"}), 404

#################################
# Task Group Endpoints
#################################
@app.route('/create-task-group', methods=['POST'])
def create_task_group():
    data = request.get_json()
    file_name = data.get('fileName')
    if not file_name:
        return jsonify({"error": "fileName not provided"}), 400
    if not file_name.endswith('.csv'):
        file_name += '.csv'
    file_path = os.path.join(TASK_DIR, file_name)
    if os.path.exists(file_path):
        return jsonify({"error": "Task group already exists"}), 400

    input_val = data.get('input', '')
    site_val  = data.get('site', '')
    size_val  = data.get('size', '')

    if input_val.startswith("http"):
        parsed_site, result = parse_link_input(input_val, size_val)
        site_val = parsed_site or site_val
        if isinstance(result, dict) and result.get("fallback"):
            input_val = result["input"]
        else:
            input_val = result

    # Handle multiple profiles
    profile_names = data.get("profileNames")
    if isinstance(profile_names, list) and profile_names:
        names = profile_names
    else:
        single = data.get("profileName", "")
        names = [single] if single else []

    base_row = [
        data.get('profileGroup', ''),
        None,  # placeholder for profileName
        data.get('proxyGroup', ''),
        data.get('accountGroup', ''),
        input_val,
        size_val,
        data.get('color', ''),
        site_val,
        data.get('mode', ''),
        data.get('cartQuantity', ''),
        data.get('delay', '')
    ]

    try:
        per_profile = int(data.get('taskQuantity', 1))
        with open(file_path, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                "profileGroup","profileName","proxyGroup","accountGroup",
                "input","size","color","site","mode","cartQuantity","delay"
            ])
            for profile in names:
                row = base_row[:]         # copy
                row[1] = profile          # set profileName
                for _ in range(per_profile):
                    writer.writerow(row)
        return jsonify({"message": "Task group created"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/task-groups', methods=['GET'])
def get_task_groups():
    try:
        files = [f for f in os.listdir(TASK_DIR) if f.endswith('.csv')]
        return jsonify(files), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/tasks', methods=['GET'])
def get_tasks():
    file_name = request.args.get('file')
    if not file_name:
        return jsonify({"error": "file parameter required"}), 400
    file_path = os.path.join(TASK_DIR, file_name)
    if not os.path.exists(file_path):
        return jsonify({"error": "Task file not found"}), 404
    try:
        with open(file_path, mode="r", newline="") as csvfile:
            reader = csv.DictReader(csvfile)
            tasks = [row for row in reader]
        return jsonify(tasks), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/delete-task-group', methods=['DELETE'])
def delete_task_group():
    file_name = request.args.get('file')
    if not file_name:
        return jsonify({"error": "file parameter required"}), 400
    file_path = os.path.join(TASK_DIR, file_name)
    if not os.path.exists(file_path):
        return jsonify({"error": "Task file not found"}), 404
    try:
        os.remove(file_path)
        return jsonify({"message": "Task group deleted successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

#################################
# Endpoints for Editing and Adding Tasks
#################################
@app.route('/edit-task', methods=['POST'])
def edit_task():
    data = request.get_json()
    group_name = data.get('groupName')
    try:
        index = int(data.get('index'))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid task index"}), 400
    task_data = data.get('task')
    if not group_name or task_data is None:
        return jsonify({"error": "Missing groupName or task data"}), 400

    file_path = os.path.join(TASK_DIR, group_name)
    if not os.path.exists(file_path):
        return jsonify({"error": "Task file not found"}), 404

    try:
        with open(file_path, mode="r", newline="") as csvfile:
            rows = list(csv.reader(csvfile))

        if len(rows) < 2:
            return jsonify({"error": "No tasks in file"}), 400
        if index < 0 or index >= len(rows) - 1:
            return jsonify({"error": "Task index out of range"}), 400

        row = rows[index + 1]

        input_val = task_data.get('input', row[4])
        site_val = task_data.get('site', row[7])
        if input_val.startswith("http"):
            parsed_site, parsed_keywords = parse_link_input(input_val)
            if parsed_site:
                site_val = parsed_site
                input_val = parsed_keywords

        row[1] = task_data.get('profileName', row[1])
        row[2] = task_data.get('proxyGroup', row[2])
        row[4] = input_val
        row[5] = task_data.get('size', row[5])
        row[7] = site_val
        row[8] = task_data.get('mode', row[8])

        with open(file_path, mode="w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerows(rows)

        return jsonify({"message": "Task updated successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/add-task', methods=['POST'])
def add_task():
    data = request.get_json()
    group_name    = data.get('groupName')
    task_data     = data.get('task')
    profile_names = data.get('profileNames', [])
    quantity      = int(data.get('taskQuantity', 1))

    if not group_name or not task_data or not profile_names:
        return jsonify({"error": "Missing groupName, task data, or profileNames"}), 400

    file_path = os.path.join(TASK_DIR, group_name)
    if not os.path.exists(file_path):
        return jsonify({"error": "Task file not found"}), 404

    # parse link input if needed
    input_val = task_data.get("input", "")
    site_val  = task_data.get("site", "")
    if input_val.startswith("http"):
        parsed_site, result = parse_link_input(input_val, task_data.get("size", ""))
        site_val  = parsed_site or site_val
        input_val = result if not (isinstance(result, dict) and result.get("fallback")) else result["input"]

    # build one “template” row, with profileName placeholder at index 1
    template_row = [
        task_data.get("profileGroup", ""),
        None,  # profileName will go here
        task_data.get("proxyGroup", ""),
        "example",
        input_val,
        task_data.get("size", ""),
        task_data.get("color", "random"),
        site_val,
        task_data.get("mode", ""),
        "1",       # cartQuantity
        "3333"     # delay
    ]

    try:
        with open(file_path, "a", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            for profile in profile_names:
                row = list(template_row)
                row[1] = profile
                for _ in range(quantity):
                    writer.writerow(row)
        return jsonify({"message": "Task(s) added successfully"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/edit-task-group', methods=['POST'])
def edit_task_group():
      data = request.get_json()
      file_name = data.get('fileName')
      task_group_data = data.get('taskGroupData')
      if not file_name or task_group_data is None:
            return jsonify({"error": "Missing fileName or taskGroupData"}), 400
      file_path = os.path.join(TASK_DIR, file_name)
      if not os.path.exists(file_path):
            return jsonify({"error": "Task file not found"}), 404

      try:
            with open(file_path, "r", newline="") as f:
                  rows = list(csv.reader(f))
            if not rows:
                  return jsonify({"error": "CSV file is empty"}), 400

            # Parse link input
            input_val = task_group_data.get('input', '')
            site_val = task_group_data.get('site', '')
            if input_val.startswith("http"):
                  parsed_site, result = parse_link_input(input_val, task_group_data.get("size", ""))
                  site_val = parsed_site or site_val
                  if isinstance(result, dict) and result.get("fallback"):
                        input_val = result["input"]
                  else:
                        input_val = result
                  task_group_data["input"] = input_val
                  task_group_data["site"] = site_val

            # Update each task
            for i in range(1, len(rows)):
                  row = rows[i]
                  # 0: profileGroup
                  if 'profileGroup' in task_group_data:
                        row[0] = task_group_data['profileGroup']
                  # 1: profileName
                  if 'profileName' in task_group_data:
                        row[1] = task_group_data['profileName']
                  # 2: proxyGroup
                  if 'proxyGroup' in task_group_data:
                        row[2] = task_group_data['proxyGroup']
                  # 4: input
                  if 'input' in task_group_data:
                        row[4] = task_group_data['input']
                  # 5: size
                  if 'size' in task_group_data:
                        row[5] = expand_size_presets(task_group_data['size'])
                  # 6: color
                  if 'color' in task_group_data:
                        row[6] = task_group_data['color']
                  # 7: site
                  if 'site' in task_group_data:
                        row[7] = task_group_data['site']
                  # 8: mode
                  if 'mode' in task_group_data:
                        row[8] = task_group_data['mode']
                  # 9: cartQuantity
                  if 'cartQuantity' in task_group_data:
                        row[9] = task_group_data['cartQuantity']
                  # 10: delay
                  if 'delay' in task_group_data:
                        row[10] = task_group_data['delay']

            # Resize with balanced distribution
            new_quantity = task_group_data.get("taskQuantity")
            if new_quantity is not None:
                  try:
                        from collections import defaultdict
                        import random

                        new_quantity = int(new_quantity)
                        header, *data_rows = rows
                        group_map = defaultdict(list)

                        for row in data_rows:
                              key = tuple(row)
                              group_map[key].append(row)

                        grouped_rows = list(group_map.values())
                        for group in grouped_rows:
                              random.shuffle(group)

                        balanced_rows = []
                        while len(balanced_rows) < new_quantity:
                              for group in grouped_rows:
                                    if len(balanced_rows) >= new_quantity:
                                          break
                                    if group:
                                          index = len(balanced_rows) % len(group)
                                          balanced_rows.append(group[index % len(group)])
                                    else:
                                          fallback_group = random.choice(list(group_map.values()))
                                          balanced_rows.append(random.choice(fallback_group))

                        rows = [header] + balanced_rows
                  except Exception as e:
                        return jsonify({"error": f"Invalid task quantity: {str(e)}"}), 400

            with open(file_path, "w", newline="") as f:
                  writer = csv.writer(f)
                  writer.writerows(rows)

            # Rename if needed
            new_name = task_group_data.get("taskGroupName", "").strip()
            if new_name and new_name + ".csv" != file_name:
                  new_path = os.path.join(TASK_DIR, new_name + ".csv")
                  os.rename(file_path, new_path)
                  return jsonify({"message": "Renamed", "file": new_name + ".csv"}), 200

            return jsonify({"message": "Updated successfully"}), 200
      except Exception as e:
            return jsonify({"error": str(e)}), 500

# ────────────────────────────────────────────────────────────────
# Profiles CSV editor endpoints
# ────────────────────────────────────────────────────────────────

@app.route('/profile-groups', methods=['GET'])
def get_profile_groups():
    try:
        groups = [f[:-4] for f in os.listdir(PROFILE_DIR) if f.endswith('.csv')]
        return jsonify(groups), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/profile-files', methods=['GET'])
def profile_files():
    try:
        files = [f for f in os.listdir(PROFILE_DIR) if f.endswith('.csv')]
        return jsonify(files), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/profile-content', methods=['GET'])
def profile_content():
    file_name = request.args.get('file')
    if not file_name:
        return jsonify({"error": "file parameter required"}), 400
    try:
        safe_name = _safe_data_filename(file_name, ".csv")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    file_path = os.path.join(PROFILE_DIR, safe_name)
    if not os.path.exists(file_path):
        return jsonify({"error": "Profile file not found"}), 404
    try:
        with open(file_path, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = [row for row in reader]
        return jsonify(rows), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/save-profile', methods=['POST'])
def save_profile():
    file_name = request.args.get('file')
    if not file_name:
        return jsonify({"error": "file parameter required"}), 400
    try:
        safe_name = _safe_data_filename(file_name, ".csv")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    file_path = os.path.join(PROFILE_DIR, safe_name)
    data = request.get_json()
    rows = data.get('rows')
    if rows is None or not isinstance(rows, list):
        return jsonify({"error": "Invalid payload"}), 400
    try:
        with open(file_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=PROFILE_FIELDS)
            writer.writeheader()
            normalized = []
            for row in rows:
                normalized.append({field: row.get(field, "") for field in PROFILE_FIELDS})
            writer.writerows(normalized)
        return jsonify({"message": "Profile saved"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


#################################
# Proxy Endpoints
#################################
@app.route('/proxy-files', methods=['GET'])
def get_proxy_files():
    try:
        files = [f for f in os.listdir(PROXY_DIR) if f.endswith('.txt')]
        return jsonify(files), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/proxy-content', methods=['GET'])
def get_proxy_content():
    file_name = request.args.get('file')
    if not file_name:
        return jsonify({"error": "file parameter required"}), 400
    file_path = os.path.join(PROXY_DIR, file_name)
    if not os.path.exists(file_path):
        return jsonify({"error": "Proxy file not found"}), 404
    try:
        with open(file_path, mode="r", encoding="utf-8") as f:
            content = f.read()
        return content, 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/save-proxy', methods=['POST'])
def save_proxy():
    file_name = request.args.get('file')
    if not file_name:
        return jsonify({"error": "file parameter required"}), 400
    file_path = os.path.join(PROXY_DIR, file_name)
    data = request.get_json()
    content = data.get('content')
    if content is None:
        return jsonify({"error": "No content provided"}), 400
    try:
        with open(file_path, mode="w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"message": "Proxy file saved successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

#################################
# Start/Stop Task and Status Endpoints
#################################
@app.route('/start-task', methods=['POST'])
def start_task():
    data = request.get_json()
    group_name = data.get("groupName")
    if not group_name:
        return jsonify({"error": "groupName required"}), 400

    # simply start (or reuse) the engine for this group—do NOT stop any others
    start_task_process(group_name)

    return jsonify({
        "message": "Task group started successfully",
        "startedGroup": group_name.replace(".csv", "")
    }), 200


@app.route('/stop-task', methods=['POST'])
def stop_task():
    data = request.get_json()
    group_name = data.get("groupName")
    if not group_name:
        return jsonify({"error": "groupName required"}), 400
    stop_task_process(group_name)
    return jsonify({"message": "Task group stopped successfully"}), 200

@app.route('/task-status', methods=['GET'])
def get_task_status():
    group = request.args.get('group')
    if not group:
        return jsonify({"error": "group parameter required"}), 400
    group = group.replace(".csv", "")
    statuses = task_status.get(group, {})
    return jsonify({"statuses": statuses}), 200

@app.route('/running-groups')
def running_groups():
    # Return all groups whose global_status is still “Running”
    running = [
        group
        for group, statuses in task_status.items()
        if statuses.get("global_status", "").lower() == "running"
    ]
    return jsonify(running), 200


#################################
# Main Login Endpoint
#################################

LICENSEGATE_API_BASE = "https://api.licensegate.io"
LICENSEGATE_USER_ID = "a24f5"


def validate_license_key(key):
    key = (key or "").strip()
    startup_log(f"license validate start key_prefix={key[:6] if key else ''}")
    if not key:
        startup_log("license validate fail reason=empty_key")
        return False, "License key is required."
    if not LICENSEGATE_USER_ID:
        startup_log("license validate fail reason=missing_user_id")
        return False, "LicenseGate user ID is not configured in backend."

    try:
        startup_log(f"licensegate request GET {LICENSEGATE_API_BASE}/license/{LICENSEGATE_USER_ID}/<key>/verify")
        resp = requests.get(
            f"{LICENSEGATE_API_BASE}/license/{LICENSEGATE_USER_ID}/{key}/verify",
            timeout=8
        )
    except Exception as e:
        print(f"[license] request error: {e}")
        startup_log(f"licensegate request error={e}")
        return False, f"LicenseGate request failed: {e}"

    if resp.status_code >= 400:
        msg = f"LicenseGate HTTP {resp.status_code}"
        try:
            body = resp.json()
            msg = body.get("message") or body.get("result") or msg
        except Exception:
            pass
        print(f"[license] http error: {msg}")
        startup_log(f"licensegate http_error status={resp.status_code} detail={msg}")
        return False, msg
    try:
        body = resp.json()
    except Exception:
        startup_log("licensegate invalid_json_response")
        return False, "Invalid response from LicenseGate."

    valid_val = body.get("valid")
    if isinstance(valid_val, str):
        valid = valid_val.lower() == "true"
    else:
        valid = bool(valid_val)

    if not valid:
        detail = body.get("result") or body.get("message") or "Invalid license key."
        if isinstance(detail, str):
            upper = detail.upper()
            if upper == "NOT_FOUND":
                detail = "Invalid key or wrong LicenseGate user ID."
            elif upper == "EXPIRED":
                detail = "License key has expired."
            elif upper == "SUSPENDED":
                detail = "License key is suspended."
        print(f"[license] invalid key: {detail}")
        startup_log(f"license validate invalid detail={detail}")
        return False, detail
    print("[license] key validated successfully")
    startup_log("license validate success")
    return True, ""

@app.route("/", methods=["GET", "POST"])
def login():
    try:
        startup_log(f"route / method={request.method}")
        # Check if a license key is already saved in settings.json.
        saved_key = SETTINGS.get("license_key", "").strip()
        if saved_key:
            startup_log("route / checking_saved_license")
            if bool(SETTINGS.get("device_authenticated")):
                session["authenticated"] = True
                startup_log("route / saved_license_valid via_device_auth")
                return redirect(url_for("home"))
            valid, _ = validate_license_key(saved_key)
            if valid:
                session["authenticated"] = True
                SETTINGS["device_authenticated"] = True
                save_settings()
                startup_log("route / saved_license_valid")
                return redirect(url_for("home"))
            # Remove invalid key from settings.
            SETTINGS.pop("license_key", None)
            save_settings()
            startup_log("route / saved_license_invalid_cleared")

        if request.method == "POST":
            key = request.form.get("license_key", "").strip()
            startup_log(f"route / POST received key_prefix={key[:6] if key else ''}")
            valid, message = validate_license_key(key)
            if valid:
                session["authenticated"] = True
                SETTINGS["license_key"] = key
                SETTINGS["device_authenticated"] = True
                save_settings()
                startup_log("route / POST login_success")
                return redirect(url_for("home"))
            startup_log(f"route / POST login_failed detail={message}")
            return render_template_string(HTML_LOGIN, error=message or "License validation failed.")

        startup_log("route / render_login")
        return render_template_string(HTML_LOGIN)
    except Exception as e:
        print(f"[login] unexpected error: {e}")
        startup_log(f"route / error={e}")
        return render_template_string(HTML_LOGIN, error=f"Login error: {e}")


@app.route("/license/validate", methods=["POST"])
def validate_license_endpoint():
    payload = request.get_json(silent=True) or {}
    key = payload.get("license_key", "")
    startup_log(f"route /license/validate POST key_prefix={str(key)[:6] if key else ''}")
    valid, message = validate_license_key(key)
    status = 200 if valid else 400
    startup_log(f"route /license/validate result_valid={valid} status={status}")
    return jsonify({"valid": valid, "message": message}), status


@app.route("/license/keys", methods=["POST"])
def create_license_key():
    return jsonify({
        "error": "License creation is not enabled in this backend for LicenseGate. Create keys in the LicenseGate dashboard."
    }), 501


@app.route("/license/keys", methods=["GET"])
def list_license_keys():
    return jsonify({
        "error": "Listing licenses is not enabled in this backend for LicenseGate. Use dashboard."
    }), 501


@app.route("/license/keys/revoke", methods=["POST"])
def revoke_license_key():
    return jsonify({
        "error": "Revoking licenses is not enabled in this backend for LicenseGate. Use dashboard."
    }), 501


@app.route('/home')
def home():
    return render_template_string(HTML_CONTENT)

def open_browser():
    time.sleep(1)
    webbrowser.open("http://localhost:2345")

if __name__ == '__main__':
    # Only open the browser if this is the reloader's child process and not in Electron.
    if os.environ.get('ELECTRON_RUN') != '1' and os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        threading.Thread(target=open_browser).start()
    debug_mode = os.environ.get('FLASK_DEBUG', '1') == '1'
    app.run(host='0.0.0.0', port=2345, debug=debug_mode)
