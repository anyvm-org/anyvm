#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function
import sys
import os
import platform
import subprocess
import time
import socket
import json
import getpass
import shutil
import shlex
import re
import threading
import random
import asyncio
import base64
import hashlib
import struct
import collections
import ssl
import ipaddress

# Python 2/3 compatibility for urllib and input
try:
    # Python 3
    from urllib.request import urlopen, Request, urlretrieve
    from urllib.error import HTTPError, URLError
    from urllib.parse import urljoin
    input_func = input
except ImportError:
    # Python 2
    from urllib2 import urlopen, Request, HTTPError, URLError
    from urlparse import urljoin
    
    def urlretrieve(url, filename, reporthook=None):
        try:
            u = urlopen(url)
            total_size = 0
            try:
                total_size = int(u.headers.get('Content-Length'))
            except Exception:
                total_size = 0
            block_num = 0
            with open(filename, 'wb') as f:
                while True:
                    chunk = u.read(8192)
                    if not chunk:
                        if reporthook:
                            reporthook(block_num, 8192, total_size)
                        break
                    f.write(chunk)
                    if reporthook:
                        reporthook(block_num, len(chunk), total_size)
                    block_num += 1
        except Exception as e:
            raise e
    
    try:
        input_func = raw_input
    except NameError:
        input_func = input

IS_WINDOWS = (os.name == 'nt')

# Handle SSL certificate verification on Windows (especially Arm64/minimal installs).
if IS_WINDOWS:
    try:
        # Try to use Windows Certificate Store directly to avoid dependency on certifi.
        def _create_windows_context():
            ctx = ssl.create_default_context()
            try:
                # ROOT and CA stores contain the trusted anchors on Windows.
                for storename in ["ROOT", "CA"]:
                    for cert, encoding, trust in ssl.enum_certificates(storename):
                        if encoding == "x509_asn":
                            try:
                                # Convert DER to PEM and load into context.
                                ctx.load_verify_locations(cdata=ssl.DER_cert_to_PEM_cert(cert))
                            except Exception:
                                pass
            except Exception:
                pass
            return ctx
        
        # Test if it works, then set as default context creator for urllib.
        _test_ctx = _create_windows_context()
        ssl._create_default_https_context = _create_windows_context
    except Exception:
        # Fallback to certifi if available, then to unverified as a last resort.
        try:
            import certifi
            os.environ['SSL_CERT_FILE'] = certifi.where()
        except ImportError:
            try:
                if hasattr(ssl, '_create_unverified_context'):
                    ssl._create_default_https_context = ssl._create_unverified_context
            except Exception:
                pass

try:
    DEVNULL = subprocess.DEVNULL  # Python 3.3+
except AttributeError:
    DEVNULL = open(os.devnull, 'wb')

SSH_KNOWN_HOSTS_NULL = "NUL" if IS_WINDOWS else "/dev/null"

OPENBSD_E1000_RELEASES = {"7.3", "7.4", "7.5", "7.6"}


DEFAULT_BUILDER_VERSIONS = {
    "freebsd": "2.1.0",
    "openbsd": "2.0.2",
    "netbsd": "2.0.3",
    "dragonflybsd": "2.0.4",
    "solaris": "2.0.5",
    "omnios": "2.0.8",
    "haiku": "2.0.0",
    "midnightbsd": "2.0.2",
    "openindiana": "2.0.9"
}

VERSION_TOKEN_RE = re.compile(r"[0-9]+|[A-Za-z]+")


def removesuffix(text, suffix):
    """Compatibility helper mirroring str.removesuffix."""
    if not suffix:
        return text
    if hasattr(text, "removesuffix"):
        return text.removesuffix(suffix)
    if text.endswith(suffix):
        return text[:-len(suffix)]
    return text

def format_command_for_display(cmd_list):
    """Pretty-print the QEMU command with platform-appropriate quoting."""
    if IS_WINDOWS:
        def quote(arg):
            if not arg:
                return '""'
            if any(ch in arg for ch in ' \t"'):
                return '"' + arg.replace('"', '""') + '"'
            return arg
        joiner = " ^\n  "
        return joiner.join(quote(arg) for arg in cmd_list)
    return " \\\n  ".join(shlex.quote(arg) for arg in cmd_list)

def log(msg):
    t = time.time()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".{:03d}".format(int(t % 1 * 1000))
    line = "[{}] {}".format(timestamp, msg)
    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        # Clear current line (progress bar) and move cursor to beginning,
        # then emit each line with an explicit CR+LF so we don't depend on
        # the TTY's ONLCR mode being on (something upstream sometimes flips it).
        sys.stdout.write("\r\x1b[K")
        sys.stdout.write(line.replace("\n", "\r\n") + "\r\n")
        sys.stdout.flush()
    else:
        print(line)
        sys.stdout.flush()

def supports_ansi_color(stream=sys.stdout):
    """Checks if the stream supports ANSI color sequences."""
    try:
        if not hasattr(stream, "isatty") or not stream.isatty():
            return False
    except Exception:
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if IS_WINDOWS:
        if os.environ.get("WT_SESSION"):
            return True
        if os.environ.get("ANSICON"):
            return True
        if os.environ.get("ConEmuANSI", "").upper() == "ON":
            return True
        if os.environ.get("TERM"):
            return True
        return False
    return True

def debuglog(enabled, msg):
    """Conditional debug logger."""
    if enabled:
        t = time.time()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".{:03d}".format(int(t % 1 * 1000))
        line = "[{}] [DEBUG] {}".format(timestamp, msg)
        if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
            # Emit explicit CR+LF -- some subprocess on the way flips ONLCR off,
            # so plain \n would leave the cursor at the previous column.
            sys.stdout.write(line.replace("\n", "\r\n") + "\r\n")
            sys.stdout.flush()
        else:
            print(line)
            # Flush every line so CI logs reflect real time. Without this,
            # debuglog output is block-buffered when stdout is a pipe and
            # a hang in anyvm.py would lose all in-flight trace messages.
            sys.stdout.flush()

def is_browser_available():
    """Returns True if the current environment can likely open a local browser."""
    # Always return False in CI environments
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return False
    
    try:
        if IS_WINDOWS:
            return True
        # Check for WSL environment
        if platform.system() == 'Linux':
            try:
                if os.path.exists('/proc/version'):
                    with open('/proc/version', 'r') as f:
                        if 'microsoft' in f.read().lower():
                            return True
            except:
                pass
            # Linux: Check if DISPLAY or WAYLAND_DISPLAY is set
            if os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
                return True
        elif platform.system() == 'Darwin':
            # macOS: Check if likely in a GUI session (not over SSH)
            if not os.environ.get('SSH_CLIENT') and not os.environ.get('SSH_TTY'):
                return True
    except:
        pass
    return False

def open_vnc_page(web_port):
    """Automatically open the VNC web page in the browser based on environment."""
    if not web_port or not is_browser_available():
        return

    def _open_in_background():
        # Give VNC proxy/QEMU a moment to initialize ports properly
        time.sleep(1)
        url = "http://localhost:{}".format(web_port)
        try:
            if IS_WINDOWS or (platform.system() == 'Linux' and 'microsoft' in open('/proc/version').read().lower()):
                # Windows or WSL
                subprocess.Popen(['explorer.exe', url], shell=IS_WINDOWS)
            elif platform.system() == 'Darwin':
                subprocess.Popen(['open', url], stdout=DEVNULL, stderr=DEVNULL)
            elif platform.system() == 'Linux':
                subprocess.Popen(['xdg-open', url], stdout=DEVNULL, stderr=DEVNULL)
        except Exception:
            pass

    t = threading.Thread(target=_open_in_background)
    t.daemon = True
    t.start()

def fatal(msg):
    print("Error: {}".format(msg), file=sys.stderr)
    sys.exit(1)

# --- VNC Web Proxy ---
VNC_WEB_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>AnyVM - VNC Viewer</title>
    <placeholder_scripts>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body { 
            background: #0f172a; 
            width: 100%;
            height: 100%;
            overflow: hidden;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            color: #f1f5f9;
            direction: ltr;
        }
        #container {
            position: relative;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            border-radius: 8px;
            overflow: hidden;
            background: #000;
            border: 1px solid #334155;
            display: flex;
            flex-direction: column;
            width: fit-content;
            height: fit-content;
        }
        #terminal-container {
            position: absolute;
            top: 20px;
            bottom: 20px;
            left: 15px;
            right: 15px;
            display: none;
            background: transparent;
        }
        .xterm-rows { font-family: 'JetBrains Mono', 'Fira Code', 'Courier New', monospace !important; }
        #status {
            color: #94a3b8;
            font-size: 10px;
            font-family: 'JetBrains Mono', 'Fira Code', monospace;
            padding: 2px 10px;
            border-radius: 99px;
            background: rgba(30, 41, 59, 0.5);
            border: 1px solid rgba(71, 85, 105, 0.4);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            white-space: nowrap;
            display: flex;
            align-items: center;
        }
        #status.connected {
            color: #4ade80;
            background: rgba(20, 83, 45, 0.3);
            border-color: rgba(34, 197, 94, 0.4);
        }
        #status.reconnecting {
            position: fixed;
            z-index: 5000;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            padding: 30px 60px;
            font-size: 20px;
            background: rgba(15, 23, 42, 0.95);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7),
                        0 0 0 100vmax rgba(0, 0, 0, 0.6);
            border: 1px solid rgba(71, 85, 105, 0.5) !important;
            color: #f1f5f9 !important;
            border-radius: 16px;
            font-weight: 500;
            pointer-events: none;
            text-align: center;
            line-height: 1.6;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        #screen {
            background: #000;
            display: block;
            /* Nearest-neighbor scaling for maximum sharpness */
            image-rendering: -webkit-optimize-contrast;
            image-rendering: pixelated;
            image-rendering: crisp-edges;
            -ms-interpolation-mode: nearest-neighbor;
            
            max-width: calc(100vw - 40px);
            max-height: calc(100vh - 100px);
            width: auto;
            height: auto;
            transition: filter 0.5s ease;
            outline: none; /* Hide focus outline on canvas */
            cursor: default;
        }
        #screen:fullscreen {
            width: auto; /* Managed by JS for integer scaling */
            height: auto;
            object-fit: contain;
            background: #000;
            image-rendering: pixelated;
        }
        #screen:-webkit-full-screen { 
            width: auto; 
            height: auto; 
            object-fit: contain; 
            image-rendering: pixelated; 
        }
        #screen.disconnected {
            filter: grayscale(100%) brightness(0.7);
            cursor: auto !important;
        }
        .error { color: #f87171 !important; border-color: #7f1d1d !important; }
        .toolbar {
            position: fixed;
            left: 50%;
            transform: translateX(-50%) translateY(0);
            bottom: 0;
            display: flex;
            align-items: center;
            gap: 8px;
            z-index: 1000;
            background: rgba(15, 23, 42, 0.4);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            padding: 6px 16px;
            border-radius: 12px 12px 0 0;
            border: 1px solid rgba(51, 65, 85, 0.5);
            border-bottom: none;
            box-shadow: 0 -4px 15px rgba(0, 0, 0, 0.3);
            transition: transform 0.4s cubic-bezier(0.19, 1, 0.22, 1), background 0.3s ease;
        }
        .toolbar.auto-hide {
            transform: translateX(-50%) translateY(calc(100% - 6px));
        }
        .toolbar::before {
            content: '';
            position: absolute;
            top: -20px;
            left: 0;
            right: 0;
            height: 20px;
        }
        .toolbar.top {
            top: 0;
            bottom: auto;
            transform: translateX(-50%) translateY(0);
            border-radius: 0 0 12px 12px;
            border-top: none;
            border-bottom: 1px solid rgba(51, 65, 85, 0.5);
        }
        .toolbar.top.auto-hide {
            transform: translateX(-50%) translateY(calc(-100% + 6px));
        }
        .toolbar.top::before {
            top: auto;
            bottom: -20px;
        }
        .toolbar-group {
            display: flex;
            align-items: center;
            gap: 6px;
            border-right: 1px solid rgba(71, 85, 105, 0.3);
            padding-right: 8px;
            margin-right: 4px;
        }
        .toolbar.top button {
            padding: 4px 12px;
            font-size: 11px;
        }
        .toolbar-group:last-child {
            border-right: none;
            padding-right: 0;
            margin-right: 0;
        }
        .toolbar.auto-hide:hover {
            transform: translateX(-50%) translateY(0);
            background: rgba(15, 23, 42, 0.9);
            border-color: rgba(59, 130, 246, 0.5);
            box-shadow: 0 -10px 40px rgba(0, 0, 0, 0.5);
        }
        button {
            background: rgba(51, 65, 85, 0.3);
            color: rgba(241, 245, 249, 0.8);
            border: 1px solid rgba(71, 85, 105, 0.4);
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            transition: all 0.2s ease;
            display: flex;
            flex-direction: row !important;
            align-items: center;
            justify-content: center;
            gap: 8px;
            white-space: nowrap;
        }
        button:hover {
            background: #1e293b;
            border-color: #3b82f6;
            color: #f1f5f9;
            transform: translateY(-1px);
        }
        button.danger:hover {
            border-color: #ef4444 !important;
            background: rgba(239, 68, 68, 0.1) !important;
            color: #ef4444 !important;
        }
        .toolbar:hover button {
            background: rgba(51, 65, 85, 0.8);
            color: #f1f5f9;
        }
        .toolbar:hover button:hover {
            background: #1e293b;
        }
        button:active {
            transform: translateY(0);
        }
        button:disabled {
            background: rgba(51, 65, 85, 0.1) !important;
            color: rgba(148, 163, 184, 0.4) !important;
            border-color: rgba(51, 65, 85, 0.2) !important;
            cursor: not-allowed;
            transform: none !important;
        }
        button.active {
            background: #3b82f6 !important;
            color: #ffffff !important;
            border-color: #60a5fa !important;
            box-shadow: 0 0 10px rgba(59, 130, 246, 0.5);
        }
        button.audio-active {
            background: #10b981 !important;
            border-color: #34d399 !important;
            color: white !important;
            box-shadow: 0 0 15px rgba(16, 185, 129, 0.3);
        }
        #stats {
            display: flex;
            gap: 8px;
            align-items: center;
        }
        .stat-pill {
            display: flex;
            align-items: center;
            gap: 4px;
            background: rgba(30, 41, 59, 0.5);
            border: 1px solid rgba(71, 85, 105, 0.4);
            padding: 2px 10px;
            border-radius: 99px;
            color: #94a3b8;
            font-size: 10px;
            font-family: 'JetBrains Mono', 'Fira Code', monospace;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }
        .stat-label { opacity: 0.5; font-size: 9px; }
        .stat-value { 
            color: #f1f5f9; 
            font-weight: 700; 
            display: inline-block;
            min-width: 25px;
            text-align: center;
        }
        .powered-by {
            position: fixed;
            bottom: 12px;
            right: 16px;
            font-size: 11px;
            color: #64748b;
            font-family: 'JetBrains Mono', 'Fira Code', monospace;
            z-index: 50;
            display: flex;
            align-items: center;
            gap: 4px;
            opacity: 0.6;
            transition: opacity 0.2s ease;
            text-decoration: none;
        }
        .powered-by:hover {
            opacity: 1;
        }
        .powered-by span {
            color: #3b82f6;
            font-weight: 600;
        }
        .toolbar-link {
            text-decoration: none;
            display: flex;
            align-items: center;
            gap: 4px;
            font-size: 10px;
            font-family: 'JetBrains Mono', 'Fira Code', monospace;
            color: #94a3b8;
            opacity: 0.8;
            transition: all 0.2s ease;
            padding: 2px 8px;
            border-radius: 6px;
            margin-left: 4px;
        }
        .toolbar-link:hover {
            opacity: 1;
            background: rgba(51, 65, 85, 0.3);
            color: #f1f5f9;
        }
        .toolbar-link span {
            color: #3b82f6;
            font-weight: 700;
        }
    </style>
</head>
<body>
    <div id="container">
        <canvas id="screen" tabindex="0"></canvas>
        <div id="terminal-container"></div>
    </div>
    <div id="status">Connecting...</div>
    <div class="toolbar top">
        <div class="toolbar-group" style="display: none;" id="status-container">
        </div>
        <div class="toolbar-group" id="vnc-only-fkeys">
            <button id="btn-f1" onclick="sendCtrlAltF(1)" title="Ctrl+Alt+F1">Ctrl+Alt-F1</button>
            <button id="btn-f2" onclick="sendCtrlAltF(2)" title="Ctrl+Alt+F2">Ctrl+Alt-F2</button>
            <button id="btn-f3" onclick="sendCtrlAltF(3)" title="Ctrl+Alt+F3">Ctrl+Alt-F3</button>
            <button id="btn-f4" onclick="sendCtrlAltF(4)" title="Ctrl+Alt+F4">Ctrl+Alt-F4</button>
        </div>
        <div class="toolbar-group" style="border-right: none; padding-right: 0; margin-right: 0;">
            <div id="stats">
                <div class="stat-pill"><span class="stat-label">LAT</span><span id="lat-val" class="stat-value">0</span><span class="stat-label">MS</span></div>
                <div class="stat-pill"><span class="stat-label">FPS</span><span id="fps-val" class="stat-value">0</span></div>
                <div class="stat-pill"><span class="stat-label">BW</span><span id="bw-val" class="stat-value">0</span><span id="bw-unit" class="stat-label">KB/s</span></div>
            </div>
            <!-- Timestamp Toggle (Console Mode Only) -->
            <div id="timestamp-toggle" style="display: none; align-items: center; gap: 6px; margin-left: 10px;">
                <input type="checkbox" id="cb-timestamp" onchange="toggleTimestamp(this)" style="cursor: pointer;">
                <label for="cb-timestamp" style="font-size: 11px; cursor: pointer; user-select: none;">Timestamp</label>
            </div>
        </div>
        <div class="toolbar-group" style="border-right: none; padding-right: 0; margin-right: 0;">
            <a href="https://anyvm.org" target="_blank" class="toolbar-link">
                <span>AnyVM.org</span>
            </a>
        </div>
    </div>
    <div class="toolbar">
        <div class="toolbar-group" id="vnc-only-sticky">
            <button id="btn-sticky-shift" onclick="toggleSticky('ShiftLeft', 0xffe1, this)" title="Sticky Shift">Shift</button>
            <button id="btn-sticky-ctrl" onclick="toggleSticky('ControlLeft', 0xffe3, this)" title="Sticky Ctrl">Ctrl</button>
            <button id="btn-sticky-alt" onclick="toggleSticky('AltLeft', 0xffe9, this)" title="Sticky Alt">Alt</button>
            <button id="btn-sticky-meta" onclick="toggleSticky('MetaLeft', 0xffeb, this)" title="Sticky Meta">
                <span id="meta-btn-content">Opt</span>
            </button>
        </div>
        <div class="toolbar-group">
            <button onclick="sendCtrlAltDel()">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
                Ctrl + Alt + Del
            </button>
        </div>
        <div class="toolbar-group">
            <button onclick="pasteText()">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path><rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect></svg>
                Paste
            </button>
        </div>
        <div class="toolbar-group">
            <button id="btn-audio" onclick="toggleAudio()" title="Enable Audio">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5L6 9H2v6h4l5 4V5z"></path><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"></path></svg>
                Audio
            </button>
        </div>
        <div class="toolbar-group">
            <button onclick="rebootVM()" title="Reboot">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"></path><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg>
                Reboot
            </button>
            <button onclick="shutdownVM()" title="Shutdown (ACPI)">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"></path><line x1="12" y1="2" x2="12" y2="12"></line></svg>
                Shutdown
            </button>
            <button class="danger" onclick="forceShutdownVM()" title="Force Kill VM (Direct Quit)">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"></path></svg>
                Force Quit
            </button>
        </div>
        <button onclick="toggleFullscreen()">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"></path></svg>
        </button>
    </div>
    <a href="https://anyvm.org" target="_blank" class="powered-by">
        Powered by <span>AnyVM.org</span>
    </a>

<script>
var AUDIO_ENABLED = AUDIO_ENABLED || false;
const canvas = document.getElementById('screen');
const ctx = canvas.getContext('2d', { alpha: false });
// Force disable all smoothing for nearest-neighbor rendering
ctx.imageSmoothingEnabled = false;
ctx.mozImageSmoothingEnabled = false;
ctx.webkitImageSmoothingEnabled = false;
ctx.msImageSmoothingEnabled = false;
const status = document.getElementById('status');

let ws;
let connected = false;
let stickyStates = {};
let fbWidth = 800, fbHeight = 600;
let pendingUpdate = false;
let updateInterval = null;

// Global UI / Terminal State
let term = null;
let fitAddon = null;
let frameCount = 0;
let lastFpsTime = performance.now();
let lastLatency = 0;
let bytesReceived = 0;
let requestStartTime = 0;
let reconnectTimer = null;
let countdownInterval = null;
let isPasting = false;
let needsTimestamp = true;
let showTimestamp = false; 
let audioContext = null;
let isConnecting = true;
const decoder = new TextDecoder();

if (typeof IS_CONSOLE_VNC !== 'undefined' && IS_CONSOLE_VNC) {
    canvas.style.display = 'none';
    const termContainer = document.getElementById('terminal-container');
    const mainContainer = document.getElementById('container');
    
    termContainer.style.display = 'block';
    
    // Initial setup will be refined by handleResize()
    mainContainer.style.background = '#000';
    mainContainer.style.overflow = 'hidden';
    mainContainer.style.position = 'relative';
    
    // Layout is managed by handleResize and CSS classes
    
    // Show timestamp toggle in console mode and restore preference
    const tsToggle = document.getElementById('timestamp-toggle');
    const cbTimestamp = document.getElementById('cb-timestamp');
    if (tsToggle && typeof showTimestamp !== 'undefined') {
        tsToggle.style.display = 'flex';
        // Restore from localStorage
        const stored = localStorage.getItem('anyvm_show_timestamp');
        if (stored !== null) {
            showTimestamp = (stored === 'true');
            if (cbTimestamp) cbTimestamp.checked = showTimestamp;
        }
    }

    const vncSticky = document.getElementById('vnc-only-sticky');
    if (vncSticky) vncSticky.style.display = 'none';
    const vncFkeys = document.getElementById('vnc-only-fkeys');
    if (vncFkeys) vncFkeys.style.display = 'none';

    window.initTerminal = function() {
        if (term) return;
        try {
            if (typeof Terminal !== 'undefined') {
                term = new Terminal({
                    cursorBlink: true,
                    theme: {
                        background: '#000000',
                        foreground: '#f1f5f9',
                        cursor: '#3b82f6',
                        selectionBackground: 'rgba(59, 130, 246, 0.3)',
                        black: '#000000', // Ensure ANSI black matches terminal background
                        brightBlack: '#444444',
                    },
                    fontSize: 14,
                    fontFamily: "'JetBrains Mono', 'Fira Code', 'Courier New', monospace"
                });
                fitAddon = new FitAddon.FitAddon();
                term.loadAddon(fitAddon);
                term.open(termContainer);
                
                // Delay fit and focus to ensure DOM is ready and dimensions are accurate
                setTimeout(() => {
                    if (fitAddon) fitAddon.fit();
                    term.focus();
                }, 100);

                // Add resize listener for responsive layout
                window.addEventListener('resize', () => {
                    if (fitAddon) fitAddon.fit();
                });
                
                // Delay binding onData to prevent terminal response loops (like CPR)
                // when processing historical buffer on page load/refresh.
                term.onData(data => {
                    if (isConnecting) return;
                    if (ws && ws.readyState === WebSocket.OPEN) {
                        ws.send(new TextEncoder().encode(data));
                    }
                });
            } else {
                termContainer.innerHTML = '<div style="color: #f87171; padding: 20px; font-family: sans-serif;">Error: xterm.js not loaded. Serial output will appear as plain text below.</div><pre id="fallback-term" style="color: #f1f5f9; padding: 20px; white-space: pre-wrap; font-family: monospace; height: 100%; overflow: auto;"></pre>';
            }
        } catch (e) { console.error(e); }
    };
}


function toggleTimestamp(cb) {
    showTimestamp = cb.checked;
    localStorage.setItem('anyvm_show_timestamp', showTimestamp);
    location.reload();
}

function getTimeStr() {
    if (!showTimestamp) return "";
    const now = new Date();
    const f = (n) => n.toString().padStart(2, '0');
    return `[${f(now.getHours())}:${f(now.getMinutes())}:${f(now.getSeconds())}.${now.getMilliseconds().toString().padStart(3, '0')}] `;
}
let audioNextTime = 0;
let audioEnabled = false;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const fpsVal = document.getElementById('fps-val');
const latVal = document.getElementById('lat-val');
const statsDiv = document.getElementById('stats');

const RFB_VERSION = "RFB 003.008\\n";

function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/websockify`);
    ws.binaryType = 'arraybuffer';
    
    let state = 'version';
    let buffer = new Uint8Array(0);
    
    ws.onopen = () => {
        if (typeof IS_CONSOLE_VNC !== 'undefined' && IS_CONSOLE_VNC) {
            window.initTerminal();
            connected = true;
            isConnecting = true;
            setTimeout(() => { isConnecting = false; if (term) term.focus(); }, 1000);
        } else {
            canvas.focus();
        }
        if (!IS_CONSOLE_VNC && canvas.classList.contains('disconnected')) {
            location.reload();
            return;
        }
        status.textContent = IS_CONSOLE_VNC ? 'Connected to Serial' : 'Connected, negotiating...';
        status.classList.remove('error', 'reconnecting');
        const statusContainer = document.getElementById('status-container');
        if (statusContainer) {
            statusContainer.style.display = 'flex';
            statusContainer.appendChild(status);
        }
        canvas.classList.remove('disconnected');
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
        if (countdownInterval) {
            clearInterval(countdownInterval);
            countdownInterval = null;
        }
    };
    
    ws.onclose = () => {
        status.classList.add('reconnecting');
        document.body.appendChild(status);
        const statusContainer = document.getElementById('status-container');
        if (statusContainer) statusContainer.style.display = 'none';
        canvas.classList.add('disconnected');
        connected = false;
        if (updateInterval) clearInterval(updateInterval);
        
        let timeLeft = 5;
        const updateStatus = () => {
            status.innerHTML = `<span style="color: #3b82f6; font-weight: 600;">Disconnected</span><span style="font-size: 13px; color: #94a3b8; font-weight: 400;">Retrying in ${timeLeft}s...</span>`;
        };
        
        updateStatus();
        if (countdownInterval) clearInterval(countdownInterval);
        countdownInterval = setInterval(() => {
            timeLeft--;
            if (timeLeft < 0) {
                clearInterval(countdownInterval);
                countdownInterval = null;
            } else {
                updateStatus();
            }
        }, 1000);
        
        if (!reconnectTimer) {
            reconnectTimer = setTimeout(() => {
                reconnectTimer = null;
                connect();
            }, 5000);
        }
    };
    
    ws.onerror = (e) => {
        status.textContent = 'Connection error';
        status.className = 'error';
        status.classList.remove('connected');
    };
    
    ws.onmessage = (e) => {
        const data = new Uint8Array(e.data);
        bytesReceived += data.length;
        if (typeof IS_CONSOLE_VNC !== 'undefined' && IS_CONSOLE_VNC) {
            if (term) {
                try {
                    if (!showTimestamp) {
                        term.write(data);
                    } else {
                        const text = decoder.decode(data);
                        const parts = text.split(/(\\r?\\n)/);
                        
                        for (const part of parts) {
                            if (part === '\\n' || part === '\\r\\n') {
                                if (needsTimestamp) {
                                     term.write(getTimeStr());
                                     needsTimestamp = false;
                                }
                                term.write(part);
                                needsTimestamp = true;
                            } else if (part.length > 0) {
                                if (needsTimestamp) {
                                    term.write(getTimeStr() + part);
                                    needsTimestamp = false;
                                } else {
                                    term.write(part);
                                }
                            }
                        }
                    }
                } catch (e) {
                    console.error("Decoder error:", e);
                    term.write(data); // Fallback: write raw data if decode fails
                }
                term.scrollToBottom();
            } else {
                const fb = document.getElementById('fallback-term');
                if (fb) fb.textContent += new TextDecoder().decode(data);
            }
            return;
        }
        buffer = concatBuffers(buffer, data);
        processBuffer();
    };
    
    let bufCap = 1024 * 1024; // 1MB pre-allocated
    let bufStore = new Uint8Array(bufCap);
    let bufLen = 0;

    function concatBuffers(a, b) {
        const needed = bufLen + b.length;
        if (needed > bufCap) {
            bufCap = Math.max(bufCap * 2, needed);
            const newStore = new Uint8Array(bufCap);
            newStore.set(bufStore.subarray(0, bufLen));
            bufStore = newStore;
        }
        bufStore.set(b, bufLen);
        bufLen += b.length;
        buffer = bufStore.subarray(0, bufLen);
        return buffer;
    }

    function consume(n) {
        bufStore.copyWithin(0, n, bufLen);
        bufLen -= n;
        buffer = bufStore.subarray(0, bufLen);
        return buffer;
    }
    
    function processBuffer() {
        while (true) {
            if (state === 'version') {
                if (buffer.length >= 12) {
                    consume(12);
                    ws.send(new TextEncoder().encode(RFB_VERSION));
                    state = 'security';
                } else break;
            }
            else if (state === 'security') {
                if (buffer.length >= 1) {
                    const numTypes = buffer[0];
                    if (buffer.length >= 1 + numTypes) {
                        consume(1 + numTypes);
                        ws.send(new Uint8Array([1]));
                        state = 'security_result';
                    } else break;
                } else break;
            }
            else if (state === 'security_result') {
                if (buffer.length >= 4) {
                    const result = new DataView(consume(4).buffer).getUint32(0);
                    if (result === 0) {
                        ws.send(new Uint8Array([1]));
                        state = 'server_init';
                    } else {
                        status.textContent = 'Auth failed';
                        status.className = 'error';
                        return;
                    }
                } else break;
            }
            else if (state === 'server_init') {
                if (buffer.length >= 24) {
                    const view = new DataView(buffer.buffer, buffer.byteOffset);
                    fbWidth = view.getUint16(0);
                    fbHeight = view.getUint16(2);
                    const nameLen = view.getUint32(20);
                    
                    if (buffer.length >= 24 + nameLen) {
                        consume(24 + nameLen);
                        canvas.width = fbWidth;
                        canvas.height = fbHeight;
                        
                        const setPixelFormat = new Uint8Array([
                            0, 0, 0, 0,
                            32, 24, 0, 1,
                            0, 255, 0, 255, 0, 255,
                            16, 8, 0,
                            0, 0, 0
                        ]);
                        ws.send(setPixelFormat);
                        
                        const setEncodings = new Uint8Array([
                            2, 0,
                            0, 2,
                            0, 0, 0, 0,
                            255, 255, 255, 33 // DesktopSize pseudo-encoding (-223)
                        ]);
                        ws.send(setEncodings);
                        
                        connected = true;
                        pendingUpdate = false;
                        status.textContent = `Connected: ${fbWidth}x${fbHeight}`;
                        status.classList.add('connected');
                        state = 'normal';
                        
                        // Force disable smoothing after any resolution change
                        ctx.imageSmoothingEnabled = false;
                        ctx.webkitImageSmoothingEnabled = false;
                        
                        handleResize();

                        // Request updates as fast as possible
                        requestUpdate(false);
                    } else break;
                } else break;
            }
            else if (state === 'normal') {
                if (buffer.length < 1) break;
                const msgType = buffer[0];
                
                if (msgType === 0) {
                    if (buffer.length < 4) break;
                    const numRects = new DataView(buffer.buffer, buffer.byteOffset).getUint16(2);
                    let offset = 4;
                    let complete = true;
                    // Pipeline: request next frame immediately, don't wait for render
                    if (pendingUpdate) {
                        pendingUpdate = false;
                        requestUpdate(true);
                    }
                    
                    for (let i = 0; i < numRects; i++) {
                        if (buffer.length < offset + 12) { complete = false; break; }
                        const view = new DataView(buffer.buffer, buffer.byteOffset + offset);
                        const x = view.getUint16(0);
                        const y = view.getUint16(2);
                        const w = view.getUint16(4);
                        const h = view.getUint16(6);
                        const enc = view.getInt32(8);
                        offset += 12;

                        if (enc === -223) { // VM resolution changed
                            location.reload();
                            return;
                        }
                        
                        // Detect VM software cursor by looking for small updates near host mouse position
                        if (isCheckingCursor && !cursorDetected) {
                            if (performance.now() - checkStartTime > 120) {
                                isCheckingCursor = false;
                            } else {
                                const isSmall = w <= 64 && h <= 64;
                                const isNear = x < lastMouseX + 32 && x + w > lastMouseX - 32 &&
                                               y < lastMouseY + 32 && y + h > lastMouseY - 32;
                                if (isSmall && isNear) {
                                    cursorDetected = true;
                                    canvas.style.cursor = 'none';
                                }
                            }
                        }
                        
                        if (enc === 0) { // Raw
                            const pixelBytes = w * h * 4;
                            if (buffer.length < offset + pixelBytes) { complete = false; break; }
                            const pixels = buffer.slice(offset, offset + pixelBytes);
                            offset += pixelBytes;

                            const imgData = ctx.createImageData(w, h);
                            const src = pixels;
                            const dst = imgData.data;
                            for (let j = 0, len = w * h; j < len; j++) {
                                const si = j * 4;
                                const di = j * 4;
                                dst[di]     = src[si + 2];
                                dst[di + 1] = src[si + 1];
                                dst[di + 2] = src[si];
                                dst[di + 3] = 255;
                            }
                            ctx.putImageData(imgData, x, y);
                        } else if (enc === 1) { // CopyRect
                            if (buffer.length < offset + 4) { complete = false; break; }
                            const srcX = new DataView(buffer.buffer, buffer.byteOffset + offset).getUint16(0);
                            const srcY = new DataView(buffer.buffer, buffer.byteOffset + offset).getUint16(2);
                            offset += 4;
                            const imgData = ctx.getImageData(srcX, srcY, w, h);
                            ctx.putImageData(imgData, x, y);
                        } else if (enc === 5) { // Hextile
                            let htBg = [0,0,0,255], htFg = [255,255,255,255];
                            for (let ty = y; ty < y + h; ty += 16) {
                                for (let tx = x; tx < x + w; tx += 16) {
                                    const tw = Math.min(16, x + w - tx);
                                    const th = Math.min(16, y + h - ty);
                                    if (buffer.length < offset + 1) { complete = false; break; }
                                    const sub = buffer[offset++];
                                    if (sub & 1) { // Raw
                                        const rawBytes = tw * th * 4;
                                        if (buffer.length < offset + rawBytes) { complete = false; break; }
                                        const imgData = ctx.createImageData(tw, th);
                                        for (let p = 0; p < tw * th; p++) {
                                            const si = offset + p * 4;
                                            imgData.data[p*4]     = buffer[si+2];
                                            imgData.data[p*4+1]   = buffer[si+1];
                                            imgData.data[p*4+2]   = buffer[si];
                                            imgData.data[p*4+3]   = 255;
                                        }
                                        offset += rawBytes;
                                        ctx.putImageData(imgData, tx, ty);
                                        continue;
                                    }
                                    if (sub & 2) { // BackgroundSpecified
                                        if (buffer.length < offset + 4) { complete = false; break; }
                                        htBg = [buffer[offset+2], buffer[offset+1], buffer[offset], 255];
                                        offset += 4;
                                    }
                                    if (sub & 4) { // ForegroundSpecified
                                        if (buffer.length < offset + 4) { complete = false; break; }
                                        htFg = [buffer[offset+2], buffer[offset+1], buffer[offset], 255];
                                        offset += 4;
                                    }
                                    // Fill background using fillRect (fast path)
                                    ctx.fillStyle = `rgb(${htBg[0]},${htBg[1]},${htBg[2]})`;
                                    ctx.fillRect(tx, ty, tw, th);
                                    if (sub & 8) { // AnySubrects
                                        if (buffer.length < offset + 1) { complete = false; break; }
                                        const numSub = buffer[offset++];
                                        const subColored = !!(sub & 16);
                                        const subBytes = numSub * (subColored ? 6 : 2);
                                        if (buffer.length < offset + subBytes) { complete = false; break; }
                                        for (let s = 0; s < numSub; s++) {
                                            let sr, sg, sb;
                                            if (subColored) {
                                                sb = buffer[offset]; sg = buffer[offset+1]; sr = buffer[offset+2];
                                                offset += 4;
                                            } else {
                                                sr = htFg[0]; sg = htFg[1]; sb = htFg[2];
                                            }
                                            const xy = buffer[offset], wh = buffer[offset+1];
                                            offset += 2;
                                            const sx = (xy >> 4) & 0xF, sy = xy & 0xF;
                                            const sw = ((wh >> 4) & 0xF) + 1, sh = (wh & 0xF) + 1;
                                            ctx.fillStyle = `rgb(${sr},${sg},${sb})`;
                                            ctx.fillRect(tx + sx, ty + sy, sw, sh);
                                        }
                                    }
                                }
                                if (!complete) break;
                            }
                        }
                    }

                    if (complete) {
                        consume(offset);
                        frameCount++;
                        if (requestStartTime) {
                            lastLatency = Math.round(performance.now() - requestStartTime);
                            latVal.textContent = lastLatency;
                        }
                    } else break;
                }
                else if (msgType === 1) {
                    if (buffer.length < 6) break;
                    const numColors = new DataView(buffer.buffer, buffer.byteOffset).getUint16(4);
                    const totalLen = 6 + numColors * 6;
                    if (buffer.length < totalLen) break;
                    consume(totalLen);
                }
                else if (msgType === 2) {
                    consume(1);
                }
                else if (msgType === 3) {
                    if (buffer.length < 8) break;
                    const textLen = new DataView(buffer.buffer, buffer.byteOffset).getUint32(4);
                    if (buffer.length < 8 + textLen) break;
                    consume(8 + textLen);
                }
                else if (msgType === 255) {
                    if (buffer.length < 4) break;
                    const subType = buffer[1];
                    const operation = (buffer[2] << 8) | buffer[3];
                    if (subType === 1 && operation === 2) { // Audio Data
                        if (buffer.length < 8) break;
                        const len = new DataView(buffer.buffer, buffer.byteOffset).getUint32(4);
                        if (buffer.length < 8 + len) break;
                        const audioData = consume(8 + len).slice(8);
                        playAudio(audioData);
                    } else {
                        consume(4);
                    }
                }
                else {
                    consume(1);
                }
            }
            else break;
        }
    }

    function requestUpdate(incremental) {
        if (!connected) return;
        pendingUpdate = true;
        requestStartTime = performance.now();
        const req = new Uint8Array([
            3,
            incremental ? 1 : 0,
            0, 0, 0, 0,
            (fbWidth >> 8) & 0xff, fbWidth & 0xff,
            (fbHeight >> 8) & 0xff, fbHeight & 0xff
        ]);
        ws.send(req);
    }
    
    let lastMouseX = 0, lastMouseY = 0, lastButtons = 0;
    let isCheckingCursor = false, cursorDetected = false, checkStartTime = 0;
    
    canvas.addEventListener('mousemove', sendMouse);
    canvas.addEventListener('mouseenter', sendMouse);
    canvas.addEventListener('mousedown', (e) => {
        canvas.focus();
        sendMouse(e);
    });
    canvas.addEventListener('mouseup', sendMouse);
    canvas.addEventListener('contextmenu', e => e.preventDefault());
    
    function sendMouse(e) {
        if (!connected) return;
        
        if (e.type === 'mouseenter') {
            isCheckingCursor = true;
            checkStartTime = performance.now();
            cursorDetected = false;
            canvas.style.cursor = 'default';
        }
        
        e.preventDefault();
        
        const rect = canvas.getBoundingClientRect();
        const clientX = e.clientX - rect.left;
        const clientY = e.clientY - rect.top;

        // Robust mapping that works for both "width:auto" (no bars)
        // and "object-fit:contain" (bars in fullscreen).
        const canvasRatio = fbWidth / fbHeight;
        const containerRatio = rect.width / rect.height;
        
        let drawWidth, drawHeight, offsetX, offsetY;
        if (containerRatio > canvasRatio) {
            // Screen is wider than VM (black bars on sides)
            drawHeight = rect.height;
            drawWidth = drawHeight * canvasRatio;
            offsetX = (rect.width - drawWidth) / 2;
            offsetY = 0;
        } else {
            // Screen is taller than VM (black bars on top/bottom)
            drawWidth = rect.width;
            drawHeight = drawWidth / canvasRatio;
            offsetX = 0;
            offsetY = (rect.height - drawHeight) / 2;
        }

        const x = Math.floor((clientX - offsetX) * (fbWidth / drawWidth));
        const y = Math.floor((clientY - offsetY) * (fbHeight / drawHeight));
        
        const clampedX = Math.max(0, Math.min(fbWidth - 1, x));
        const clampedY = Math.max(0, Math.min(fbHeight - 1, y));
        
        let buttons = 0;
        if (e.buttons & 1) buttons |= 1;
        if (e.buttons & 2) buttons |= 4;
        if (e.buttons & 4) buttons |= 2;
        
        if (clampedX !== lastMouseX || clampedY !== lastMouseY || buttons !== lastButtons) {
            lastMouseX = clampedX;
            lastMouseY = clampedY;
            lastButtons = buttons;
            
            const msg = new Uint8Array([
                5, buttons,
                (clampedX >> 8) & 0xff, clampedX & 0xff,
                (clampedY >> 8) & 0xff, clampedY & 0xff
            ]);
            ws.send(msg);
        }
    }
    
    canvas.addEventListener('wheel', (e) => {
        if (!connected) return;
        e.preventDefault();
        
        const rect = canvas.getBoundingClientRect();
        const clientX = e.clientX - rect.left;
        const clientY = e.clientY - rect.top;

        const canvasRatio = fbWidth / fbHeight;
        const containerRatio = rect.width / rect.height;
        
        let drawWidth, drawHeight, offsetX, offsetY;
        if (containerRatio > canvasRatio) {
            drawHeight = rect.height;
            drawWidth = drawHeight * canvasRatio;
            offsetX = (rect.width - drawWidth) / 2;
            offsetY = 0;
        } else {
            drawWidth = rect.width;
            drawHeight = drawWidth / canvasRatio;
            offsetX = 0;
            offsetY = (rect.height - drawHeight) / 2;
        }

        const x = Math.floor((clientX - offsetX) * (fbWidth / drawWidth));
        const y = Math.floor((clientY - offsetY) * (fbHeight / drawHeight));
        const clampedX = Math.max(0, Math.min(fbWidth - 1, x));
        const clampedY = Math.max(0, Math.min(fbHeight - 1, y));
        
        const btn = e.deltaY < 0 ? 8 : 16;
        
        ws.send(new Uint8Array([5, btn, (clampedX >> 8) & 0xff, clampedX & 0xff, (clampedY >> 8) & 0xff, clampedY & 0xff]));
        ws.send(new Uint8Array([5, 0, (clampedX >> 8) & 0xff, clampedX & 0xff, (clampedY >> 8) & 0xff, clampedY & 0xff]));
    }, { passive: false });
}
    
document.addEventListener('keydown', e => sendKey(e, true));
document.addEventListener('keyup', e => sendKey(e, false));

// Track which keysym was sent for each physical code to ensure consistent keyup
const pressedKeysyms = {};

function sendKey(e, down) {
    if (!ws) return;
    if (typeof IS_CONSOLE_VNC !== 'undefined' && IS_CONSOLE_VNC) return;
    
    const code = e.code;
    const key = e.key;

    // Support Ctrl+V (Windows/Linux) or Cmd+V (Mac) for pasting
    if ((e.ctrlKey || e.metaKey) && (key === 'v' || key === 'V' || code === 'KeyV')) {
        return;
    }

    // Update active desktop button on Ctrl+Alt+Fx
    if (down && e.ctrlKey && e.altKey) {
        if (code === 'F1') setDesktopActive(1);
        else if (code === 'F2') setDesktopActive(2);
        else if (code === 'F3') setDesktopActive(3);
        else if (code === 'F4') setDesktopActive(4);
    }

    // If releasing a key that is sticky, keep it down in VNC
    if (!down && stickyStates[code]) {
        e.preventDefault();
        return;
    }

    const keyMap = {
        'Backspace': 0xff08, 'Tab': 0xff09, 'Enter': 0xff0d, 'Escape': 0xff1b, 'Delete': 0xffff,
        'Home': 0xff50, 'End': 0xff57, 'PageUp': 0xff55, 'PageDown': 0xff56,
        'ArrowLeft': 0xff51, 'ArrowUp': 0xff52, 'ArrowRight': 0xff53, 'ArrowDown': 0xff54, 'Insert': 0xff63,
        'F1': 0xffbe, 'F2': 0xffbf, 'F3': 0xffc0, 'F4': 0xffc1, 'F5': 0xffc2, 'F6': 0xffc3,
        'F7': 0xffc4, 'F8': 0xffc5, 'F9': 0xffc6, 'F10': 0xffc7, 'F11': 0xffc8, 'F12': 0xffc9,
        'ShiftLeft': 0xffe1, 'ShiftRight': 0xffe2, 'ControlLeft': 0xffe3, 'ControlRight': 0xffe4,
        'AltLeft': 0xffe9, 'AltRight': 0xffea, 'MetaLeft': 0xffeb, 'MetaRight': 0xffec, 'Space': 0x0020,
        'Shift': 0xffe1, 'Control': 0xffe3, 'Alt': 0xffe9, 'Meta': 0xffeb
    };

    let keysym = 0;
    if (down) {
        // Prioritize specific control keys
        if (keyMap[code]) {
            keysym = keyMap[code];
        } else if (keyMap[key]) {
            keysym = keyMap[key];
        } else if (key.length === 1) {
            let char = key;
            const softShift = stickyStates['ShiftLeft'] || stickyStates['ShiftRight'];
            if (softShift && !e.shiftKey) {
                // If software Shift is on but physical is not, escalate letters to uppercase.
                // This is necessary because VNC servers often interpret keysyms literally.
                if (char >= 'a' && char <= 'z') char = char.toUpperCase();
                else if (char >= 'A' && char <= 'Z') char = char.toLowerCase(); // Caps lock inverse? No, stick to shift logic.
            }
            
            keysym = char.charCodeAt(0);
            // If Ctrl or Alt is down, we want the base keysym (e.g. 'c' for Ctrl+C)
            if ((e.ctrlKey || e.altKey || e.metaKey) && keysym < 32) {
                if (keysym >= 1 && keysym <= 26) keysym += 96;
            }
        }
        
        if (keysym) {
            pressedKeysyms[code] = keysym;
        }
    } else {
        keysym = pressedKeysyms[code];
        delete pressedKeysyms[code];
        
        // Fallback for keyup if keydown was missed
        if (!keysym) {
            if (keyMap[code]) keysym = keyMap[code];
            else if (keyMap[key]) keysym = keyMap[key];
            else if (key.length === 1) keysym = key.charCodeAt(0);
        }
    }

    if (!keysym) return;

    e.preventDefault();
    
    try {
        ws.send(new Uint8Array([
            4, down ? 1 : 0, 0, 0,
            (keysym >> 24) & 0xff, (keysym >> 16) & 0xff, (keysym >> 8) & 0xff, keysym & 0xff
        ]));
    } catch (err) {
        console.error("Failed to send key:", err);
    }
}

function setDesktopActive(n) {
    for (let i = 1; i <= 4; i++) {
        const btn = document.getElementById('btn-f' + i);
        if (btn) {
            if (i === n) btn.classList.add('active');
            else btn.classList.remove('active');
        }
    }
}

function toggleSticky(code, keysym, btn) {
    if (!ws) return;
    stickyStates[code] = !stickyStates[code];
    if (stickyStates[code]) {
        btn.classList.add('active');
        ws.send(new Uint8Array([4, 1, 0, 0, (keysym>>24)&0xff, (keysym>>16)&0xff, (keysym>>8)&0xff, keysym&0xff]));
    } else {
        btn.classList.remove('active');
        ws.send(new Uint8Array([4, 0, 0, 0, (keysym>>24)&0xff, (keysym>>16)&0xff, (keysym>>8)&0xff, keysym&0xff]));
    }
}

function sendCtrlAltDel() {
    if (!connected || !ws) return;
    const keys = [0xffe3, 0xffe9, 0xffff];
    keys.forEach(k => {
        ws.send(new Uint8Array([4, 1, 0, 0, (k>>24)&0xff, (k>>16)&0xff, (k>>8)&0xff, k&0xff]));
    });
    keys.reverse().forEach(k => {
        ws.send(new Uint8Array([4, 0, 0, 0, (k>>24)&0xff, (k>>16)&0xff, (k>>8)&0xff, k&0xff]));
    });
}

function sendCtrlAltF(n) {
    if (!connected || !ws) return;
    setDesktopActive(n);
    const fKey = 0xffbe + (n - 1);
    const keys = [0xffe3, 0xffe9, fKey];
    keys.forEach(k => {
        ws.send(new Uint8Array([4, 1, 0, 0, (k>>24)&0xff, (k>>16)&0xff, (k>>8)&0xff, k&0xff]));
    });
    keys.reverse().forEach(k => {
        ws.send(new Uint8Array([4, 0, 0, 0, (k>>24)&0xff, (k>>16)&0xff, (k>>8)&0xff, k&0xff]));
    });
}

function toggleFullscreen() {
    if (document.fullscreenElement) {
        document.exitFullscreen();
    } else {
        if (typeof IS_CONSOLE_VNC !== 'undefined' && IS_CONSOLE_VNC) {
            document.getElementById('terminal-container').requestFullscreen();
        } else {
            canvas.requestFullscreen();
        }
    }
}

function updateToolbars() {
    const container = document.getElementById('container');
    if (!container) return;
    
    const rect = container.getBoundingClientRect();
    const threshold = 48; // Compact threshold for reserved space (100/2)

    document.querySelectorAll('.toolbar').forEach(tb => {
        const isTop = tb.classList.contains('top');
        const space = isTop ? rect.top : (window.innerHeight - rect.bottom);
        
        if (space < threshold) {
            tb.classList.add('auto-hide');
        } else {
            tb.classList.remove('auto-hide');
        }
    });
}

function handleResize() {
    if (typeof IS_CONSOLE_VNC !== 'undefined' && IS_CONSOLE_VNC) {
        const container = document.getElementById('container');
        if (container && !document.fullscreenElement) {
            const vw = window.innerWidth - 80;
            const vh = window.innerHeight - 160;
            const ratio = 1280 / 800;
            
            let w = 1280;
            let h = 800;
            
            if (w > vw) { w = vw; h = w / ratio; }
            if (h > vh) { h = vh; w = h * ratio; }
            
            container.style.width = Math.floor(w) + 'px';
            container.style.height = Math.floor(h) + 'px';
        } else if (container && document.fullscreenElement) {
            container.style.width = '100vw';
            container.style.height = '100vh';
        }
        
        // Use a small timeout to ensure absolute child dimensions are recalculated by the browser
        setTimeout(() => {
            if (fitAddon) fitAddon.fit();
            updateToolbars();
        }, 50);
        return;
    }
    
    // VNC Mode resize
    ctx.imageSmoothingEnabled = false;
    ctx.webkitImageSmoothingEnabled = false;
    ctx.mozImageSmoothingEnabled = false;

    let currentW = fbWidth;
    if (document.fullscreenElement === canvas) {
        const dpr = window.devicePixelRatio || 1;
        const physicalWidth = window.innerWidth * dpr;
        const physicalHeight = window.innerHeight * dpr;
        const scale = Math.max(1, Math.min(Math.floor(physicalWidth / fbWidth), Math.floor(physicalHeight / fbHeight)));
        currentW = (fbWidth * scale / dpr);
        canvas.style.width = currentW + "px";
        canvas.style.height = (fbHeight * scale / dpr) + "px";
    } else {
        // VNC Scaling logic: scale up if smaller than area, cap at 1280x800
        const vw = window.innerWidth - 60;
        const vh = window.innerHeight - 120;
        const maxW = 1280;
        const maxH = 800;

        const targetW = Math.min(vw, maxW);
        const targetH = Math.min(vh, maxH);

        const canvasRatio = fbWidth / fbHeight;
        const targetRatio = targetW / targetH;

        let w, h;
        if (targetRatio > canvasRatio) {
            h = targetH;
            w = h * canvasRatio;
        } else {
            w = targetW;
            h = w / canvasRatio;
        }

        currentW = Math.floor(w);
        canvas.style.width = currentW + "px";
        canvas.style.height = Math.floor(h) + "px";
    }

    if (connected && status) {
        const zoom = (currentW / fbWidth).toFixed(1);
        status.textContent = `Connected: ${fbWidth}X${fbHeight} (${zoom}X)`;
    }

    // Use ResizeObserver for reliability if not already set
    if (!window.toolbarObserver) {
        window.toolbarObserver = new ResizeObserver(() => {
            updateToolbars();
            setTimeout(updateToolbars, 100);
        });
        window.toolbarObserver.observe(document.body);
        window.toolbarObserver.observe(document.getElementById('container'));
    }
    
    updateToolbars();
}

document.addEventListener('fullscreenchange', handleResize);
window.addEventListener('resize', handleResize);

async function pasteText() {
    if (!ws || isPasting) return;
    if (!navigator.clipboard || !navigator.clipboard.readText) {
        alert('Clipboard API not available. Please use a secure connection (localhost or HTTPS).');
        return;
    }
    isPasting = true;
    try {
        const text = await navigator.clipboard.readText();
        await doPaste(text);
    } catch (err) {
        console.error('Failed to read clipboard:', err);
        alert('Could not read clipboard. Please ensure you have granted permission.');
    } finally {
        isPasting = false;
    }
}

async function doPaste(text) {
    if (!text || !ws) return;
    if (typeof IS_CONSOLE_VNC !== 'undefined' && IS_CONSOLE_VNC) {
        ws.send(new TextEncoder().encode(text));
        return;
    }
    // Release any existing modifiers first
    [0xffe1, 0xffe2, 0xffe3, 0xffe4, 0xffe9, 0xffea].forEach(k => {
        ws.send(new Uint8Array([4, 0, 0, 0, (k>>24)&0xff, (k>>16)&0xff, (k>>8)&0xff, k&0xff]));
    });
    
    for (let i = 0; i < text.length; i++) {
        let ch = text[i];
        let keysym = ch.charCodeAt(0);
        if (ch === '\\r') continue;
        if (ch === '\\n') keysym = 0xff0d;

        const shiftChars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ~!@#$%^&*()_+{}|:<>?"';
        const needsShift = shiftChars.indexOf(ch) !== -1;

        if (needsShift) {
            ws.send(new Uint8Array([4, 1, 0, 0, 0, 0, 0xff, 0xe1])); // Shift_L Down
            await sleep(2);
        }

        let msgDown = new Uint8Array([4, 1, 0, 0, (keysym>>24)&0xff, (keysym>>16)&0xff, (keysym>>8)&0xff, keysym&0xff]);
        let msgUp = new Uint8Array([4, 0, 0, 0, (keysym>>24)&0xff, (keysym>>16)&0xff, (keysym>>8)&0xff, keysym&0xff]);
        
        ws.send(msgDown);
        await sleep(5);
        ws.send(msgUp);

        if (needsShift) {
            await sleep(2);
            ws.send(new Uint8Array([4, 0, 0, 0, 0, 0, 0xff, 0xe1])); // Shift_L Up
        }
        await sleep(30);
    }
}

document.addEventListener('paste', async (e) => {
    // If we are in an input or textarea, let it be (unlikely in this app)
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData).getData('text');
    if (text && !isPasting) {
        isPasting = true;
        try {
            await doPaste(text);
        } finally {
            isPasting = false;
        }
    }
});

function toggleAudio() {
    if (!AUDIO_ENABLED) {
        alert('Audio is not supported by your QEMU installation.');
        return;
    }
    if (!audioContext) {
        audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 44100 });
        audioNextTime = audioContext.currentTime;
    }
    if (audioContext.state === 'suspended') {
        audioContext.resume();
    }
    
    audioEnabled = !audioEnabled;
    const btn = document.getElementById('btn-audio');
    if (audioEnabled) {
        btn.classList.add('audio-active');
        // Enable: [255, 1, 0, 0]
        ws.send(new Uint8Array([255, 1, 0, 0]));
    } else {
        btn.classList.remove('audio-active');
        // Disable: [255, 1, 0, 1]
        ws.send(new Uint8Array([255, 1, 0, 1]));
    }
}

function playAudio(data) {
    if (!audioContext || audioContext.state === 'suspended' || !audioEnabled) return;
    
    const samples = data.length / 4; // 16-bit stereo = 4 bytes/frame
    if (samples === 0) return;
    
    const buffer = audioContext.createBuffer(2, samples, 44100);
    const left = buffer.getChannelData(0);
    const right = buffer.getChannelData(1);
    const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
    
    for (let i = 0; i < samples; i++) {
        left[i] = view.getInt16(i * 4, true) / 32768.0;
        right[i] = view.getInt16(i * 4 + 2, true) / 32768.0;
    }
    
    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(audioContext.destination);
    
    const now = audioContext.currentTime;
    if (audioNextTime < now) {
        audioNextTime = now + 0.05;
    }
    source.start(audioNextTime);
    audioNextTime += buffer.duration;
}

function rebootVM() {
    if (!connected || !ws) return;
    if (confirm('Are you sure you want to reboot the VM?')) {
        // [255, 2, 1] for system_reset
        ws.send(new Uint8Array([255, 2, 1]));
    }
}

function shutdownVM() {
    if (!connected || !ws) return;
    if (confirm('Are you sure you want to send a ACPI shutdown signal to the VM?')) {
        // [255, 2, 2] for system_powerdown
        ws.send(new Uint8Array([255, 2, 2]));
    }
}

function forceShutdownVM() {
    if (!connected || !ws) return;
    if (confirm('DANGER: This will immediately KILL the VM process. Unsaved data will be lost. Continue?')) {
        // [255, 2, 3] for quit
        ws.send(new Uint8Array([255, 2, 3]));
    }
}

setInterval(() => {
    const now = performance.now();
    const dt = now - lastFpsTime;
    if (dt >= 500) {
        const fps = Math.round((frameCount * 1000) / dt);
        document.getElementById('fps-val').textContent = fps;
        const bps = (bytesReceived * 1000) / dt;
        let bwNum, bwUnit;
        if (bps >= 1048576) { bwNum = (bps / 1048576).toFixed(1); bwUnit = 'MB/s'; }
        else if (bps >= 1024) { bwNum = Math.round(bps / 1024); bwUnit = 'KB/s'; }
        else { bwNum = Math.round(bps); bwUnit = 'B/s'; }
        document.getElementById('bw-val').textContent = bwNum;
        document.getElementById('bw-unit').textContent = bwUnit;
        frameCount = 0;
        bytesReceived = 0;
        lastFpsTime = now;
    }
    // Statistics should only show when not in fullscreen
    if (document.fullscreenElement) {
        statsDiv.style.display = 'none';
    } else {
        statsDiv.style.display = 'flex';
    }
}, 500);

setDesktopActive(1);
// OS Detection for Meta Key Label
const metaContent = document.getElementById('meta-btn-content');
const isWin = navigator.userAgent.includes('Windows');
const isMac = navigator.userAgent.includes('Macintosh');

if (isWin) {
    metaContent.style.display = 'flex';
    metaContent.style.alignItems = 'center';
    metaContent.style.gap = '8px';
    metaContent.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 12h18M12 3v18"/></svg> <span>Win</span>';
    document.getElementById('btn-sticky-meta').title = "Sticky Windows Key";
} else if (isMac) {
    metaContent.style.display = 'flex';
    metaContent.style.alignItems = 'center';
    metaContent.style.gap = '8px';
    metaContent.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 3a3 3 0 0 0-3 3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3z"/></svg> <span>Mac</span>';
    document.getElementById('btn-sticky-meta').title = "Sticky Command Key";
}

if (!AUDIO_ENABLED) {
    const btn = document.getElementById('btn-audio');
    if (btn) {
        btn.disabled = true;
        btn.title = 'Audio not supported by QEMU installation';
        btn.innerHTML = btn.innerHTML.replace('Audio', 'Unsupported');
    }
}
connect();

// Focus management: ensure keyboard input always goes to terminal or canvas
document.addEventListener('mousedown', function(e) {
    const isInteractive = e.target.closest('input, label, button');
    
    if (typeof IS_CONSOLE_VNC !== 'undefined' && IS_CONSOLE_VNC && term) {
        // In serial mode, always refocus terminal unless clicking the checkbox
        if (!e.target.closest('#timestamp-toggle')) {
            setTimeout(() => term.focus(), 10);
        }
    } else {
        // In VNC mode
        if (e.target.id === 'screen') {
            canvas.focus();
        } else if (isInteractive) {
            // Briefly allow button/input interaction, then return focus to canvas
            setTimeout(() => {
                const active = document.activeElement;
                if (active && (active.tagName === 'BUTTON' || (active.tagName === 'INPUT' && active.id !== 'cb-timestamp'))) {
                    active.blur();
                    canvas.focus();
                }
            }, 100);
        }
    }
});

// Initialize toolbars and layout
handleResize();
</script>
</body>
</html>
"""

class VNCWebProxy:
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    
    def __init__(self, vnc_host, vnc_port, web_port, vm_info="", qemu_pid=None, audio_enabled=False, qmon_port=None, error_log_path=None, is_console_vnc=False, listen_addr='127.0.0.1', vnc_password="", tunnel_port=None):
        self.vnc_host = vnc_host
        self.vnc_port = vnc_port
        self.web_port = web_port
        self.vm_info = vm_info
        self.qemu_pid = qemu_pid
        self.audio_enabled = audio_enabled
        self.qmon_port = qmon_port
        self.error_log_path = error_log_path
        self.is_console_vnc = is_console_vnc
        self.listen_addr = listen_addr
        self.vnc_password = vnc_password
        # Dedicated 127.0.0.1 port reserved for the remote tunnel agent. Connections
        # arriving on this port always require the password — the IP-based bypass
        # is intentionally skipped, since the peer IP will be 127.0.0.1 (tunnel
        # process running locally) but the actual user is on the public internet.
        self.tunnel_port = tunnel_port
        self.clients = set()
        self.serial_buffer = collections.deque(maxlen=1024 * 100) # 100KB binary buffer (optimized for refresh speed)
        self.serial_writer = None
        self.stop_event = None # Initialized in run()
        self.kill_tunnels_func = None
    
    async def handle_client(self, reader, writer):
        try:
            sock = writer.get_extra_info('socket')
            if sock: sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer = writer.get_extra_info('peername')
            peer_ip = peer[0] if peer else None
            sockname = writer.get_extra_info('sockname')
            local_port = sockname[1] if sockname else None
            is_tunnel_socket = (self.tunnel_port is not None and local_port == self.tunnel_port)
            request = await reader.read(4096)
            if not request: return

            request_text = request.decode('utf-8', errors='ignore')
            lines = request_text.splitlines()
            if not lines: return

            parts = lines[0].split()
            if len(parts) < 2: return
            path = parts[1]

            headers = {}
            for line in lines[1:]:
                if ':' in line:
                    key, val = line.split(':', 1)
                    headers[key.strip().lower()] = val.strip()

            if self.vnc_password and (is_tunnel_socket or not self._is_trusted_client_ip(peer_ip)):
                auth_header = headers.get('authorization', '')
                is_auth_ok = self.check_auth(auth_header)
                if not is_auth_ok:
                    await self.request_auth(writer)
                    return

            if 'upgrade' in headers and headers.get('upgrade', '').lower() == 'websocket':
                await self.handle_websocket(reader, writer, headers)
            else:
                await self.handle_http(writer, path)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except:
                pass
    
    def _is_trusted_client_ip(self, peer_ip):
        # Skip VNC password prompt for clients on loopback, RFC1918 private
        # networks, link-local, or CGNAT (100.64.0.0/10, used by Tailscale etc.).
        if not peer_ip:
            return False
        try:
            addr = ipaddress.ip_address(peer_ip)
            if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
                addr = addr.ipv4_mapped
            if addr.is_loopback or addr.is_private or addr.is_link_local:
                return True
            if isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network('100.64.0.0/10'):
                return True
            return False
        except (ValueError, TypeError):
            return False

    def check_auth(self, auth_header):
        if not auth_header or not auth_header.lower().startswith('basic '):
            return False
        try:
            cred_part = auth_header.split(None, 1)[1]
            decoded = base64.b64decode(cred_part).decode('utf-8')
            pwd = decoded.split(':', 1)[1] if ':' in decoded else decoded
            return pwd == self.vnc_password
        except:
            return False

    async def request_auth(self, writer):
        body = b"401 Unauthorized"
        response = (
            "HTTP/1.1 401 Unauthorized\r\n"
            "WWW-Authenticate: Basic realm=\"AnyVM\"\r\n"
            "Content-Type: text/plain\r\n"
            "Content-Length: {}\r\n"
            "Connection: close\r\n"
            "\r\n".format(len(body))
        ).encode('utf-8') + body
        writer.write(response)
        await writer.drain()


    async def handle_http(self, writer, path):
        title = "AnyVM - VNC Viewer"
        if self.vm_info:
            title = "AnyVM - {} - VNC Viewer".format(self.vm_info)
        
        terminal_scripts = ""
        if self.is_console_vnc:
            terminal_scripts = """
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" />
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
"""
        html_content = VNC_WEB_HTML.replace("<title>AnyVM - VNC Viewer</title>", "<title>{}</title>".format(title))
        html_content = html_content.replace("<placeholder_scripts>", terminal_scripts)
        
        audio_status_js = "<script>var AUDIO_ENABLED = {}; var IS_CONSOLE_VNC = {};</script>".format(
            "true" if self.audio_enabled else "false",
            "true" if self.is_console_vnc else "false"
        )
        html_content = html_content.replace("<head>", "<head>" + audio_status_js)
        
        body = html_content.encode('utf-8')
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Content-Length: {}\r\n"
            "Cache-Control: no-cache, no-store, must-revalidate\r\n"
            "Pragma: no-cache\r\n"
            "Expires: 0\r\n"
            "Connection: close\r\n"
            "\r\n".format(len(body))
        ).encode('utf-8') + body
        writer.write(response)
        await writer.drain()

    async def handle_websocket(self, reader, writer, headers):
        key = headers.get('sec-websocket-key', '')
        accept = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest()).decode()
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: {}\r\n"
            "\r\n".format(accept)
        )
        writer.write(response.encode())
        await writer.drain()

        if self.is_console_vnc:
            # Track client queue for broadcasting
            client_queue = asyncio.Queue()
            self.clients.add(client_queue)
            
            # Send initial historical buffer
            if self.serial_buffer:
                await self.send_ws_frame(writer, b"".join(self.serial_buffer))
            
            async def ws_to_serial_bridge():
                try:
                    while True:
                        frame = await self.read_ws_frame(reader)
                        if frame is None: break
                        
                        if frame[0] == 255 and frame[1] == 2 and len(frame) >= 3:
                            operation = frame[2]
                            if self.qmon_port:
                                if operation == 1:
                                    cmd = "system_reset"
                                elif operation == 2:
                                    cmd = "system_powerdown"
                                elif operation == 3:
                                    cmd = "quit"
                                else:
                                    cmd = None
                                
                                if cmd:
                                    asyncio.create_task(self.send_monitor_command(cmd))
                            continue

                        if self.serial_writer:
                            self.serial_writer.write(frame)
                            await self.serial_writer.drain()
                except: pass
            
            async def serial_out_to_ws_bridge():
                try:
                    while True:
                        data = await client_queue.get()
                        await self.send_ws_frame(writer, data)
                except: pass
            
            try:
                await asyncio.gather(ws_to_serial_bridge(), serial_out_to_ws_bridge())
            finally:
                self.clients.remove(client_queue)
            return

        # Regular VNC logic below
        vnc_reader, vnc_writer = None, None
        for i in range(10):
            try:
                vnc_reader, vnc_writer = await asyncio.open_connection(self.vnc_host, self.vnc_port)
                sock = vnc_writer.get_extra_info('socket')
                if sock: sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                break
            except:
                if i == 9: return
                await asyncio.sleep(0.5)
        async def ws_to_vnc():
            try:
                while True:
                    frame = await self.read_ws_frame(reader)
                    if frame is None: break
                    
                    # Intercept custom control messages [255, 2, operation]
                    # 1: system_reset, 2: system_powerdown
                    if frame[0] == 255 and frame[1] == 2 and len(frame) >= 3:
                        operation = frame[2]
                        if self.qmon_port:
                            if operation == 1:
                                cmd = "system_reset"
                            elif operation == 2:
                                cmd = "system_powerdown"
                            elif operation == 3:
                                cmd = "quit"
                            else:
                                cmd = None
                            
                            if cmd:
                                asyncio.create_task(self.send_monitor_command(cmd))
                        continue

                    vnc_writer.write(frame)
                    await vnc_writer.drain()
            except: pass
            finally: vnc_writer.close()
        
        async def vnc_to_ws():
            try:
                while True:
                    data = await vnc_reader.read(65536)
                    if not data: break
                    await self.send_ws_frame(writer, data)
            except: pass
        
        await asyncio.gather(ws_to_vnc(), vnc_to_ws(), return_exceptions=True)
    
    async def read_ws_frame(self, reader):
        try:
            header = await reader.readexactly(2)
            opcode = header[0] & 0x0f
            if opcode == 0x8: return None
            masked = (header[1] & 0x80) != 0
            length = header[1] & 0x7f
            if length == 126:
                length = struct.unpack('>H', await reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack('>Q', await reader.readexactly(8))[0]
            
            if masked:
                mask = await reader.readexactly(4)
                data = bytearray(await reader.readexactly(length))
                # Vectorized XOR: process 4 bytes at a time using int32
                mask_int = int.from_bytes(mask, 'little')
                mv = memoryview(data)
                # Process aligned 4-byte chunks
                end4 = length & ~3
                for i in range(0, end4, 4):
                    v = int.from_bytes(mv[i:i+4], 'little') ^ mask_int
                    mv[i:i+4] = v.to_bytes(4, 'little')
                # Process remaining bytes
                for i in range(end4, length):
                    data[i] ^= mask[i & 3]
                return bytes(data)
            return await reader.readexactly(length)
        except: return None
    
    async def send_ws_frame(self, writer, data):
        try:
            length = len(data)
            if length <= 125: header = bytes([0x82, length])
            elif length <= 65535: header = bytes([0x82, 126]) + struct.pack('>H', length)
            else: header = bytes([0x82, 127]) + struct.pack('>Q', length)
            writer.write(header + data)
            # Only drain when write buffer is getting large
            if writer.transport.get_write_buffer_size() > 131072:
                await writer.drain()
        except:
            pass

    def monitor_qemu_thread(self, loop):
        """Thread-based monitor to ensure we catch QEMU exit even if loop is busy."""
        while True:
            time.sleep(1)
            if self.qemu_pid:
                if not self.is_pid_alive(self.qemu_pid):
                    log_msg = "[VNCProxy] QEMU (PID: {}) is no longer running. Exiting.".format(self.qemu_pid)
                    debuglog(True, log_msg)
                    if self.error_log_path:
                        try:
                            with open(self.error_log_path, 'a') as f:
                                t = time.time()
                                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".{:03d}".format(int(t % 1 * 1000))
                                f.write("[{}] {}\n".format(ts, log_msg))
                        except: pass
                    # Force exit the entire proxy process to ensure all threads and tunnels die
                    if self.kill_tunnels_func:
                        try: self.kill_tunnels_func()
                        except: pass
                    os._exit(0)
                    return

    def is_pid_alive(self, pid):
        try:
            if os.name == 'nt':
                import ctypes
                kernel32 = ctypes.windll.kernel32
                h_process = kernel32.OpenProcess(0x1000, False, pid)
                if h_process:
                    exit_code = ctypes.c_ulong()
                    kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
                    kernel32.CloseHandle(h_process)
                    return (exit_code.value == 259) # STILL_ACTIVE
                return False
            else:
                try:
                    os.kill(pid, 0)
                    # On Linux, a zombie process still satisfies os.kill(pid, 0)
                    # Check if it's a zombie if we can
                    if os.path.exists("/proc/{}/status".format(pid)):
                        with open("/proc/{}/status".format(pid), 'r') as f:
                            for line in f:
                                if line.startswith("State:"):
                                    if "Z (zombie)" in line:
                                        return False
                                    break
                    return True
                except OSError as e:
                    # EPERM (1) means process exists but we can't signal it.
                    # ESRCH (3) means process does not exist.
                    import errno
                    return e.errno == errno.EPERM
                except:
                    return False
        except:
            return False

    async def send_monitor_command(self, cmd):
        if not self.qmon_port:
            return
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', self.qmon_port)
            sock = writer.get_extra_info('socket')
            if sock: sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            writer.write((cmd + "\n").encode())
            await writer.drain()
            # Wait a bit for command to be processed
            await asyncio.sleep(0.1)
            writer.close()
            try:
                await writer.wait_closed()
            except:
                pass
        except Exception as e:
            # Re-implement log locally as we're in the proxy process
            t = time.time()
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".{:03d}".format(int(t % 1 * 1000))
            err_msg = "[{}] [VNCProxy] Failed to send monitor command '{}' to 127.0.0.1:{}: {}\n".format(ts, cmd, self.qmon_port, e)
            print(err_msg.strip())
            if self.error_log_path:
                try:
                    with open(self.error_log_path, 'a') as f:
                        f.write(err_msg)
                except:
                    pass
            pass
    async def serial_worker(self):
        """Persistent serial bridge to collect output and broadcast to clients."""
        while True:
            writer = None
            try:
                reader, writer = await asyncio.open_connection(self.vnc_host, self.vnc_port)
                self.serial_writer = writer
                if self.error_log_path:
                    try:
                        with open(self.error_log_path, 'a') as f:
                            f.write("[SerialWorker] Connected to QEMU serial port.\n")
                    except: pass
                
                while True:
                    data = await reader.read(65536)
                    if not data: break
                    self.serial_buffer.append(data)
                    # Broadcast to all connected clients
                    for q in list(self.clients):
                        try:
                            q.put_nowait(data)
                        except asyncio.QueueFull:
                            pass # Or handle accordingly
                
            except Exception:
                pass
            finally:
                self.serial_writer = None
                if writer:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except: pass
                await asyncio.sleep(1)

    async def run(self):
        self.stop_event = asyncio.Event()
        # Start serial worker if in console mode
        if self.is_console_vnc:
            asyncio.create_task(self.serial_worker())

        self.server = await asyncio.start_server(self.handle_client, self.listen_addr, self.web_port)
        self.tunnel_server = None
        if self.tunnel_port is not None:
            self.tunnel_server = await asyncio.start_server(self.handle_client, '127.0.0.1', self.tunnel_port)
        # Start the monitor in a background thread
        t = threading.Thread(target=self.monitor_qemu_thread, args=(asyncio.get_event_loop(),))
        t.daemon = True
        t.start()

        try:
            async with self.server:
                if self.tunnel_server is not None:
                    async with self.tunnel_server:
                        await self.stop_event.wait()
                else:
                    await self.stop_event.wait()
        except asyncio.CancelledError:
            pass

def strip_ansi(text):
    """Removes ANSI escape sequences from text."""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

def start_vnc_web_proxy(vnc_port, web_port, vm_info="", qemu_pid=None, audio_enabled=False, qmon_port=None, error_log_path=None, is_console_vnc=False, listen_addr='127.0.0.1', remote_vnc=False, debug=False, remote_vnc_link_file=None, vnc_password=""):
    # Handle termination signals for immediate cleanup
    def signal_handler(sig, frame):
        sys.exit(0)

    if platform.system() != "Windows":
        import signal
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    if error_log_path:
        try:
            remote_file = remote_vnc_link_file if remote_vnc_link_file else error_log_path.replace(".vncproxy.log", ".remote")
            if os.path.exists(remote_file):
                os.remove(remote_file)
            
            with open(error_log_path, 'w') as f:
                t = time.time()
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".{:03d}".format(int(t % 1 * 1000))
                remote_vnc_desc = "True" if remote_vnc == "1" else (remote_vnc if remote_vnc else "False")
                listen_display = ','.join(listen_addr) if isinstance(listen_addr, list) else listen_addr
                f.write("[{}] [VNCProxy] Proxy starting. VNC/Serial: {}, Web: {}, QEMU PID: {}, Monitor Port: {}, Console Mode: {}, Listen: {}, Remote VNC: {}\n".format(ts, vnc_port, web_port, qemu_pid, qmon_port, is_console_vnc, listen_display, remote_vnc_desc))
        except:
            pass
    
    tunnel_procs = [] # Track all started tunnel processes

    def kill_all_tunnels():
        for p in list(tunnel_procs):
            if p.poll() is None:
                try:
                    p.terminate()
                    try:
                        p.wait(timeout=2)
                    except:
                        p.kill()
                except: pass
            if p in tunnel_procs:
                tunnel_procs.remove(p)

    # Allocate a dedicated 127.0.0.1 port for the tunnel agent so that
    # tunnel-borne traffic can be distinguished from local browser traffic at
    # the TCP layer. Connections to this port always require the password
    # (see VNCWebProxy.handle_client). If allocation fails we refuse to start
    # the tunnel rather than fall back to web_port, which would silently
    # disable password protection for tunnel users.
    tunnel_port = None
    if remote_vnc:
        tunnel_port = get_free_port(start=16080, end=16180)
        if tunnel_port is None:
            if error_log_path:
                try:
                    with open(error_log_path, 'a') as f:
                        f.write("[VNCProxy] Failed to allocate tunnel_port (16080-16180 exhausted). Remote tunnel disabled to preserve password enforcement.\n")
                except: pass
            remote_vnc = False

    if remote_vnc:
        def tunnel_manager():
            # Attempt strategies in order
            data_dir = os.path.dirname(error_log_path) if error_log_path else os.getcwd()
            sys_name = platform.system().lower()
            machine = platform.machine().lower()

            # Method 1: Cloudflare
            cf_bin = "cloudflared" + (".exe" if sys_name == "windows" else "")
            cf_path = os.path.join(data_dir, cf_bin)
            
            # (Download logic omitted here for brevity, assuming it runs first or integrated)
            # Actually I should keep the download logic
            if not os.path.exists(cf_path):
                # ... download logic ...
                # (I will keep the existing download logic)
                pass

            strategies = [
                {
                    'name': 'Cloudflare',
                    'cmd': lambda: [cf_path, "tunnel", "--url", "http://127.0.0.1:{}".format(tunnel_port)] if os.path.exists(cf_path) else None,
                    'regex': r"https://[a-z0-9-]+\.trycloudflare\.com",
                    'msg': "Open this link to access WebVNC (via Cloudflare): {}"
                },
                {
                    'name': 'Localhost.run',
                    'cmd': lambda: ["ssh", "-F", "/dev/null" if sys_name != "windows" else "NUL", "-T", "-o", "StrictHostKeyChecking=no", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10", "-R", "80:localhost:{}".format(tunnel_port), "lx@localhost.run"],
                    'regex': r"https?://[a-z0-9.-]+\.lhr\.(?:life|proxy\.localhost\.run|localhost\.run)",
                    'msg': "Open this link to access WebVNC (via Localhost.run): {}"
                },
                {
                    'name': 'Pinggy',
                    'cmd': lambda: ["ssh", "-F", "/dev/null" if sys_name != "windows" else "NUL", "-T", "-o", "StrictHostKeyChecking=no", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10", "-p", "443", "-R", "80:localhost:{}".format(tunnel_port), "a.pinggy.io"],
                    'regex': r"https?://[a-z0-9.-]+\.pinggy\.link",
                    'msg': "Open this link to access WebVNC (via Pinggy): {}"
                },
                {
                    'name': 'Serveo',
                    'cmd': lambda: ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10", "-R", "80:localhost:{}".format(tunnel_port), "serveo.net"],
                    'regex': r"https?://[a-z0-9.-]+\.(?:serveo\.net|serveousercontent\.com)",
                    'msg': "Open this link to access WebVNC (via Serveo): {}"
                }
            ]

            # Filter strategies if a specific one is requested
            if remote_vnc not in [True, "1", "0", False]:
                req = str(remote_vnc).lower()
                if "cf" in req or "cloudflare" in req:
                    strategies = [s for s in strategies if s['name'] == 'Cloudflare']
                elif "localhost" in req or "lhr" in req:
                    strategies = [s for s in strategies if s['name'] == 'Localhost.run']
                elif "pinggy" in req:
                    strategies = [s for s in strategies if s['name'] == 'Pinggy']
                elif "serveo" in req:
                    strategies = [s for s in strategies if s['name'] == 'Serveo']

            for strat in strategies:
                cmd = strat['cmd']()
                if not cmd: continue
                
                log_msg = "Tunnel: Trying {}...".format(strat['name'])
                debuglog(debug, log_msg)
                if error_log_path:
                    try:
                        with open(error_log_path, 'a') as f:
                            f.write("[VNCProxy] " + log_msg + "\n")
                    except: pass
                
                # Pre-cleanup of the link file if it was specified
                if remote_vnc_link_file:
                    try:
                        if os.path.exists(remote_vnc_link_file):
                            os.remove(remote_vnc_link_file)
                    except: pass

                try:
                    kwargs = {
                        "stdin": subprocess.DEVNULL,
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.STDOUT
                    }
                    if sys_name == "windows":
                        kwargs["creationflags"] = 0x08000000 | 0x00000008
                    
                    p = subprocess.Popen(cmd, **kwargs)
                    tunnel_procs.append(p)
                    
                    found_url = [None]
                    
                    def monitor():
                        try:
                            while True:
                                line = p.stdout.readline()
                                if not line: break
                                text = line.decode('utf-8', errors='ignore')
                                clean_text = strip_ansi(text)
                                if error_log_path:
                                    with open(error_log_path, 'a') as f:
                                        f.write("[{}] {}".format(strat['name'], text))
                                
                                match = re.search(strat['regex'], clean_text)
                                if match:
                                    found_url[0] = match.group(0)
                                    msg = strat['msg'].format(found_url[0])
                                    if error_log_path:
                                        try:
                                            with open(error_log_path, 'a') as f:
                                                f.write("[VNCProxy] " + msg + "\n")
                                            # Write URL to .remote file
                                            remote_file = remote_vnc_link_file if remote_vnc_link_file else error_log_path.replace(".vncproxy.log", ".remote")
                                            with open(remote_file, 'w') as f:
                                                f.write(found_url[0] + "\n")
                                        except: pass
                                    debuglog(debug, "Tunnel: {} URL found: {}".format(strat['name'], found_url[0]))
                                    break
                                
                                if "429 Too Many Requests" in text:
                                    debuglog(debug, "Tunnel: {} failed with 429".format(strat['name']))
                                    break
                        except: pass

                    m_thread = threading.Thread(target=monitor)
                    m_thread.daemon = True
                    m_thread.start()
                    
                    # Wait for URL or process exit
                    start_wait = time.time()
                    while time.time() - start_wait < 30:
                        if found_url[0]:
                            # Keep process running and return
                            # Start a new monitor thread for the rest of the output
                            def follow_output(proc, name, log_path):
                                try:
                                    while True:
                                        line = proc.stdout.readline()
                                        if not line: break
                                        if log_path:
                                            with open(log_path, 'a') as f:
                                                f.write("[{}] {}".format(name, line.decode('utf-8', errors='ignore')))
                                except: pass
                            
                            f_thread = threading.Thread(target=follow_output, args=(p, strat['name'], error_log_path))
                            f_thread.daemon = True
                            f_thread.start()
                            return
                        
                        if p.poll() is not None:
                            debuglog(debug, "Tunnel: {} process exited early".format(strat['name']))
                            break
                        time.sleep(0.5)
                    
                    debuglog(debug, "Tunnel: {} failed, killing and trying next...".format(strat['name']))
                    if error_log_path:
                        try:
                            with open(error_log_path, 'a') as f:
                                f.write("[VNCProxy] Tunnel: {} failed or timed out. Trying next strategy...\n".format(strat['name']))
                        except: pass
                    try:
                        p.kill()
                        p.wait(timeout=1)
                    except: pass
                    if p in tunnel_procs: tunnel_procs.remove(p)
                    
                except Exception as e:
                    err_msg = "Tunnel: Strategy {} failed with error: {}".format(strat['name'], e)
                    debuglog(debug, err_msg)
                    if error_log_path:
                        try:
                            with open(error_log_path, 'a') as f:
                                f.write("[VNCProxy] " + err_msg + "\n")
                        except: pass

        def download_and_run_manager():
            # Integrated download logic
            data_dir = os.path.dirname(error_log_path) if error_log_path else os.getcwd()
            sys_name = platform.system().lower()
            machine = platform.machine().lower()
            cf_bin = "cloudflared" + (".exe" if sys_name == "windows" else "")
            cf_path = os.path.join(data_dir, cf_bin)
            
            if not os.path.exists(cf_path):
                debuglog(debug, "CF Tunnel: cloudflared not found, starting download...")
                arch_map = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
                cf_arch = arch_map.get(machine, "amd64")
                base_url = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
                download_url = ""
                if sys_name == "windows": download_url = base_url + "cloudflared-windows-{}.exe".format(cf_arch)
                elif sys_name == "linux": download_url = base_url + "cloudflared-linux-{}".format(cf_arch)
                elif sys_name == "darwin": download_url = base_url + "cloudflared-darwin-{}.tgz".format(cf_arch)
                
                if download_url:
                    debuglog(debug, "CF Tunnel: downloading from {}".format(download_url))
                    tmp_file = cf_path + ".tmp" + (".tgz" if sys_name == "darwin" else "")
                    if download_file(download_url, tmp_file, debug=True):
                        if sys_name == "darwin":
                            try:
                                subprocess.call(["tar", "-xzf", tmp_file, "-C", data_dir])
                                os.remove(tmp_file)
                            except: pass
                        else:
                            if hasattr(os, 'replace'): os.replace(tmp_file, cf_path)
                            else: shutil.move(tmp_file, cf_path)
                        if sys_name != "windows": os.chmod(cf_path, 0o755)
            
            tunnel_manager()

        t = threading.Thread(target=download_and_run_manager)
        t.daemon = True
        t.start()

    try:
        proxy = VNCWebProxy('127.0.0.1', vnc_port, web_port, vm_info, qemu_pid, audio_enabled, qmon_port, error_log_path, is_console_vnc, listen_addr=listen_addr, vnc_password=vnc_password, tunnel_port=tunnel_port)
        proxy.kill_tunnels_func = kill_all_tunnels
        asyncio.run(proxy.run())
    finally:
        debuglog(debug, "Tunnel: cleaning up all tunnel processes...")
        kill_all_tunnels()
        if error_log_path:
            try:
                remote_file = error_log_path.replace(".vncproxy.log", ".remote")
                if os.path.exists(remote_file):
                    os.remove(remote_file)
            except: pass
        debuglog(debug, "Tunnel: stopped")

def fatal(msg):
    print("Error: {}".format(msg), file=sys.stderr)
    sys.exit(1)

def print_usage():
    print("""
Usage: python qemu.py [OPTIONS]

Description:
  Automated QEMU VM launcher script. Downloads images/keys and boots a VM 
  with SSH access and folder synchronization.

Options:
  --os <name>            Operating System name (Required).
                         Supported: freebsd, midnightbsd, openbsd, netbsd, dragonflybsd, solaris, haiku
  --release <ver>        OS Release version (e.g., 15.0, 7.4). 
                         If invalid or omitted, tries to detect from available releases.
  --arch <arch>          Architecture: x86_64 or aarch64.
                         Default: Host architecture.
  --mem <MB>             Memory size in MB (Default: 2048).
  --cpu <num>            Number of CPU cores (Default: all host cores).
  --cpu-type <type>      Specific CPU model (e.g., cortex-a72, host).
  --nc <type>            Network card model (e.g., virtio-net-pci, e1000).
  --ssh-port <port>      Host port forwarding for SSH (Default: auto-detected free port).
  --ssh-name <name>      Add an extra SSH alias for the VM (e.g., ssh vmname).
                                                 When set, it will be added to the port-based alias entry.
  --host-ssh-port <port> Host SSH port reachable from the guest (Default: 22).
  --serial <port>        Expose the VM serial console on the given TCP port (auto-select starting 7000 if omitted).
  --enable-ipv6          Enable IPv6 in QEMU user networking (slirp). (Default: disabled)
  -p <mapping>           Custom port mapping. Can be used multiple times.
                         Formats: host:guest, tcp:host:guest, udp:host:guest.
                         Example: -p 8080:80 -p udp:3000:3000
  -v <mapping>           Folder synchronization. Can be used multiple times.
                         Format: /host/path:/guest/path
                         Example: -v /home/user/data:/mnt/data
  --sync <mode>          Synchronization mode for -v folders.
                         Supported: rsync (default), sshfs, nfs, scp, no/off (disable sync).
                         Note: sshfs/nfs not supported on Windows hosts; rsync requires rsync.exe.
  --data-dir <dir>       Directory to store images and metadata (Default: ./output).
  --cache-dir <dir>      Directory to cache extracted qcow2 files (avoids re-download and re-extract).
  --disktype <type>      Disk interface type (e.g., virtio, ide).
                         Default: virtio (ide for dragonflybsd).
  --uefi                 Enable UEFI boot (Implicit for FreeBSD).
  --vnc <display>        Enable VNC on specified display (e.g., 0 for :0). 
                         Default: enabled (display 0). Web UI starts at 6080 (increments if busy).
                         Use "--vnc off" to disable.
  --vnc-password <pwd>   Set a password for the VNC Web UI. A random 6-char password is generated if omitted.
  --remote-vnc           Create a public URL for the VNC Web UI using Cloudflare, Localhost.run, Pinggy, or Serveo.
                         Usage: --remote-vnc (auto), --remote-vnc cf, --remote-vnc lhr, --remote-vnc pinggy, --remote-vnc serveo.
                         Enabled by default if no local browser is detected (e.g., in Cloud Shell).
                         Use "--remote-vnc no" to disable.
  --remote-vnc-link-file Specify a file to write the remote VNC link to (instead of the default .remote file).
  --vga <type>           VGA device type (e.g., virtio, std, virtio-gpu). Default: virtio (std for NetBSD).
  --res, --resolution    Set initial screen resolution (e.g., 1280x800). Default: 1280x800.
  --mon <port>           QEMU monitor telnet port (localhost).
  --public               Listen on 0.0.0.0 for mapped ports instead of 127.0.0.1 + LAN IPs.
  --public-vnc           Listen on 0.0.0.0 for the VNC web interface instead of 127.0.0.1 + LAN IPs.
  --public-ssh           Listen on 0.0.0.0 for the SSH port instead of 127.0.0.1 + LAN IPs.
  --accept-vm-ssh        Authorize the VM's public key on the host (enables reverse SSH).
  --whpx                 (Windows) Attempt to use WHPX acceleration instead of TCG.
  --debug                Enable verbose debug logging.
  --detach, -d           Run QEMU in background.
  --console, -c          Run QEMU in foreground (console mode).
  --builder <ver>        Specify a specific vmactions builder version tag.
  --snapshot             Enable QEMU snapshot mode (changes are not saved).
  --boot-timeout-sec <n> Boot timeout in seconds before QEMU is killed and retried once.
                         Default: 600. Exceptions (only when this flag is not
                         explicitly passed): OpenBSD/aarch64 -> 1200; TCG mode
                         (no KVM/HVF/WHPX) -> 1800, since software emulation
                         is 10-50x slower than hardware acceleration.
  --enable-pmu           Expose the host PMU (performance counters) to the guest.
                         Disabled by default to avoid intermittent #GP-in-wrmsr
                         crashes seen on some host CPUs (DragonFlyBSD is the
                         most affected). Required if you want perf / pmcstat /
                         VTune to work inside the guest.
  --sync-time [off]      Synchronize VM time using NTP inside the guest after boot.
                         (Default: enabled for DragonFlyBSD/Solaris family, disabled otherwise).
  --                     Send all following args to the final ssh command (executes inside the VM).
  --help, -h             Show this help message.

Examples:
  # Basic FreeBSD VM
  python qemu.py --os freebsd --release 14.0

  # ARM64 VM on x86_64 host with port mapping and folder sync
  python qemu.py --os openbsd --arch aarch64 -p 8080:80 -v $(pwd):/data

  # Windows host using SCP sync
  python qemu.py --os solaris --sync scp -v D:\\data:/data

  # Run a command inside the VM (arguments after -- go to ssh)
  python qemu.py --os freebsd -- uname -a

""")

def get_private_ips():
    """Return a list of non-public IPv4 addresses on this machine (RFC1918, CGNAT/Tailscale, etc.)."""
    import ipaddress
    private_ips = []
    def _is_lan(addr_str):
        try:
            ip = ipaddress.ip_address(addr_str)
            if ip.version != 4:
                return False
            return not ip.is_global and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified and not ip.is_multicast and not ip.is_reserved
        except ValueError:
            return False
    def _add(addr):
        if _is_lan(addr) and addr not in private_ips:
            private_ips.append(addr)
    # Method 1: parse OS command output to enumerate all interface IPs (most reliable)
    try:
        if IS_WINDOWS:
            out = subprocess.check_output(["ipconfig"], stderr=subprocess.DEVNULL, timeout=5).decode('utf-8', errors='replace')
            for line in out.splitlines():
                line = line.strip()
                # Match "IPv4 Address" lines in any locale (look for ": x.x.x.x")
                if ':' in line:
                    part = line.split(':', 1)[1].strip()
                    try:
                        ipaddress.ip_address(part)
                        _add(part)
                    except ValueError:
                        pass
        else:
            out = subprocess.check_output(["ip", "-4", "-o", "addr", "show", "scope", "global"], stderr=subprocess.DEVNULL, timeout=5).decode('utf-8', errors='replace')
            for line in out.splitlines():
                # Format: "2: eth0    inet 192.168.1.100/24 brd ... scope global ..."
                parts = line.split()
                # Get interface name (field index 1, e.g. "eth0")
                iface = parts[1] if len(parts) > 1 else ""
                for j, tok in enumerate(parts):
                    if tok == "inet" and j + 1 < len(parts):
                        cidr = parts[j + 1]
                        prefix = cidr.split('/')[1] if '/' in cidr else '24'
                        if prefix == '32' and iface == 'lo':
                            continue  # skip /32 on loopback (e.g. WSL virtual addresses)
                        _add(cidr.split('/')[0])
    except Exception:
        pass
    # Method 2: getaddrinfo (fallback, may miss tun/tap interfaces)
    # Only used if Method 1 found nothing
    if not private_ips:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                _add(info[4][0])
        except socket.gaierror:
            pass
    return private_ips

def is_port_available(addr, port):
    """Check if a TCP port is available on a specific address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.bind((addr, port))
        return True
    except Exception:
        return False
    finally:
        try: s.close()
        except: pass

def get_free_port(start=10022, end=20000):
    """Return an available TCP port that works for both 0.0.0.0 and 127.0.0.1 binds."""
    probe_addrs = ("0.0.0.0", "127.0.0.1")
    for port in range(start, end):
        for addr in probe_addrs:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            try:
                s.bind((addr, port))
            except Exception:
                break
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        else:
            return port
    return None


def fetch_url_content(url, debug=False, headers=None):
    attempts = 20
    max_redirects = 5
    chrome_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    headers = headers or {}
    for attempt in range(attempts):
        current_url = url
        debuglog(debug, "fetch attempt {} for {}".format(attempt + 1, current_url))
        for _ in range(max_redirects):
            user_agents = [chrome_ua]
            for ua in user_agents:
                req = Request(current_url)
                req.add_header('User-Agent', ua)
                for hk, hv in headers.items():
                    req.add_header(hk, hv)
                try:
                    resp = urlopen(req)
                    try:
                        data = resp.read()
                    finally:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    if data:
                        debuglog(debug, "fetched {} bytes from {} with UA {}".format(len(data), current_url, ua))
                        return data.decode('utf-8')
                    debuglog(debug, "empty response from {} with UA {}; retrying".format(current_url, ua))
                    break  # empty body, retry outer loop
                except HTTPError as e:
                    if e.code in (301, 302, 303, 307, 308):
                        loc = e.headers.get('Location')
                        if loc:
                            debuglog(debug, "redirect {} -> {} (UA {})".format(current_url, loc, ua))
                            current_url = urljoin(current_url, loc)
                            break  # follow redirect with default UA list
                    if e.code == 404:
                        log("404: " + current_url)
                        return None
                    debuglog(debug, "HTTPError {} on {} with UA {}".format(e.code, current_url, ua))
                    continue  # try next UA
                except Exception as exc:
                    debuglog(debug, "Exception on {} with UA {}: {}".format(current_url, ua, exc))
                    continue  # try next UA
            else:
                # exhausted user agents
                break
            # if we hit a redirect, restart UA loop with new URL
            continue
        if attempt < attempts - 1:
            delay = random.uniform(1, 20)
            debuglog(debug, "retrying in {:.1f}s".format(delay))
            time.sleep(delay)
    debuglog(debug, "fetch failed for {}".format(url))
    return None

def get_remote_file_info(url, debug=False):
    req = Request(url)
    req.add_header('User-Agent', 'python-qemu-script')
    if hasattr(req, 'method'):
        req.method = 'HEAD'
    else:
        try:
            req.get_method = lambda: 'HEAD'
        except Exception:
            pass
    try:
        resp = urlopen(req)
        length = int(resp.headers.get('Content-Length', '0'))
        accept_ranges = resp.headers.get('Accept-Ranges', '').lower() == 'bytes'
        debuglog(debug, "HEAD {} -> length {}, accept_ranges {}".format(url, length, accept_ranges))
        try:
            resp.close()
        except Exception:
            pass
        return length, accept_ranges
    except Exception as exc:
        debuglog(debug, "HEAD failed for {}: {}".format(url, exc))
        return 0, False

def check_url_exists(url, debug=False):
    try:
        req = Request(url)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        u = urlopen(req, timeout=10)
        u.close()
        return True
    except Exception as e:
        debuglog(debug, "Check URL failed: {} - {}".format(url, str(e)))
        return False

def download_file_multithread(url, dest, total_size, show_progress, debug=False):
    tmp_dest = dest + ".part"
    try:
        with open(tmp_dest, 'wb') as f:
            f.truncate(total_size)
    except IOError:
        return False

    num_threads = min(4, max(1, total_size // (8 * 1024 * 1024)))
    chunk_size = (total_size + num_threads - 1) // num_threads
    progress_lock = threading.Lock()
    downloaded = [0]
    last_percent = [-1]
    errors = []
    stop_event = threading.Event()
    debuglog(debug, "multithread download: {} bytes, threads {}, chunk {}".format(total_size, num_threads, chunk_size))

    def update_progress():
        if not show_progress:
            return
        percent = int(downloaded[0] * 100 / total_size)
        if percent != last_percent[0]:
            last_percent[0] = percent
            sys.stdout.write("\r  {:3d}% ({:.1f}/{:.1f} MB)".format(
                percent,
                downloaded[0] / (1024 * 1024.0),
                total_size / (1024 * 1024.0)
            ))
            sys.stdout.flush()

    def worker(start, end):
        if stop_event.is_set():
            return
        req = Request(url)
        req.add_header('User-Agent', 'python-qemu-script')
        req.add_header('Range', 'bytes={}-{}'.format(start, end))
        try:
            resp = urlopen(req)
            with open(tmp_dest, 'r+b') as f:
                f.seek(start)
                while not stop_event.is_set():
                    chunk = resp.read(128 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    if show_progress:
                        with progress_lock:
                            downloaded[0] += len(chunk)
                            update_progress()
            try:
                resp.close()
            except Exception:
                pass
        except Exception as e:
            stop_event.set()
            with progress_lock:
                errors.append(e)
            debuglog(debug, "worker range {}-{} failed: {}".format(start, end, e))

    threads = []
    for index in range(num_threads):
        start = index * chunk_size
        end = min(total_size - 1, start + chunk_size - 1)
        if start > end:
            break
        t = threading.Thread(target=worker, args=(start, end))
        t.daemon = True
        t.start()
        threads.append(t)
        debuglog(debug, "started worker {} range {}-{}".format(index, start, end))

    for t in threads:
        t.join()
    debuglog(debug, "all workers finished")

    if show_progress:
        sys.stdout.write("\n")
        sys.stdout.flush()

    if errors or stop_event.is_set():
        try:
            os.remove(tmp_dest)
        except OSError:
            pass
        debuglog(debug, "multithread download failed; errors: {}".format(errors))
        return False

    try:
        if hasattr(os, 'replace'):
            os.replace(tmp_dest, dest)
        else:
            shutil.move(tmp_dest, dest)
    except Exception:
        return False
    debuglog(debug, "multithread download succeeded: {}".format(dest))
    return True

def download_file(url, dest, debug=False):
    log("Downloading " + url)
    show_progress = sys.stdout.isatty()

    size, can_range = get_remote_file_info(url, debug)
    if can_range and size > 0:
        debuglog(debug, "server supports range; size {}".format(size))
        if download_file_multithread(url, dest, size, show_progress, debug):
            return True
        log("Falling back to single-thread download...")
    else:
        debuglog(debug, "range not supported or size unknown (size {}, can_range {})".format(size, can_range))

    def make_progress_hook():
        if not show_progress:
            return None
        last_msg = {'percent': -1}

        def hook(block_num, block_size, total_size):
            downloaded = block_num * block_size
            if total_size > 0:
                percent = min(100, int(downloaded * 100 / total_size))
                if percent == last_msg['percent']:
                    return
                last_msg['percent'] = percent
                sys.stdout.write("\r  {:3d}% ({:.1f}/{:.1f} MB)".format(
                    percent,
                    downloaded / (1024 * 1024.0),
                    total_size / (1024 * 1024.0)
                ))
            else:
                sys.stdout.write("\r  {:.1f} MB".format(downloaded / (1024 * 1024.0)))
            sys.stdout.flush()

        return hook

    for i in range(5):
        hook = make_progress_hook()
        try:
            if hook:
                urlretrieve(url, dest, hook)
                sys.stdout.write("\n")
            else:
                urlretrieve(url, dest)
            return True
        except Exception as exc:
            debuglog(debug, "single-thread attempt {} failed: {}".format(i + 1, exc))
            if hook:
                sys.stdout.write("\n")
            time.sleep(2)
    return False


def url_exists(url):
    req = Request(url)
    req.add_header('User-Agent', 'python-qemu-script')
    if not hasattr(req, 'method'):
        try:
            req.get_method = lambda: 'HEAD'
        except Exception:
            pass
    else:
        req.method = 'HEAD'
    try:
        urlopen(req)
        return True
    except HTTPError as e:
        if e.code == 404:
            return False
        return False
    except URLError:
        return False


def download_optional_parts(base_url, base_path, max_parts=9, debug=False):
    for idx in range(1, max_parts + 1):
        part_url = "{}.{}".format(base_url, idx)
        if not url_exists(part_url):
            break
        log("Appending extra part: " + part_url)
        if not append_url_to_file(part_url, base_path, debug):
            log("Warning: Failed to append optional part {}".format(part_url))
            break


def append_url_to_file(url, dest_path, debug=False):
    req = Request(url)
    req.add_header('User-Agent', 'python-qemu-script')
    try:
        resp = urlopen(req)
    except Exception as exc:
        debuglog(debug, "failed to open {}: {}".format(url, exc))
        return False

    try:
        with open(dest_path, 'ab') as f_main:
            start_pos = f_main.tell()
            try:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f_main.write(chunk)
            except Exception as exc:
                debuglog(debug, "failed while appending {}: {}".format(url, exc))
                f_main.truncate(start_pos)
                return False
    finally:
        try:
            resp.close()
        except Exception:
            pass
    debuglog(debug, "appended {}".format(url))
    return True

def terminate_process(proc, name="process", grace_seconds=10):
    """Attempts to gracefully stop a subprocess before forcing termination."""
    if not proc or proc.poll() is not None:
        return
    log("Terminating qemu")
    log("Stopping {} (PID: {})".format(name, proc.pid))
    try:
        proc.terminate()
    except Exception:
        pass

    deadline = time.time() + max(0, grace_seconds)
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.2)

    if proc.poll() is None:
        log("{} did not exit gracefully; killing.".format(name))
        try:
            proc.kill()
        except Exception:
            pass

def tighten_windows_permissions(path):
    """Removes inherited ACLs on Windows to mimic chmod 600 semantics."""
    if not IS_WINDOWS:
        return
    try:
        subprocess.check_call(["icacls", path, "/inheritance:r"], stdout=DEVNULL, stderr=DEVNULL)
        user = os.environ.get("USERNAME")
        if not user:
            return
        domain = os.environ.get("USERDOMAIN")
        principal = "{}\\{}".format(domain, user) if domain else user
        subprocess.check_call(["icacls", path, "/grant:r", "{}:F".format(principal)], stdout=DEVNULL, stderr=DEVNULL)
    except Exception as exc:
        log("Warning: Failed to adjust ACLs for {}: {}".format(path, exc))

def call_with_timeout(cmd, timeout_seconds, **popen_kwargs):
    """Runs a subprocess with a hard timeout, returning (returncode, timed_out)."""
    proc = subprocess.Popen(cmd, **popen_kwargs)
    deadline = time.time() + max(0, timeout_seconds)
    while True:
        ret = proc.poll()
        if ret is not None:
            return ret, False
        if time.time() >= deadline:
            break
        time.sleep(0.1)

    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(1)
    except Exception:
        pass
    return None, True

# Slirp / DHCP defaults baked into the netdev_args string below.
# Keep these in sync if you ever change net=/dhcpstart= in the netdev string.
SLIRP_NETWORK_PREFIX = "192.168.122."
SLIRP_EXPECTED_GUEST_IP = SLIRP_NETWORK_PREFIX + "10"

def _qmon_send(monitor_port, command, timeout=2.0):
    """Send a single HMP command to the QEMU monitor TCP port, return the reply text or None.

    CRITICAL: do NOT send the HMP 'quit' command -- it terminates QEMU itself.
    We close the TCP socket from our side instead; the monitor server (started
    with server,nowait) keeps listening for new clients.
    """
    try:
        s = socket.create_connection(('127.0.0.1', int(monitor_port)), timeout=2.0)
    except (socket.error, OSError, ValueError):
        return None
    chunks = []
    try:
        s.settimeout(timeout)
        s.sendall((command + "\n").encode('utf-8'))
        # Read whatever the monitor emits until a brief idle period (timeout).
        while True:
            try:
                data = s.recv(4096)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass
    text = b''.join(chunks).decode('utf-8', errors='ignore')
    # QEMU monitor echoes input with readline control bytes; strip them.
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = text.replace('\b', '').replace('\r', '')
    return text

def get_vm_ip_from_monitor(monitor_port, network_prefix=SLIRP_NETWORK_PREFIX):
    """Detect the actual VM IP from slirp's connection table.

    Looks at lines from 'info usernet' where the source IP is in our guest subnet
    and the destination is external -- those are VM-originated outbound flows,
    and their source IP is the VM's real DHCP-assigned address.

    Returns None if no such traffic is visible yet (VM idle / not booted enough).
    """
    text = _qmon_send(monitor_port, 'info usernet')
    if not text:
        return None
    line_pattern = re.compile(
        r'^\s*\w+\[[^\]]+\]\s+\d+\s+(\S+)\s+\d+\s+(\S+)\s+\d+',
        re.MULTILINE,
    )
    reserved_last = {0, 1, 2, 3, 255}  # network/gateway/dns/broadcast in slirp's /24
    candidates = {}
    for m in line_pattern.finditer(text):
        src_ip, dst_ip = m.group(1), m.group(2)
        if src_ip.startswith(network_prefix) and not dst_ip.startswith(network_prefix):
            try:
                last = int(src_ip.rsplit('.', 1)[1])
            except ValueError:
                continue
            if last in reserved_last:
                continue
            candidates[src_ip] = candidates.get(src_ip, 0) + 1
    if not candidates:
        return None
    return max(candidates.items(), key=lambda kv: kv[1])[0]

def rewrite_hostfwd_target(monitor_port, hostfwd_specs, new_guest_ip, debug=False):
    """Rebind every hostfwd entry to point at new_guest_ip via the QEMU monitor.

    hostfwd_specs items are (proto, host_addr, host_port, guest_port) -- all strings.
    Returns True if every (remove, add) pair appeared to succeed.
    """
    if not hostfwd_specs:
        return False
    all_ok = True
    for proto, host_addr, host_port, guest_port in hostfwd_specs:
        remove_cmd = "hostfwd_remove {}:{}:{}".format(proto, host_addr, host_port)
        add_cmd = "hostfwd_add {}:{}:{}-{}:{}".format(proto, host_addr, host_port, new_guest_ip, guest_port)
        rem = _qmon_send(monitor_port, remove_cmd)
        add = _qmon_send(monitor_port, add_cmd)
        # QEMU monitor prints nothing on success; "Error" or "not found" on failure.
        rem_ok = bool(rem) and "Error" not in rem and "not found" not in rem.lower()
        add_ok = bool(add) and "Error" not in add and "could not" not in add.lower()
        if debug:
            debuglog(debug, "hostfwd rewrite {}:{}:{} -> {}:{}: remove={} add={}".format(
                proto, host_addr, host_port, new_guest_ip, guest_port,
                "ok" if rem_ok else "fail", "ok" if add_ok else "fail",
            ))
        if not (rem_ok and add_ok):
            all_ok = False
    return all_ok

def sync_vm_time(config, ssh_base_cmd):
    """Synchronizes VM time using NTP-like commands inside the guest."""
    guest_os = config.get('os', '').lower()
    debug = config.get('debug')
    
    def get_guest_time():
        try:
            # Try to get date with milliseconds
            cmd = "date '+%Y-%m-%d %H:%M:%S.%3N'"
            if guest_os in ['freebsd', 'midnightbsd', 'openbsd', 'netbsd', 'dragonflybsd', 'solaris', 'omnios', 'openindiana', 'haiku']:
                cmd = "date '+%Y-%m-%d %H:%M:%S.000'"
            
            p = subprocess.Popen(ssh_base_cmd + [cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, _ = p.communicate()
            if p.returncode == 0:
                return out.decode('utf-8', errors='replace').strip()
        except:
            pass
        return "unknown"

    def format_host_time(t):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".{:03d}".format(int((t % 1) * 1000))

    host_now = time.time()
    log("Host time:           {}".format(format_host_time(host_now)))

    time_before = get_guest_time()
    log("VM time before sync: {}".format(time_before))

    log("Syncing VM time for OS: {}".format(guest_os))
    # Construct NTP-like sync commands based on OS
    ntp_servers = "pool.ntp.org time.google.com"
    sync_cmd = ""
    
    if guest_os == 'openbsd':
        # OpenBSD uses rdate -n for SNTP sync
        major_ntp = ntp_servers.split()[0]
        sync_cmd = "rdate -n {0} || rdate {0}".format(major_ntp)
    elif guest_os == 'dragonflybsd':
        # DragonflyBSD specific: dntpd is the native daemon and was confirmed to work.
        sync_cmd = ("/usr/sbin/dntpd -s || dntpd -s || "
                    "/usr/sbin/ntpd -g -q || ntpd -g -q || /usr/sbin/ntpd -s || ntpd -s || "
                    "/usr/sbin/ntpdate -u {0} || /usr/bin/ntpdate -u {0} || "
                    "/usr/sbin/ntpdig -S {0} || /usr/bin/ntpdig -S {0} || "
                    "/usr/sbin/rdate time.nist.gov || /usr/bin/rdate time.nist.gov || rdate time.nist.gov").format(ntp_servers)
    elif guest_os in ['freebsd', 'netbsd']:
        # Try common BSD NTP tools with rdate fallback
        sync_cmd = "ntpdate -u {0} || ntpdig -S {0} || sntp -sS {0} || rdate pool.ntp.org || rdate time.nist.gov".format(ntp_servers)
    elif guest_os == 'midnightbsd':
        # MidnightBSD base system: ntpdate/ntpdig/sntp are not included by default.
        # Prefer rdate (base system, always works). Older MidnightBSD's ntpd lacks -q flag.
        sync_cmd = ("/usr/sbin/rdate -s time.nist.gov || /usr/sbin/rdate time.nist.gov || rdate time.nist.gov || "
                    "/usr/sbin/ntpd -q -g || ntpd -q -g || "
                    "/usr/local/sbin/ntpdate -u {0} || ntpdate -u {0} || "
                    "/usr/local/bin/ntpdig -S {0} || ntpdig -S {0} || "
                    "/usr/local/bin/sntp -sS {0} || sntp -sS {0}").format(ntp_servers)
    elif guest_os == 'omnios':
        # OmniOS: chrony is the preferred and often only functional tool.
        sync_cmd = "chronyc -a makestep || (svcadm enable chrony && sleep 2 && chronyc -a makestep)"
    elif guest_os == 'solaris':
        # Oracle Solaris: Typically has ntpdate in /usr/sbin
        sync_cmd = "ntpdate -u {0} || sntp -sS {0}".format(ntp_servers)
    elif guest_os == 'openindiana':
        # OpenIndiana: Use ntpdate for time sync.
        sync_cmd = "/usr/sbin/ntpdate -u {0} || /usr/bin/ntpdate -u {0} || ntpdate -u {0}".format(ntp_servers)
    elif guest_os == 'haiku':
        # Haiku uses Time --update to sync with configured NTP servers
        sync_cmd = "Time --update || ntpdate -u {0}".format(ntp_servers)
    else:
        # Linux default: try common tool chain
        sync_cmd = "ntpdate -u {0} || sntp -sS {0} || chronyc -a makestep || timeout 5 pulse-sync || (timedatectl set-ntp false && timedatectl set-ntp true)".format(ntp_servers)

    full_cmd = sync_cmd
    debuglog(debug, "Attempting NTP sync inside VM...")
    debuglog(debug, "NTP Sync Command: {}".format(full_cmd))
    
    try:
        # Increase timeout for NTP as network might be slow initially
        ret, timed_out = call_with_timeout(
            ssh_base_cmd + [full_cmd],
            timeout_seconds=15,
            stdout=None if debug else DEVNULL,
            stderr=None if debug else DEVNULL
        )
        log("NTP sync finished (ret={}, timeout={})".format(ret, timed_out))
    except Exception as e:
        log("NTP sync failed with exception: {}".format(e))
        pass

    time_after = get_guest_time()
    log("VM time after sync:  {}".format(time_after))
    log("Host time:           {}".format(format_host_time(time.time())))

def create_sized_file(path, size_mb):
    """Creates a zero-filled file of size_mb."""
    chunk_size = 1024 * 1024 # 1MB
    try:
        with open(path, 'wb') as f:
            zeros = b'\0' * chunk_size
            for _ in range(size_mb):
                f.write(zeros)
    except IOError as e:
        fatal("Failed to create file {}: {}".format(path, e))

def copy_content_to_file(src, dest):
    """Copies content from src to the beginning of dest (like dd conv=notrunc)."""
    try:
        with open(src, 'rb') as f_src:
            content = f_src.read()
        
        # Open dest in read-write binary mode to overwrite without truncating
        with open(dest, 'r+b') as f_dest:
            f_dest.write(content)
    except IOError as e:
        fatal("Failed to copy content from {} to {}: {}".format(src, dest, e))

def find_qemu(binary_name):
    """Finds QEMU binary in PATH or default Windows location."""
    path = None
    # Try shutil.which (Python 3.3+)
    if hasattr(shutil, 'which'):
        path = shutil.which(binary_name)
    else:
        # Python 2 fallback
        try:
            from distutils.spawn import find_executable
            path = find_executable(binary_name)
        except ImportError:
            pass
    
    if path:
        return path
        
    if IS_WINDOWS:
        # Try default install location
        # Handle standard Program Files
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidate = os.path.join(prog_files, "qemu", binary_name + ".exe")
        if os.path.exists(candidate):
            return candidate
            
        # Handle x86 Program Files (less likely for 64-bit qemu but possible)
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        candidate_x86 = os.path.join(prog_files_x86, "qemu", binary_name + ".exe")
        if os.path.exists(candidate_x86):
            return candidate_x86

        # Handle MSYS2 UCRT64 location
        msys_path = r"C:\msys64\ucrt64\bin"
        candidate_msys = os.path.join(msys_path, binary_name + ".exe")
        if os.path.exists(candidate_msys):
            return candidate_msys
            
    return None

def check_qemu_audio_backend(qemu_bin, backend_name):
    """Checks if the QEMU binary supports the specified audio backend."""
    try:
        proc = subprocess.Popen([qemu_bin, "-audiodev", "help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()
        output = (stdout.decode('utf-8', errors='ignore') + 
                  stderr.decode('utf-8', errors='ignore'))
        return backend_name in output
    except Exception:
        return False

def find_rsync():
    """Find rsync on host; returns absolute path or None."""
    path = None
    if hasattr(shutil, 'which'):
        path = shutil.which("rsync")
    if path:
        return path
    candidates = [
        r"C:\Program Files\Git\usr\bin\rsync.exe",
        r"C:\Program Files (x86)\Git\usr\bin\rsync.exe",
        r"C:\msys64\usr\bin\rsync.exe",
        r"C:\cygwin64\bin\rsync.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

def hvf_supported():
    """Returns True if macOS Hypervisor.framework (HVF) is available."""
    if platform.system() != "Darwin":
        return False
    try:
        out = subprocess.check_output(["sysctl", "-n", "kern.hv_support"], stderr=DEVNULL)
        return out.strip() == b"1"
    except Exception:
        return False

def sync_sshfs(ssh_cmd, vhost, vguest, os_name, os_release=None):
    """Mounts a host directory into the guest using SSHFS."""
    if IS_WINDOWS:
        log("Warning: SSHFS sync not supported on Windows host.")
        return

    # FreeBSD 13.2/14.0 images may not ship with sshfs; install on demand.
    needs_pkg_install = (os_name == "freebsd" and os_release in ("13.2", "14.0"))

    mount_script = """
{pre}
mkdir -p "{vguest}"
if [ "{os}" = "netbsd" ]; then
  if ! /usr/sbin/mount_psshfs host:"{vhost}" "{vguest}" >/dev/null 2>&1; then
    exit 1
  fi
else
  if [ "{os}" = "freebsd" ] || [ "{os}" = "midnightbsd" ]; then
    kldload fusefs >/dev/null 2>&1 || kldload fuse >/dev/null 2>&1 || true
  fi
  if sshfs -o reconnect,ServerAliveCountMax=2,allow_other,default_permissions host:"{vhost}" "{vguest}" ; then
    /sbin/mount >/dev/null 2>&1 || mount >/dev/null 2>&1
  else
    exit 1
  fi
fi
""".format(
                pre=("""
if command -v pkg >/dev/null 2>&1; then
    IGNORE_OSVERSION=yes pkg install -y fusefs-sshfs >/dev/null 2>&1 || true
fi
""" if needs_pkg_install else ""),
                vguest=vguest,
                vhost=vhost,
                os=os_name,
        )

    mounted = False
    for attempt in range(10):
        p_mount = subprocess.Popen(ssh_cmd + ["sh"], stdin=subprocess.PIPE)
        try:
            # Cap each attempt at 60s so a sshfs reconnect-loop inside the VM
            # (when host-side ssh keeps dropping the inner connection) does NOT
            # hang anyvm.py forever. sshfs runs with `-o reconnect`, so on a
            # flaky connect it will retry without ever returning to the shell.
            p_mount.communicate(input=mount_script.encode('utf-8'), timeout=60)
        except subprocess.TimeoutExpired:
            log("SSHFS mount attempt {} hung beyond 60s, killing".format(attempt + 1))
            p_mount.kill()
            try:
                p_mount.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if p_mount.returncode == 0:
            mounted = True
            break
        log("SSHFS mount failed (attempt {}), retrying...".format(attempt + 1))
        time.sleep(2)
    
    if not mounted:
        log("Warning: Failed to mount shared folder via sshfs.")

def sync_nfs(ssh_cmd, vhost, vguest, os_name, sudo_cmd):
    """Configures host NFS exports and mounts in guest."""
    if IS_WINDOWS:
        log("Warning: NFS sync not supported on Windows host.")
        return

    # Host side configuration
    uid = os.getuid()
    gid = os.getgid()
    entry_line = "{} *(rw,insecure,async,no_subtree_check,anonuid={},anongid={})".format(vhost, uid, gid)
    
    need_add = True
    try:
        if os.path.exists("/etc/exports"):
            with open("/etc/exports", "r") as f:
                content = f.read()
                # Check if the exact path is already exported
                # Simple string check might be flaky, but good enough for now
                if vhost + " " in content:
                    need_add = False
    except:
        pass

    if need_add:
        log("Configuring NFS export on host (requires sudo)...")
        debuglog(True, "Adding export: " + entry_line)
        if sudo_cmd or os.geteuid() == 0:
            subprocess.call(sudo_cmd + ["mkdir", "-p", "/run/sendsigs.omit.d/"])
            cmd_write = sudo_cmd + ["tee", "-a", "/etc/exports"]
            p_write = subprocess.Popen(cmd_write, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            p_write.communicate(input=(entry_line + "\n").encode('utf-8'))
            
            if p_write.returncode == 0:
                subprocess.call(sudo_cmd + ["exportfs", "-a"])
                if subprocess.call(sudo_cmd + ["service", "nfs-kernel-server", "restart"]) != 0:
                    if subprocess.call(sudo_cmd + ["service", "nfs-server", "restart"]) != 0:
                        subprocess.call(sudo_cmd + ["systemctl", "restart", "nfs-server"])
            else:
                log("Failed to write to /etc/exports")
        else:
            log("Warning: Cannot configure NFS without sudo/root.")

    # Guest side mounting
    mount_script = """
mkdir -p "{vguest}"
if [ "{os}" = "openbsd" ]; then
  mount -t nfs -o -T 192.168.122.2:"{vhost}" "{vguest}"
elif [ -e "/sbin/mount" ]; then
  /sbin/mount 192.168.122.2:"{vhost}" "{vguest}"
else
  mount 192.168.122.2:"{vhost}" "{vguest}"
fi
""".format(vguest=vguest, vhost=vhost, os=os_name)

    mounted = False
    for _ in range(10):
        p_mount = subprocess.Popen(ssh_cmd + ["sh"], stdin=subprocess.PIPE)
        p_mount.communicate(input=mount_script.encode('utf-8'))
        if p_mount.returncode == 0:
            mounted = True
            break
        log("NFS mount failed, retrying...")
        time.sleep(2)
    
    if not mounted:
        log("Warning: Failed to mount shared folder via NFS.")

def sync_rsync(ssh_cmd, vhost, vguest, os_name, output_dir, vm_name, excludes=None):
    """Syncs a host directory to the guest using rsync (Push mode)."""
    host_rsync = find_rsync()
    if not host_rsync:
        log("Warning: rsync not found on host. Install rsync to use rsync sync mode.")
        return

    # 1. Ensure destination directory exists in guest
    try:
        # Use a simpler check for directory existence and creation
        p = subprocess.Popen(ssh_cmd + ["mkdir -p \"{}\"".format(vguest)], stdout=DEVNULL, stderr=DEVNULL)
        p.wait(timeout=10)
    except Exception:
        pass

    log("Syncing via rsync: {} -> {}".format(vhost, vguest))
    
    if not ssh_cmd or len(ssh_cmd) < 2:
        return

    # Extract destination from ssh_cmd (last element)
    remote_host = ssh_cmd[-1]
    
    # On Windows, rsync -e commands are often executed by a mini-sh (part of msys2/git-bash).
    # Using /dev/null is often safer than NUL in that context.
    # We find the identity file path and port from the original ssh_cmd
    ssh_port = "22"
    id_file = None
    i = 0
    while i < len(ssh_cmd):
        if ssh_cmd[i] == "-p" and i + 1 < len(ssh_cmd):
            ssh_port = ssh_cmd[i+1]
        elif ssh_cmd[i] == "-i" and i + 1 < len(ssh_cmd):
            id_file = ssh_cmd[i+1].replace("\\", "/")
        i += 1

    # 0. Manage known_hosts file in output_dir
    kh_path = os.path.join(output_dir, "{}.knownhosts".format(vm_name))
    try:
        # Clear or create the file
        open(kh_path, 'w').close()
    except Exception:
        pass

    # Find absolute path to ssh, prioritizing bundled tools
    ssh_cmd_base = "ssh"
    if IS_WINDOWS and host_rsync:
        rsync_dir = os.path.dirname(os.path.abspath(host_rsync))
        # Search relative to rsync executable: 
        # C:\ProgramData\chocolatey\bin\rsync.exe -> ../lib/rsync/tools/bin/ssh.exe
        search_dirs = [
            rsync_dir, 
            os.path.join(rsync_dir, "..", "tools", "bin"),
            os.path.join(rsync_dir, "..", "lib", "rsync", "tools", "bin"),
            os.path.join(rsync_dir, "tools", "bin")
        ]
        for d in search_dirs:
            candidate = os.path.join(d, "ssh.exe")
            if os.path.exists(candidate):
                # Use normalized Windows path with forward slashes. 
                # This is more compatible than /c/ style on various Windows rsync ports.
                clean_path = os.path.normpath(candidate).replace("\\", "/")
                ssh_cmd_base = '"{}"'.format(clean_path)
                debuglog(True, "Using bundled SSH for rsync: {}".format(clean_path))
                break
    
    if ssh_cmd_base == "ssh":
        debuglog(True, "Using system 'ssh' command for rsync.")

    # Helper for path fields inside the -e string. 
    # Must be absolute for Windows SSH but handle separators correctly.
    # Build a minimal, robust SSH string for rsync -e
    # On Windows, within the rsync -e command string, we use Drive:/Path/Style
    # but wrap them in quotes if they contain spaces or colons.
    def to_ssh_path(p):
        if IS_WINDOWS:
            return os.path.abspath(p).replace("\\", "/")
        return p

    # Build a minimal, robust SSH string for rsync -e
    # -T: Disable pseudo-terminal, -q: quiet, -o BatchMode=yes: no password prompt
    ssh_parts = [
        ssh_cmd_base,
        "-T", "-q",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=\"{}\"".format(to_ssh_path(kh_path)),
        "-p", ssh_port
    ]
    if id_file:
        ssh_parts.extend(["-i", "\"{}\"".format(to_ssh_path(id_file))])
    
    ssh_opts_str = " ".join(ssh_parts)
    
    # Normalize source path for rsync to avoid "double remote" error on Windows.
    # We use a RELATIVE path here because relative paths don't have colons,
    # thus preventing rsync from mistaking the drive letter for a remote hostname.
    if IS_WINDOWS:
        try:
            # Try to get relative path from current working directory
            src = os.path.relpath(vhost).replace("\\", "/")
        except ValueError:
            # Cross-drive case: we use absolute path with forward slashes. 
            # Note: Native Windows rsync might still struggle here if it sees a colon.
            src = to_ssh_path(vhost)
    else:
        src = vhost

    if os.path.isdir(vhost) and not src.endswith('/'):
        src += "/"
    
    # Build rsync command
    # -a: archive, -v: verbose, -r: recursive, -t: times, -o: owner, -p: perms, -g: group, -L: follow symlinks
    # --blocking-io: Essential for Windows SSH pipes.
    cmd = [host_rsync, "-avrtopg", "-L", "--blocking-io", "--delete", "-e", ssh_opts_str]
    
    # Specify remote rsync path as it might not be in default non-interactive PATH.
    # These MUST come before the source/destination arguments.
    if os_name in ("freebsd", "midnightbsd"):
        cmd.extend(["--rsync-path", "/usr/local/bin/rsync"])
    elif os_name in ["openindiana", "solaris", "omnios"]:
        cmd.extend(["--rsync-path", "/usr/bin/rsync"])
        
    if excludes:
        for ex in excludes:
            cmd.extend(["--exclude", ex.replace("\\", "/")])
        
    # Source and Destination come last
    cmd.extend([src, "{}:{}".format(remote_host, vguest)])
    
    debuglog(True, "Full rsync command: {}".format(" ".join(cmd)))
    
    synced = False
    # Attempt sync with retries
    for i in range(10):
        try:
            # On Windows, Popen with explicit wait works best for rsync child processes
            p = subprocess.Popen(cmd)
            p.wait()
            if p.returncode == 0:
                synced = True
                break
        except Exception as e:
            debuglog(True, "Rsync execution error: {}".format(e))
            
        log("Rsync sync failed, retrying ({})...".format(i+1))
        time.sleep(2)
    
    if not synced:
        log("Warning: Failed to sync shared folder via rsync.")

def sync_scp(ssh_cmd, vhost, vguest, sshport, hostid_file, ssh_user, excludes=None):
    """Syncs via scp (Push mode from host to guest)."""
    log("Syncing via scp: {} -> {}".format(vhost, vguest))
    
    # Ensure destination directory exists in guest
    try:
        # ssh_cmd is like ['ssh', ..., '<user>@localhost']
        # We append mkdir command
        subprocess.call(ssh_cmd + ["mkdir", "-p", vguest])
    except Exception:
        pass

    if not os.path.exists(vhost):
        log("Warning: Host path {} does not exist; skipping.".format(vhost))
        return

    if os.path.isdir(vhost):
        try:
            entries = os.listdir(vhost)
            if excludes:
                entries = [e for e in entries if e not in excludes]
        except OSError as exc:
            log("Warning: Failed to read {}: {}".format(vhost, exc))
            return
        if not entries:
            log("Host dir {} is empty; nothing to sync.".format(vhost))
            return
        sources = [os.path.join(vhost, entry) for entry in entries]
    else:
        sources = [vhost]

    # SCP command to push files
    # We use a retry loop because initial connections might be flaky on some OSs.
    synced = False
    for i in range(5):
        # Added -O option for legacy protocol support as a fallback if needed
        # but try modern SFTP-based protocol first on some attempts.
        mode_desc = "Standard (SFTP)"
        cmd = [
            "scp", "-r", "-q",
            "-P", str(sshport),
        ]
        if i % 2 == 1:
            cmd.append("-O")
            mode_desc = "Legacy (SCP)"
            
        if hostid_file:
            cmd.extend(["-i", hostid_file])
            
        cmd.extend([
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile={}".format(SSH_KNOWN_HOSTS_NULL),
            "-o", "LogLevel=ERROR",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
        ] + sources + ["{}@127.0.0.1:".format(ssh_user) + vguest + "/"])
        
        debuglog(True, "SCP Attempt {} ({}): Executing sync...".format(i + 1, mode_desc))
        try:
            ret = subprocess.call(cmd)
            if ret == 0:
                debuglog(True, "SCP Attempt {} successful.".format(i + 1))
                synced = True
                break
            else:
                debuglog(True, "SCP Attempt {} failed with return code {}.".format(i + 1, ret))
        except Exception as e:
            debuglog(True, "SCP Attempt {} encounterd exception: {}".format(i + 1, e))
        
        time.sleep(2)
        
    if not synced:
        log("Warning: SCP sync failed.")

def version_tokens(text):
    if not text:
        return []
    tokens = []
    for token in VERSION_TOKEN_RE.findall(text):
        if token.isdigit():
            tokens.append((0, int(token)))
        else:
            tokens.append((1, token.lower()))
    return tokens


def cmp_version(a, b):
    parts_a = version_tokens(a)
    parts_b = version_tokens(b)
    max_len = max(len(parts_a), len(parts_b))
    parts_a += [(0, 0)] * (max_len - len(parts_a))
    parts_b += [(0, 0)] * (max_len - len(parts_b))
    if parts_a > parts_b:
        return 1
    if parts_a < parts_b:
        return -1
    return 0

def tail_serial_log(path, stop_event):
    # Wait for file creation
    start_wait = time.time()
    while not os.path.exists(path):
        if stop_event.is_set() or (time.time() - start_wait > 10):
            return
        time.sleep(0.1)
        
    try:
        with open(path, 'r') as f:
            while not stop_event.is_set():
                data = f.read()
                if data:
                    sys.stdout.write(data)
                    sys.stdout.flush()
                else:
                    time.sleep(0.1)
    except Exception:
        pass


def _dump_boot_debug_snapshot(config, label, serial_log_file, qmon_port, output_dir, vm_name, proc, cmd_list=None):
    """Dump diagnostic info on a boot-wait timeout. All output via debuglog so it only
    fires under --debug. Useful for triaging intermittent CI boot failures.
    """
    debug = config.get('debug')
    debuglog(debug, "===== boot-debug snapshot [{}] begin =====".format(label))

    # QEMU process status
    try:
        rc = proc.poll()
        debuglog(debug, "QEMU PID {} poll={} (None means still running)".format(proc.pid, rc))
    except Exception as e:
        debuglog(debug, "QEMU proc inspect failed: {}".format(e))

    # QEMU full launch command line -- the exact args we passed
    if cmd_list:
        try:
            debuglog(debug, "QEMU cmd_list ({} args):\n  {}".format(
                len(cmd_list), " \\\n  ".join(shlex.quote(str(a)) for a in cmd_list)))
        except Exception:
            try:
                debuglog(debug, "QEMU cmd_list ({} args):\n  {}".format(len(cmd_list), " ".join(str(a) for a in cmd_list)))
            except Exception as e:
                debuglog(debug, "cmd_list dump failed: {}".format(e))

    # Host-side process info: is QEMU using CPU? memory? how long alive?
    try:
        if IS_WINDOWS:
            ps_cmd = ["tasklist", "/FI", "PID eq {}".format(proc.pid), "/V", "/FO", "LIST"]
        else:
            ps_cmd = ["ps", "-p", str(proc.pid), "-o", "pid,ppid,stat,pcpu,pmem,rss,vsz,etime,command"]
        ps_out = subprocess.check_output(ps_cmd, stderr=subprocess.STDOUT, timeout=5).decode('utf-8', errors='replace')
        debuglog(debug, "host ps for QEMU PID {}:\n{}".format(proc.pid, ps_out.rstrip()))
    except Exception as e:
        debuglog(debug, "host ps for QEMU failed: {}".format(e))

    # On Linux, /proc/<pid>/status has rich per-process info
    if not IS_WINDOWS:
        try:
            status_path = "/proc/{}/status".format(proc.pid)
            if os.path.exists(status_path):
                with open(status_path) as f:
                    status_lines = f.read().splitlines()
                wanted = ("State", "Threads", "VmSize", "VmRSS", "VmData", "VmPeak",
                          "voluntary_ctxt_switches", "nonvoluntary_ctxt_switches")
                picked = [ln for ln in status_lines if ln.split(":")[0] in wanted]
                if picked:
                    debuglog(debug, "{}:\n{}".format(status_path, "\n".join(picked)))
        except Exception as e:
            debuglog(debug, "/proc status read failed: {}".format(e))

    # Tail of serial console log (kernel + init messages, panic traces, etc.)
    try:
        if serial_log_file and os.path.exists(serial_log_file):
            size = os.path.getsize(serial_log_file)
            tail_size = min(size, 16384)
            with open(serial_log_file, 'rb') as f:
                if size > tail_size:
                    f.seek(size - tail_size)
                tail = f.read().decode('utf-8', errors='replace')
            debuglog(debug, "--- serial.log tail ({} of {} bytes) ---\n{}\n--- end serial.log tail ---".format(tail_size, size, tail))
        else:
            debuglog(debug, "serial.log not present: {}".format(serial_log_file))
    except Exception as e:
        debuglog(debug, "serial.log tail failed: {}".format(e))

    # QEMU monitor info commands (VM running? paused? network up? CPU stuck?)
    if qmon_port:
        qmon_cmds = (
            'info version',
            'info name',
            'info uuid',
            'info kvm',
            'info status',
            'info cpus',
            'info registers',
            'info roms',
            'info memory_size_summary',
            'info block',
            'info pci',
            'info network',
            'info usernet',
            'info chardev',
            'info vnc',
            'info migrate',
        )
        for cmd in qmon_cmds:
            try:
                resp = _qmon_send(qmon_port, cmd, timeout=2.0)
                debuglog(debug, "qmon> {}\n{}".format(cmd, (resp or "<no response>").strip()))
            except Exception as e:
                debuglog(debug, "qmon> {} failed: {}".format(cmd, e))
    else:
        debuglog(debug, "qmon not available; skipping monitor commands")

    # VNC screendump (PPM) -- visual snapshot of guest console at the moment of timeout
    if qmon_port and output_dir:
        try:
            screenshot_path = os.path.abspath(os.path.join(output_dir, "{}.boot-debug-{}.ppm".format(vm_name, label)))
            resp = _qmon_send(qmon_port, "screendump {}".format(screenshot_path), timeout=5.0)
            if os.path.exists(screenshot_path):
                debuglog(debug, "VM screen captured -> {} ({} bytes)".format(screenshot_path, os.path.getsize(screenshot_path)))
            else:
                debuglog(debug, "screendump produced no file; qmon resp: {}".format((resp or '').strip()))
        except Exception as e:
            debuglog(debug, "screendump failed: {}".format(e))

    debuglog(debug, "===== boot-debug snapshot [{}] end =====".format(label))


def watch_vnc_tunnel_log(log_path, stop_event, is_default_notice=False):
    """Monitor VNC proxy log and print tunnel URL as soon as it appears."""
    if not log_path:
        return
    start_wait = time.time()
    while not os.path.exists(log_path):
        if stop_event.is_set() or (time.time() - start_wait > 30):
            return
        time.sleep(0.5)
        
    try:
        with open(log_path, 'r') as f:
            while not stop_event.is_set():
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                match = re.search(r"Open this link to access WebVNC \(via ([^)]+)\): (https?://[^\s]+)", line)
                if match:
                    service = match.group(1)
                    url = match.group(2)
                    display_url = url
                    if supports_ansi_color():
                        display_url = "\x1b[32m{}\x1b[0m".format(url)
                    log("Open this link to access WebVNC (via {}): {}".format(service, display_url))
                    if is_default_notice:
                        log("Notice: Remote VNC tunnel is enabled by default as no local browser was detected.")
                        log("        Use '--remote-vnc off' to disable it.")
                    return
    except Exception:
        pass


def detect_host_ssh_port(sshd_config_path="/etc/ssh/sshd_config"):
    try:
        with open(sshd_config_path, 'r') as f:
            for line in f:
                line = line.split('#', 1)[0].strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == "port":
                    port = parts[1]
                    if port.isdigit():
                        return port
    except FileNotFoundError:
        return ""
    except OSError:
        return ""
    return ""


def main():
    # Handle internal VNC proxy mode
    if len(sys.argv) > 1 and sys.argv[1] == '--internal-vnc-proxy':
        try:
            vnc_port = int(sys.argv[2])
            web_port = int(sys.argv[3])
            vm_info = sys.argv[4]
            qemu_pid = int(sys.argv[5])
            audio_enabled = sys.argv[6] == '1' if len(sys.argv) > 6 else False
            qmon_port = int(sys.argv[7]) if len(sys.argv) > 7 and sys.argv[7].isdigit() else None
            error_log_path = sys.argv[8] if len(sys.argv) > 8 else None
            is_console_vnc = sys.argv[9] == '1' if len(sys.argv) > 9 else False
            listen_addr_raw = sys.argv[10] if len(sys.argv) > 10 else '127.0.0.1'
            listen_addr = listen_addr_raw.split(',') if ',' in listen_addr_raw else listen_addr_raw
            remote_vnc_val = sys.argv[11] if len(sys.argv) > 11 else '0'
            # Correctly parse string values like 'True', 'cf', 'lhr'
            remote_vnc = remote_vnc_val if remote_vnc_val not in ['0', 'False', 'false', None] else False
            debug_vnc = sys.argv[12] == '1' if len(sys.argv) > 12 else False
            link_file = sys.argv[13] if len(sys.argv) > 13 and sys.argv[13] != '0' else None
            vnc_pwd = sys.argv[14] if len(sys.argv) > 14 else ""
            start_vnc_web_proxy(vnc_port, web_port, vm_info, qemu_pid, audio_enabled, qmon_port, error_log_path, is_console_vnc, listen_addr=listen_addr, remote_vnc=remote_vnc, debug=debug_vnc, remote_vnc_link_file=link_file, vnc_password=vnc_pwd)
        except Exception as e:
            # If we have an error log path, try to write to it even if startup fails
            try:
                if len(sys.argv) > 8:
                    with open(sys.argv[8], 'a') as f:
                        f.write("[ProxyStarter] Fatal error during startup: {}\n".format(e))
            except:
                pass
            print("VNC Proxy startup error: {}".format(e), file=sys.stderr)
            pass
        return

    # Default configuration
    default_cpu = str(max(1, os.cpu_count() or 1))
    config = {
        'mem': "2048",
        'cpu': default_cpu,
        'cputype': "",
        'nc': "",
        'sshport': "",
        'sshname': "",
        'hostsshport': "",
        'console': False,
        'useefi': False,
        'detach': False,
        'vpaths': [],
        'ports': [],
        'vnc': "",
        'sync': "rsync",
        'qmon': "",
        'disktype': "",
        'public': False,
        'os': "",
        'release': "",
        'arch': "",
        'builder': "",
        'whpx': False,
        'serialport': "",
        # QEMU user networking (slirp) IPv6 is disabled by default.
        'enable_ipv6': False,
        'debug': False,
        'qcow2': "",
        'cachedir': "",
        'vga': "",
        'resolution': "1280x800",
        'snapshot': False,
        'synctime': None,
        'public_vnc': False,
        'public_ssh': False,
        'accept_vm_ssh': False,
        'remote_vnc': None,
        'remote_vnc_is_default': False,
        'remote_vnc_link_file': None,
        'vnc_password': "",
        'boot_timeout_sec': 600,
        'enable_pmu': False
    }

    ssh_passthrough = []
    cpu_specified = False
    serial_user_specified = False
    vnc_user_specified = False
    arch_specified = False
    boot_timeout_user_specified = False


    script_home = os.path.dirname(os.path.abspath(__file__))
    working_dir = os.path.join(script_home, "output")
    
    if os.environ.get("GOOGLE_CLOUD_SHELL") == "true":
        working_dir = "/tmp/anyvm.org"
        if not os.path.exists(working_dir):
            os.makedirs(working_dir)
        config['remote_vnc'] = True
        config['remote_vnc_is_default'] = True

    # Manual argument parsing
    args = sys.argv[1:]
    
    if len(args) == 0 or "--help" in args or "-h" in args:
        print_usage()
        sys.exit(0)

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            ssh_passthrough = args[i+1:]
            break
        if arg == "--os":
            config['os'] = args[i+1].lower()
            i += 1
        elif arg == "--release":
            config['release'] = args[i+1]
            i += 1
        elif arg == "--arch":
            config['arch'] = args[i+1].lower()
            arch_specified = True
            i += 1
        elif arg == "--mem":
            config['mem'] = args[i+1]
            i += 1
        elif arg == "--cpu":
            config['cpu'] = args[i+1]
            cpu_specified = True
            i += 1
        elif arg == "--cpu-type":
            config['cputype'] = args[i+1]
            i += 1
        elif arg in ["--data-dir", "--workingdir"]:
            working_dir = os.path.abspath(args[i+1])
            i += 1
        elif arg == "--nc":
            config['nc'] = args[i+1]
            i += 1
        elif arg in ["--sshport", "--ssh-port"]:
            config['sshport'] = args[i+1]
            i += 1
        elif arg == "--ssh-name":
            config['sshname'] = args[i+1]
            i += 1
        elif arg == "--host-ssh-port":
            config['hostsshport'] = args[i+1]
            i += 1
        elif arg == "--builder":
            config['builder'] = args[i+1]
            i += 1
        elif arg == "--uefi":
            config['useefi'] = True
        elif arg in ["--detach", "-d"]:
            config['detach'] = True
        elif arg in ["--console", "-c"]:
            config['console'] = True
        elif arg == "-v":
            config['vpaths'].append(args[i+1])
            i += 1
        elif arg == "-p":
            config['ports'].append(args[i+1])
            i += 1
        elif arg == "--mon":
            config['qmon'] = args[i+1]
            i += 1
        elif arg == "--vnc":
            config['vnc'] = args[i+1]
            vnc_user_specified = True
            i += 1
        elif arg == "--vnc-password":
            config['vnc_password'] = args[i+1]
            i += 1
        elif arg in ["--res", "--resolution"]:
            config['resolution'] = args[i+1]
            i += 1
        elif arg == "--vga":
            config['vga'] = args[i+1]
            i += 1
        elif arg == "--sync":
            val = args[i+1].lower()
            if val == "":
                val = "rsync"
            if val in ["no", "off"]:
                val = "no"
            if val not in ["sshfs", "nfs", "rsync", "scp", "no"]:
                 fatal("Invalid --sync mode: {}. Supported: rsync, sshfs, nfs, scp, no/off.".format(val))
            config['sync'] = val
            i += 1
        elif arg == "--disktype":
            config['disktype'] = args[i+1]
            i += 1
        elif arg == "--debug":
            config['debug'] = True
        elif arg == "--public":
            config['public'] = True
        elif arg == "--public-vnc":
            config['public_vnc'] = True
        elif arg == "--public-ssh":
            config['public_ssh'] = True
        elif arg == "--remote-vnc":
            if i + 1 < len(args) and not args[i+1].startswith("-"):
                val = args[i+1]
                if val.lower() in ["no", "off", "false", "0"]:
                    config['remote_vnc'] = False
                else:
                    config['remote_vnc'] = val
                i += 1
            else:
                config['remote_vnc'] = True
        elif arg == "--remote-vnc-link-file":
            config['remote_vnc_link_file'] = os.path.abspath(args[i+1])
            i += 1
        elif arg == "--accept-vm-ssh":
            config['accept_vm_ssh'] = True
        elif arg == "--whpx":
            config['whpx'] = True
        elif arg == "--serial":
            config['serialport'] = args[i+1]
            serial_user_specified = True
            i += 1
        elif arg == "--enable-ipv6":
            config['enable_ipv6'] = True
        elif arg == "--qcow2":
            config['qcow2'] = args[i+1]
            i += 1
        elif arg == "--cache-dir":
            config['cachedir'] = os.path.abspath(args[i+1])
            i += 1
        elif arg == "--snapshot":
            config['snapshot'] = True
        elif arg == "--enable-pmu":
            config['enable_pmu'] = True
        elif arg == "--boot-timeout-sec":
            try:
                val = int(args[i+1])
            except ValueError:
                fatal("--boot-timeout-sec requires an integer (seconds), got: {}".format(args[i+1]))
            if val <= 0:
                fatal("--boot-timeout-sec must be positive, got: {}".format(val))
            config['boot_timeout_sec'] = val
            boot_timeout_user_specified = True
            i += 1
        elif arg == "--sync-time":
            if i + 1 < len(args) and args[i+1] == "off":
                config['synctime'] = False
                i += 1
            else:
                config['synctime'] = True
        else:
            log("Warning: Unrecognized argument: {}".format(arg))
        i += 1

    if config['debug']:
        debuglog(True, "Debug logging enabled")

    # If remote VNC not explicitly specified, enable it by default if browser is unavailable
    if config['remote_vnc'] is None:
        if config['vnc'].lower() != "off" and not is_browser_available():
            config['remote_vnc'] = True
            config['remote_vnc_is_default'] = True
        else:
            config['remote_vnc'] = False

    is_vnc_console = (config.get('vnc') == "console")

    if not config['os']:
        print_usage()
        fatal("Missing required argument: --os")

    if config.get('sshname'):
        # Keep this conservative: Host patterns are space-delimited in ssh config.
        # Disallow whitespace and other separators to avoid generating invalid config.
        if not re.match(r'^[A-Za-z0-9._-]+$', config['sshname']):
            fatal("Invalid --ssh-name value: {} (allowed: A-Z a-z 0-9 . _ -)".format(config['sshname']))

    if config['os'] == "freebsd":
        config['useefi'] = True
    

    if config['whpx'] and not IS_WINDOWS:
        log("Warning: --whpx is only meaningful on Windows hosts; ignoring.")
        config['whpx'] = False

    # Arch detection
    host_machine = platform.machine()
    # Normalize Windows AMD64 to x86_64
    if host_machine == "AMD64":
        host_machine = "x86_64"
        
    if host_machine in ["arm64", "aarch64", "ARM64"]:
        host_arch = "aarch64"
    else:
        host_arch = host_machine
        # On macOS, if running under Rosetta 2, platform.machine() returns x86_64.
        # Check if the host is actually aarch64.
        if platform.system() == "Darwin" and host_arch == "x86_64":
            try:
                if subprocess.check_output(["sysctl", "-n", "hw.optional.arm64"], stderr=DEVNULL).strip() == b"1":
                    host_arch = "aarch64"
                    debuglog(config.get('debug', False), "Detected macOS Aarch64 host (running under Rosetta 2)")
            except:
                pass
    
    if not config['arch']:
        debuglog(config['debug'], "Host arch: " + host_arch)
        config['arch'] = host_arch

    # Normalize arch string
    if config['arch'] in ["x86_64", "amd64"]:
        config['arch'] = ""
    if config['arch'] in ["arm", "arm64", "ARM64"]:
        config['arch'] = "aarch64"

    # OpenBSD on aarch64 boots much slower under emulation; bump default timeout
    # unless the user passed --boot-timeout-sec explicitly.
    if not boot_timeout_user_specified and config['os'] == "openbsd" and config['arch'] == "aarch64":
        config['boot_timeout_sec'] = 1200
        debuglog(config['debug'], "OpenBSD/aarch64: default boot timeout raised to 1200s")

    if not config['vga']:
        if config['os'] == "netbsd" and config['arch'] != "aarch64":
            config['vga'] = "std"
        elif config['os'] == "haiku":
            config['vga'] = "std"
        else:
            config['vga'] = "virtio"

    arepo = "vmactions/{}-builder".format(config['os'])
    brepo = "anyvm-org/{}-builder".format(config['os'])
    if config['builder']:
        builder_repo = brepo if cmp_version(config['builder'], "2.0.0") >= 0 else arepo
        release_repo_candidates = [builder_repo]
    elif config['release']:
        builder_repo = brepo
        release_repo_candidates = [brepo, arepo]
    else:
        builder_repo = brepo
        release_repo_candidates = [brepo]
    working_dir_os = os.path.join(working_dir, config['os'])
    if not os.path.exists(working_dir_os):
        os.makedirs(working_dir_os)

    # Fetch release info
    releases_cache = {}
    
    def get_releases(repo_slug, force_refresh=False):
        cache_name = "{}-releases.json".format(repo_slug.replace("/", "_"))
        cache_path = os.path.join(working_dir_os, cache_name)
        if not force_refresh and repo_slug in releases_cache:
            return releases_cache[repo_slug]
        if not force_refresh and os.path.exists(cache_path):
            try:
                with open(cache_path, 'r') as f:
                    releases_cache[repo_slug] = json.load(f)
                    return releases_cache[repo_slug]
            except ValueError:
                pass
        
        debuglog(config['debug'], "Fetching fresh releases for {} (force_refresh={})".format(repo_slug, force_refresh))
        
        gh_headers = {
            "Accept": "application/vnd.github+json",
        }
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            gh_headers["Authorization"] = "Bearer {}".format(token)
            debuglog(config['debug'], "Using GitHub token auth for releases")

        url = "https://api.github.com/repos/{}/releases".format(repo_slug)
        content = fetch_url_content(url, config['debug'], headers=gh_headers)
        if content:
            try:
                data = json.loads(content)
                with open(cache_path, 'w') as f:
                    f.write(content)
                releases_cache[repo_slug] = data
                return data
            except ValueError:
                return []
        return []

    zst_link = ""

    if not config['qcow2']:
        search_builder = config['builder']
        search_repo = builder_repo
        is_default = False
        if not search_builder and config['os'] in DEFAULT_BUILDER_VERSIONS:
            search_builder = DEFAULT_BUILDER_VERSIONS[config['os']]
            search_repo = brepo if cmp_version(search_builder, "2.0.0") >= 0 else arepo
            is_default = True
        
        if search_builder:
            debuglog(config['debug'], "Checking builder {} in {}".format(search_builder, search_repo))
            
            use_this_builder = False
            found_zst_link = ""
            
            if config['release']:
                # Try to construct the URL directly
                target_zst = "{}-{}.qcow2.zst".format(config['os'], config['release'])
                if config['arch'] and config['arch'] != 'x86_64':
                    target_zst = "{}-{}-{}.qcow2.zst".format(config['os'], config['release'], config['arch'])
                
                # URL format: https://github.com/{repo}/releases/download/v{ver}/{filename}
                tag = "v" + search_builder if not search_builder.startswith("v") else search_builder
                
                candidate_url = "https://github.com/{}/releases/download/{}/{}".format(search_repo, tag, target_zst)
                debuglog(config['debug'], "Checking candidate URL: {}".format(candidate_url))
                
                if check_url_exists(candidate_url, config['debug']):
                    debuglog(config['debug'], "Candidate URL exists!")
                    use_this_builder = True
                    found_zst_link = candidate_url
                else:
                    # Try xz as fallback
                    target_xz = target_zst.replace('.zst', '.xz')
                    candidate_url_xz = "https://github.com/{}/releases/download/{}/{}".format(search_repo, tag, target_xz)
                    debuglog(config['debug'], "Checking candidate URL (xz): {}".format(candidate_url_xz))
                    if check_url_exists(candidate_url_xz, config['debug']):
                        debuglog(config['debug'], "Candidate URL (xz) exists!")
                        use_this_builder = True
                        found_zst_link = candidate_url_xz
                    elif config['arch'] == "aarch64" and not arch_specified:
                        # Fallback to x86_64 if aarch64 failed and arch was not user-specified
                        log("No aarch64 image found for {} {} in {}. Trying x86_64 fallback...".format(config['os'], config['release'], search_repo))
                        config['arch'] = "" # Empty string means x86_64 in anyvm
                        # Sync VM arch for debug logging later
                        debuglog(config['debug'], "Fallback to x86_64 due to missing aarch64 asset")
                        target_zst_fallback = "{}-{}.qcow2.zst".format(config['os'], config['release'])
                        candidate_url_fallback = "https://github.com/{}/releases/download/{}/{}".format(search_repo, tag, target_zst_fallback)
                        debuglog(config['debug'], "Checking fallback x86_64 URL: {}".format(candidate_url_fallback))
                        if check_url_exists(candidate_url_fallback, config['debug']):
                            debuglog(config['debug'], "Fallback x86_64 URL exists!")
                            use_this_builder = True
                            found_zst_link = candidate_url_fallback
                        else:
                            target_xz_fallback = target_zst_fallback.replace('.zst', '.xz')
                            candidate_url_xz_fallback = "https://github.com/{}/releases/download/{}/{}".format(search_repo, tag, target_xz_fallback)
                            debuglog(config['debug'], "Checking fallback x86_64 URL (xz): {}".format(candidate_url_xz_fallback))
                            if check_url_exists(candidate_url_xz_fallback, config['debug']):
                                debuglog(config['debug'], "Fallback x86_64 URL (xz) exists!")
                                use_this_builder = True
                                found_zst_link = candidate_url_xz_fallback
                            else:
                                debuglog(config['debug'], "Candidate URL not found (including fallback), falling back to full search")
                    else:
                        debuglog(config['debug'], "Candidate URL not found, falling back to full search")
            else:
                # If no release provided, we can't construct URL, but if we're using default builder, we force it
                if is_default:
                    use_this_builder = True
            
            if use_this_builder:
                config['builder'] = search_builder
                builder_repo = search_repo
                release_repo_candidates = [builder_repo]
                if is_default:
                    debuglog(config['debug'], "Using default builder: {} from {}".format(search_builder, search_repo))
                if found_zst_link:
                    zst_link = found_zst_link
                    debuglog(config['debug'], "Successfully constructed direct download link: {}".format(zst_link))
                else:
                    debuglog(config['debug'], "Target builder {} set, but no release specified to construct link yet.".format(search_builder))
            else:
                debuglog(config['debug'], "Could not construct direct link for builder {} (release {}), will fallback to API search.".format(search_builder, config['release']))

    if config['arch']:
        debuglog(config['debug'],"Using VM arch: " + config['arch'])
    else:
        debuglog(config['debug'],"Using VM arch: x86_64")

    if config['qcow2']:
        if not os.path.exists(config['qcow2']):
            fatal("Specified qcow2 file not found: " + config['qcow2'])
        qcow_name = os.path.abspath(config['qcow2'])
        output_dir = working_dir_os
        vm_name = "{}-custom".format(config['os'])
        hostid_file = None
        vmpub_file = None
        log("Using local qcow2: " + qcow_name)
    else:
        if not zst_link:
            releases_data = get_releases(builder_repo)
    
            if not releases_data and (config['builder'] or not config['release']):
                 fatal("Unsupported OS: {}. Builder repository {} not found.".format(config['os'], builder_repo))
    
            if config['builder']:
                target_tag = config['builder']
                if not target_tag.startswith('v'):
                    target_tag = "v" + target_tag
                
                def filter_releases(data, tag):
                    return [r for r in data if r.get('tag_name') == tag]
                
                debuglog(config['debug'], "Filtering releases for tag: {}".format(target_tag))
                filtered = filter_releases(releases_data, target_tag)
                
                if not filtered:
                    debuglog(config['debug'], "Builder version {} not found in cache. Refreshing...".format(target_tag))
                    releases_data = get_releases(builder_repo, force_refresh=True)
                    if not releases_data:
                         fatal("Unsupported OS: {}. Builder repository {} not found or inaccessible.".format(config['os'], builder_repo))
                    filtered = filter_releases(releases_data, target_tag)
                
                releases_data = filtered
                if not releases_data:
                    fatal("Builder version {} not found in repository {} even after refresh.".format(target_tag, builder_repo))
        else:
            releases_data = []

        published_at = ""
        # Find release version if not provided
        if not config['release']:
            def find_latest_release(data, arch):
                p_at = ""
                found_v = ""
                for r in data:
                    for asset in r.get('assets', []):
                        u = asset.get('browser_download_url', '')
                        if u.endswith("qcow2.zst") or u.endswith("qcow2.xz"):
                            if arch and arch != "x86_64" and arch not in u:
                                continue
                            filename=u.split('/')[-1]
                            filename= removesuffix(filename, ".qcow2.zst")
                            filename= removesuffix(filename, ".qcow2.xz")
                            parts = filename.split('-')
                            if len(parts) > 1:
                                ver = parts[1]
                                debuglog(config['debug'], "Candidate release found: {} from asset {}".format(ver, filename))
                                if p_at and p_at > r.get('published_at', ''):
                                    continue
                                if not p_at or cmp_version(ver, found_v) > 0:
                                    p_at = r.get('published_at', '')
                                    found_v = ver
                return found_v, p_at

            config['release'], published_at = find_latest_release(releases_data, config['arch'])
            
            if not config['release'] and config['arch'] == "aarch64" and not arch_specified:
                debuglog(config['debug'], "No aarch64 release found, searching for x86_64 fallback release...")
                config['release'], published_at = find_latest_release(releases_data, "")
                if config['release']:
                    log("No aarch64 release found for {}. Falling back to x86_64.".format(config['os']))
                    config['arch'] = ""


        log("Using release: " + config['release'])
        # Find download link
        def find_image_link(releases, target_zst, target_xz):
            for r in releases:
                for asset in r.get('assets', []):
                    u = asset.get('browser_download_url', '')
                    if u.endswith(target_zst) or u.endswith(target_xz):
                        return u
            return ""

        target_zst = "{}-{}.qcow2.zst".format(config['os'], config['release'])
        target_xz = "{}-{}.qcow2.xz".format(config['os'], config['release'])
        
        if config['arch'] and config['arch'] != 'x86_64':
            target_zst = "{}-{}-{}.qcow2.zst".format(config['os'], config['release'], config['arch'])
            target_xz = "{}-{}-{}.qcow2.xz".format(config['os'], config['release'], config['arch'])

        if not zst_link:
            search_repos = release_repo_candidates if config['release'] else [builder_repo]
            searched = set()
            for repo in search_repos:
                if repo in searched:
                    continue
                searched.add(repo)
                repo_releases = releases_data if repo == builder_repo else get_releases(repo)
                link = find_image_link(repo_releases, target_zst, target_xz)
                if link:
                    builder_repo = repo
                    releases_data = repo_releases
                    zst_link = link
                    break
            
            # If still no link and we are on aarch64 and it wasn't specified, fallback to x86_64 full search
            if not zst_link and config['arch'] == "aarch64" and not arch_specified:
                log("No aarch64 image found in any repository. Trying x86_64 fallback search...")
                config['arch'] = "" # x86_64
                target_zst_fallback = "{}-{}.qcow2.zst".format(config['os'], config['release'])
                target_xz_fallback = "{}-{}.qcow2.xz".format(config['os'], config['release'])
                
                searched = set()
                for repo in search_repos:
                    if repo in searched:
                        continue
                    searched.add(repo)
                    repo_releases = releases_data if repo == builder_repo else get_releases(repo)
                    link = find_image_link(repo_releases, target_zst_fallback, target_xz_fallback)
                    if link:
                        builder_repo = repo
                        releases_data = repo_releases
                        zst_link = link
                        break

        if not zst_link:
            fatal("Cannot find the image link.")

        debuglog(config['debug'],"Using link: " + zst_link)

        if not config['builder']:
            parts = zst_link.split('/')
            for p in parts:
                if p.startswith('v') and len(p) > 1 and p[1].isdigit():
                    config['builder'] = p[1:]
                    break
        
        output_dir = os.path.join(working_dir_os, "v" + config['builder'])
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        ova_file = os.path.join(output_dir, zst_link.split('/')[-1])
        qcow_name = ova_file.replace('.zst', '').replace('.xz', '')
        if not qcow_name.endswith('.qcow2'):
            qcow_name += ".qcow2"

        # Download and Extract
        cached_qcow2 = None
        if config.get('cachedir'):
            rel_path = os.path.relpath(output_dir, working_dir)
            cache_output_dir = os.path.join(config['cachedir'], rel_path)
            if not os.path.exists(cache_output_dir):
                debuglog(config['debug'], "Creating cache directory: {}".format(cache_output_dir))
                os.makedirs(cache_output_dir)
            cached_qcow2 = os.path.join(cache_output_dir, os.path.basename(qcow_name))

        if config['snapshot'] and cached_qcow2 and os.path.exists(cached_qcow2):
            debuglog(config['debug'], "Snapshot mode: Using cached qcow2 directly: {}".format(cached_qcow2))
            qcow_name = cached_qcow2
        elif not os.path.exists(qcow_name):
            if cached_qcow2 and os.path.exists(cached_qcow2):
                # Cache hit: copy qcow2 from cache to data-dir
                debuglog(config['debug'], "Found cached qcow2: {}".format(cached_qcow2))
                log("Copying cached image: {} -> {}".format(cached_qcow2, qcow_name))
                start_time = time.time()
                shutil.copy2(cached_qcow2, qcow_name)
                duration = time.time() - start_time
                debuglog(config['debug'], "Copying from cache took {:.2f} seconds".format(duration))
            else:
                # Cache miss or no cache-dir: download and extract
                if not os.path.exists(ova_file):
                    if download_file(zst_link, ova_file, config['debug']):
                        download_optional_parts(zst_link, ova_file, debug=config['debug'])
                
                if not os.path.exists(ova_file):
                    fatal("Failed to download image: " + ova_file)
                
                log("Extracting " + ova_file)
                extract_start_time = time.time()
                
                def cmd_exists(cmd):
                    test_cmd = [cmd, '--version']
                    try:
                        startupinfo = None
                        if IS_WINDOWS:
                            startupinfo = subprocess.STARTUPINFO()
                            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        subprocess.call(test_cmd, stdout=DEVNULL, stderr=DEVNULL, startupinfo=startupinfo)
                        return True
                    except:
                        return False

                if ova_file.endswith('.zst'):
                    if not cmd_exists('zstd'):
                        msg = "Error: 'zstd' command not found. This is required to extract the image.\n"
                        if IS_WINDOWS:
                            msg += "Please install it via winget: winget install facebook.zstd\n"
                        else:
                            msg += "Please install it via your package manager (e.g. apt install zstd, brew install zstd)\n"
                        fatal(msg)
                    if subprocess.call(['zstd', '-d', ova_file, '-o', qcow_name]) != 0:
                        fatal("zstd extraction failed")
                elif ova_file.endswith('.xz'):
                    if not cmd_exists('xz'):
                        msg = "Error: 'xz' command not found. This is required to extract the image.\n"
                        if IS_WINDOWS:
                            msg += "Please install it via winget: winget install Tukaani.XZ\n"
                        else:
                            msg += "Please install it via your package manager (e.g. apt install xz-utils, brew install xz)\n"
                        fatal(msg)
                    with open(qcow_name, 'wb') as f:
                        if subprocess.call(['xz', '-d', '-c', ova_file], stdout=f) != 0:
                            fatal("xz extraction failed")
                extract_duration = time.time() - extract_start_time
                debuglog(config['debug'], "Extraction took {:.2f} seconds".format(extract_duration))
                
                if not os.path.exists(qcow_name):
                    fatal("Extraction failed")
                
                # Delete zst from data-dir
                try:
                    os.remove(ova_file)
                except OSError:
                    pass
                
                if cached_qcow2:
                    # Copy qcow2 to cache
                    debuglog(config['debug'], "Copying qcow2 to cache: {} -> {}".format(qcow_name, cached_qcow2))
                    log("Caching extracted image: {}".format(cached_qcow2))
                    shutil.copy2(qcow_name, cached_qcow2)
                    if config['snapshot']:
                        debuglog(config['debug'], "Snapshot mode: Removing extracted qcow2 from data-dir and using cache")
                        try:
                            os.remove(qcow_name)
                        except OSError:
                            pass
                        qcow_name = cached_qcow2

        # Key files
        vm_name = "{}-{}".format(config['os'], config['release'])
        if config['arch'] and config['arch'] != "x86_64":
            vm_name += "-" + config['arch']

        hostid_url = "https://github.com/{}/releases/download/v{}/{}-host.id_rsa".format(builder_repo, config['builder'], vm_name)
        hostid_file = os.path.join(output_dir, hostid_url.split('/')[-1])
        
        if not os.path.exists(hostid_file):
            if config.get('cachedir'):
                rel_path = os.path.relpath(output_dir, working_dir)
                cache_output_dir = os.path.join(config['cachedir'], rel_path)
                if not os.path.exists(cache_output_dir):
                    debuglog(config['debug'], "Creating cache directory: {}".format(cache_output_dir))
                    os.makedirs(cache_output_dir)
                cached_hostid = os.path.join(cache_output_dir, os.path.basename(hostid_file))
                if not os.path.exists(cached_hostid):
                    debuglog(config['debug'], "host.id_rsa not found in cache, downloading to: {}".format(cached_hostid))
                    download_file(hostid_url, cached_hostid, config['debug'])
                if os.path.exists(cached_hostid):
                    debuglog(config['debug'], "Copying host.id_rsa from cache to: {}".format(hostid_file))
                    shutil.copy2(cached_hostid, hostid_file)
            else:
                download_file(hostid_url, hostid_file, config['debug'])
        
        if os.path.exists(hostid_file):
            if IS_WINDOWS:
                tighten_windows_permissions(hostid_file)
            else:
                os.chmod(hostid_file, 0o600)

        vmpub_url = "https://github.com/{}/releases/download/v{}/{}-id_rsa.pub".format(builder_repo, config['builder'], vm_name)
        vmpub_file = os.path.join(output_dir, vmpub_url.split('/')[-1])
        if not os.path.exists(vmpub_file):
            if config.get('cachedir'):
                rel_path = os.path.relpath(output_dir, working_dir)
                cache_output_dir = os.path.join(config['cachedir'], rel_path)
                if not os.path.exists(cache_output_dir):
                    debuglog(config['debug'], "Creating cache directory: {}".format(cache_output_dir))
                    os.makedirs(cache_output_dir)
                cached_vmpub = os.path.join(cache_output_dir, os.path.basename(vmpub_file))
                if not os.path.exists(cached_vmpub):
                    debuglog(config['debug'], "id_rsa.pub not found in cache, downloading to: {}".format(cached_vmpub))
                    download_file(vmpub_url, cached_vmpub, config['debug'])
                if os.path.exists(cached_vmpub):
                    debuglog(config['debug'], "Copying id_rsa.pub from cache to: {}".format(vmpub_file))
                    shutil.copy2(cached_vmpub, vmpub_file)
            else:
                download_file(vmpub_url, vmpub_file, config['debug'])

    vm_user = "user" if config['os'] == "haiku" else "root"

    # Ports
    if not config['sshport']:
        config['sshport'] = get_free_port()
        if not config['sshport']:
            fatal("No free port")

    if config['public'] or config['public_ssh']:
        ssh_addr = ""
        ssh_extra_addrs = []
    else:
        ssh_addr = "127.0.0.1"
        ssh_extra_addrs = get_private_ips()
        debuglog(config['debug'], "Private IPs for SSH: {}".format(ssh_extra_addrs if ssh_extra_addrs else "(none)"))

    if config['public']:
        p_addr = ""
        p_extra_addrs = []
    else:
        p_addr = "127.0.0.1"
        p_extra_addrs = get_private_ips()
        debuglog(config['debug'], "Private IPs for port mappings: {}".format(p_extra_addrs if p_extra_addrs else "(none)"))

    # Ensure serial port is allocated for background logging and VNC console
    if not config['serialport']:
        serial_port = get_free_port(start=7000, end=9000)
        if not serial_port:
            fatal("No free serial ports available")
        config['serialport'] = str(serial_port)

    if serial_user_specified:
        serial_bind_addr = "0.0.0.0" if config['public'] else "127.0.0.1"
    else:
        serial_bind_addr = "127.0.0.1"

    # Always prepare serial log file
    serial_log_file = os.path.join(output_dir, "{}.serial.log".format(vm_name))
    if os.path.exists(serial_log_file):
        try:
            os.remove(serial_log_file)
        except:
            pass
    
    serial_chardev_id = "serial0"
    serial_chardev_def = "socket,id={},host={},port={},server=on,wait=off,logfile={}".format(
        serial_chardev_id, serial_bind_addr, config['serialport'], serial_log_file)
    
    # Default to using this log-enabled chardev
    serial_arg = "chardev:{}".format(serial_chardev_id)

    if config['console']:
        # For foreground console mode, prioritize stdio interaction
        serial_arg = "mon:stdio"
    
    debuglog(config['debug'], "Serial console logging to: " + serial_log_file)
    debuglog(config['debug'], "Serial console listening on {}:{} (tcp)".format(serial_bind_addr, config['serialport']))

    # QEMU Construction
    bin_name = "qemu-system-x86_64"
    if config['arch'] == "riscv64":
        bin_name = "qemu-system-riscv64"
    elif config['arch'] == "aarch64":
        bin_name = "qemu-system-aarch64"
    qemu_bin = find_qemu(bin_name)
    
    if not qemu_bin:
        fatal("QEMU binary '{}' not found. Please install QEMU or check PATH.".format(bin_name))

    # VNC Console Auto-detection logic:
    vnc_val = config.get('vnc', '')
    auto_reason = None
    
    if not vnc_val and not is_vnc_console:
        if config['os'] == "openindiana":
            if "202510" in config['release']:
                # Rule for OpenIndiana: Default to 'console' if not specified.
                auto_reason = "OpenIndiana (requires console for login display)"
        elif "x86_64" not in bin_name:
            # Rule for non-x86 architectures: Default to 'console' if not specified.
            # Exception: OpenBSD on aarch64 starting at 7.4 has a working
            # graphical framebuffer via virtio-gpu-pci, so prefer regular VNC
            # there. 7.3 and earlier lack this and still need console.
            openbsd_aarch64_has_fb = False
            if config['os'] == "openbsd" and config['arch'] == "aarch64":
                try:
                    rel_parts = tuple(int(x) for x in config['release'].split('.')[:2])
                    openbsd_aarch64_has_fb = len(rel_parts) >= 2 and rel_parts >= (7, 4)
                except (ValueError, AttributeError):
                    openbsd_aarch64_has_fb = False
            if not openbsd_aarch64_has_fb:
                auto_reason = "non-x86 arch ({})".format(bin_name)
             
    if auto_reason:
         debuglog(config['debug'], "Auto-enabling VNC Console: " + auto_reason)
         config['vnc'] = 'console'
         is_vnc_console = True
         
         # If we previously defaulted to stdio, we must switch back to the log-enabled chardev for VNC compatibility
         if serial_arg == "mon:stdio":
              serial_arg = "chardev:{}".format(serial_chardev_id)
              debuglog(config['debug'], "Switched serial back to chardev for VNC Console compatibility: " + serial_arg)
    
    # Acceleration determination
    accel = "tcg"
    if config['arch'] == "aarch64":
        if host_arch == "aarch64":
            if os.path.exists("/dev/kvm"):
                if os.access("/dev/kvm", os.R_OK | os.W_OK):
                    accel = "kvm"
                else:
                    log("Warning: /dev/kvm exists but is not writable. Falling back to TCG.")
            elif platform.system() == "Darwin" and hvf_supported():
                accel = "hvf"
    elif config['arch'] == "riscv64":
        accel = "tcg"
    else: # x86_64
        if host_arch in ["x86_64", "amd64"]:
            if IS_WINDOWS:
                if config['whpx']:
                    accel = "whpx"
            elif os.path.exists("/dev/kvm"):
                if os.access("/dev/kvm", os.R_OK | os.W_OK):
                    accel = "kvm"
                else:
                    log("Warning: /dev/kvm exists but is not writable. Falling back to TCG.")
            elif platform.system() == "Darwin" and hvf_supported():
                if config['os'] != "haiku":
                    accel = "tcg"
                else:
                    accel = "hvf"

    # CPU optimization for TCG
    if not cpu_specified and accel == "tcg":
        try:
            if int(config['cpu']) > 2:
                debuglog(config['debug'], "TCG mode detected and no CPU count specified, limiting to 2 cores for performance optimization.")
                config['cpu'] = "2"
        except (ValueError, TypeError):
            pass

    # TCG (pure software emulation) is 10-50x slower than KVM/HVF/WHPX, so the
    # default 600s boot timeout is often not enough for heavier guests
    # (Solaris, DragonFlyBSD, etc.). Bump it unless the user pinned a value.
    if not boot_timeout_user_specified and accel == "tcg" and config['boot_timeout_sec'] < 1800:
        config['boot_timeout_sec'] = 1800
        debuglog(config['debug'], "TCG (no hardware acceleration): default boot timeout raised to {}s".format(config['boot_timeout_sec']))

    # Disk type selection
    if config['disktype']:
        disk_if = config['disktype']
    else:
        if config['os'] != "dragonflybsd":
            disk_if = "virtio"
        else:
            disk_if = "ide"

    # Build Netdev Argument
    # Always include standard SSH mapping
    netdev_args = "user,id=net0,net=192.168.122.0/24,dhcpstart=192.168.122.10"
    if not config.get('enable_ipv6'):
        netdev_args += ",ipv6=off"

    # Track every hostfwd we install so we can rebind them at runtime via the
    # QEMU monitor if the VM ends up with an unexpected IP (e.g. stale DHCP lease).
    # Each entry: (proto, host_addr, host_port_str, guest_port_str)
    hostfwd_specs = []

    netdev_args += ",hostfwd=tcp:{}:{}-:22".format(ssh_addr, config['sshport'])
    hostfwd_specs.append(("tcp", ssh_addr, str(config['sshport']), "22"))
    for extra_addr in ssh_extra_addrs:
        if is_port_available(extra_addr, int(config['sshport'])):
            netdev_args += ",hostfwd=tcp:{}:{}-:22".format(extra_addr, config['sshport'])
            hostfwd_specs.append(("tcp", extra_addr, str(config['sshport']), "22"))
            debuglog(config['debug'], "hostfwd: SSH {}:{} -> :22 OK".format(extra_addr, config['sshport']))
        else:
            debuglog(config['debug'], "hostfwd: SSH {}:{} -> :22 SKIPPED (port in use)".format(extra_addr, config['sshport']))

    # Add custom port mappings
    for p in config['ports']:
        parts = p.split(':')
        # Format: host:guest (tcp default), tcp:host:guest, udp:host:guest
        if len(parts) == 2:
            # host:guest -> tcp:addr:host-:guest
            netdev_args += ",hostfwd=tcp:{}:{}-:{}".format(p_addr, parts[0], parts[1])
            hostfwd_specs.append(("tcp", p_addr, parts[0], parts[1]))
            for extra_addr in p_extra_addrs:
                if is_port_available(extra_addr, int(parts[0])):
                    netdev_args += ",hostfwd=tcp:{}:{}-:{}".format(extra_addr, parts[0], parts[1])
                    hostfwd_specs.append(("tcp", extra_addr, parts[0], parts[1]))
                    debuglog(config['debug'], "hostfwd: tcp {}:{} -> :{} OK".format(extra_addr, parts[0], parts[1]))
                else:
                    debuglog(config['debug'], "hostfwd: tcp {}:{} -> :{} SKIPPED (port in use)".format(extra_addr, parts[0], parts[1]))
        elif len(parts) == 3:
            # proto:host:guest -> proto:addr:host-:guest
            netdev_args += ",hostfwd={}:{}:{}-:{}".format(parts[0], p_addr, parts[1], parts[2])
            hostfwd_specs.append((parts[0], p_addr, parts[1], parts[2]))
            for extra_addr in p_extra_addrs:
                if is_port_available(extra_addr, int(parts[1])):
                    netdev_args += ",hostfwd={}:{}:{}-:{}".format(parts[0], extra_addr, parts[1], parts[2])
                    hostfwd_specs.append((parts[0], extra_addr, parts[1], parts[2]))
                    debuglog(config['debug'], "hostfwd: {} {}:{} -> :{} OK".format(parts[0], extra_addr, parts[1], parts[2]))
                else:
                    debuglog(config['debug'], "hostfwd: {} {}:{} -> :{} SKIPPED (port in use)".format(parts[0], extra_addr, parts[1], parts[2]))

    args_qemu = []
    if serial_chardev_def:
        args_qemu.extend(["-chardev", serial_chardev_def])

    args_qemu.extend([
        "-serial", serial_arg,
        "-name", vm_name,
        "-smp", config['cpu'],
        "-m", config['mem'],
        "-netdev", netdev_args,
    ])

    # Disk: when we need bootindex, split into -drive + -device so we can set it
    # on the virtio-blk-pci device. bootindex only helps UEFI/EFI firmwares skip
    # slow PXE/HTTP boot attempts -- aarch64 (always EFI) and x86_64 with --uefi.
    # For BIOS x86_64 (SeaBIOS) and riscv64 (u-boot), the shortcut form is fine
    # and avoids confusing some guest bootloaders (e.g. illumos GRUB).
    needs_bootindex_disk = (
        config['arch'] == "aarch64"
        or config.get('useefi')
    )
    if disk_if == "virtio" and needs_bootindex_disk:
        args_qemu.extend([
            "-drive", "file={},format=qcow2,if=none,id=disk0,discard=unmap,detect-zeroes=unmap".format(qcow_name),
            "-device", "virtio-blk-pci,drive=disk0,bootindex=0"
        ])
    else:
        args_qemu.extend([
            "-drive", "file={},format=qcow2,if={},discard=unmap,detect-zeroes=unmap".format(qcow_name, disk_if)
        ])

    rtc_base = "utc"
    if config['os'] in ["windows", "haiku"]:
        rtc_base = "localtime"
    args_qemu.extend(["-rtc", "base={},clock=host,driftfix=slew".format(rtc_base)])

    if config['snapshot']:
        args_qemu.append("-snapshot")

    # Windows on ARM has DirectSound issues; disable audio only there.
    if IS_WINDOWS and host_arch == "aarch64":
        args_qemu.extend(["-audiodev", "none,id=snd"])

    # Network card selection
    if config['nc']:
        net_card = config['nc']
    else:
        if config['arch'] == "aarch64":
            net_card = "virtio-net-pci"
        else:
            net_card = "e1000"
        if config['os'] == "openbsd" and config['release']:
            release_base = config['release'].split('-')[0]
            if release_base in OPENBSD_E1000_RELEASES:
                net_card = "e1000"
            else:
                net_card = "virtio-net-pci"
        elif config['os'] == "dragonflybsd":
            if config['release'] != "6.4.0":
                net_card = "virtio-net-pci"
        elif config['arch'] == "riscv64":
            net_card = "virtio-net-pci"
        elif config['os'] == "netbsd" and config['arch'] == "aarch64":
            net_card = "virtio-net-pci"
        elif config['os'] == "freebsd":
            net_card = "virtio-net-pci"

    # Platform specific args
    if config['arch'] == "aarch64":
        efi_path = os.path.join(output_dir, vm_name + "-QEMU_EFI.fd")
        vars_path = os.path.join(output_dir, vm_name + "-QEMU_EFI_VARS.fd")
        
        if not os.path.exists(efi_path):
            create_sized_file(efi_path, 64)
            candidates = [
                "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd", 
                "/usr/share/AAVMF/AAVMF_CODE.fd",
                "/usr/share/qemu/edk2-aarch64-code.fd",
                "/opt/homebrew/share/qemu/edk2-aarch64-code.fd",
                "/opt/homebrew/share/edk2/aarch64/QEMU_EFI.fd",
                "/usr/local/share/qemu/edk2-aarch64-code.fd"
            ]
            for c in candidates:
                if os.path.exists(c):
                    debuglog(config['debug'], "Found Aarch64 EFI firmware: {}".format(c))
                    copy_content_to_file(c, efi_path)
                    break
        
        if config['snapshot'] and os.path.exists(vars_path):
            try: os.remove(vars_path)
            except OSError: pass
        
        if not os.path.exists(vars_path):
            create_sized_file(vars_path, 64)

        if config['cputype']:
            cpu = config['cputype']
        else:
            if accel in ["kvm", "hvf"]:
                cpu = "host"
            elif config['os'] == "openbsd":
                # OpenBSD fails with "FP exception in kernel" on cpu=max
                cpu = "neoverse-n1"
            else:
                cpu = "max"
        
        vga_type = config['vga'] if config['vga'] else "virtio-gpu-pci"
        if vga_type in ["virtio", "virtio-gpu"]:
            vga_type = "virtio-gpu-pci"
        
        # OpenBSD aarch64 < 7.4 has broken ACPI _PRT routing for legacy PCI
        # interrupts -- xhci/e1000 fail with "couldn't map interrupt", and the
        # modern virtio transport isn't fully wired up in vio(4) either. Force
        # Device Tree mode by disabling ACPI so PCI INTx routing comes from FDT.
        machine_opts = "virt,accel={},gic-version=3,usb=on".format(accel)
        try:
            obsd_rel = tuple(int(x) for x in config['release'].split('.')[:2])
        except (ValueError, AttributeError):
            obsd_rel = None
        if (config['os'] == "openbsd" and config['arch'] == "aarch64"
                and obsd_rel is not None and len(obsd_rel) >= 2 and obsd_rel < (7, 4)):
            machine_opts += ",acpi=off"
            debuglog(config['debug'], "OpenBSD aarch64 < 7.4: disabling ACPI (force FDT for PCI interrupts)")

        args_qemu.extend([
            "-machine", machine_opts,
            "-cpu", cpu,
            "-device", "qemu-xhci",
            "-device", "{},netdev=net0".format(net_card),
            "-drive", "if=pflash,format=raw,readonly=on,file={}".format(efi_path),
            "-drive", "if=pflash,format=raw,file={},unit=1".format(vars_path),
            "-device", vga_type
        ])

        if config['resolution']:
            res_parts = config['resolution'].lower().split('x')
            if len(res_parts) == 2:
                # For virtio-gpu-pci
                args_qemu.extend(["-global", "virtio-gpu-pci.xres={}".format(res_parts[0])])
                args_qemu.extend(["-global", "virtio-gpu-pci.yres={}".format(res_parts[1])])
    elif config['arch'] == "riscv64":
        machine_opts = "virt,accel=tcg,usb=on,acpi=off"
        if not is_vnc_console:
             machine_opts += ",graphics=off"
        cpu_opts = "rv64"
        
        args_qemu.extend([
            "-machine", machine_opts,
            "-cpu", cpu_opts,
            "-device", "qemu-xhci",
            "-device", "{},netdev=net0".format(net_card)
        ])

        uboot_bin = "/usr/lib/u-boot/qemu-riscv64_smode/u-boot.bin"
        if not os.path.exists(uboot_bin):
            fatal("RISC-V u-boot binary not found at {}.\nPlease install it: sudo apt-get install u-boot-qemu".format(uboot_bin))
            
        args_qemu.extend([
            "-kernel", uboot_bin,
            "-device", "virtio-balloon-pci"
        ])
    else:
        # x86_64
        machine_opts = "pc,accel={},hpet=off,smm=off,graphics=on,vmport=off,usb=on".format(accel)
        
        if accel in ["kvm", "whpx", "hvf"]:
            if accel == "kvm":
                if config['os'] == 'dragonflybsd':
                    # DragonFlyBSD's early-boot init writes to MSRs that vary by
                    # runner CPU generation. -cpu host exposes too much; even
                    # pmu=off was not enough to stop intermittent #GP-in-wrmsr
                    # right after TSC calibration. Lock to a stable named model
                    # so guest CPUID is identical across all runner hardware.
                    # Note: named CPU models do NOT support `migratable` or
                    # `l3-cache` properties (those are -cpu host only).
                    cpu_opts = "Broadwell-v4,+hypervisor,+invtsc"
                    debuglog(config['debug'], "DragonFlyBSD: using Broadwell-v4 named CPU model (avoids -cpu host MSR variance)")
                else:
                    cpu_opts = "host,kvm=on,l3-cache=on,+hypervisor,migratable=no,+invtsc"
            else:
                cpu_opts = "host,+rdrand,+rdseed"
        else:
            cpu_opts = "qemu64,+rdrand,+rdseed"

        # Disable the guest PMU by default. Exposing the host PMU via -cpu host
        # can trigger intermittent #GP-in-wrmsr crashes during early guest boot
        # (notably DragonFlyBSD) when the runner CPU generation exposes PMU
        # MSRs that KVM refuses writes to. Profiling tools inside the guest
        # (perf / pmcstat / VTune) need the PMU -- pass --enable-pmu to opt in.
        if accel in ["kvm", "whpx", "hvf"] and not config.get('enable_pmu'):
            cpu_opts += ",pmu=off"
            debuglog(config['debug'], "Guest PMU disabled (pass --enable-pmu to expose host PMU)")
            
        if config['vga']:
            vga_type = config['vga']
        else:
            vga_type = "std"

        args_qemu.extend([
            "-machine", machine_opts,
            "-cpu", cpu_opts,
            "-device", "{},netdev=net0".format(net_card),
            "-device", "virtio-balloon-pci",
            "-vga", vga_type
        ])

        if accel == "kvm":
            args_qemu.extend(["-global", "kvm-pit.lost_tick_policy=delay"])

        if config['resolution']:
            res_parts = config['resolution'].lower().split('x')
            if len(res_parts) == 2:
                # For std and virtio-vga, we can often set resolution via xres/yres
                # Note: This works best with certain video drivers in the guest.
                if vga_type == "std":
                    args_qemu.extend(["-global", "VGA.xres={}".format(res_parts[0])])
                    args_qemu.extend(["-global", "VGA.yres={}".format(res_parts[1])])
                elif vga_type == "virtio":
                    args_qemu.extend(["-global", "virtio-vga.xres={}".format(res_parts[0])])
                    args_qemu.extend(["-global", "virtio-vga.yres={}".format(res_parts[1])])
        
        # x86 UEFI handling
        if config['useefi']:
            if IS_WINDOWS:
                prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
                efi_src = os.path.join(prog_files, "qemu", "share", "edk2-x86_64-code.fd")
                if not os.path.exists(efi_src):
                    msys_efi = r"C:\msys64\ucrt64\share\qemu\edk2-x86_64-code.fd"
                    if os.path.exists(msys_efi):
                        efi_src = msys_efi
            elif platform.system() == "Darwin":
                candidates = [
                    "/opt/homebrew/share/qemu/edk2-x86_64-code.fd",
                    "/usr/local/share/qemu/edk2-x86_64-code.fd",
                    "/usr/share/qemu/OVMF.fd"
                ]
                efi_src = ""
                for c in candidates:
                    if os.path.exists(c):
                        efi_src = c
                        break
                if not efi_src:
                    efi_src = "/opt/homebrew/share/qemu/edk2-x86_64-code.fd" # Default fallback
            else:
                candidates = [
                    "/usr/share/qemu/OVMF.fd",
                    "/usr/share/OVMF/OVMF_CODE.fd",
                    "/usr/share/ovmf/OVMF_CODE.fd",
                    "/usr/share/edk2/x64/OVMF_CODE.4m.fd"
                ]
                efi_src = ""
                for c in candidates:
                    if os.path.exists(c):
                        efi_src = c
                        break
                if not efi_src:
                    efi_src = "/usr/share/qemu/OVMF.fd"
            vars_path = os.path.join(output_dir, vm_name + "-OVMF_VARS.fd")
            if config['snapshot'] and os.path.exists(vars_path):
                try: os.remove(vars_path)
                except OSError: pass

            if not os.path.exists(vars_path):
                create_sized_file(vars_path, 4)
            
            args_qemu.extend([
                "-drive", "if=pflash,format=raw,readonly=on,file={}".format(efi_src),
                "-drive", "if=pflash,format=raw,file={}".format(vars_path)
            ])

    # VNC and Monitor
    web_port = None
    if config['vnc'] != "off":
        try:
            start_disp = int(config['vnc']) if (config['vnc'] and not is_vnc_console) else 0
        except ValueError:
            start_disp = 0
        port = get_free_port(start=5900 + start_disp, end=5900 + 100)
        if port is None:
            fatal("No available VNC display ports")
        disp = port - 5900

        # Determine if VNC should listen on 0.0.0.0 or 127.0.0.1
        # It should only listen on 0.0.0.0 if user specified --vnc (and it's not off/console) AND --public is set.
        if vnc_user_specified and config['vnc'] not in ["off", "console"]:
            vnc_addr = p_addr  # Uses "" if --public else "127.0.0.1"
        else:
            vnc_addr = "127.0.0.1"

        # Add audio support if the vnc driver is available (and not in console-only mode)
        if not is_vnc_console and check_qemu_audio_backend(qemu_bin, "vnc"):
            if config['arch'] == "aarch64":
                 # Use usb-audio on aarch64 to avoid intel-hda driver issues
                 args_qemu.extend(["-device", "usb-audio,audiodev=vnc_audio"])
            else:
                 args_qemu.extend(["-device", "intel-hda", "-device", "hda-duplex"])
            args_qemu.extend(["-audiodev", "vnc,id=vnc_audio"])
            args_qemu.append("-display")
            args_qemu.append("vnc={}:{},audiodev=vnc_audio".format(vnc_addr, disp))
        else:
            args_qemu.append("-display")
            args_qemu.append("vnc={}:{}".format(vnc_addr, disp))

        # Use appropriate input devices for better VNC support
        if not is_vnc_console:
            if config['arch'] == "aarch64":
                args_qemu.extend(["-device", "usb-kbd", "-device", "virtio-tablet-pci"])
            else:
                args_qemu.extend(["-device", "usb-tablet"])

        # Prepare info for VNC Web Proxy
        web_port = get_free_port(start=6080, end=6180)
        if web_port:
            display_arch = config['arch'] if config['arch'] else host_arch
            vm_info = "-".join(filter(None, [config['os'], config['release'], display_arch]))
            
            if not config['qmon']:
                config['qmon'] = str(get_free_port(start=4444, end=4544))
                debuglog(config['debug'], "Auto-selected QEMU monitor port: {}".format(config['qmon']))
    else:
        args_qemu.extend(["-display", "none"])
    
    if config['qmon']:
        args_qemu.extend(["-monitor", "tcp:127.0.0.1:{},server,nowait,nodelay".format(config['qmon'])])

    # Always provide RNG to guest. Use rng-builtin as a cross-platform source of entropy.
    if config['os'] != "solaris":
        args_qemu.extend(["-object", "rng-builtin,id=rng0", "-device", "virtio-rng-pci,rng=rng0,max-bytes=1024,period=1000"])

    # Execution
    cmd_list = [qemu_bin] + args_qemu
    cmd_text = format_command_for_display(cmd_list)
    debuglog(config['debug'], "CMD:\n  " + cmd_text)

    # Auto-generate VNC password if not specified
    if not config['vnc_password'] and config['vnc'] != "off":
        import random, string
        config['vnc_password'] = ''.join(random.choices(string.ascii_letters, k=6))

    vnc_log_path = os.path.join(output_dir, "{}.vncproxy.log".format(vm_name))

    # Function to start (or restart) the VNC Web Proxy monitoring the given QEMU PID
    def start_vnc_proxy_for_pid(qemu_pid):
        if config['vnc'] != "off" and web_port:
            is_audio_enabled = check_qemu_audio_backend(qemu_bin, "vnc")
            proxy_args = [
                sys.executable, 
                os.path.abspath(__file__), 
                '--internal-vnc-proxy', 
                str(config['serialport'] if is_vnc_console else port), 
                str(web_port), 
                vm_info, 
                str(qemu_pid),
                '1' if is_audio_enabled else '0',
                config['qmon'] if config['qmon'] else "",
                vnc_log_path,
                '1' if is_vnc_console else '0',
                '0.0.0.0' if (config['public'] or config['public_vnc']) else ','.join(['127.0.0.1'] + get_private_ips()),
                str(config['remote_vnc']) if config['remote_vnc'] else '0',
                '1' if config['debug'] else '0',
                str(config['remote_vnc_link_file']) if config['remote_vnc_link_file'] else '0',
                config['vnc_password']
            ]
            popen_kwargs = {}
            if IS_WINDOWS:
                # CREATE_NO_WINDOW = 0x08000000, DETACHED_PROCESS = 0x00000008
                popen_kwargs['creationflags'] = 0x08000000 | 0x00000008
            else:
                popen_kwargs['start_new_session'] = True
            
            try:
                p = subprocess.Popen(proxy_args, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, **popen_kwargs)
                open_vnc_page(web_port)
                local_url = "http://localhost:{}".format(web_port)
                display_local_url = local_url
                if supports_ansi_color():
                    display_local_url = "\x1b[32m{}\x1b[0m".format(local_url)
                log("VNC Web UI available at {}".format(display_local_url))
                if config['vnc_password']:
                    pwd_display = config['vnc_password']
                    if supports_ansi_color():
                        pwd_display = "\x1b[33m{}\x1b[0m".format(pwd_display)
                    log("VNC password: {}".format(pwd_display))
                if not (config['public'] or config['public_vnc']):
                    lan_ips = get_private_ips()
                    for ip in lan_ips:
                        lan_url = "http://{}:{}".format(ip, web_port)
                        if supports_ansi_color():
                            lan_url = "\x1b[32m{}\x1b[0m".format(lan_url)
                        log("  Also accessible at {}".format(lan_url))

                # Start tunnel watcher thread if remote VNC is enabled
                if config.get('remote_vnc'):
                    t = threading.Thread(target=watch_vnc_tunnel_log, args=(vnc_log_path, tunnel_wait_stop, config.get('remote_vnc_is_default')))
                    t.daemon = True
                    t.start()
                return p
            except Exception as e:
                debuglog(config['debug'], "Failed to start VNC proxy process: {}".format(e))
                return None

    proxy_proc = None
    tunnel_wait_stop = threading.Event()
    
    # Pre-startup cleanup of VNC tunnel information
    try:
        if os.path.exists(vnc_log_path):
            os.remove(vnc_log_path)
        remote_file = config['remote_vnc_link_file'] if config['remote_vnc_link_file'] else vnc_log_path.replace(".vncproxy.log", ".remote")
        if os.path.exists(remote_file):
            os.remove(remote_file)
    except:
        pass

    if config['console']:
        proc = subprocess.Popen(cmd_list)
        proxy_proc = start_vnc_proxy_for_pid(proc.pid)
        proc.wait()
    else:
        # Background run
        try:
            proc = subprocess.Popen(cmd_list, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proxy_proc = start_vnc_proxy_for_pid(proc.pid)
        except OSError as e:
            fatal("Failed to start QEMU: {}".format(e))

        def fail_with_output(reason):
            stdout_data = proc.stdout.read() or b""
            stderr_data = proc.stderr.read() or b""
            err_msg = stderr_data.decode('utf-8', errors='replace').strip()
            out_msg = stdout_data.decode('utf-8', errors='replace').strip()
            combined = err_msg or out_msg or "(no output)"
            fatal("{} (code {}). Output:\n{}".format(reason, proc.returncode, combined))

        try:
            time.sleep(1)
            if proc.poll() is not None:
                fail_with_output("QEMU exited immediately")

            qemu_start_time = time.time()
            log("Started QEMU (PID: {})".format(proc.pid))
            
            tail_stop_event = threading.Event()
            should_tail = config['debug'] and serial_log_file
            
            # If we are likely to open a browser for Web VNC/Console, don't tail serial to terminal
            # to avoid clutter and redundant output.
            if should_tail and web_port and is_browser_available():
                should_tail = False
                debuglog(config['debug'], "Skipping serial terminal tail because Web VNC/Console is available.")
            
            if should_tail:
                 t = threading.Thread(target=tail_serial_log, args=(serial_log_file, tail_stop_event))
                 t.daemon = True
                 t.start()
            
            # Config SSH
            os.chmod(os.path.expanduser("~"), 0o755)
            ssh_dir = os.path.join(os.path.expanduser("~"), ".ssh")
            if not os.path.exists(ssh_dir):
                os.makedirs(ssh_dir)
                if IS_WINDOWS:
                    tighten_windows_permissions(ssh_dir)
                else:
                    os.chmod(ssh_dir, 0o700)
            
            if (config.get('sync') == 'sshfs' or config.get('accept_vm_ssh')) and vmpub_file and os.path.exists(vmpub_file):
                with open(vmpub_file, 'r') as f:
                    pub = f.read()
                with open(os.path.join(ssh_dir, "authorized_keys"), 'a') as f:
                    f.write(pub)

            conf_path = os.path.join(ssh_dir, "config.d")
            if not os.path.exists(conf_path):
                os.makedirs(conf_path)

            global_identity_block = ""
            if hostid_file:
                # Apply the VM key to all SSH hosts (requested behavior).
                global_identity_block = "Host *\n  ConnectTimeout 60\n  ConnectionAttempts 3\n  ServerAliveInterval 30\n  ServerAliveCountMax 6\n  IdentityFile {}\n  IdentityFile ~/.ssh/id_rsa\n  IdentityFile ~/.ssh/id_ed25519\n  IdentityFile ~/.ssh/id_ecdsa\n\n".format(
                    hostid_file,
                )

            def build_ssh_host_config(host_aliases):
                host_spec = " ".join(str(x) for x in host_aliases if x)
                host_block = "Host {}\n  StrictHostKeyChecking no\n  UserKnownHostsFile {}\n  ConnectTimeout 60\n  ConnectionAttempts 3\n  ServerAliveInterval 30\n  ServerAliveCountMax 6\n  User {}\n  HostName 127.0.0.1\n  Port {}\n".format(
                    host_spec,
                    SSH_KNOWN_HOSTS_NULL,
                    vm_user,
                    config['sshport'],
                )
                return "\n" + global_identity_block + host_block

            # Primary alias (vm_name)
            ssh_config_content = build_ssh_host_config([vm_name])

            vm_conf_file = os.path.join(conf_path, "{}.conf".format(vm_name))
            debuglog(config['debug'], "Generated SSH config (vm name) -> {}:\n{}".format(vm_conf_file, ssh_config_content.strip()))
            
            os.unlink(vm_conf_file) if os.path.exists(vm_conf_file) else None
            
            # Write config for VM name
            with open(vm_conf_file, 'w') as f:
                f.write(ssh_config_content)

            if IS_WINDOWS:
                tighten_windows_permissions(os.path.join(conf_path, "{}.conf".format(vm_name)))
            else:
                os.chmod(os.path.join(conf_path, "{}.conf".format(vm_name)), 0o600)

            # Write config for Port
            port_aliases = [str(config['sshport'])]
            if config.get('sshname'):
                port_aliases.append(config['sshname'])
            port_conf_content = build_ssh_host_config(port_aliases)

            port_conf_file = os.path.join(conf_path, "{}.conf".format(config['sshport']))
            debuglog(config['debug'], "Generated SSH config (port alias) -> {}:\n{}".format(port_conf_file, port_conf_content.strip()))
            
            os.unlink(port_conf_file) if os.path.exists(port_conf_file) else None
            
            with open(port_conf_file, 'w') as f: 
                 f.write(port_conf_content)
            
            if IS_WINDOWS:
                tighten_windows_permissions(os.path.join(conf_path, "{}.conf".format(config['sshport'])))
            else:
                os.chmod(os.path.join(conf_path, "{}.conf".format(config['sshport'])), 0o600)

            main_conf = os.path.join(ssh_dir, "config")
            if not os.path.exists(main_conf):
                open(main_conf, 'w').close()
                if IS_WINDOWS:
                  tighten_windows_permissions(main_conf)
                else:
                  os.chmod(main_conf, 0o600)
            
            with open(main_conf, 'r') as f:
                content = f.read()
                if "Include config.d" not in content:
                    with open(main_conf, 'a') as fa:
                        fa.write("\nInclude config.d/*.conf\n")

            # Wait for boot
            wait_msg = "Waiting for VM to boot (port {})...".format(config['sshport'])
            debuglog(config['debug'], wait_msg)
            success = False
            interactive_wait = sys.stdout.isatty()
            wait_start = time.time()
            last_wait_tick = [-1]  # hundredth-of-a-second ticks
            wait_timer_stop = threading.Event()
            wait_timer_thread = None

            def update_wait_timer():
                if not interactive_wait:
                    return
                tick = int((time.time() - wait_start) * 100)
                if tick == last_wait_tick[0]:
                    return
                last_wait_tick[0] = tick
                elapsed = tick / 100.0

                def supports_ansi_color(stream):
                    try:
                        if not hasattr(stream, "isatty") or not stream.isatty():
                            return False
                    except Exception:
                        return False
                    if os.environ.get("NO_COLOR") is not None:
                        return False
                    if os.environ.get("TERM") == "dumb":
                        return False
                    if IS_WINDOWS:
                        # Best-effort heuristics: modern terminals set one of these.
                        if os.environ.get("WT_SESSION"):
                            return True
                        if os.environ.get("ANSICON"):
                            return True
                        if os.environ.get("ConEmuANSI", "").upper() == "ON":
                            return True
                        if os.environ.get("TERM"):
                            return True
                        return False
                    return True

                use_color = supports_ansi_color(sys.stdout)
                green = "\x1b[32m"
                reset = "\x1b[0m"

                try:
                    cols = shutil.get_terminal_size(fallback=(80, 20)).columns
                except Exception:
                    cols = 80

                prefix = "{} {:.2f}s".format(wait_msg, elapsed)

                # Leave at least a small bar area; if the terminal is too narrow, just print the prefix.
                bar_total = max(0, cols - len(prefix) - 1)
                if bar_total < 10:
                    line = prefix
                    visible_len = len(prefix)
                else:
                    # Build a progress bar that repeatedly:
                    # 1) fills left->right to full
                    # 2) clears left->right back to empty
                    inner = max(1, bar_total - 2)  # brackets take 2 chars

                    speed_cells_per_sec = 18.0

                    bg_char = "░"

                    def shade_for_fraction(filled_fraction):
                        # filled_fraction: 0.0 (empty) .. 1.0 (full)
                        # Only render a fully solid block when truly full.
                        if filled_fraction >= 1.0:
                            return "█"
                        if filled_fraction >= 0.75:
                            return "▓"
                        if filled_fraction >= 0.50:
                            return "▒"
                        if filled_fraction >= 0.25:
                            return "░"
                        return bg_char

                    cells = [bg_char] * inner
                    bright = [False] * inner
                    if inner == 1:
                        # Tiny terminal; just blink between empty/full-ish
                        frac = (elapsed * speed_cells_per_sec) % 1.0
                        cells[0] = shade_for_fraction(frac)
                        bright[0] = (cells[0] != bg_char)
                    else:
                        # One full cycle = fill across inner cells, then clear across inner cells.
                        fill_duration = float(inner) / max(0.001, speed_cells_per_sec)
                        cycle = 2.0 * fill_duration
                        t = elapsed % cycle

                        if t < fill_duration:
                            # Filling: boundary moves from 0 -> inner
                            boundary = t * speed_cells_per_sec
                            # Prevent float rounding from hitting the next cell early.
                            if boundary >= inner:
                                boundary = inner - 1e-9
                            full = int(boundary)
                            frac = boundary - full
                            for idx in range(min(full, inner)):
                                cells[idx] = "█"
                                bright[idx] = True
                            if 0 <= full < inner:
                                cells[full] = shade_for_fraction(frac)
                                bright[full] = (frac > 0.0)
                        else:
                            # Clearing: left edge moves from 0 -> inner
                            cleared = (t - fill_duration) * speed_cells_per_sec
                            if cleared >= inner:
                                cleared = inner - 1e-9
                            full_empty = int(cleared)
                            frac = cleared - full_empty
                            # left side empty
                            for idx in range(min(full_empty, inner)):
                                cells[idx] = bg_char
                                bright[idx] = False
                            # boundary cell fades out as we clear
                            if 0 <= full_empty < inner:
                                cells[full_empty] = shade_for_fraction(1.0 - frac)
                                bright[full_empty] = (frac < 1.0)
                            # remaining cells stay filled
                            for idx in range(full_empty + 1, inner):
                                cells[idx] = "█"
                                bright[idx] = True

                    bar_text = "[{}]".format("".join(cells))
                    if use_color:
                        dim_green = "\x1b[2;32m"
                        bar_cells = []
                        current_bright = None
                        for idx, ch in enumerate(cells):
                            want_bright = bright[idx]
                            if want_bright != current_bright:
                                bar_cells.append(green if want_bright else dim_green)
                                current_bright = want_bright
                            bar_cells.append(ch)
                        bar_render = "[" + "".join(bar_cells) + reset + "]"
                    else:
                        bar_render = bar_text
                    line = "{} {}".format(prefix, bar_render)
                    visible_len = len(prefix) + 1 + len(bar_text)

                # Pad to clear any leftover chars from previous frame.
                if cols and visible_len < cols:
                    line = line + (" " * (cols - visible_len))

                sys.stdout.write("\r" + line)
                sys.stdout.flush()


            def wait_timer_worker():
                while not wait_timer_stop.is_set():
                    update_wait_timer()
                    # 15 updates per second
                    time.sleep(1.0 / 15.0)

            if interactive_wait:
                wait_timer_thread = threading.Thread(target=wait_timer_worker)
                wait_timer_thread.daemon = True
                wait_timer_thread.start()

            def finish_wait_timer():
                if not interactive_wait or last_wait_tick[0] < 0:
                    return
                # Render a final, fully-filled bar so the last frame doesn't look partial.
                elapsed = last_wait_tick[0] / 100.0


                use_color = supports_ansi_color(sys.stdout)
                green = "\x1b[32m"
                reset = "\x1b[0m"

                try:
                    cols = shutil.get_terminal_size(fallback=(80, 20)).columns
                except Exception:
                    cols = 80

                prefix = "{} {:.2f}s".format(wait_msg, elapsed)
                bar_total = max(0, cols - len(prefix) - 1)
                if bar_total < 10:
                    line = prefix
                    visible_len = len(prefix)
                else:
                    inner = max(1, bar_total - 2)
                    bar_text = "[{}]".format("█" * inner)
                    if use_color:
                        bar_render = "[" + green + ("█" * inner) + reset + "]"
                    else:
                        bar_render = bar_text
                    line = "{} {}".format(prefix, bar_render)
                    visible_len = len(prefix) + 1 + len(bar_text)

                if cols and visible_len < cols:
                    line = line + (" " * (cols - visible_len))

                sys.stdout.write("\r" + line + "\n")
                sys.stdout.flush()
            
            ssh_base_cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile={}".format(SSH_KNOWN_HOSTS_NULL),
                "-o", "LogLevel=ERROR",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10",
            ]
            if hostid_file:
                ssh_base_cmd.extend(["-i", hostid_file])
            
            ssh_base_cmd.extend([
                "-p", str(config['sshport']),
                "{}@127.0.0.1".format(vm_user)
            ])
            
            boot_timeout_seconds = config['boot_timeout_sec']
            probe_timeout_sec = 5 if (IS_WINDOWS or config['arch'] == "aarch64") else 3
            boot_start_time = time.time()

            # Fallback for stale-DHCP-lease scenarios: if the VM ends up with an IP
            # different from dhcpstart (= what hostfwd was wired to at QEMU launch),
            # rebind hostfwd at runtime via the monitor. Only attempted once per boot.
            hostfwd_guard_done = False
            hostfwd_guard_last_check = 0.0
            last_boot_progress_log = -1.0
            last_probe_result = "(none yet)"

            debuglog(config['debug'], "Boot wait begin: QEMU PID={}, timeout={}s, probe_timeout={}s, qmon={}, ssh_port={}".format(
                proc.pid, boot_timeout_seconds, probe_timeout_sec, config.get('qmon') or '<unset>', config['sshport']))

            while True:
                if proc.poll() is not None:
                    fail_with_output("QEMU terminated during boot")

                elapsed = time.time() - boot_start_time
                if elapsed >= boot_timeout_seconds:
                    break

                if elapsed - last_boot_progress_log >= 15:
                    debuglog(config['debug'], "Boot wait: elapsed={:.1f}s/{}s, last probe={}".format(
                        elapsed, boot_timeout_seconds, last_probe_result))
                    last_boot_progress_log = elapsed

                ret, timed_out = call_with_timeout(
                    ssh_base_cmd + ["exit"],
                    timeout_seconds=probe_timeout_sec,
                    stdout=DEVNULL,
                    stderr=DEVNULL
                )
                last_probe_result = "rc={} timed_out={}".format(ret, timed_out)
                if ret == 0:
                    success = True
                    break

                # While waiting for SSH to come up, periodically poll the QEMU monitor
                # to detect if the VM got an unexpected IP and fix hostfwd accordingly.
                if (not hostfwd_guard_done and config.get('qmon') and hostfwd_specs
                        and elapsed >= 10 and (time.time() - hostfwd_guard_last_check) >= 5):
                    hostfwd_guard_last_check = time.time()
                    actual_ip = get_vm_ip_from_monitor(config['qmon'])
                    if actual_ip:
                        if actual_ip == SLIRP_EXPECTED_GUEST_IP:
                            debuglog(config['debug'], "VM IP {} matches expected, hostfwd OK".format(actual_ip))
                            hostfwd_guard_done = True
                        else:
                            log("VM has IP {} (expected {}); rewriting hostfwd via monitor.".format(actual_ip, SLIRP_EXPECTED_GUEST_IP))
                            if rewrite_hostfwd_target(config['qmon'], hostfwd_specs, actual_ip, debug=config['debug']):
                                log("Hostfwd rewritten to target {}.".format(actual_ip))
                                hostfwd_guard_done = True

                if timed_out:
                    continue

            
            wait_timer_stop.set()
            tunnel_wait_stop.set()
            if wait_timer_thread:
                wait_timer_thread.join(0.2)
            finish_wait_timer()
            
            tunnel_url = None
            tunnel_service = "Remote"
            if config['remote_vnc']:
                # Attempt to find and display the Cloudflare Tunnel URL from the proxy log
                vnc_log = os.path.join(output_dir, "{}.vncproxy.log".format(vm_name))
                # Give it a tiny bit of time to settle if it just finished
                if os.path.exists(vnc_log):
                    try:
                        with open(vnc_log, 'r') as f:
                            log_text = f.read()
                            match = re.search(r"Open this link to access WebVNC \(via ([^)]+)\): (https?://[^\s]+)", log_text)
                            if match:
                                tunnel_service = match.group(1)
                                tunnel_url = match.group(2)
                                display_url = tunnel_url
                                if supports_ansi_color():
                                    display_url = "\x1b[32m{}\x1b[0m".format(tunnel_url)
                                # Redundant log removed, already handled by watch_vnc_tunnel_log
                            else:
                                # Check for errors
                                err_match = re.search(r"(?:Cloudflare )?Tunnel Error: (.*)", log_text)
                                if err_match:
                                    log("Tunnel Error: {}".format(err_match.group(1)))
                                else:
                                    log("Remote Tunnel failed to provide a URL. Check {} for details.".format(vnc_log))
                    except:
                        pass
            
            if not success:
                # First timeout - dump diagnostics, then kill QEMU and retry once
                log("Boot timed out after {} seconds. Killing QEMU and retrying...".format(boot_timeout_seconds))
                _dump_boot_debug_snapshot(config, "first-timeout", serial_log_file, config.get('qmon'), output_dir, vm_name, proc, cmd_list=cmd_list)
                terminate_process(proc, "QEMU")
                if proxy_proc:
                    terminate_process(proxy_proc, "VNC Proxy")
                # Wait for old proxy to exit
                time.sleep(1.5)
                
                # Restart QEMU
                try:
                    proc = subprocess.Popen(cmd_list, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    proxy_proc = start_vnc_proxy_for_pid(proc.pid)
                except OSError as e:
                    fatal("Failed to restart QEMU: {}".format(e))
                
                time.sleep(1)
                if proc.poll() is not None:
                    fail_with_output("QEMU exited immediately on retry")
                
                log("Restarted QEMU (PID: {}), waiting for boot (retry)...".format(proc.pid))
                
                # Reset wait timer for retry
                wait_start = time.time()
                last_wait_tick[0] = -1
                wait_timer_stop.clear()
                if interactive_wait:
                    wait_timer_thread = threading.Thread(target=wait_timer_worker)
                    wait_timer_thread.daemon = True
                    wait_timer_thread.start()
                
                # Second boot attempt -- QEMU restarted from scratch, so hostfwd was
                # reset to its initial guest-IP target. Reset the guard so we check again.
                boot_start_time = time.time()
                success = False
                hostfwd_guard_done = False
                hostfwd_guard_last_check = 0.0
                last_boot_progress_log = -1.0
                last_probe_result = "(none yet)"

                debuglog(config['debug'], "Boot wait begin (retry): QEMU PID={}, timeout={}s, probe_timeout={}s, qmon={}, ssh_port={}".format(
                    proc.pid, boot_timeout_seconds, probe_timeout_sec, config.get('qmon') or '<unset>', config['sshport']))

                while True:
                    if proc.poll() is not None:
                        fail_with_output("QEMU terminated during boot (retry)")

                    elapsed = time.time() - boot_start_time
                    if elapsed >= boot_timeout_seconds:
                        break

                    if elapsed - last_boot_progress_log >= 15:
                        debuglog(config['debug'], "Boot wait (retry): elapsed={:.1f}s/{}s, last probe={}".format(
                            elapsed, boot_timeout_seconds, last_probe_result))
                        last_boot_progress_log = elapsed

                    ret, timed_out = call_with_timeout(
                        ssh_base_cmd + ["exit"],
                        timeout_seconds=probe_timeout_sec,
                        stdout=DEVNULL,
                        stderr=DEVNULL
                    )
                    last_probe_result = "rc={} timed_out={}".format(ret, timed_out)
                    if ret == 0:
                        success = True
                        break

                    if (not hostfwd_guard_done and config.get('qmon') and hostfwd_specs
                            and elapsed >= 10 and (time.time() - hostfwd_guard_last_check) >= 5):
                        hostfwd_guard_last_check = time.time()
                        actual_ip = get_vm_ip_from_monitor(config['qmon'])
                        if actual_ip:
                            if actual_ip == SLIRP_EXPECTED_GUEST_IP:
                                debuglog(config['debug'], "VM IP {} matches expected (retry)".format(actual_ip))
                                hostfwd_guard_done = True
                            else:
                                log("VM has IP {} (expected {}); rewriting hostfwd via monitor (retry).".format(actual_ip, SLIRP_EXPECTED_GUEST_IP))
                                if rewrite_hostfwd_target(config['qmon'], hostfwd_specs, actual_ip, debug=config['debug']):
                                    log("Hostfwd rewritten to target {}.".format(actual_ip))
                                    hostfwd_guard_done = True

                    if timed_out:
                        continue
                    time.sleep(2)
                
                wait_timer_stop.set()
                if wait_timer_thread:
                    wait_timer_thread.join(0.2)
                finish_wait_timer()
                
                if not success:
                    terminate_process(proc, "QEMU")
                    fatal("Boot timed out after retry. Giving up.")
            
            qemu_elapsed = time.time() - qemu_start_time
            debuglog(config['debug'], "VM Ready! Boot took {:.2f} seconds. Connect with: ssh {}".format(qemu_elapsed, vm_name))
            
            # illumos DNS: the guest's DNS (slirp's built-in proxy x.x.x.3, handed
            # out via DHCP) drops empty AAAA (NODATA) replies for IPv4-only hosts,
            # so getaddrinfo() hangs ~15s and name resolution intermittently fails
            # (e.g. pkg E_COULDNT_RESOLVE_HOST). Point the resolver at public DNS.
            # This must run BEFORE sync_vm_time() below, which resolves NTP hosts
            # (pool.ntp.org etc.). Done post-boot so nwam (which rewrites resolv.conf
            # from DHCP at boot) does not clobber it.
            if config['os'] in ('omnios', 'openindiana', 'solaris'):
                debuglog(config['debug'], "[trace] writing /etc/resolv.conf on {} ...".format(config['os']))
                p = subprocess.Popen(ssh_base_cmd + ["sh"], stdin=subprocess.PIPE)
                p.communicate(input=b'echo "nameserver 1.1.1.1" > /etc/resolv.conf; echo "nameserver 8.8.8.8" >> /etc/resolv.conf\n')
                p.wait()
                debuglog(config['debug'], "[trace] illumos resolv.conf rc={}".format(p.returncode))

            # Sync VM time with host if requested
            should_sync = config['synctime']
            if should_sync is None:
                # Default behavior: Only sync for DragonFlyBSD and Solaris family
                if config['os'] in ['dragonflybsd', 'solaris', 'omnios', 'openindiana']:
                    should_sync = True
                else:
                    should_sync = False
            
            if should_sync:
                # On slow emulated systems (like Apple Silicon running x86), 
                # Solaris/OpenIndiana services might need a moment to settle after SSH becomes responsive
                # to avoid 'logout without login' audit errors.
                is_apple_silicon = (platform.system() == 'Darwin' and platform.machine() == 'arm64')
                if is_apple_silicon and config['os'] == 'openindiana':
                    log("Apple Silicon detected: waiting 5s for OpenIndiana services to settle...")
                    time.sleep(5)
                sync_vm_time(config, ssh_base_cmd)
                debuglog(config['debug'], "[trace] sync_vm_time returned")

            # Defensive: brief pause before opening more SSH sessions. Without
            # this we saw flaky "remote host has disconnected" on MidnightBSD,
            # likely because the previous SSH session's utmp/logout/PAM
            # accounting hadn't fully settled. The symptom is timing sensitive
            # and was masked by debug-log overhead, so we explicitly insert a
            # small cushion here that applies to all guests.
            time.sleep(0.5)
            debuglog(config['debug'], "[trace] post-sync settle delay done")

            # Post-boot config: Setup reverse SSH config inside VM
            debuglog(config['debug'], "[trace] entering post-boot config block")
            current_user = getpass.getuser()
            host_port_line = ""
            if not config['hostsshport']:
                config['hostsshport'] = detect_host_ssh_port()
                if config['hostsshport']:
                    debuglog(config['debug'], "Detected host SSH port {}".format(config['hostsshport']))
            if config['hostsshport']:
                host_port_line = "  Port {}\n".format(config['hostsshport'])
            debuglog(config['debug'], "[trace] sync={!r} accept_vm_ssh={} -> will inject VM .ssh/config: {}".format(
                config.get('sync'), config.get('accept_vm_ssh'),
                config.get('sync') == 'sshfs' or config.get('accept_vm_ssh')))
            if config.get('sync') == 'sshfs' or config.get('accept_vm_ssh'):
                vm_ssh_config = """
StrictHostKeyChecking=no

Host host
  HostName  192.168.122.2
{host_port}  User {user}
  ServerAliveInterval 10
""".format(host_port=host_port_line, user=current_user)

                debuglog(config['debug'], "[trace] injecting VM .ssh/config via ssh ...")
                p = subprocess.Popen(ssh_base_cmd + ["cat - > .ssh/config"], stdin=subprocess.PIPE)
                p.communicate(input=vm_ssh_config.encode('utf-8'))
                p.wait()
                debuglog(config['debug'], "[trace] VM .ssh/config injection rc={}".format(p.returncode))
            # Mount Shared Folders
            debuglog(config['debug'], "[trace] vpaths={!r} sync={!r} -> will mount: {}".format(
                config['vpaths'], config.get('sync'),
                bool(config['vpaths']) and config['sync'] != 'no'))
            if config['vpaths'] and config['sync'] != 'no':
                sudo_cmd = []
                if config['sync'] == 'nfs':
                    # Check if sudo exists in path (unix only)
                    if not IS_WINDOWS:
                        try:
                            with open(os.devnull, 'w') as devnull:
                                if subprocess.call("command -v sudo", shell=True, stdout=devnull, stderr=devnull) == 0:
                                     sudo_cmd = ["sudo"]
                        except:
                            pass

                for vpath_str in config['vpaths']:
                    try:
                        if ':' not in vpath_str:
                            raise ValueError
                        debuglog(config['debug'], "Processing -v argument: {}".format(vpath_str))
                        vhost, vguest = vpath_str.rsplit(':', 1)
                        vhost = os.path.abspath(vhost)
                        if not vhost or not vguest:
                            raise ValueError
                        
                        excludes = []
                        for ex_dir in [working_dir, config.get('cachedir')]:
                            if ex_dir:
                                try:
                                    if os.path.commonpath([vhost, ex_dir]) == vhost:
                                        rel = os.path.relpath(ex_dir, vhost)
                                        if rel != "." and not rel.startswith(".."):
                                            excludes.append(rel)
                                except ValueError:
                                    pass

                        debuglog(config['debug'], "Mounting host dir: {} to guest: {}".format(vhost, vguest))
                        if excludes:
                            debuglog(config['debug'], "Excluding paths from sync: {}".format(", ".join(excludes)))
                        
                        if config['sync'] == 'nfs':
                            sync_nfs(ssh_base_cmd, vhost, vguest, config['os'], sudo_cmd)
                        elif config['sync'] == 'rsync':
                            sync_rsync(ssh_base_cmd, vhost, vguest, config['os'], output_dir, vm_name, excludes=excludes)
                        elif config['sync'] == 'scp':
                            sync_scp(ssh_base_cmd, vhost, vguest, config['sshport'], hostid_file, vm_user, excludes=excludes)
                        else:
                            sync_sshfs(ssh_base_cmd, vhost, vguest, config['os'], config.get('release'))

                    except ValueError:
                        log("Invalid format for -v. Use host_path:guest_path")

            if config['console']:
                 log("======================================")
                 log("")
                 log("You can login the vm with: ssh " + vm_name)
                 log("Or just:  ssh " + str(config['sshport']))
                 if config.get('sshname'):
                     log("Or just:  ssh " + str(config['sshname']))
                 if web_port:
                     local_url = "http://localhost:{}".format(web_port)
                     display_local_url = local_url
                     if supports_ansi_color():
                         display_local_url = "\x1b[32m{}\x1b[0m".format(local_url)
                     log("VNC Web UI: {}".format(display_local_url))
                     if tunnel_url:
                         display_url = tunnel_url
                         if supports_ansi_color():
                             display_url = "\x1b[32m{}\x1b[0m".format(tunnel_url)
                         log("WebVNC ({}): {}".format(tunnel_service, display_url))
                         if config.get('remote_vnc_is_default'):
                             log("Notice: Remote VNC tunnel is enabled by default as no local browser was detected.")
                             log("        Use '--remote-vnc off' to disable it.")
                 log("======================================")

            debuglog(config['debug'], "[trace] reached final-SSH gate, detach={} console={}".format(
                config['detach'], config['console']))
            if not config['detach']:
                ssh_cmd = ssh_base_cmd + ssh_passthrough
                debuglog(config['debug'], "SSH command: {}".format(format_command_for_display(ssh_cmd)))
                # Skip the final interactive SSH when there's nothing to run AND
                # stdin isn't a TTY (typical CI environment). An empty session
                # with EOF stdin makes some guests' login/csh hang on logout
                # (observed on MidnightBSD 3.2.4), which then blocks anyvm
                # forever. Users running interactively still get the shell;
                # users with `-- cmd ...` still get their command executed.
                stdin_is_tty = bool(hasattr(sys.stdin, 'isatty') and sys.stdin.isatty())
                stdout_is_tty = bool(hasattr(sys.stdout, 'isatty') and sys.stdout.isatty())
                skip_final_ssh = (not ssh_passthrough) and (not stdin_is_tty)
                debuglog(config['debug'], "[trace] final-SSH decision: ssh_passthrough={!r} stdin_tty={} stdout_tty={} skip={}".format(
                    ssh_passthrough, stdin_is_tty, stdout_is_tty, skip_final_ssh))
                if skip_final_ssh:
                    debuglog(config['debug'], "Skipping final interactive SSH: non-TTY stdin and no passthrough command.")
                else:
                    debuglog(config['debug'], "[trace] final-SSH calling subprocess.call ...")
                    rc = subprocess.call(ssh_cmd)
                    debuglog(config['debug'], "[trace] final-SSH returned rc={}".format(rc))
            else:
                debuglog(config['debug'], "[trace] detach mode -- skipping final SSH")
            # Avoid noisy banner when running as PID 1 inside a container or if QEMU already exited
            if os.getpid() != 1:
                if not config['detach']:
                    # Give a moment for QEMU to fully exit if it was powered off
                    time.sleep(1)
                if is_pid_alive_main(proc.pid):
                    log("======================================")
                    log("The VM is still running in background.")
                    log("You can login the VM with:  ssh " + vm_name)
                    log("Or just:  ssh " + str(config['sshport']))
                    if config.get('sshname'):
                        log("Or just:  ssh " + str(config['sshname']))
                    if web_port:
                        local_url = "http://localhost:{}".format(web_port)
                        display_local_url = local_url
                        if supports_ansi_color():
                            display_local_url = "\x1b[32m{}\x1b[0m".format(local_url)
                        log("VNC Web UI: {}".format(display_local_url))
                        if not (config['public'] or config['public_vnc']):
                            for ip in get_private_ips():
                                lan_url = "http://{}:{}".format(ip, web_port)
                                if supports_ansi_color():
                                    lan_url = "\x1b[32m{}\x1b[0m".format(lan_url)
                                log("  Also accessible at {}".format(lan_url))
                    if tunnel_url:
                        display_url = tunnel_url
                        if supports_ansi_color():
                            display_url = "\x1b[32m{}\x1b[0m".format(tunnel_url)
                        log("WebVNC ({}): {}".format(tunnel_service, display_url))
                        if config.get('remote_vnc_is_default'):
                            log("Notice: Remote VNC tunnel is enabled by default as no local browser was detected.")
                            log("        Use '--remote-vnc off' to disable it.")
                    log("======================================")
                else:
                    log("VM has exited")
        except KeyboardInterrupt:
            if not config['detach']:
                terminate_process(proc, "QEMU")
                if 'proxy_proc' in locals() and proxy_proc:
                    # On Windows, wait a bit for proxy to exit gracefully via its own monitor
                    if IS_WINDOWS:
                        start_wait = time.time()
                        while time.time() - start_wait < 3:
                            if proxy_proc.poll() is not None: break
                            time.sleep(0.5)
                    terminate_process(proxy_proc, "VNC Proxy")
            raise

def is_pid_alive_main(pid):
    """Helper for the main process to check PID status (duplicated to avoid circular/proxy dependency issues)"""
    try:
        if os.name == 'nt':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h_process = kernel32.OpenProcess(0x1000, False, pid)
            if h_process:
                exit_code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
                kernel32.CloseHandle(h_process)
                return (exit_code.value == 259) # STILL_ACTIVE
            return False
        else:
            try:
                os.kill(pid, 0)
                if os.path.exists("/proc/{}/status".format(pid)):
                    with open("/proc/{}/status".format(pid), 'r') as f:
                        for line in f:
                            if line.startswith("State:"):
                                if "Z (zombie)" in line: return False
                                break
                return True
            except OSError as e:
                import errno
                return e.errno == errno.EPERM
            except: return False
    except: return False

if __name__ == '__main__':
    main()
