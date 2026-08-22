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
import tarfile
import tempfile
import re
import select
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
    from urllib.request import ProxyHandler, build_opener, install_opener
    from urllib.request import HTTPHandler, HTTPSHandler, proxy_bypass
    from urllib.error import HTTPError, URLError
    from urllib.parse import urljoin, urlsplit, unquote
    import http.client as http_client
    input_func = input
except ImportError:
    # Python 2
    from urllib2 import urlopen, Request, HTTPError, URLError
    from urllib2 import ProxyHandler, build_opener, install_opener
    from urllib2 import HTTPHandler, HTTPSHandler
    from urllib import proxy_bypass, unquote
    from urlparse import urljoin, urlsplit
    # The native SOCKS5 client below needs the Python 3 http.client /
    # ssl.SSLContext plumbing; disable it on Python 2.
    http_client = None
    
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

# True when this copy is a frozen single-file executable -- the PyInstaller
# build published as anyvm-windows-x64.exe and packaged for winget. The
# bootloader sets sys.frozen, so this is still the packager telling us rather
# than a guess about our own path (same principle as INSTALLED below). Two
# things differ when frozen: sys.executable is anyvm.exe itself instead of a
# Python interpreter, and __file__ points into the bootloader's throwaway
# extraction directory instead of at a real script.
FROZEN = bool(getattr(sys, "frozen", False))

# Set to True by the entry points that packaging installs (see main_installed()
# and [project.scripts] in pyproject.toml, plus the Homebrew formula's wrapper).
# The packager knows it packaged this; anyvm deliberately does NOT try to work
# it out from its own path. Matching "site-packages" in __file__ misclassifies a
# vendored copy or a checkout that merely lives under such a path, and the only
# symptom is that images quietly appear somewhere the user did not expect.
# A frozen build is packaged by definition and has no wrapper script to set the
# environment variable, so it counts as installed on its own.
INSTALLED = FROZEN or bool(os.environ.get("ANYVM_INSTALLED"))

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
    "freebsd": "2.2.6",
    "openbsd": "2.1.0",
    "netbsd": "2.2.3",
    "dragonflybsd": "2.0.7",
    "solaris": "2.0.7",
    "omnios": "2.1.3",
    "haiku": "2.0.3",
    "midnightbsd": "2.0.7",
    "tribblix": "2.0.7",
    "openindiana": "2.1.2",
    "ubuntu": "2.0.9",
    "openeuler": "2.0.2",
    "ghostbsd": "2.0.8",
    "blissos": "2.0.3",
    "hurd": "2.0.1",
    "plan9": "2.0.1",
    "nextbsd": "2.0.1",
    "reactos": "2.0.2",
    "riscos": "2.0.0",
    "redox": "2.0.1"
}

# Pinned, self-contained QEMU builds published as release assets by
# ubuntu-builder's release-files job (Linux x86_64 binaries built on/for
# ubuntu noble; see that repo's files/README.md). The builder no longer
# commits these tarballs to git -- its release-files job compiles them on
# the fly with files/build-qemu10.sh and uploads them to the release, so
# the release asset is the only place to fetch them. Downloaded on demand
# by ensure_pinned_qemu() when the system QEMU is too old for a guest (the
# asset file names are explicit on purpose -- they match
# ubuntu-builder/.github/data/uploadfiles.yml one to one).
PINNED_QEMU_ASSETS = {
    "riscv64": "qemu-10.2.3-riscv64-noble.tar.zst",
    "s390x": "qemu-10.2.3-s390x-noble.tar.zst",
    "ppc64le": "qemu-10.2.3-ppc64le-noble.tar.zst",
    "loongarch64": "qemu-10.2.3-loongarch64-noble.tar.zst",
    # 10.2.3 PLUS the builder's own files/qemu-sabre-irq-clobber.patch: every
    # released qemu-system-sparc64 carries the sun4u sabre IRQ-dispatch
    # clobber bug (see the sparc64 branch at the ensure_pinned_qemu call
    # site), so this pin is preferred REGARDLESS of the system version.
    "sparc64": "qemu-10.2.3-sparc64-noble.tar.zst",
    # 10.2.3 PLUS riscos-builder's files/qemu-riscos-raspi.patch. Unlike the
    # pins above this is not "newer than the distro build" -- NO released QEMU
    # can boot RISC OS on any raspi machine, so this one is mandatory rather
    # than preferred. Eight fixes plus a new usb-net-smsc95xx device model
    # (the SMSC LAN9512 is the Pi 2's real NIC and the only one RISC OS can
    # drive); four of the eight are generic QEMU defects, three in dwc2 and
    # one in bcm2835_dma where a non-multiple-of-four length wraps a uint32_t
    # and spins ~2^30 times over guest memory.
    "armv7": "qemu-10.2.3-riscos-arm-noble.tar.zst",
}
# There is deliberately NO table mapping an arch to the repo that publishes
# its pinned QEMU. The asset is always fetched from the guest's OWN builder,
# at the guest's OWN pinned release (the same builder_repo + config['builder']
# the image itself came from). Every builder builds and publishes whatever
# its own guests need -- two builders that need the same patched QEMU each
# ship their own copy on purpose. That is what keeps builders and VM actions
# mutually independent: deleting any one builder must not affect any other.
# A per-arch table would quietly reintroduce the coupling the moment a second
# OS gained that arch, so the repo is derived, never looked up.

# Patched OpenBIOS published as a release asset by the openbsd builder (see
# that repo's bios/README.md): the OpenBIOS bundled with QEMU crashes every
# OpenBSD >= 7.3 sparc64 kernel on cold boot and names IDE channel nodes
# "ide" instead of OBP's "ata", which breaks root-device autodetection.
# The builder no longer commits the blob to git -- its release-files job
# rebuilds it from source (bios/build-openbios.sh) and uploads it to the
# release, so the release asset is the only place to fetch it. Downloaded
# on demand for openbsd/sparc64 guests and passed via -bios. Like every
# other pinned asset it is fetched from the image's own builder_repo at the
# image's own release, so only the file name is a constant here.
OPENBIOS_SPARC64_ASSET = "openbios-sparc64.elf"

# The RISC OS ROM, published as a release asset by riscos-builder. The raspi
# machines have no firmware of their own for QEMU to fall back on, so this is
# mandatory rather than a patched replacement. It cannot be read out of the
# qcow2 at run time even though a copy lives in the image's FAT boot
# partition: that would mean parsing a partition table and a FAT volume on
# every start. Fetched from the image's own builder at the image's own
# release, like every other pinned asset.
RISCOS_ROM_ASSET = "RISCOS.IMG"

# Pinned user-space NFS server (github.com/anyvm-org/nfsd): one pure-Python
# stdlib-only file serving NFSv3/v4.0/v4.1 plus a portmapper (-pmap), runs
# on Linux/macOS/Windows without root and without a kernel nfsd. Downloaded
# on demand from the release asset: it is the default backend for
# `--sync nfs` (alias: mynfs); `--sync sys-nfs` forces the host kernel NFS
# server instead.
MYNFSD_VERSION = "0.1.0"
MYNFSD_URL = ("https://github.com/anyvm-org/nfsd/releases/download/"
              "v{}/nfsd.py".format(MYNFSD_VERSION))

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

def user_cache_dir():
    """Per-user cache directory, following each platform's convention."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "anyvm")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Caches", "anyvm")
    base = os.environ.get("XDG_CACHE_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "anyvm")

def self_argv():
    """The argv prefix that re-runs this same program.

    anyvm re-execs itself for its two detached helpers (--internal-nfsd and
    --internal-vnc-proxy). As a script that is "<python> <this file>"; frozen
    it is just the executable, because sys.executable IS anyvm and __file__
    then points into the bootloader's extraction directory -- passing it on
    would hand the child a bogus first argument, and that directory is deleted
    when the parent exits anyway.
    """
    if FROZEN:
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]

def self_home():
    """The directory this program was started from (its own file, or the
    frozen executable -- never the bootloader's extraction directory)."""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

def python_argv(script_path):
    """The argv prefix that runs a SEPARATE Python script (nfsd.py).

    Not plain sys.executable when frozen: there sys.executable is anyvm.exe,
    which would swallow the script path as its own first argument. The frozen
    build carries a whole CPython, so it re-enters itself and runs the script
    in-process instead -- which keeps that build's "no Python on the machine"
    promise true for --sync nfs as well. The PyInstaller build therefore has
    to bundle the stdlib modules nfsd.py needs but anyvm.py does not import
    itself; they are listed as --hidden-import in .github/workflows/winget.yml.
    """
    if FROZEN:
        return [sys.executable, "--internal-run-python", script_path]
    return [sys.executable, script_path]

def run_internal_python(script_path, script_args):
    """--internal-run-python: execute another Python script in this process.

    Only the frozen build ever takes this path (see python_argv). runpy gives
    the script the __main__ name and a sys.argv that starts at its own path,
    which is what a script invoked as "python script.py ..." sees.
    """
    import runpy
    sys.argv = [script_path] + list(script_args)
    runpy.run_path(script_path, run_name="__main__")

def default_data_dir(script_home):
    """Pick the default --data-dir.

    A source checkout keeps the historical <checkout>/output, so nothing
    changes for anyone working in the tree.  An installed copy goes to the
    per-user cache instead, which is what these files are -- every image can be
    fetched again from its builder's release.
    """
    if INSTALLED:
        return os.path.join(user_cache_dir(), "images")
    return os.path.join(script_home, "output")

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

def open_vnc_page(web_port, debug=False):
    """Automatically open the VNC web page in the browser based on environment."""
    if not web_port or not is_browser_available():
        return

    def _open_in_background():
        # Give VNC proxy/QEMU a moment to initialize ports properly
        time.sleep(1)
        url = "http://localhost:{}".format(web_port)
        launcher = None
        try:
            if IS_WINDOWS or (platform.system() == 'Linux' and 'microsoft' in open('/proc/version').read().lower()):
                # Windows or WSL
                launcher = 'explorer.exe'
                subprocess.Popen([launcher, url], shell=IS_WINDOWS)
            elif platform.system() == 'Darwin':
                launcher = 'open'
                subprocess.Popen([launcher, url], stdout=DEVNULL, stderr=DEVNULL)
            elif platform.system() == 'Linux':
                launcher = 'xdg-open'
                subprocess.Popen([launcher, url], stdout=DEVNULL, stderr=DEVNULL)
        except Exception as e:
            # Never fatal -- the URL is printed anyway. But stay visible under
            # --debug: on WSL a missing WSLInterop binfmt entry makes every
            # Windows .exe fail with "Exec format error", and a silent pass
            # here makes that look like anyvm simply chose not to open a
            # browser. Re-register with:
            #   sudo sh -c 'echo ":WSLInterop:M::MZ::/init:PF" \
            #       > /proc/sys/fs/binfmt_misc/register'
            debuglog(debug, "Failed to open browser via {}: {}: {}".format(
                launcher or "(no launcher)", type(e).__name__, e))

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
        # arriving on this port always require the password -- the IP-based bypass
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
Usage: python anyvm.py [OPTIONS]

Description:
  Automated QEMU VM launcher script. Downloads images/keys and boots a VM 
  with SSH access and folder synchronization.

Options:
  --os <name>            Operating System name (Required).
                         Supported: freebsd, ghostbsd, midnightbsd, nextbsd, openbsd, netbsd,
                                    dragonflybsd, solaris, omnios, openindiana, tribblix, haiku,
                                    ubuntu, openeuler, blissos, hurd, plan9, reactos, riscos,
                                    redox
  --release <ver>        OS Release version (e.g., 15.0, 7.4).
                         If invalid or omitted, tries to detect from available releases.
  --arch <arch>          Architecture: x86_64, i386, aarch64, riscv64, sparc64, powerpc64,
                         s390x, armv7 or loongarch64. Default: Host architecture.
                         Single-arch guests override the host default: reactos
                         is i386, riscos is armv7, redox is x86_64.
  --mem <MB>             Memory size in MB (Default: 4096 when the host
                         has more than 4 GB of RAM, else 2048).
  --cpu <num>            Number of CPU cores (Default: host cores, capped at
                         8 with hardware acceleration, 2 under TCG).
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
                         Supported: rsync (default), sshfs, nfs, sys-nfs, scp, tar, 9p, no/off (disable sync).
                         nfs runs the bundled user-space NFS server
                         (anyvm-org/nfsd, v3/v4 + portmapper) on the host:
                         no kernel nfsd, no root needed, works on
                         Linux/macOS/Windows hosts (mynfs is an accepted
                         alias). The v3-only guests (openbsd, netbsd,
                         dragonflybsd) mount it via its portmapper on port
                         111 -- free on Windows/macOS hosts, but usually
                         owned by the system rpcbind (or root-only) on
                         Linux hosts: use sys-nfs for them there. sys-nfs
                         uses the host kernel NFS server (needs root/sudo;
                         not available on macOS/Windows hosts).
                         tar streams the folder as a ustar archive over the
                         guest's remote-exec channel (ssh; telnet or a raw
                         TCP shell for guests without an sshd) and unpacks
                         it on the other side: a one-shot push at boot plus
                         a pull back after the `-- cmd` finishes. Needs no
                         package in the guest beyond the base-system tar
                         and no tar on the host at all.
                         Note: sshfs not supported on Windows hosts; rsync
                         requires rsync.exe.
  --data-dir <dir>       Directory to store images and metadata. Default is
                         <anyvm.py dir>/output when running from a source
                         checkout, otherwise a per-user cache: %LOCALAPPDATA%\\
                         anyvm\\images on Windows, ~/Library/Caches/anyvm/images
                         on macOS, $XDG_CACHE_HOME/anyvm/images (~/.cache/anyvm/
                         images) elsewhere. An installed copy never writes into
                         its own package directory.
  --cache-dir <dir>      Directory to cache extracted qcow2 files (avoids re-download and re-extract).
  --disktype <type>      Disk interface type (e.g., virtio, ide).
                         Default: virtio (ide for dragonflybsd).
  --uefi                 Enable UEFI boot (Implicit for FreeBSD).
  --firmware <path>      Path to the UEFI CODE firmware (e.g. OVMF_CODE.fd).
                         Overrides auto-detection and implies --uefi. When
                         omitted, anyvm searches next to the QEMU binary first
                         (share/edk2/ovmf, share/OVMF, share/qemu) so a
                         relocated install like ~/qemu-local works, then the
                         usual system paths.
  --firmware-vars <path> Path to the matching UEFI VARS template (e.g.
                         OVMF_VARS.fd). Copied per-VM as the writable variable
                         store. Auto-detected next to the CODE firmware if
                         omitted.
  --vnc <display>        Enable VNC on specified display (e.g., 0 for :0). 
                         Default: enabled (display 0). Web UI starts at 6080 (increments if busy).
                         Use "--vnc off" to disable.
  --vnc-password <pwd>   Set a password for the VNC Web UI. A random 6-char password is generated if omitted.
  --remote-vnc           Create a public URL for the VNC Web UI using Cloudflare, Localhost.run, Pinggy, or Serveo.
                         Usage: --remote-vnc (auto), --remote-vnc cf, --remote-vnc lhr, --remote-vnc pinggy, --remote-vnc serveo.
                         Enabled by default if no local browser is detected (e.g., in Cloud Shell).
                         Use "--remote-vnc no" to disable.
  --remote-vnc-link-file Specify a file to write the remote VNC link to (instead of the default .remote file).
  --vga <type>           VGA device type (e.g., virtio, std, virtio-gpu, cirrus). Default: virtio
                         (std for NetBSD and Haiku; cirrus for OpenBSD/amd64 desktop releases like
                         7.9-xfce; virtio kept for OpenBSD/aarch64 desktop releases since
                         cirrus_drv is amd64-only and viogpu+wsfb works on arm64).
  --res, --resolution    Set initial screen resolution (e.g., 1280x800). Default: 1280x800.
  --mon <port>           QEMU monitor telnet port (localhost).
  --public               Listen on 0.0.0.0 for mapped ports instead of 127.0.0.1 + LAN IPs.
  --public-vnc           Listen on 0.0.0.0 for the VNC web interface instead of 127.0.0.1 + LAN IPs.
  --public-ssh           Listen on 0.0.0.0 for the SSH port instead of 127.0.0.1 + LAN IPs.
  --accept-vm-ssh        Authorize the VM's public key on the host (enables reverse SSH).
  --whpx                 (Windows) Force WHPX acceleration. Normally not needed:
                         WHPX is auto-enabled when the Windows Hypervisor
                         Platform is available (use --tcg to opt out).
  --tcg                  Force pure software emulation (no KVM/HVF/WHPX). Slow;
                         useful when hardware acceleration is unavailable or
                         misbehaving. Generic -- applies to any guest.
  --debug                Enable verbose debug logging.
  --detach, -d           Run QEMU in background.
  --console, -c          Run QEMU in foreground (console mode).
  --builder <ver>        Specify a specific vmactions builder version tag.
  --snapshot             Enable QEMU snapshot mode (changes are not saved).
  --boot-timeout-sec <n> Boot timeout in seconds before QEMU is killed and retried once.
                         Default: 600.
  --exec-timeout-sec <n> Max seconds to wait for a telnet-guest command
                         (plan9/reactos/riscos/redox) to finish. Default: 7200.
  --attach               Do not boot anything: talk to an already-running
                         telnet-transport guest at --ssh-port. With '-- <cmd>'
                         runs the command and exits with its status; with
                         --pull-files copies the -v trees back to the host.
  --pull-files           With --attach: copy each -v host:guest pair from the
                         running guest back to the host -- a tar stream over
                         the telnet channel, or over 9P when --sync 9p and
                         --p9-port name a Plan 9 guest's forward.
  --p9-port <n>          Pin the host port of the 9P forward (Plan 9 guests,
                         --sync 9p). Needed when a later --attach
                         --pull-files has to reopen the same channel.
  --sync-exclude <name>  Do not share this path (relative to each -v host
                         path). Repeatable. Applies to the tar and 9P
                         backends; rsync/scp callers pass their own.
  --enable-pmu           Expose the host PMU (performance counters) to the guest.
                         Disabled by default to avoid intermittent #GP-in-wrmsr
                         crashes seen on some host CPUs (DragonFlyBSD is the
                         most affected). Required if you want perf / pmcstat /
                         VTune to work inside the guest.
  --sync-time [off]      Synchronize VM time using NTP inside the guest after boot.
                         (Default: enabled for DragonFlyBSD/Solaris family, disabled otherwise).
  --                     Send all following args to the final ssh command (executes inside the VM).
                         anyvm exits with that command's status, so it fails a
                         script or a CI step the way the command itself would.
                         Quoting is the guest shell's, as with plain ssh: the
                         args are joined and re-parsed there, so wrap a snippet
                         in one argument -- "sh -c 'exit 42'" -- to keep it.
  --help, -h             Show this help message.

Examples:
  # Basic FreeBSD VM
  python anyvm.py --os freebsd --release 14.0

  # ARM64 VM on x86_64 host with port mapping and folder sync
  python anyvm.py --os openbsd --arch aarch64 -p 8080:80 -v $(pwd):/data

  # Windows host using SCP sync
  python anyvm.py --os solaris --sync scp -v D:\\data:/data

  # Run a command inside the VM (arguments after -- go to ssh)
  python anyvm.py --os freebsd -- uname -a

  # Ubuntu Linux guest
  python anyvm.py --os ubuntu
  python anyvm.py --os ubuntu --release 24.04

  # GhostBSD guest (FreeBSD-based desktop OS)
  python anyvm.py --os ghostbsd
  python anyvm.py --os ghostbsd --release 26.1-xfce

  # BlissOS guest (Android-x86; root ssh + Android desktop on the VNC console)
  python anyvm.py --os blissos
  python anyvm.py --os blissos --release 14

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
            # TIME_WAIT leftovers must not count as busy: when a VM is shut
            # down and rebooted in the same session (e.g. the vmactions
            # cache-after-prepare flow), the old ssh port's TIME_WAIT sockets
            # made this plain bind fail, so the reboot drifted to a new port
            # (10022 -> 10023) and stranded the stale ssh aliases. With
            # SO_REUSEADDR the probe binds through TIME_WAIT -- matching
            # QEMU/slirp's own SO_REUSEADDR hostfwd listener -- while a real
            # active listener still fails the bind. Not on Windows: there
            # SO_REUSEADDR also allows binding over an ACTIVE listener, which
            # would report genuinely busy ports as free.
            if not IS_WINDOWS:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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


# SOCKS5 wire constants -- copied from RFC 1928 (SOCKS Protocol Version 5)
# and RFC 1929 (Username/Password Authentication for SOCKS V5).
SOCKS5_VERSION = 0x05             # RFC 1928 sec 3: "The VER field is set to X'05'"
SOCKS5_AUTH_NONE = 0x00           # RFC 1928 sec 3: NO AUTHENTICATION REQUIRED
SOCKS5_AUTH_USERPASS = 0x02       # RFC 1928 sec 3: USERNAME/PASSWORD
SOCKS5_CMD_CONNECT = 0x01         # RFC 1928 sec 4: CONNECT X'01'
SOCKS5_ATYP_IPV4 = 0x01           # RFC 1928 sec 4: IP V4 address X'01'
SOCKS5_ATYP_DOMAIN = 0x03         # RFC 1928 sec 4: DOMAINNAME X'03'
SOCKS5_ATYP_IPV6 = 0x04           # RFC 1928 sec 4: IP V6 address X'04'
SOCKS5_REP_SUCCEEDED = 0x00       # RFC 1928 sec 6: X'00' succeeded
SOCKS5_USERPASS_VERSION = 0x01    # RFC 1929 sec 2: "version ... X'01'"
SOCKS5_USERPASS_SUCCESS = 0x00    # RFC 1929 sec 2: "STATUS field of X'00'"
SOCKS5_DEFAULT_PORT = 1080        # RFC 1928 sec 3: "conventionally located on TCP port 1080"

SOCKS5_REP_ERRORS = {             # RFC 1928 sec 6 reply codes
    0x01: "general SOCKS server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


def parse_socks_proxy_url(proxy_url):
    """Parse socks5://[user:pass@]host[:port] (also socks5h:// and the
    loose socks:// alias) into a spec dict for socks5_open_socket().
    Returns None for anything else (socks4 is not supported)."""
    try:
        parts = urlsplit(proxy_url)
    except Exception:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("socks5", "socks5h", "socks"):
        return None
    try:
        host = parts.hostname
        port = parts.port or SOCKS5_DEFAULT_PORT
    except Exception:
        return None
    if not host:
        return None
    return {
        "host": host,
        "port": port,
        "user": unquote(parts.username) if parts.username else "",
        "password": unquote(parts.password) if parts.password else "",
        # socks5h:// resolves destination hostnames on the proxy (curl
        # semantics); plain socks5:// resolves locally, falling back to
        # proxy-side resolution when local DNS fails.
        "remote_dns": scheme != "socks5",
    }


def _socks5_recv_exact(sock, count, what):
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise IOError("SOCKS5 proxy closed the connection during " + what)
        buf += chunk
    return bytearray(buf)


def socks5_open_socket(spec, dest_host, dest_port, timeout):
    """Open a TCP connection to dest_host:dest_port through the SOCKS5
    proxy described by spec (see parse_socks_proxy_url) and return the
    connected socket. Implements the RFC 1928 CONNECT command with the
    RFC 1929 username/password subnegotiation when the proxy URL carries
    credentials."""
    sock = socket.create_connection((spec["host"], spec["port"]), timeout)
    try:
        # Method selection (RFC 1928 sec 3).
        if spec["user"] or spec["password"]:
            methods = bytearray([SOCKS5_AUTH_NONE, SOCKS5_AUTH_USERPASS])
        else:
            methods = bytearray([SOCKS5_AUTH_NONE])
        sock.sendall(bytes(bytearray([SOCKS5_VERSION, len(methods)]) + methods))
        reply = _socks5_recv_exact(sock, 2, "method selection")
        if reply[0] != SOCKS5_VERSION:
            raise IOError("SOCKS5 proxy replied with version 0x{:02x}".format(reply[0]))
        method = reply[1]
        if method == SOCKS5_AUTH_USERPASS:
            # Username/password subnegotiation (RFC 1929 sec 2).
            user = spec["user"].encode("utf-8")
            password = spec["password"].encode("utf-8")
            if len(user) > 255 or len(password) > 255:
                raise IOError("SOCKS5 username/password longer than 255 bytes")
            sock.sendall(bytes(bytearray([SOCKS5_USERPASS_VERSION, len(user)]) + user
                               + bytearray([len(password)]) + password))
            auth_reply = _socks5_recv_exact(sock, 2, "authentication")
            if auth_reply[1] != SOCKS5_USERPASS_SUCCESS:
                raise IOError("SOCKS5 proxy rejected the username/password")
        elif method != SOCKS5_AUTH_NONE:
            # Covers X'FF' NO ACCEPTABLE METHODS and anything unexpected.
            raise IOError("SOCKS5 proxy accepted no offered auth method "
                          "(server chose 0x{:02x})".format(method))
        # CONNECT request (RFC 1928 sec 4). Send address literals as
        # their native ATYP; for hostnames, resolve locally for socks5://
        # (fall back to proxy-side) and on the proxy for socks5h://.
        addr_blob = None
        try:
            addr_blob = bytearray([SOCKS5_ATYP_IPV4]) + socket.inet_aton(dest_host)
        except (socket.error, OSError, ValueError):
            pass
        if addr_blob is None:
            try:
                addr_blob = (bytearray([SOCKS5_ATYP_IPV6])
                             + socket.inet_pton(socket.AF_INET6, dest_host))
            except (socket.error, OSError, ValueError):
                pass
        if addr_blob is None and not spec["remote_dns"]:
            try:
                addr_blob = (bytearray([SOCKS5_ATYP_IPV4])
                             + socket.inet_aton(socket.gethostbyname(dest_host)))
            except (socket.error, OSError, ValueError):
                pass
        if addr_blob is None:
            try:
                name = dest_host.encode("ascii")
            except UnicodeError:
                name = dest_host.encode("idna")
            if len(name) > 255:
                raise IOError("hostname too long for SOCKS5: " + dest_host)
            addr_blob = bytearray([SOCKS5_ATYP_DOMAIN, len(name)]) + name
        sock.sendall(bytes(bytearray([SOCKS5_VERSION, SOCKS5_CMD_CONNECT, 0x00])
                           + addr_blob + bytearray(struct.pack("!H", dest_port))))
        # Reply (RFC 1928 sec 6): VER REP RSV ATYP BND.ADDR BND.PORT.
        reply = _socks5_recv_exact(sock, 4, "connect reply")
        if reply[1] != SOCKS5_REP_SUCCEEDED:
            raise IOError("SOCKS5 CONNECT to {}:{} failed: {}".format(
                dest_host, dest_port,
                SOCKS5_REP_ERRORS.get(reply[1], "reply 0x{:02x}".format(reply[1]))))
        atyp = reply[3]
        if atyp == SOCKS5_ATYP_IPV4:
            _socks5_recv_exact(sock, 4 + 2, "bound address")
        elif atyp == SOCKS5_ATYP_IPV6:
            _socks5_recv_exact(sock, 16 + 2, "bound address")
        elif atyp == SOCKS5_ATYP_DOMAIN:
            name_len = _socks5_recv_exact(sock, 1, "bound address")[0]
            _socks5_recv_exact(sock, name_len + 2, "bound address")
        else:
            raise IOError("SOCKS5 proxy sent unknown ATYP 0x{:02x}".format(atyp))
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


if http_client is not None:
    class Socks5HTTPConnection(http_client.HTTPConnection):
        def __init__(self, host, port=None, socks_proxy=None, **kwargs):
            http_client.HTTPConnection.__init__(self, host, port, **kwargs)
            self.socks_proxy = socks_proxy

        def connect(self):
            self.sock = socks5_open_socket(
                self.socks_proxy, self.host, self.port, self.timeout)
            if self._tunnel_host:
                self._tunnel()

    class Socks5HTTPSConnection(http_client.HTTPSConnection):
        def __init__(self, host, port=None, socks_proxy=None, **kwargs):
            http_client.HTTPSConnection.__init__(self, host, port, **kwargs)
            self.socks_proxy = socks_proxy

        def connect(self):
            # Mirrors HTTPSConnection.connect() with the TCP dial replaced
            # by the SOCKS5 tunnel. self._context is set by
            # HTTPSConnection.__init__ (stable attribute since 3.4).
            self.sock = socks5_open_socket(
                self.socks_proxy, self.host, self.port, self.timeout)
            server_hostname = self.host
            if self._tunnel_host:
                self._tunnel()
                server_hostname = self._tunnel_host
            self.sock = self._context.wrap_socket(
                self.sock, server_hostname=server_hostname)

    class Socks5ProxyHandler(HTTPHandler, HTTPSHandler):
        """Routes http/https requests through per-scheme SOCKS5 proxies.
        Subclassing both default handlers makes build_opener() use this
        instance instead of them; schemes without a SOCKS proxy and hosts
        matched by no_proxy fall through to a direct connection."""

        def __init__(self, socks_proxies):
            HTTPHandler.__init__(self)
            HTTPSHandler.__init__(self)
            self.socks_proxies = socks_proxies

        def _spec_for(self, req, scheme):
            spec = self.socks_proxies.get(scheme)
            if spec is not None and req.host:
                try:
                    if proxy_bypass(req.host):
                        return None
                except Exception:
                    pass
            return spec

        def http_open(self, req):
            spec = self._spec_for(req, "http")
            if spec is None:
                return HTTPHandler.http_open(self, req)

            def factory(host, **kwargs):
                return Socks5HTTPConnection(host, socks_proxy=spec, **kwargs)
            return self.do_open(factory, req)

        def https_open(self, req):
            spec = self._spec_for(req, "https")
            if spec is None:
                return HTTPSHandler.https_open(self, req)

            def factory(host, **kwargs):
                return Socks5HTTPSConnection(host, socks_proxy=spec, **kwargs)
            return self.do_open(factory, req, context=self._context)


def _proxy_url_for_log(proxy_url):
    """Return the proxy URL with any user:password userinfo masked, so
    credentials never leak into logs."""
    try:
        at = proxy_url.rfind('@')
        if at == -1:
            return proxy_url
        scheme_end = proxy_url.find('://')
        start = scheme_end + 3 if scheme_end != -1 else 0
        return proxy_url[:start] + "***@" + proxy_url[at + 1:]
    except Exception:
        return proxy_url


def setup_download_proxy():
    """Detect proxy settings in the environment and install them as the
    default urllib opener so every download goes through the proxy.

    urllib honors http_proxy/https_proxy on its own, but it silently
    ignores all_proxy/ALL_PROXY, cannot speak SOCKS at all, and never
    tells the user a proxy is in effect. Detect the usual variables
    explicitly, map all_proxy onto the schemes that have no dedicated
    setting, route socks5://[h] URLs through the built-in RFC 1928
    client, and log what will be used. no_proxy is still honored for
    both proxy kinds."""
    proxies = {}
    for scheme in ("http", "https"):
        for var in (scheme + "_proxy", scheme.upper() + "_PROXY"):
            val = os.environ.get(var)
            if val:
                proxies[scheme] = val
                break
    all_proxy = os.environ.get("all_proxy") or os.environ.get("ALL_PROXY")
    if all_proxy:
        for scheme in ("http", "https"):
            proxies.setdefault(scheme, all_proxy)
    if not proxies:
        return
    plain = {}
    socks = {}
    for scheme in sorted(proxies):
        proxy_url = proxies[scheme]
        if proxy_url.lower().startswith("socks"):
            spec = parse_socks_proxy_url(proxy_url)
            if spec is None or http_client is None:
                log("Warning: ignoring {} proxy {} (only socks5:// and "
                    "socks5h:// SOCKS proxies are supported)".format(
                        scheme, _proxy_url_for_log(proxy_url)))
                continue
            socks[scheme] = (spec, proxy_url)
        else:
            plain[scheme] = proxy_url
    if not plain and not socks:
        return
    # Always supply our own ProxyHandler (even when plain is empty):
    # build_opener() otherwise adds a default ProxyHandler that re-reads
    # the same environment and chokes on socks5:// URLs with
    # "unknown url type: socks5".
    handlers = [ProxyHandler(plain)]
    if socks:
        handlers.append(Socks5ProxyHandler(
            dict((scheme, entry[0]) for scheme, entry in socks.items())))
    install_opener(build_opener(*handlers))
    for scheme in sorted(plain):
        log("Using {} proxy from environment: {}".format(
            scheme, _proxy_url_for_log(plain[scheme])))
    for scheme in sorted(socks):
        log("Using {} SOCKS5 proxy from environment: {}".format(
            scheme, _proxy_url_for_log(socks[scheme][1])))


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
        # A server (or middlebox) can close the connection early, in which
        # case resp.read() returns b"" without raising. Treating that EOF as
        # completion silently truncates the chunk, so track how many bytes
        # actually arrived and resume the remaining range until complete.
        expected = end - start + 1
        got = 0
        attempts = 0
        max_attempts = 5
        try:
            with open(tmp_dest, 'r+b') as f:
                f.seek(start)
                while got < expected and not stop_event.is_set():
                    attempts += 1
                    if attempts > max_attempts:
                        raise IOError("range {}-{} incomplete after {} attempts: got {} of {} bytes".format(
                            start, end, max_attempts, got, expected))
                    req = Request(url)
                    req.add_header('User-Agent', 'python-qemu-script')
                    req.add_header('Range', 'bytes={}-{}'.format(start + got, end))
                    try:
                        resp = urlopen(req)
                    except Exception as exc:
                        debuglog(debug, "worker range {}-{} attempt {} open failed: {}".format(
                            start, end, attempts, exc))
                        time.sleep(2)
                        continue
                    code = resp.getcode()
                    if code != 206 and not (code == 200 and start + got == 0):
                        # 200 on a resumed offset means the server ignored the
                        # Range header and is sending the whole file.
                        try:
                            resp.close()
                        except Exception:
                            pass
                        raise IOError("server ignored range request (HTTP {})".format(code))
                    try:
                        while got < expected and not stop_event.is_set():
                            chunk = resp.read(min(128 * 1024, expected - got))
                            if not chunk:
                                break
                            f.write(chunk)
                            got += len(chunk)
                            if show_progress:
                                with progress_lock:
                                    downloaded[0] += len(chunk)
                                    update_progress()
                    except Exception as exc:
                        debuglog(debug, "worker range {}-{} attempt {} read failed at {}: {}".format(
                            start, end, attempts, got, exc))
                        time.sleep(2)
                    finally:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    if got < expected:
                        debuglog(debug, "worker range {}-{} attempt {} short: got {} of {} bytes; resuming".format(
                            start, end, attempts, got, expected))
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
    # Same hazard as the multithread workers: an early connection close makes
    # resp.read() return b"" without raising, silently truncating the part.
    # Verify the received length against the server-reported size and resume
    # with a Range request when the transfer stops short.
    total_size, can_range = get_remote_file_info(url, debug)
    with open(dest_path, 'ab') as f_main:
        start_pos = f_main.tell()
        got = 0
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            req = Request(url)
            req.add_header('User-Agent', 'python-qemu-script')
            if got:
                if can_range:
                    req.add_header('Range', 'bytes={}-'.format(got))
                else:
                    # Cannot resume; restart the part from scratch. Append
                    # mode ignores seek positions, so truncate back instead.
                    f_main.truncate(start_pos)
                    got = 0
            try:
                resp = urlopen(req)
            except Exception as exc:
                debuglog(debug, "failed to open {} (attempt {}): {}".format(url, attempt, exc))
                time.sleep(2)
                continue
            try:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f_main.write(chunk)
                    got += len(chunk)
            except Exception as exc:
                debuglog(debug, "failed while appending {} at {} (attempt {}): {}".format(
                    url, got, attempt, exc))
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            if total_size > 0 and got < total_size:
                debuglog(debug, "append {} attempt {} short: got {} of {} bytes; resuming".format(
                    url, attempt, got, total_size))
                time.sleep(2)
                continue
            debuglog(debug, "appended {}".format(url))
            return True
        f_main.truncate(start_pos)
    debuglog(debug, "failed to append {} after {} attempts".format(url, max_attempts))
    return False

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

# How long into the boot wait to run the dead-VM check (see the boot loop).
# It only fires when the serial log is EMPTY and the QEMU monitor answers
# nothing, so it needs to sit past the slowest plausible "firmware has not
# printed its first byte yet" moment, not past a slow boot: the monitor is
# served by QEMU's main loop and replies regardless of guest speed.
DEAD_VM_CHECK_SECONDS = 120

# How long to wait for the guest to report its clock (sync_vm_time). This is
# the first command sent after the boot probe already proved ssh works, and it
# is only a `date`, so a guest that has not answered in this long is not going
# to. Deliberately the same 15s as the NTP step it sits next to and as the boot
# probe itself. It is capped ONLY here: the folder sync and the user's own
# command that follow may legitimately run for hours, so they stay unbounded.
GUEST_TIME_READ_TIMEOUT_SEC = 15

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

def get_vm_ip_from_serial(serial_log_file, network_prefix=SLIRP_NETWORK_PREFIX):
    """Detect the guest's DHCP-assigned IP by scanning the serial console log.

    Many guests print their lease on the console during boot, e.g. FreeBSD's
    dhclient logs "bound to 192.168.122.20 -- renewal in ...". This works even
    before the guest makes any outbound TCP connection -- which is what
    get_vm_ip_from_monitor relies on -- so it catches stale-lease cases on
    headless arches (riscv64, -display none) where slirp's usernet table is
    still empty during the boot-wait window.

    Returns the most recent plausible guest IP found, or None.
    """
    if not serial_log_file or not os.path.exists(serial_log_file):
        return None
    try:
        with open(serial_log_file, 'rb') as f:
            data = f.read().decode('utf-8', errors='replace')
    except OSError:
        return None
    reserved_last = {0, 1, 2, 3, 255}  # network/gateway/dns/broadcast in slirp's /24
    prefix_re = re.escape(network_prefix)
    found = None
    # "bound to <ip>" (dhclient) is the strongest signal; "inet <ip>" covers
    # ifconfig-style console output as a fallback. Keep the latest match.
    for m in re.finditer(r'(?:bound to|inet)\s+(' + prefix_re + r'\d+)', data):
        ip = m.group(1)
        try:
            last = int(ip.rsplit('.', 1)[1])
        except ValueError:
            continue
        if last in reserved_last:
            continue
        found = ip
    return found

def probe_guest_by_ip_sweep(monitor_port, hostfwd_specs, ssh_probe_cmd,
                            network_prefix=SLIRP_NETWORK_PREFIX,
                            start=10, end=254, probe_timeout=2, time_budget=120,
                            debug=False):
    """Last-resort brute-force: find a booted-but-unreachable guest by IP sweep.

    When the boot-wait loop times out, the VM may actually be up and listening
    on SSH while slirp's hostfwd points at the wrong guest IP (e.g. the guest
    took a stale DHCP lease and the IP-detection guard never saw it). Sweep
    candidate guest IPs <prefix>.start .. <prefix>.end: for each, repoint just
    the SSH hostfwd rule at it and probe SSH. On the first success, repoint ALL
    hostfwd entries at the winning IP and return it; otherwise return None.

    Requires the QEMU monitor (hostfwd_remove/_add) and slirp user networking.

    Cost model: a live IP answers near-instantly here (the VM has already had
    the full boot timeout to come up, so sshd responds over local TCP in well
    under a second), while a dead IP costs the full probe_timeout. Keep
    probe_timeout small. time_budget caps the total wall-clock so a genuinely
    dead VM (kernel panic, hang) does not stall the whole sweep before we fall
    through to the QEMU kill + retry. Realistic slirp DHCP leases are low in the
    range, so the budget is reached only when nothing is actually listening.
    """
    if not monitor_port or not hostfwd_specs:
        return None
    # Drive the sweep off the SSH forward (guest port 22) only -- repoint one
    # rule per candidate instead of all of them, then fix the rest once found.
    ssh_spec = next((s for s in hostfwd_specs if s[3] == "22"), None)
    if ssh_spec is None:
        return None
    proto, host_addr, host_port, guest_port = ssh_spec

    reserved_last = {0, 1, 2, 3, 255}  # network/gateway/dns/broadcast in slirp's /24
    sweep_start = time.time()
    last_cand = None
    for n in range(start, end + 1):
        if n in reserved_last:
            continue
        if time.time() - sweep_start >= time_budget:
            debuglog(debug, "IP sweep: hit {}s time budget at {}{}; giving up sweep".format(
                time_budget, network_prefix, n))
            break
        cand = "{}{}".format(network_prefix, n)
        last_cand = cand
        # Repoint just the SSH rule at this candidate (remove drops whatever the
        # previous iteration / original launch installed for this host port).
        _qmon_send(monitor_port, "hostfwd_remove {}:{}:{}".format(proto, host_addr, host_port))
        add = _qmon_send(monitor_port, "hostfwd_add {}:{}:{}-{}:{}".format(
            proto, host_addr, host_port, cand, guest_port))
        if add and ("Error" in add or "could not" in add.lower()):
            continue
        ret, _ = call_with_timeout(ssh_probe_cmd + ["exit"], timeout_seconds=probe_timeout,
                                   stdout=DEVNULL, stderr=DEVNULL)
        if ret == 0:
            debuglog(debug, "IP sweep: guest reachable at {}; repointing all hostfwd entries".format(cand))
            rewrite_hostfwd_target(monitor_port, hostfwd_specs, cand, debug=debug)
            return cand
        if debug and (n - start) % 20 == 0 and n != start:
            debuglog(debug, "IP sweep: probed through {} (no response yet)".format(cand))
    return None

def sync_vm_time(config, ssh_base_cmd):
    """Synchronizes VM time using NTP-like commands inside the guest."""
    guest_os = config.get('os', '').lower()
    debug = config.get('debug')

    if guest_os == 'blissos':
        # Android/toybox ships no ntpdate/sntp/chrony and the shell cannot set
        # the system clock; Android keeps time itself (and the VM boots with
        # -rtc base=utc,clock=host anyway).
        log("Time sync is not supported on Android/BlissOS guests; skipping.")
        return
    
    def get_guest_time():
        # Bounded on purpose. This is the first thing anyvm sends to the guest
        # after the boot probe succeeds, and an unbounded communicate() here
        # does not fail when the guest goes quiet -- it blocks, which the
        # enclosing try/except cannot catch because blocking is not an
        # exception. Three haiku legs on the macOS runner sat in exactly this
        # call until GitHub's 6-hour job ceiling killed them (2026-08-07 and
        # again 2026-08-12), each leaving its ssh behind as an orphan process.
        # The run reported "cancelled", so it did not even read as broken.
        p = None
        try:
            # Try to get date with milliseconds
            cmd = "date '+%Y-%m-%d %H:%M:%S.%3N'"
            if guest_os in ['freebsd', 'ghostbsd', 'midnightbsd', 'nextbsd', 'openbsd', 'netbsd', 'dragonflybsd', 'solaris', 'omnios', 'openindiana', 'haiku']:
                cmd = "date '+%Y-%m-%d %H:%M:%S.000'"

            p = subprocess.Popen(ssh_base_cmd + [cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, _ = p.communicate(timeout=GUEST_TIME_READ_TIMEOUT_SEC)
            if p.returncode == 0:
                return out.decode('utf-8', errors='replace').strip()
        except subprocess.TimeoutExpired:
            # Say so rather than returning a quiet "unknown": the guest just
            # stopped answering ssh one command after the boot probe passed,
            # which is worth seeing in the log even though the clock itself is
            # cosmetic. Whatever runs next will hit the same wall and report
            # its own failure.
            log("Guest clock read timed out after {}s; the guest is not "
                "answering ssh.".format(GUEST_TIME_READ_TIMEOUT_SEC))
            try:
                # Without this the ssh outlives anyvm -- that is the orphan the
                # runner had to terminate at cleanup in the hung CI jobs.
                p.kill()
                # Close the pipes and wait on the child only, with a bound.
                # NOT communicate(): it waits for EOF on the pipes, and any
                # grandchild that inherited the write end keeps them open long
                # after the child is dead -- which turned this very cleanup
                # into a second unbounded wait when it was first written.
                for _pipe in (p.stdout, p.stderr):
                    try:
                        _pipe.close()
                    except Exception:
                        pass
                p.wait(timeout=5)
            except Exception:
                pass
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
    elif guest_os in ['freebsd', 'ghostbsd', 'netbsd']:
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
    elif guest_os == 'nextbsd':
        # NextBSD: a FreeBSD 15 base, which no longer ships ntp in base at
        # all, and its curated userland has no ports installed by default --
        # so all of these may legitimately be absent. The whole chain is
        # best-effort (a failed time sync is only a warning), and the VM
        # boots with -rtc base=utc,clock=host anyway, so the clock starts
        # correct. There is no rc.d/service(8) to enable a daemon with.
        sync_cmd = ("ntpdate -u {0} || ntpdig -S {0} || sntp -sS {0} || "
                    "rdate -s time.nist.gov || rdate time.nist.gov").format(ntp_servers)
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

# Highest guest-profile schema this anyvm.py understands. A profile carrying a
# different version is ignored (we fall back to the built-in launch logic), so
# a newer builder can never break an older anyvm.py. Mirrors
# base-builder/build.py GUEST_PROFILE_VERSION.
GUEST_PROFILE_SUPPORTED_VERSION = 1


def load_guest_profile(path, debug=False):
    """Parse a guest hardware profile published beside the image as
    <vm_name>.profile.json (written by base-builder/build.py
    build_guest_profile).

    The profile is the single source of truth for the guest's QEMU hardware
    shape (machine, NIC, disk bus, VGA, RNG, firmware kind, ...), emitted by the
    same code that built the image. Reading it keeps the launch in lock-step
    with the build instead of re-deriving every per-(os,arch,release) device
    choice here -- those two assemblers silently drifted and shipped images that
    built green but would not boot.

    Best-effort: returns None when the asset is absent (releases predating it),
    unreadable, malformed, or carries a schema this anyvm.py does not
    understand. In every such case the caller falls back to its built-in logic,
    so old images and forward compatibility both keep working."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            prof = json.load(f)
    except (OSError, ValueError) as e:
        debuglog(debug, "Guest profile {}: unreadable ({}); using built-in launch logic".format(path, e))
        return None
    if not isinstance(prof, dict):
        return None
    ver = prof.get("anyvm_profile_version")
    if ver != GUEST_PROFILE_SUPPORTED_VERSION:
        debuglog(debug, "Guest profile schema v{} unsupported (this anyvm.py understands v{}); using built-in launch logic".format(ver, GUEST_PROFILE_SUPPORTED_VERSION))
        return None
    debuglog(debug, "Guest profile loaded: {}".format(prof))
    return prof


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

def host_total_mem_mb():
    """Returns total physical host memory in MB, or 0 if unknown."""
    try:
        if IS_WINDOWS:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_uint32),
                    ("dwMemoryLoad", ctypes.c_uint32),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            stat = MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(MemoryStatusEx)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return 0
            return int(stat.ullTotalPhys // (1024 * 1024))
        if sys.platform == "darwin":
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"])
            return int(out.strip()) // (1024 * 1024)
        try:
            with open("/proc/meminfo") as meminfo:
                for line in meminfo:
                    if line.startswith("MemTotal:"):
                        # value is in kB
                        return int(line.split()[1]) // 1024
        except IOError:
            pass
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        return page_size * page_count // (1024 * 1024)
    except Exception:
        return 0

def qemu_binary_name(arch):
    """Maps a guest arch (config['arch'], "" = x86_64) to the qemu-system
    binary that runs it."""
    if arch == "riscv64":
        return "qemu-system-riscv64"
    if arch == "loongarch64":
        # Ships in qemu-system-misc on Debian/Ubuntu.
        return "qemu-system-loongarch64"
    if arch == "aarch64":
        return "qemu-system-aarch64"
    if arch in ("armv7", "arm"):
        # 32-bit ARM (RISC OS on raspi2b). QEMU spells the binary "arm", not
        # after the profile; ships in qemu-system-arm on Debian/Ubuntu, which
        # APT_ALL_DEPS already installs for the aarch64 guests.
        return "qemu-system-arm"
    if arch == "sparc64":
        return "qemu-system-sparc64"
    if arch in ("powerpc64", "powerpc64le", "ppc64", "ppc64le"):
        # QEMU ships powerpc64 (big-endian) and powerpc64le (little-endian)
        # under the same qemu-system-ppc64 binary; -M pseries + -cpu picks
        # the guest mode.
        return "qemu-system-ppc64"
    if arch == "s390x":
        return "qemu-system-s390x"
    if arch == "i386":
        # 32-bit x86 guests (Debian GNU/Hurd hurd-i386). Ships in the same
        # qemu-system-x86 package as qemu-system-x86_64 on Debian/Ubuntu.
        return "qemu-system-i386"
    return "qemu-system-x86_64"

# The complete Debian/Ubuntu host dependency set -- the same package list as
# the README "Install dependencies" section; keep the two in lockstep.
APT_ALL_DEPS = ("zstd ovmf xz-utils qemu-utils ca-certificates"
                " qemu-system-x86 qemu-system-arm qemu-efi-aarch64"
                " qemu-efi-riscv64 qemu-system-riscv64 qemu-system-misc"
                " u-boot-qemu qemu-system-ppc qemu-system-s390x"
                " qemu-system-sparc ssh-client")

def deps_install_hint():
    """Returns the platform-specific command that installs the COMPLETE host
    dependency set (QEMU for every guest arch, firmware, zstd/xz, ssh),
    matching the README "Install dependencies" section."""
    if IS_WINDOWS:
        return ("Install the dependencies with:\n"
                "  winget install --id SoftwareFreedomConservancy.QEMU\n"
                "  winget install facebook.zstd\n"
                "(or: choco install qemu), then open a new terminal so PATH"
                " is refreshed.\n"
                "ssh ships with Windows: Settings > System > Optional"
                " features > OpenSSH Client.")
    if platform.system() == "Darwin":
        return ("Install the dependencies with:\n"
                "  brew install qemu")
    apt_cmd = ("sudo apt-get update && sudo apt-get"
               " --no-install-recommends -y install " + APT_ALL_DEPS)
    if shutil.which("apt-get"):
        return ("Install the dependencies with:\n"
                "  " + apt_cmd)
    return ("Install QEMU, zstd, xz and an ssh client with your"
            " distribution's package manager\n"
            "(on Debian/Ubuntu: {}).".format(apt_cmd))

_accel_help_cache = {}


def qemu_has_accel(qemu_bin, accel_name):
    """True if this QEMU binary was BUILT with the named accelerator.

    Accelerator support is per-target and per-build, not per-host: Windows
    QEMU ships WHPX in qemu-system-x86_64.exe only, while
    qemu-system-i386.exe lists tcg alone. Passing an accelerator the binary
    lacks is a hard startup error ("invalid accelerator whpx"), so this is
    asked before the machine string is built. Cached: one probe per binary.
    """
    if not qemu_bin:
        return False
    if qemu_bin not in _accel_help_cache:
        try:
            proc = subprocess.Popen([qemu_bin, "-accel", "help"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = proc.communicate()
            output = (stdout.decode('utf-8', errors='ignore') +
                      stderr.decode('utf-8', errors='ignore'))
        except Exception:
            output = ""
        _accel_help_cache[qemu_bin] = output.split()
    return accel_name in _accel_help_cache[qemu_bin]


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

def qemu_version(qemu_bin):
    """Returns the QEMU version as a (major, minor) int tuple, or None."""
    if not qemu_bin:
        return None
    try:
        out = subprocess.check_output([qemu_bin, "--version"], stderr=DEVNULL)
        m = re.search(r"version\s+(\d+)\.(\d+)", out.decode('utf-8', errors='ignore'))
        if m:
            return (int(m.group(1)), int(m.group(2)))
    except Exception:
        pass
    return None

def ensure_pinned_qemu(arch, qemu_bin, min_version, working_dir, debug=False, bin_name=None,
                       repo=None, builder_tag=None, force=False):
    """Returns a qemu-system binary that is at least min_version for arch.

    If the system binary is new enough it is returned unchanged. Otherwise,
    on Linux x86_64 hosts, the pinned build published by the guest's OWN
    builder (PINNED_QEMU_ASSETS names the file) is downloaded into
    <working_dir>/tools, extracted, and returned instead. QEMU locates its
    firmware (opensbi, s390-ccw.img, ...) relative to the binary, so the
    extracted tree is self-contained. On any failure (no pinned build for
    this host, download or extraction error, binary that does not run) the
    system binary is returned with a warning so the caller can still try it.

    repo / builder_tag: the repo and release the IMAGE itself came from
    (builder_repo and config['builder']). The asset is fetched from exactly
    that release and NOWHERE else -- there is no releases/latest fallback and
    no other builder's repo is ever consulted. Two reasons:
      * releases/latest is a moving target on someone else's release
        schedule. It also races a freshly-cut tag: the tag becomes "latest"
        the moment it is pushed, but its assets only appear once the release
        workflow's upload jobs finish, so every download in that window 404s
        (this bit the openeuler v2.0.1 cut on 2026-07-22).
      * pinning to the image's own release is what makes the QEMU and the
        image a matched, reproducible pair.
    Both are required; without them the pin is skipped (system QEMU is used).

    force: ignore the version check and always prefer the pinned build. Used
    when the pin fixes a bug present in EVERY upstream version (sparc64: the
    sabre IRQ-clobber patch), where a newer system QEMU is not "good enough".
    min_version is then irrelevant to the decision.
    """
    asset = PINNED_QEMU_ASSETS.get(arch)
    if not asset:
        return qemu_bin
    ver = qemu_version(qemu_bin)
    if not force and ver and ver >= min_version:
        return qemu_bin
    if not repo or not builder_tag:
        # No pinned release to fetch from. Deliberately NOT falling back to
        # releases/latest or to another builder: an unpinned asset is not a
        # matched pair with the image, and reaching into a sibling builder
        # would couple two repos that must stay independent.
        log("Warning: no pinned builder release known for {} ({}); "
            "continuing with system QEMU.".format(arch, asset))
        return qemu_bin
    want = "patched" if force else "{}.{}".format(min_version[0], min_version[1])
    if ver:
        have = "{}.{}".format(ver[0], ver[1])
    else:
        have = "missing" if not qemu_bin else "unknown"

    # The published binaries are built on/for ubuntu noble (Linux x86_64).
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        log("Warning: system QEMU for {} is {} (recommended >= {}) and no "
            "pinned build exists for this host platform; the guest may "
            "misbehave.".format(arch, have, want))
        return qemu_bin

    tools_dir = os.path.join(working_dir, "tools")
    extract_dir = os.path.join(tools_dir, removesuffix(asset, ".tar.zst"))
    # The binary name usually follows the arch key; ppc64le is the
    # exception (qemu-system-ppc64 serves both endiannesses).
    bin_rel = os.path.join("bin", bin_name or ("qemu-system-" + arch))

    def find_extracted():
        # Find the binary by scanning the single top-level dir instead of
        # hardcoding its name: build-qemu10.sh now packs every arch under
        # qemu10-<arch>/, but tarballs published before that normalization
        # used qemu10/ for riscv64. Scanning handles both.
        if not os.path.isdir(extract_dir):
            return None
        for entry in sorted(os.listdir(extract_dir)):
            cand = os.path.join(extract_dir, entry, bin_rel)
            if os.path.isfile(cand):
                return cand
        return None

    pinned = find_extracted()
    if not pinned:
        if not os.path.isdir(extract_dir):
            os.makedirs(extract_dir)
        tar_path = os.path.join(tools_dir, asset)
        # Exactly one URL: the guest's own builder, at the guest's own
        # release. No releases/latest, no sibling builder (see the module
        # comment next to PINNED_QEMU_ASSETS).
        url = "https://github.com/{}/releases/download/v{}/{}".format(
            repo, str(builder_tag).lstrip("v"), asset)
        if not os.path.exists(tar_path):
            log("System QEMU for {} is {} (need >= {}); downloading pinned build...".format(arch, have, want))
            if not download_file(url, tar_path, debug):
                log("Warning: failed to download {}; continuing with system "
                    "QEMU ({}).".format(url, have))
                return qemu_bin
        # tarfile in the Python versions we target has no zstd support;
        # GNU tar on any current Linux does.
        rc = subprocess.call(["tar", "--zstd", "-xf", tar_path, "-C", extract_dir])
        if rc != 0:
            log("Warning: failed to extract {} (is zstd installed?); continuing with system QEMU ({}).".format(tar_path, have))
            return qemu_bin
        try:
            os.remove(tar_path)
        except OSError:
            pass
        pinned = find_extracted()
        if not pinned:
            log("Warning: {} did not contain {}; continuing with system QEMU ({}).".format(asset, bin_rel, have))
            return qemu_bin

    pver = qemu_version(pinned)
    if not pver:
        log("Warning: pinned QEMU at {} does not run on this host; continuing with system QEMU ({}).".format(pinned, have))
        return qemu_bin
    log("Using pinned QEMU {}.{}: {} (system QEMU is {})".format(pver[0], pver[1], pinned, have))
    return pinned

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

def host_nested_amd_with_avx512():
    """True if the host is an AMD CPU that exposes AVX512 AND is itself
    running under a hypervisor (i.e. nested virtualization).

    Nested AMD-V -- e.g. KVM inside WSL2 / Hyper-V -- mishandles the L2
    guest's AVX512 XSAVE state. A modern guest whose glibc selects the
    AVX512 string/memory routines (Ubuntu 26.04 and newer) then suffers
    random SIGSEGV across nearly every dynamically linked binary while the
    kernel itself stays up. Detected so the x86_64 KVM path can drop just
    AVX512 from -cpu host in that exact case; bare-metal hosts have no
    'hypervisor' flag and keep full AVX512.
    """
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/cpuinfo") as f:
            info = f.read()
    except Exception:
        return False
    # 'hypervisor' is a CPU flag present only when we are ourselves a guest;
    # the other two are distinctive enough that a plain substring test is safe.
    return ("AuthenticAMD" in info
            and "hypervisor" in info
            and "avx512f" in info)

# Named CPU models used instead of -cpu host for WHPX, newest first, per
# host vendor. QEMU's WHPX host-CPUID enumeration path (-cpu host and
# -cpu max) can wedge the whole QEMU process (validated on a Zen 5 Ryzen
# AI MAX+ 395: 0-byte serial log, unresponsive monitor, ~2s CPU time
# after 12 min); named models skip that path entirely. The model's own
# feature set is safe on ANY host because under WHPX the guest CPUID
# comes from Hyper-V's host-derived values, not from the model
# (validated: -cpu EPYC-Milan-v3, a Zen 3 model without AVX512, still
# showed the guest the host brand string and the full Zen 5 AVX512
# feature set). The lists are probed against 'qemu -cpu help' so older
# QEMU builds fall back to older names. See the whpx branch in the
# x86_64 -cpu selection and the boot-timeout retry, which falls back
# from these to qemu64.
WHPX_AMD_CPU_MODELS = ("EPYC-Turin-v1", "EPYC-Genoa-v2", "EPYC-Milan-v3",
                       "EPYC-Rome-v5", "EPYC-v4")
WHPX_INTEL_CPU_MODELS = ("GraniteRapids-v2", "SapphireRapids-v3",
                         "Icelake-Server-v7", "Cascadelake-Server-v5",
                         "Skylake-Client-v4")

def windows_host_cpu_vendor():
    """Returns 'amd', 'intel', or '' for the Windows host CPU vendor.

    PROCESSOR_IDENTIFIER looks like
    'AMD64 Family 26 Model 112 Stepping 0, AuthenticAMD'; falls back to
    platform.processor() which carries the same vendor suffix.
    """
    if platform.system() != "Windows":
        return ""
    ident = os.environ.get("PROCESSOR_IDENTIFIER", "") + " " + platform.processor()
    if "AuthenticAMD" in ident:
        return "amd"
    if "GenuineIntel" in ident:
        return "intel"
    return ""

# SMBIOS strings that mean "this Windows is itself running in a VM". Matched
# case-insensitively as substrings against the manufacturer / product / BIOS
# vendor fields. Bare metal reports its real OEM instead (verified on an ASUS
# ProArt: SystemManufacturer=ASUS, SystemProductName=ProArt PX13 HN7306EA).
WINDOWS_VM_SMBIOS_SIGNS = (
    "virtual machine", "virtualbox", "vmware", "qemu", "kvm", "bochs",
    "xen", "parallels", "amazon ec2", "google compute engine", "openstack",
    "innotek", "hyper-v",
)


def windows_host_is_virtual():
    """(is_vm, evidence) -- is this Windows itself running inside a VM?

    NOT the same question as "is a hypervisor present": with Hyper-V or WSL2
    enabled, Windows runs in the hypervisor's root partition and reports
    HypervisorPresent=True on bare metal too (measured on the ASUS laptop
    above). The SMBIOS identity does distinguish them -- a VM reports its
    virtual platform there, a physical machine reports its OEM.

    Read from the registry, not WMI: the key is a plain read, while a
    Get-CimInstance subprocess would cost a second or two on every launch.
    """
    if platform.system() != "Windows":
        return (False, "")
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DESCRIPTION\System\BIOS")
        try:
            fields = []
            for value in ("SystemManufacturer", "SystemProductName",
                          "BIOSVendor", "BaseBoardManufacturer"):
                try:
                    fields.append((value, str(winreg.QueryValueEx(key, value)[0]).strip()))
                except OSError:
                    pass
        finally:
            winreg.CloseKey(key)
    except Exception:
        return (False, "smbios unreadable")
    for name, val in fields:
        low = val.lower()
        for sign in WINDOWS_VM_SMBIOS_SIGNS:
            if sign in low:
                return (True, "{}={}".format(name, val))
    return (False, "; ".join("{}={}".format(n, v) for n, v in fields) or "no smbios fields")


def windows_host_cpu_gen():
    """(vendor, family, model) for the Windows host, from PROCESSOR_IDENTIFIER.

    'AMD64 Family 26 Model 112 Stepping 0, AuthenticAMD' -> ('amd', 26, 112).
    Returns ('', 0, 0) when it cannot be parsed.
    """
    if platform.system() != "Windows":
        return ("", 0, 0)
    ident = os.environ.get("PROCESSOR_IDENTIFIER", "") + " " + platform.processor()
    vendor = ""
    if "AuthenticAMD" in ident:
        vendor = "amd"
    elif "GenuineIntel" in ident:
        vendor = "intel"
    m = re.search(r"Family\s+(\d+)\s+Model\s+(\d+)", ident)
    if not m:
        return (vendor, 0, 0)
    return (vendor, int(m.group(1)), int(m.group(2)))


def whpx_named_model_candidates(vendor):
    """WHPX_*_CPU_MODELS trimmed so nothing NEWER than this host is offered.

    Handing WHPX a CPU model from a LATER generation than the host wedges QEMU
    before the guest runs a single instruction: the process stays alive with
    its sockets listening, the serial log stays empty, the monitor answers
    nothing, and only a restart with another model recovers it. Both GitHub
    Windows runner fleets do this, measured 2026-07-29 with the host identity
    now logged (anyvm run 30418497615):

      * AMD64 Family 25 Model 1   given EPYC-Turin-v1     -> wedged
      * Intel64 Family 6 Model 106 given GraniteRapids-v2 -> wedged

    The reverse direction is fine -- an older model on a newer host is what
    the lists were validated with, and it costs nothing because under WHPX
    the guest's CPUID comes from Hyper-V's host-derived values rather than
    from the model (see WHPX_*_CPU_MODELS).

    So: for a host generation we can actually IDENTIFY, start the list at that
    generation. For anything we cannot identify, keep the previous behaviour
    and start at the NEWEST entry (Zen 5 / Xeon 6): an unrecognised host is
    more likely to be newer than these lists than older, and a wrong guess is
    no longer expensive -- the dead-VM fast fail in the boot wait catches it
    in ~2 min instead of the 12.5 min it used to cost. Guessing a mapping for
    families nobody here has run on would be worse than that.
    """
    vendor_models = {"amd": WHPX_AMD_CPU_MODELS,
                     "intel": WHPX_INTEL_CPU_MODELS}.get(vendor, ())
    if not vendor_models:
        return ((), "no vendor match")

    _v, family, model = windows_host_cpu_gen()
    start = None
    why = ""

    if vendor == "amd":
        if family >= 26:
            # Family 1Ah. Verified directly: this branch was developed on an
            # "AMD RYZEN AI MAX+ 395" reporting Family 26 Model 112, where
            # -cpu EPYC-Turin-v1 under WHPX starts and runs normally.
            start, why = "EPYC-Turin-v1", "family {} (>=26)".format(family)
        elif family == 25:
            # Family 19h spans several server generations, and the CI runner
            # in it (Model 1) wedged on EPYC-Turin-v1. Start at the OLDEST
            # member of the family so the choice holds for every model in it.
            start, why = "EPYC-Milan-v3", "family 25 (model {})".format(model)
        elif family and family <= 23:
            start, why = "EPYC-Rome-v5", "family {} (<=23)".format(family)
    elif vendor == "intel":
        # Intel family 6 covers everything from Core 2 to the newest Xeon, so
        # the family number alone identifies nothing -- only the model does.
        # The one model measured here is 106 (the runner that wedged on
        # GraniteRapids-v2); anything at or below it starts at Icelake-Server.
        if family == 6 and model and model <= 106:
            start, why = "Icelake-Server-v7", "family 6 model {} (<=106)".format(model)

    if start is None:
        # Unidentified host: unchanged behaviour, newest model first.
        return (vendor_models, "unidentified host (family {} model {}), "
                               "keeping the newest model".format(family, model))

    try:
        idx = vendor_models.index(start)
    except ValueError:
        return (vendor_models, "{} not in the list, keeping the newest".format(start))
    return (vendor_models[idx:], why)


def whpx_available():
    """True if the Windows Hypervisor Platform can run guests right now.

    Calls WHvGetCapability(WHvCapabilityCodeHypervisorPresent) from
    WinHvPlatform.dll. The DLL only loads when the optional 'Windows
    Hypervisor Platform' feature is installed, and the capability reports
    TRUE only when the Microsoft hypervisor is actually running, so this
    is a real runtime check, not a presence heuristic.
    WHvCapabilityCodeHypervisorPresent = 0x00000000 and the output buffer
    is a 32-bit BOOL -- Microsoft Learn WHvGetCapability page,
    cross-checked against mingw-w64 winhvplatformdefs.h.
    """
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        dll = ctypes.WinDLL("WinHvPlatform.dll")
        present = ctypes.c_uint32(0)
        written = ctypes.c_uint32(0)
        hr = dll.WHvGetCapability(0, ctypes.byref(present), 4,
                                  ctypes.byref(written))
        return hr == 0 and present.value != 0
    except Exception:
        return False

def qemu_cpu_models(qemu_bin):
    """Returns the set of CPU model names listed by 'qemu -cpu help',
    or an empty set on any failure."""
    if not qemu_bin:
        return set()
    try:
        out = subprocess.check_output([qemu_bin, "-cpu", "help"], stderr=DEVNULL)
    except Exception:
        return set()
    models = set()
    for line in out.decode('utf-8', errors='ignore').splitlines():
        # Model lines look like '  EPYC-Genoa-v2   AMD EPYC-Genoa-v2 Processor'.
        parts = line.split()
        if parts:
            models.add(parts[0])
    return models

def hurd_nfs_mount_cmd(vguest, remote, nfs_port, mount_port):
    """Returns the guest commands mounting `remote` at vguest via the
    /hurd/nfs translator. Shared by sync_mynfs and sync_nfs; only the
    ports and the remote path differ between the two.

    The translator is UDP-only and always sends MOUNT v1 (its
    --mount-program flag aborts on an upstream argp bug), so the server
    must serve MOUNT v1. --nfs-program=100003.3 selects NFSv3 for the
    file protocol; explicit --nfs-port/--mount-port skip the portmapper;
    --read-size/--write-size 1024 keep every RPC datagram under the MTU
    (default 8192 fragments UDP packets, which wedges the whole guest
    NIC under slirp -- verified 2026-07-20, killed even the ssh session).

    The </dev/null >/dev/null 2>&1 is LOAD-BEARING: the translator
    process settrans spawns runs forever (it IS the mounted filesystem)
    and inherits these fds -- without the redirect it keeps the ssh
    session's stdout/stderr open, the ssh never exits, and a capped
    mount attempt gets killed even though the mount itself succeeded in
    under a second."""
    return ('settrans -ga "{vguest}" 2>/dev/null\n'
            'settrans -a "{vguest}" /hurd/nfs --soft=3 '
            '--nfs-port={nfs_port} --mount-port={mount_port} '
            '--nfs-program=100003.3 '
            '--read-size=1024 --write-size=1024 '
            '"{remote}" </dev/null >/dev/null 2>&1').format(
        vguest=vguest, remote=remote,
        nfs_port=nfs_port, mount_port=mount_port)

def run_guest_mount(ssh_cmd, vguest, mount_cmd, what, attempts,
                    timeout=None):
    """Creates vguest inside the guest and runs mount_cmd (a ready-made
    shell command string) over ssh, retrying up to `attempts` times.
    When `timeout` is set, each attempt is capped and a hung ssh session
    is killed. Returns True once the script exits 0."""
    mount_script = 'mkdir -p "{}"\n{}\n'.format(vguest, mount_cmd)
    for attempt in range(attempts):
        p_mount = subprocess.Popen(ssh_cmd + ["sh"], stdin=subprocess.PIPE)
        try:
            p_mount.communicate(input=mount_script.encode('utf-8'),
                                timeout=timeout)
        except subprocess.TimeoutExpired:
            log("{} mount attempt {} hung beyond {}s, killing".format(
                what, attempt + 1, timeout))
            p_mount.kill()
            try:
                p_mount.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if p_mount.returncode == 0:
            return True
        log("{} mount failed (attempt {}), retrying...".format(
            what, attempt + 1))
        time.sleep(2)
    return False

def sync_sshfs(ssh_cmd, vhost, vguest, os_name):
    """Mounts a host directory into the guest using SSHFS."""
    if IS_WINDOWS:
        log("Warning: SSHFS sync not supported on Windows host.")
        return

    if os_name == "netbsd":
        # NetBSD uses the base mount_psshfs with -t -1: never refetch node
        # attributes / directory contents from the server (REFRESHTIMEOUT
        # in NetBSD src usr.sbin/puffs/mount_psshfs/psshfs.h; the local
        # attr cache is still updated by the guest's own writes). The
        # default (30s expiry) and -t 0 (always expired) both let an
        # open()-time getattr pull server attrs while async writeback is
        # still in flight, which invalidates the dirty page cache and
        # makes an immediate re-read return zero-filled or stale content
        # -- the vmactions/netbsd-vm#21 corruption (short .o reads,
        # corrupted build.ninja; CI coherence stress with -t 0 failed
        # 4/25 sshfs legs, rewrites re-read as all zeros).
        # Trade-off of -t -1: files the HOST creates/changes AFTER the
        # mount are not noticed by the guest (host syncs before, guest
        # builds, host reads results via its own filesystem, so the CI
        # pattern is unaffected).
        # pkgsrc fuse-sshfs (baked into the images) was tried and
        # REJECTED: NetBSD's refuse/perfuse layer cannot resolve getcwd()
        # for a cwd inside the mount, so `cd workspace && pkg_add/make/
        # meson ...` dies instantly with "getcwd failed" (netbsd-vm
        # test.yml run 29084790384).
        mount_cmd = '/usr/sbin/mount_psshfs -t -1 host:"{vhost}" ' \
                    '"{vguest}" >/dev/null 2>&1'
    elif os_name == "haiku":
        # Haiku has no sshfs CLI: sshfs_fuse installs as a userlandfs
        # FUSE add-on, mounted via `mount -t userlandfs -p "sshfs <src>"
        # <dir>`. The kernel's on-demand launch of userlandfs_server is
        # broken on r1beta5 (runtime_loader tries to load the add-ons
        # DIRECTORY as an ELF object -> the server never registers its
        # port -> mount fails with "Bad port ID"). Pre-starting the named
        # server bypasses that: `userlandfs_server sshfs` stays resident
        # and registers the port, after which the mount succeeds. Whether
        # the server is already running can only be probed inside the
        # guest, so that check stays in shell. The </dev/null redirects
        # are LOAD-BEARING (same as the hurd settrans case): the server
        # is a long-lived process and would otherwise hold this ssh
        # session's fds open forever.
        mount_cmd = 'ps | grep userlandfs_server | grep -v grep ' \
                    '>/dev/null 2>&1 || {{\n' \
                    '  nohup /boot/system/servers/userlandfs_server sshfs ' \
                    '</dev/null >/dev/null 2>&1 &\n' \
                    '  sleep 2\n' \
                    '}}\n' \
                    'mount -t userlandfs -p "sshfs host:{vhost}" "{vguest}"'
    else:
        mount_cmd = ''
        if os_name in ("freebsd", "ghostbsd", "midnightbsd"):
            mount_cmd += 'kldload fusefs >/dev/null 2>&1 || ' \
                         'kldload fuse >/dev/null 2>&1 || true\n'
        mount_cmd += 'sshfs -o reconnect,ServerAliveCountMax=2,' \
                     'allow_other,default_permissions ' \
                     'host:"{vhost}" "{vguest}" || exit 1\n' \
                     '/sbin/mount >/dev/null 2>&1 || mount >/dev/null 2>&1'
    mount_cmd = mount_cmd.format(vguest=vguest, vhost=vhost)

    # Cap each attempt at 60s so a sshfs reconnect-loop inside the VM
    # (when host-side ssh keeps dropping the inner connection) does NOT
    # hang anyvm.py forever. sshfs runs with `-o reconnect`, so on a
    # flaky connect it will retry without ever returning to the shell.
    if not run_guest_mount(ssh_cmd, vguest, mount_cmd, "SSHFS",
                           attempts=10, timeout=60):
        log("Warning: Failed to mount shared folder via sshfs.")

# Guests whose base system has no NFSv4 client AND whose mount_nfs has no
# mountport= option, so they mount the user-space nfsd over NFSv3 via its
# -pmap portmapper on port 111. CI-verified (anyvm run 29190082415):
# dragonflybsd mount_nfs rejects `-o nfsv4` outright; openbsd/netbsd
# mount_nfs speak NFSv3 only, and all three discover the MOUNT/NFS ports
# exclusively through a portmapper query. Port 111 is free and unprivileged
# on Windows and macOS hosts; on Linux hosts it usually belongs to the
# system rpcbind (or needs root), in which case sync_mynfs warns and the
# user should pass --sync sys-nfs explicitly -- there is deliberately no
# automatic kernel-NFS fallback.
NFSV4LESS_GUESTS = ("openbsd", "netbsd", "dragonflybsd")

def ensure_mynfsd(output_dir, debug=False):
    """Fetches the bundled user-space NFS server (anyvm-org/nfsd, a single
    pure-Python stdlib-only file) pinned at MYNFSD_VERSION into output_dir,
    verifying it against the release's nfsd.py.sha256 sidecar. Returns the
    local path, or None when the download or the verification fails."""
    dest = os.path.join(output_dir, "nfsd-v{}.py".format(MYNFSD_VERSION))
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        # verified when it was downloaded
        return dest
    # pid-suffixed so two concurrent anyvm processes doing the first
    # download cannot clobber each other's partial file; both end with an
    # atomic os.replace of the same verified content.
    tmp = "{}.part.{}".format(dest, os.getpid())
    if not download_file(MYNFSD_URL, tmp, debug):
        log("Warning: failed to download the user-space nfsd from " + MYNFSD_URL)
        return None

    # The release publishes a "<sha256>  nfsd.py" sidecar next to the file.
    expected = ""
    for _ in range(3):
        try:
            resp = urlopen(Request(MYNFSD_URL + ".sha256"), timeout=30)
            expected = resp.read(1024).decode(
                "ascii", "replace").split()[0].strip().lower()
            break
        except Exception as e:
            debuglog(debug, "sha256 sidecar fetch failed: {}".format(e))
            time.sleep(2)
    if not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        log("Warning: could not fetch the nfsd.py sha256 sidecar from {}; "
            "refusing the unverified download.".format(MYNFSD_URL + ".sha256"))
        return None

    h = hashlib.sha256()
    with open(tmp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        log("Warning: nfsd.py sha256 mismatch (expected {}, got {}); "
            "discarding the download.".format(expected, actual))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    debuglog(debug, "nfsd.py sha256 verified: {}".format(actual))
    os.replace(tmp, dest)
    return dest

def run_internal_nfsd(nfsd_path, export_dir, port, qemu_pid, log_path,
                      debug=False, vers="", pmap=False):
    """--internal-nfsd watchdog: runs nfsd.py as a child and stops it once the
    QEMU process it serves is gone. Spawned detached so a --detach VM keeps
    its NFS share after anyvm.py exits (same pattern as the VNC web proxy).

    vers ("3"/"4"/"" for both) selects the NFS major version the nfsd
    serves; pmap additionally serves the portmapper v2 on port 111, which
    only the v3-only BSD guests (NFSV4LESS_GUESTS) need. When 111 is taken
    (system rpcbind on Linux, or a second -v mount's nfsd instance), nfsd
    logs a warning and keeps serving."""
    cmd = python_argv(nfsd_path) + [
        "-dir", export_dir, "-port", str(port), "-bind", "127.0.0.1"]
    if vers:
        cmd.extend(["-vers", vers])
    if pmap:
        cmd.append("-pmap")
    # Report file owners to the guest as the launching user, mirroring the
    # anonuid/anongid mapping the kernel-nfsd export line uses.
    if hasattr(os, "getuid"):
        cmd.extend(["-anonuid", str(os.getuid()), "-anongid", str(os.getgid())])
    if debug:
        cmd.append("-vv")
    with open(log_path, "a") as logf:
        child = subprocess.Popen(cmd, stdin=DEVNULL, stdout=logf, stderr=logf)
        try:
            while child.poll() is None:
                if not is_pid_alive_main(qemu_pid):
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except Exception:
                        child.kill()
                    break
                time.sleep(5)
        finally:
            if child.poll() is None:
                try:
                    child.kill()
                except Exception:
                    pass

def sudo_noninteractive():
    """True when passwordless sudo works right now (`sudo -n true`)."""
    if IS_WINDOWS:
        return False
    try:
        with open(os.devnull, 'w') as devnull:
            return subprocess.call(["sudo", "-n", "true"],
                                   stdout=devnull, stderr=devnull) == 0
    except Exception:
        return False

def probe_privileged_bind(port):
    """Can THIS process bind the given (privileged) TCP port right now?

    Returns "ok", "denied" (EACCES/EPERM: no privilege -- sudo would
    help), or "busy" (EADDRINUSE: another service owns it -- sudo would
    NOT help). "ok" covers every mechanism that grants the bind: running
    as root, a setcap cap_net_bind_service interpreter, a lowered
    net.ipv4.ip_unprivileged_port_start, or macOS wildcard binds -- so
    the caller never has to enumerate them. Note the kernel checks the
    privilege BEFORE the address conflict, so an unprivileged process
    sees "denied" even when the port is also busy."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", port))
        return "ok"
    except PermissionError:
        return "denied"
    except OSError:
        return "busy"
    finally:
        try:
            s.close()
        except Exception:
            pass

def start_mynfsd(nfsd_path, vhost, port, log_path, qemu_pid, debug=False,
                 use_sudo=False, vers="", pmap=False):
    """Starts the user-space nfsd for one exported directory, detached behind
    the --internal-nfsd watchdog. Returns True once the port accepts TCP.

    vers/pmap are forwarded to the nfsd (see run_internal_nfsd). use_sudo
    runs the whole watchdog (and therefore the nfsd) as root via
    passwordless sudo, so the -pmap portmapper can bind privileged port 111.
    The sudo must wrap the WATCHDOG, not just the inner nfsd: an
    unprivileged watchdog cannot signal a root child to stop it."""
    args = self_argv() + [
        "--internal-nfsd", nfsd_path, vhost, str(port), str(qemu_pid),
        log_path, "1" if debug else "0", vers, "1" if pmap else "0"]
    if use_sudo:
        args = ["sudo", "-n"] + args
    popen_kwargs = {}
    if IS_WINDOWS:
        # CREATE_NO_WINDOW = 0x08000000, DETACHED_PROCESS = 0x00000008
        popen_kwargs['creationflags'] = 0x08000000 | 0x00000008
    else:
        popen_kwargs['start_new_session'] = True
    subprocess.Popen(args, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
                     **popen_kwargs)
    for _ in range(30):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except Exception:
            time.sleep(1)
        finally:
            try:
                s.close()
            except Exception:
                pass
    return False

def sync_mynfs(ssh_cmd, vhost, vguest, os_name, output_dir, vm_name, qemu_pid, debug=False):
    """Shares vhost into the guest with the bundled user-space NFSv4.0 server
    (github.com/anyvm-org/nfsd): no kernel nfsd, no root, works on Linux,
    macOS and Windows hosts. The guest mounts it with its NFSv4 client."""
    nfsd_path = ensure_mynfsd(output_dir, debug)
    if not nfsd_path:
        log("Warning: cannot set up mynfs sync without nfsd.py; skipping {}".format(vhost))
        return
    port = get_free_port(start=2049, end=2149)
    if port is None:
        log("Warning: no free TCP port for the user-space nfsd; skipping mynfs sync.")
        return
    log_path = os.path.join(output_dir, "{}.nfsd.{}.log".format(vm_name, port))
    # v4-capable guests need no portmapper: start the nfsd directly with
    # -vers 4, no probing. Only the v3-only BSD guests need -vers 3 plus
    # the -pmap portmapper on privileged port 111 (RFC 1833 PMAP_PORT),
    # and only then is the probe worth doing.
    need_v3 = os_name in NFSV4LESS_GUESTS
    # The Hurd's /hurd/nfs translator is v3-only as well, but it accepts
    # explicit --nfs-port/--mount-port arguments, so it needs `-vers 3`
    # WITHOUT the port-111 portmapper (no sudo dance either). See
    # hurd_nfs_mount_cmd for the whole story.
    hurd_v3 = os_name == "hurd"
    use_sudo = False
    if need_v3 and not IS_WINDOWS:
        # Probe instead of guessing the mechanism: "ok" covers root, a
        # setcap cap_net_bind_service python, a lowered
        # ip_unprivileged_port_start, and macOS wildcard binds -- run
        # directly. "denied" means sudo would help; "busy" means some
        # other service (usually the system rpcbind) owns 111 and not
        # even root can bind it.
        verdict = probe_privileged_bind(111)
        if verdict == "ok":
            debuglog(debug, "port 111 bindable by this process; "
                            "starting the nfsd without sudo")
        elif verdict == "denied":
            use_sudo = sudo_noninteractive()
            if use_sudo:
                log("Starting the user-space nfsd with sudo ({} guests need "
                    "the portmapper on port 111).".format(os_name))
        else:
            log("Warning: port 111 is already taken by another service "
                "(the system rpcbind?), so the nfsd portmapper cannot "
                "start and {} guests cannot mount; use --sync sys-nfs "
                "on this host.".format(os_name))
    log("Starting user-space nfsd (NFSv{}) on port {} exporting {}".format(
        "3" if (need_v3 or hurd_v3) else "4", port, vhost))
    if not start_mynfsd(nfsd_path, vhost, port, log_path, qemu_pid, debug,
                        use_sudo=use_sudo,
                        vers="3" if (need_v3 or hurd_v3) else "4",
                        pmap=need_v3):
        log("Warning: the user-space nfsd did not come up on port {}; see {}".format(port, log_path))
        return

    if os_name in NFSV4LESS_GUESTS:
        # These guests can only find the server through the portmapper --
        # check its TCP listener is actually up before mounting, so a
        # failed port-111 bind surfaces here instead of as a guest-side
        # portmap timeout.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        try:
            s.connect(("127.0.0.1", 111))
            debuglog(debug, "nfsd portmapper reachable on 127.0.0.1:111")
        except Exception as e:
            log("Warning: the nfsd portmapper is not reachable on port 111 "
                "({}); {} guests cannot discover the NFS ports (see {}). "
                "On a Linux host use --sync sys-nfs instead.".format(
                    e, os_name, log_path))
        finally:
            try:
                s.close()
            except Exception:
                pass

    # NFSv4 needs no portmapper/mountd, so a plain port option (or NFS URL on
    # illumos) reaches the server directly. The exported directory is the
    # server's root, hence the "/" path.
    # The v3-only BSDs (NFSV4LESS_GUESTS) have no port options at all: their
    # mount_nfs discovers the MOUNT/NFS ports via the nfsd -pmap portmapper
    # on port 111 (query goes over UDP; NFS itself then runs over TCP, so
    # TCP must be selected explicitly where it is not the default).
    # Option sources: Linux -- nfsd.py's own README; FreeBSD family --
    # mount_nfs(8) (nfsv4, minorversion=, port=, tcp); illumos family --
    # mount_nfs(8) (vers=, port=); openbsd/netbsd/dragonflybsd --
    # mount_nfs(8) (-T tcp flag; dragonfly -3 forces v3).
    # haiku -- the in-kernel nfs4 add-on (NFSv4.0 client): mount syntax is
    # `mount -t nfs4 -p "server:path opt1 opt2" dir`, space-separated opts,
    # path after the LAST colon; port= and proto= (tcp default) per
    # ParseArguments in src/add-ons/kernel/file_systems/nfs4/
    # kernel_interface.cpp.
    # hurd -- see hurd_nfs_mount_cmd (needs nfsd >= 0.0.9 with the UDP
    # transport + MOUNT v1 compat).
    if os_name == "hurd":
        mount_cmd = hurd_nfs_mount_cmd(vguest, "192.168.122.2:/",
                                       port, port)
    else:
        if os_name in ("solaris", "omnios", "openindiana", "tribblix"):
            mount_cmd = 'mount -F nfs -o vers=4,port={port} ' \
                        '192.168.122.2:/ "{vguest}"'
        elif os_name in ("freebsd", "ghostbsd", "midnightbsd", "nextbsd"):
            # nextbsd runs a FreeBSD 15 kernel + mount_nfs, so it takes the
            # FreeBSD syntax. Its image needs /etc/netconfig for any RPC at
            # all (nextbsd-builder bakes it in; the curated /etc omits it),
            # without which this mount fails with "tcp: Netconfig database
            # not found".
            mount_cmd = 'mount -t nfs -o ' \
                        'nfsv4,minorversion=0,tcp,port={port} ' \
                        '192.168.122.2:/ "{vguest}"'
        elif os_name == "haiku":
            mount_cmd = 'mount -t nfs4 -p "192.168.122.2:/ port={port}" ' \
                        '"{vguest}"'
        elif os_name in ("openbsd", "netbsd"):
            mount_cmd = '/sbin/mount_nfs -T 192.168.122.2:/ "{vguest}"'
        elif os_name == "dragonflybsd":
            mount_cmd = '/sbin/mount_nfs -3 -T 192.168.122.2:/ "{vguest}"'
        else:
            mount_cmd = 'mount -t nfs -o vers=4.0,proto=tcp,port={port} ' \
                        '192.168.122.2:/ "{vguest}"'
        mount_cmd = mount_cmd.format(vguest=vguest, port=port)

    # Cap each attempt at 60s: when the portmapper is unreachable, BSD
    # mount_nfs retries the portmap query forever (OpenBSD logged
    # "Port mapper failure - RPC: Timed out" every 2 minutes for an hour
    # on a macOS runner) and would hang anyvm here.
    if not run_guest_mount(ssh_cmd, vguest, mount_cmd, "mynfs",
                           attempts=5, timeout=60):
        log("Warning: failed to mount the shared folder via the user-space "
            "nfsd (see {}).".format(log_path))

def sync_nfs(ssh_cmd, vhost, vguest, os_name, sudo_cmd):
    """Configures host kernel NFS exports and mounts in guest (--sync
    sys-nfs). Needs a Linux host with root/sudo and the kernel NFS server
    installed; for everything else use the user-space nfsd (sync_mynfs)."""
    if IS_WINDOWS or platform.system() == "Darwin":
        log("Warning: no kernel NFS server support on this host; "
            "use --sync nfs (the bundled user-space nfsd) instead.")
        return

    # Host side configuration
    uid = os.getuid()
    gid = os.getgid()
    opts = "rw,insecure,async,no_subtree_check,anonuid={},anongid={}".format(uid, gid)
    if os_name == "hurd":
        # The /hurd/nfs translator chowns every freshly CREATEd file to
        # uid 0; under the default root_squash that SETATTR gets EPERM and
        # the translator fails the whole open, so every guest write dies.
        # (The user-space nfsd swallows the failed chown instead; the
        # kernel nfsd needs no_root_squash to get the same effect.)
        opts += ",no_root_squash"
    entry_line = "{} *({})".format(vhost, opts)

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

    def _call_quiet(cmd):
        try:
            return subprocess.call(cmd)
        except Exception:
            return 127

    if need_add:
        log("Configuring NFS export on host (requires sudo)...")
        debuglog(True, "Adding export: " + entry_line)
        if sudo_cmd or os.geteuid() == 0:
            _call_quiet(sudo_cmd + ["mkdir", "-p", "/run/sendsigs.omit.d/"])
            cmd_write = sudo_cmd + ["tee", "-a", "/etc/exports"]
            p_write = subprocess.Popen(cmd_write, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            p_write.communicate(input=(entry_line + "\n").encode('utf-8'))

            kernel_ok = False
            if p_write.returncode == 0:
                if _call_quiet(sudo_cmd + ["exportfs", "-a"]) == 0:
                    kernel_ok = True
                if _call_quiet(sudo_cmd + ["service", "nfs-kernel-server", "restart"]) != 0:
                    if _call_quiet(sudo_cmd + ["service", "nfs-server", "restart"]) != 0:
                        if _call_quiet(sudo_cmd + ["systemctl", "restart", "nfs-server"]) == 0:
                            kernel_ok = True
                    else:
                        kernel_ok = True
                else:
                    kernel_ok = True
            else:
                log("Failed to write to /etc/exports")
            if not kernel_ok:
                log("Warning: could not configure the host kernel NFS server "
                    "(is it installed?); the guest mount will likely fail.")
        else:
            log("Warning: Cannot configure the kernel NFS server without "
                "sudo/root.")

    hurd_mnt_port = 0
    if os_name == "hurd":
        # /hurd/nfs is UDP-only, but modern nfs-utils starts the kernel
        # nfsd with the UDP transport disabled -- and `rpc.nfsd --udp` is
        # a NO-OP while knfsd is already running (nfs-utils 2.6.x prints
        # "knfsd is currently up" and exits 0 without touching the
        # transports). Add the UDP listener through the kernel's own
        # /proc/fs/nfsd/portlist interface instead; verified: the write
        # takes effect immediately and NFSv3-over-UDP serves fine. Must
        # run AFTER the service restart above (a restart resets the
        # transports to the distro default), and runs on every sync so a
        # second -v mount cannot lose the UDP socket.
        udp_ok = False
        if sudo_cmd or os.geteuid() == 0:
            try:
                cur = subprocess.check_output(
                    sudo_cmd + ["cat", "/proc/fs/nfsd/portlist"],
                    stderr=DEVNULL).decode("ascii", "replace")
            except Exception:
                cur = ""
            if "udp" in cur:
                udp_ok = True
            else:
                p_pl = subprocess.Popen(
                    sudo_cmd + ["tee", "/proc/fs/nfsd/portlist"],
                    stdin=subprocess.PIPE, stdout=DEVNULL, stderr=DEVNULL)
                p_pl.communicate(input=b"udp 2049\n")
                udp_ok = p_pl.returncode == 0
            if udp_ok:
                debuglog(True, "kernel nfsd UDP transport enabled for hurd")
        if not udp_ok:
            log("Warning: could not enable the kernel nfsd UDP transport "
                "(needs root/sudo and a UDP-capable knfsd); the hurd guest "
                "cannot mount sys-nfs without it -- use --sync nfs (the "
                "user-space nfsd) instead.")
        # The guest-side settrans uses EXPLICIT ports (the kernel does not
        # (re)register the UDP transport with rpcbind, so the translator's
        # portmapper discovery would come back empty). NFS is fixed at
        # 2049; mountd sits on a random port -- resolve its MOUNT v1/udp
        # registration host-side. rpc.mountd still serves MOUNT v1 on
        # udp+tcp (verified on nfs-utils 2.6.4), which matters because
        # /hurd/nfs always speaks MOUNT v1.
        try:
            pmap = subprocess.check_output(
                ["rpcinfo", "-p"], stderr=DEVNULL).decode("ascii", "replace")
            for pline in pmap.splitlines():
                f = pline.split()
                if len(f) >= 4 and f[0] == "100005" and f[1] == "1" \
                        and f[2] == "udp":
                    hurd_mnt_port = int(f[3])
                    break
        except Exception:
            pass
        if hurd_mnt_port:
            debuglog(True, "mountd MOUNT v1/udp port: {}".format(hurd_mnt_port))
        else:
            log("Warning: no MOUNT v1/udp registration found in rpcinfo; "
                "the hurd sys-nfs mount will fail -- use --sync nfs (the "
                "user-space nfsd) instead.")

    # Guest side mounting
    if os_name == "hurd":
        # Explicit ports: the kernel nfsd's UDP transport (added via
        # portlist above) is not registered with rpcbind, so the
        # translator's own portmapper discovery would come back empty;
        # NFS is fixed at 2049 and the mountd port was resolved
        # host-side from rpcinfo.
        mount_cmd = hurd_nfs_mount_cmd(vguest, "192.168.122.2:" + vhost,
                                       2049, hurd_mnt_port)
    else:
        if os_name == "openbsd":
            mount_cmd = 'mount -t nfs -o -T 192.168.122.2:"{vhost}" ' \
                        '"{vguest}"'
        elif os_name == "haiku":
            # Haiku's nfs4 add-on wants server, path and options as ONE
            # space-separated -p string with the path after the LAST colon;
            # the generic `mount server:path dir` below is BSD/Linux syntax
            # and Haiku answers it with "mount: No such file or directory"
            # (anyvm run 30427154344, ten attempts, every one the same).
            # sync_mynfs() already carries this syntax -- source cited there:
            # ParseArguments in
            # src/add-ons/kernel/file_systems/nfs4/kernel_interface.cpp --
            # only --sync sys-nfs was missing its branch.
            #
            # Two differences from the sync_mynfs form: the path is the real
            # exported directory (the kernel nfsd exports it in place, it is
            # not the server's root), and no port= option is needed because
            # knfsd sits on the standard 2049 that the client defaults to.
            mount_cmd = 'mount -t nfs4 -p "192.168.122.2:{vhost}" "{vguest}"'
        else:
            # Whether /sbin/mount exists can only be probed inside the
            # guest, so this one check stays in shell.
            mount_cmd = 'if [ -e "/sbin/mount" ]; then\n' \
                        '  /sbin/mount 192.168.122.2:"{vhost}" "{vguest}"\n' \
                        'else\n' \
                        '  mount 192.168.122.2:"{vhost}" "{vguest}"\n' \
                        'fi'
        mount_cmd = mount_cmd.format(vguest=vguest, vhost=vhost)

    if not run_guest_mount(ssh_cmd, vguest, mount_cmd, "NFS", attempts=10):
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
    if os_name in ("freebsd", "ghostbsd", "midnightbsd", "nextbsd"):
        # nextbsd installs rsync from the FreeBSD ports repo too -- its
        # pkg(8) is preconfigured for pkg.FreeBSD.org with ABI FreeBSD:15:amd64.
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

# ---------------------------------------------------------------------------
# plan9/9front transport: telnet exec + 9P folder sync (VM_TRANSPORT=telnet).
# 9front ships no sshd. The runtime drives the guest over its no-auth telnetd
# (baked to listen on 23, reachable only via the slirp hostfwd on 127.0.0.1)
# and mounts the guest's exportfs 9P share (Linux kernel v9fs) for -v folders.
# ---------------------------------------------------------------------------

def _telnet_eat_iac(sock, data, out, binary=None, carry=None):
    """Refuse all telnet IAC option negotiation in `data`; append plain bytes
    to `out`.

    `carry` is a bytearray the caller owns across calls, and any caller
    reading a STREAM must pass one. A telnet sequence can straddle a recv
    boundary, and an escaped 0xFF is the two-byte sequence IAC IAC: without
    a carry the trailing half was dropped and the next chunk began with a
    lone IAC, which was then read as the start of a new sequence and ate the
    byte after it. Every 0xFF in a binary payload could therefore corrupt
    the stream -- measured on RISC OS: 201 base64 files (no 0xFF anywhere)
    round-tripped perfectly while a 400 KB random blob came back the right
    size and the wrong contents, with the host tar reporting "Skipping to
    next header".

    When `binary` is a dict, option 0 (TRANSMIT-BINARY, RFC 856) is treated
    as already REQUESTED by us in both directions (the tar-sync stream path
    sends IAC WILL 0 + IAC DO 0 right after connect): an incoming DO/WILL 0
    is the peer's acceptance and only flips binary['out']/binary['in'] --
    answering it again would start a negotiation loop -- and DONT/WONT 0
    flips the flag back off. Every other option is refused as before."""
    IAC, SE, SB = 255, 240, 250
    WILL, WONT, DO, DONT = 251, 252, 253, 254
    if carry:
        data = bytes(carry) + bytes(data)
        carry[:] = b""
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            # Incomplete: hold it for the next chunk instead of dropping it.
            if carry is not None:
                carry.extend(data[i:])
            break
        cmd = data[i + 1]
        if cmd in (DO, DONT, WILL, WONT) and i + 2 >= n:
            if carry is not None:
                carry.extend(data[i:])
            break
        if cmd in (DO, DONT, WILL, WONT):
            opt = data[i + 2]
            try:
                if binary is not None and opt == 0:
                    if cmd == DO:
                        binary['out'] = True
                    elif cmd == DONT:
                        binary['out'] = False
                    elif cmd == WILL:
                        binary['in'] = True
                    elif cmd == WONT:
                        binary['in'] = False
                elif cmd == DO:
                    sock.sendall(bytes([IAC, WONT, opt]))
                elif cmd == WILL:
                    sock.sendall(bytes([IAC, DONT, opt]))
            except OSError:
                pass
            i += 3
        elif cmd == SB:
            j = i + 2
            while j + 1 < n and not (data[j] == IAC and data[j + 1] == SE):
                j += 1
            if j + 1 >= n:
                # IAC SE not in this chunk yet; keep the whole subnegotiation.
                if carry is not None:
                    carry.extend(data[i:])
                break
            i = j + 2
        elif cmd == IAC:
            out.append(IAC)
            i += 2
        else:
            i += 2


def telnet_exec(host_port, cmds, settle=2.0, connect_timeout=10):
    """Run command lines in a plan9 guest over telnet on 127.0.0.1:host_port.
    Returns (connected, transcript). No exit-status channel: callers that
    need one have the guest echo an rc marker (`... && echo done`)."""
    out = bytearray()
    try:
        sock = socket.create_connection(("127.0.0.1", int(host_port)), connect_timeout)
    except OSError:
        return False, ""

    def _read_for(seconds):
        end = time.time() + seconds
        sock.settimeout(0.5)
        while time.time() < end:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return False
            if not data:
                return False
            _telnet_eat_iac(sock, data, out)
        return True

    alive = _read_for(min(settle, 2.0))
    for c in cmds:
        if not alive:
            break
        try:
            sock.sendall(c.encode("utf-8", "replace") + b"\r\n")
        except OSError:
            alive = False
            break
        alive = _read_for(settle)
    try:
        sock.close()
    except OSError:
        pass
    return alive, out.decode("utf-8", "replace")


def _telnet_rc_lines(os_name, cmd, marker):
    """Turn one guest command into the line(s) to send so that a marker line
    announcing completion (and status, where the guest can express one)
    eventually appears in the output. Returns (lines, has_status).

    Per-OS dialects, same survey as telnet_ready():
      * reactos -- cmd.exe chains with && / ||. cmd echoes the raw command
        line, so the caret keeps that echo from matching the marker regex
        while the OUTPUT prints the bare marker (the EXEC^-OK trick from
        reactos.yml).
      * plan9 (rc) -- chains with && / ||, but a BARE word containing '='
        is an assignment token: unquoted `echo M=0` dies with
        "token '=': syntax error" (probed on a live 9front guest,
        2026-08-15). So the marker word is fully quoted (a quoted '=' is
        legal) and split with rc's ^ concatenation, so the pty echo of the
        command line cannot match the marker regex while the OUTPUT prints
        the joined marker.
      * redox (ion) -- chains with && / || and collapses the empty '' in
        an echoed command line, exactly like the "echo anyvm''-ready"
        probe ('=' is an ordinary character in ion words).
      * riscos -- anyvmd.py is NOT a shell: no operators, no status
        variable. But the agent runs each line to completion before reading
        the next, so a bare Echo sent after the command marks completion.
        Status itself is not expressible; the marker always says 0."""
    if os_name == "riscos":
        return ([cmd, "Echo {}=0".format(marker)], False)
    if os_name == "reactos":
        return (["{} && echo {}^=0 || echo {}^=1".format(cmd, marker, marker)],
                True)
    if os_name == "plan9":
        mhead, mtail = marker[:4], marker[4:]
        return (["{} && echo '{}'^'{}=0' || echo '{}'^'{}=1'".format(
            cmd, mhead, mtail, mhead, mtail)], True)
    return (["{} && echo {}''=0 || echo {}''=1".format(cmd, marker, marker)],
            True)


def telnet_exec_status(host_port, os_name, cmd, timeout_sec=7200,
                       connect_timeout=10, debug=False):
    """Run ONE guest command line over telnet on 127.0.0.1:host_port and wait
    for its completion marker instead of a fixed settle window, so a long
    build is neither cut short nor raced by whatever runs next.

    Returns (connected, transcript, rc). rc is the guest command's 0/1
    status where the guest shell can express one (reactos cmd.exe, redox
    ion, plan9 rc), always 0 on riscos (agent has no status channel), 124
    when the marker did not appear within timeout_sec, 255 when the telnet
    session itself broke. The marker lines are stripped from the returned
    transcript."""
    marker = "__ANYVM_RC_{}".format(os.urandom(4).hex())
    lines, has_status = _telnet_rc_lines(os_name, cmd, marker)
    rc_re = re.compile(re.escape(marker) + r"=([01])")

    out = bytearray()
    try:
        sock = socket.create_connection(("127.0.0.1", int(host_port)),
                                        connect_timeout)
    except OSError:
        return False, "", 255

    alive = True
    rc = 124
    try:
        # Drain the banner/prompt briefly so the deadline below measures the
        # command, not the login chatter.
        end = time.time() + 2.0
        sock.settimeout(0.5)
        while time.time() < end:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                alive = False
                break
            if not data:
                alive = False
                break
            _telnet_eat_iac(sock, data, out)
        for line in lines:
            if not alive:
                break
            try:
                sock.sendall(line.encode("utf-8", "replace") + b"\r\n")
            except OSError:
                alive = False
                break
        deadline = time.time() + max(1, int(timeout_sec))
        while alive and time.time() < deadline:
            # Only the tail can hold a fresh marker; decoding the whole
            # transcript every round would be quadratic on a chatty build.
            m = rc_re.search(out[-8192:].decode("utf-8", "replace"))
            if m:
                rc = int(m.group(1))
                break
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                alive = False
                break
            if not data:
                alive = False
                break
            _telnet_eat_iac(sock, data, out)
        else:
            if alive:
                debuglog(debug, "telnet_exec_status: marker not seen within "
                         "{}s; reporting rc 124".format(timeout_sec))
    finally:
        try:
            sock.close()
        except OSError:
            pass

    text = out.decode("utf-8", "replace")
    # Strip every line that carries the marker: the raw command-line echo
    # (caret / quote forms included, since they contain the marker token) and
    # the marker output line itself.
    text = "\n".join(l for l in text.splitlines() if marker not in l)
    if not alive:
        return False, text, 255
    return True, text, rc


def telnet_ready(host_port, os_name=None):
    """One telnet marker probe: connect, echo a marker, look for it coming back.

    Neither the shell on the far side nor the trick that keeps the guest's own
    echo of the command line from matching is universal:
      * plan9/9front answers with rc, where '' splits the word;
      * ReactOS answers with cmd.exe, where '' is not a quote at all and the
        caret is the escape character instead;
      * RISC OS answers with riscos-builder's anyvmd.py, which is NOT a shell.
        It collapses neither '' nor ^, so both of those come back with the
        separator still embedded -- verified against a live guest, which
        answered the two shell forms with the literal "anyvm''-ready" and
        "anyvm^-ready" and therefore could never match. Its `Echo` is a plain
        command, and the agent never echoes the line it was sent, so a BARE
        marker is both necessary and safe there.

    Sending the wrong form to a shell-backed guest is harmless -- it just
    prints itself and fails to match. A bare marker is NOT harmless on
    cmd.exe, which echoes the command line and would then match its own echo,
    reporting ready before anything has actually run. So riscos gets its own
    probe instead of one more entry in the shared list."""
    if os_name == "riscos":
        cmds = ["Echo anyvm-ready"]
    else:
        cmds = ["echo anyvm''-ready",     # rc (plan9/9front)
                "echo anyvm^-ready"]      # cmd.exe (reactos)
    ok, text = telnet_exec(host_port, cmds, settle=2.0, connect_timeout=5)
    return ok and ("anyvm-ready" in text)


def interactive_telnet(host_port, connect_timeout=10):
    """Attach an interactive telnet session to the plan9 guest on
    127.0.0.1:host_port -- the ssh-shell analogue for `anyvm --os plan9` when
    stdin is a TTY and no `-- cmd` was given. Full-duplex bridge: a reader
    thread pumps guest output (IAC-stripped) to stdout while the main loop
    forwards keystrokes. The guest telnetd's pty echoes typed characters, so
    no local echo is added. Returns when the guest closes the connection or
    the user presses the escape key (Ctrl-])."""
    try:
        sock = socket.create_connection(("127.0.0.1", int(host_port)), connect_timeout)
    except OSError as e:
        log("Could not open telnet session to the guest: {}".format(e))
        return
    log("Connected to the 9front guest over telnet. Press Ctrl-] to disconnect "
        "(the VM keeps running).")
    stop = threading.Event()
    # Both the reader thread (guest output) and the input loop (local echo)
    # write to stdout; serialize them so bytes don't interleave.
    out_lock = threading.Lock()

    def _emit(data):
        try:
            with out_lock:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            return True
        except (OSError, ValueError):
            return False

    def reader():
        try:
            sock.settimeout(0.3)
            while not stop.is_set():
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                out = bytearray()
                _telnet_eat_iac(sock, data, out)
                if out and not _emit(bytes(out)):
                    break
        finally:
            stop.set()

    rthread = threading.Thread(target=reader, daemon=True)
    rthread.start()

    if IS_WINDOWS:
        # No raw-tty handling on Windows: forward a line at a time. Good enough
        # to drive the rc shell (Enter -> CRLF, which the guest pty cooks).
        try:
            while not stop.is_set():
                line = sys.stdin.readline()
                if not line:
                    break
                try:
                    sock.sendall(line.rstrip("\r\n").encode("utf-8", "replace") + b"\r\n")
                except OSError:
                    break
        except (KeyboardInterrupt, OSError):
            pass
    else:
        import termios
        import tty
        import select
        fd = sys.stdin.fileno()
        old_attr = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while not stop.is_set():
                r, _, _ = select.select([fd], [], [], 0.3)
                if fd not in r:
                    continue
                ch = os.read(fd, 1)
                if not ch:
                    break
                if ch == b'\x1d':  # Ctrl-] : the telnet escape -> disconnect
                    break
                # We refused the telnet ECHO option (see _telnet_eat_iac), so
                # the server does NOT echo and raw mode disabled the terminal's
                # own echo -- the client must echo locally or typing is
                # invisible. Echo, then forward.
                if ch == b'\r' or ch == b'\n':
                    _emit(b'\r\n')
                    to_send = b'\r\n'
                elif ch in (b'\x7f', b'\x08'):   # DEL / Backspace: erase one col
                    _emit(b'\b \b')
                    to_send = b'\x08'
                elif ch == b'\x03':              # Ctrl-C: forward, don't echo
                    to_send = ch
                else:
                    _emit(ch)
                    to_send = ch
                try:
                    sock.sendall(to_send)
                except OSError:
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
    stop.set()
    try:
        sock.close()
    except OSError:
        pass
    sys.stdout.write("\n")


def sync_9p(host_port, vhost, vguest, debug=False, excludes=None):
    """Mount the guest's exportfs 9P share and copy `vhost` into `vguest`.

    Linux-host only: uses the kernel v9fs client (mount -t 9p) via sudo. The
    guest exports "/" over 9P (plan9-builder bakes the listener + the
    exportfs errstr patch that makes Twalk-before-create map correctly under
    Linux v9fs). uname=glenda + dfltuid/dfltgid map the unauthenticated
    attach to the invoking user so writes carry natural ownership."""
    if IS_WINDOWS or sys.platform == "darwin":
        log("Warning: --sync 9p needs the Linux kernel v9fs client; not "
            "available on this host. Skipping folder sync (use a Linux host, "
            "or copy files manually over the 9P console).")
        return
    if not os.path.exists(vhost):
        log("Warning: Host path {} does not exist; skipping.".format(vhost))
        return
    # mount -t 9p needs root. Use sudo only when we are not already root and
    # passwordless sudo is available.
    if os.geteuid() == 0:
        sudo = []
    elif sudo_noninteractive():
        sudo = ["sudo", "-n"]
    else:
        log("Warning: --sync 9p needs root (mount -t 9p) and passwordless "
            "sudo is unavailable; skipping folder sync.")
        return
    mnt = tempfile.mkdtemp(prefix="anyvm-9p-")
    try:
        uid = os.getuid()
        gid = os.getgid()
        opts = ("trans=tcp,port={},version=9p2000,uname=glenda,aname=/,"
                "dfltuid={},dfltgid={}".format(host_port, uid, gid))
        mount_cmd = sudo + ["mount", "-t", "9p", "-o", opts, "127.0.0.1", mnt]
        log("Syncing via 9p: {} -> {} (guest exportfs on :564)".format(vhost, vguest))
        mounted = False
        for attempt in range(1, 6):
            ret = subprocess.call(mount_cmd, stdout=DEVNULL,
                                  stderr=(None if debug else DEVNULL))
            if ret == 0:
                mounted = True
                break
            debuglog(debug, "9p mount attempt {} failed rc={}".format(attempt, ret))
            time.sleep(2)
        if not mounted:
            # Same reasoning as the tar push: a share that was asked for and
            # failed leaves every later step working on files that are not
            # there, so stop instead of continuing into a confusing failure.
            fatal("9p mount failed after retries; the guest does not have "
                  "your files, so the run was stopped here.")
        # vguest is an absolute guest path (e.g. /usr/glenda/work); the 9P
        # root is the guest's "/", so strip the leading slash to join.
        dest = os.path.join(mnt, vguest.lstrip("/"))
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as e:
            log("Warning: cannot create {} in guest over 9p: {}".format(vguest, e))
            return
        skip = set()
        for e in (excludes or []):
            # Only top-level names are meaningful here: this copies the
            # share entry by entry rather than walking it like tar does.
            top = e.replace(os.sep, '/').strip('/').split('/')[0]
            if top:
                skip.add(top)
        if os.path.isdir(vhost):
            for entry in os.listdir(vhost):
                if entry in skip:
                    debuglog(debug, "9p sync skipping excluded {}".format(entry))
                    continue
                src = os.path.join(vhost, entry)
                dst = os.path.join(dest, entry)
                try:
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                except OSError as e:
                    log("Warning: 9p copy {} failed: {}".format(src, e))
        else:
            try:
                shutil.copy2(vhost, dest)
            except OSError as e:
                log("Warning: 9p copy {} failed: {}".format(vhost, e))
        log("9p sync complete.")
    finally:
        subprocess.call(sudo + ["umount", mnt], stdout=DEVNULL, stderr=DEVNULL)
        try:
            os.rmdir(mnt)
        except OSError:
            pass


def _replace_file(src, dst):
    """copy2 src over dst, replacing dst even when it is read-only.

    A plain copy2 opens the destination for writing and fails with EACCES on
    a mode-444 file -- which is exactly what git's pack files are, so the
    first 9P copy-back over a checked-out repo died on
    .git/objects/pack/*.idx. tar and rsync do not hit this because they
    replace the entry rather than write into it; do the same."""
    if os.path.lexists(dst):
        try:
            os.chmod(dst, 0o644)
        except OSError:
            pass
        try:
            os.remove(dst)
        except OSError:
            pass
    shutil.copy2(src, dst)


def _copytree_replacing(src, dst):
    """shutil.copytree with _replace_file as the copy function, so a
    read-only file anywhere in the tree does not abort the whole copy."""
    shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=_replace_file)


def sync_9p_pull(host_port, vhost, vguest, debug=False, excludes=None):
    """Copy the guest's `vguest` tree back to the host `vhost` over 9P.

    The mirror image of sync_9p, and the copy-back path for a 9P guest: the
    push is a one-shot copy through a mount that is unmounted again, NOT a
    live share, so without this whatever the guest wrote would never reach
    the host. Doing it over 9P rather than the telnet tar stream is
    deliberate -- `tar c` piped through 9front's telnet pty came back
    corrupted ("This does not look like a tar archive", probed 2026-08-15),
    while this path is the same kernel v9fs mount the push already uses.

    Returns True when the tree came back, False on any failure the caller
    should surface (mount refused, no sudo, wrong host OS)."""
    if IS_WINDOWS or sys.platform == "darwin":
        log("Warning: --sync 9p needs the Linux kernel v9fs client; not "
            "available on this host. Skipping copy-back.")
        return False
    if os.geteuid() == 0:
        sudo = []
    elif sudo_noninteractive():
        sudo = ["sudo", "-n"]
    else:
        log("Warning: 9p copy-back needs root (mount -t 9p) and passwordless "
            "sudo is unavailable; skipping.")
        return False
    mnt = tempfile.mkdtemp(prefix="anyvm-9p-")
    ok = False
    try:
        uid = os.getuid()
        gid = os.getgid()
        opts = ("trans=tcp,port={},version=9p2000,uname=glenda,aname=/,"
                "dfltuid={},dfltgid={}".format(host_port, uid, gid))
        mount_cmd = sudo + ["mount", "-t", "9p", "-o", opts, "127.0.0.1", mnt]
        log("Syncing back via 9p: {} -> {}".format(vguest, vhost))
        mounted = False
        for attempt in range(1, 6):
            ret = subprocess.call(mount_cmd, stdout=DEVNULL,
                                  stderr=(None if debug else DEVNULL))
            if ret == 0:
                mounted = True
                break
            debuglog(debug, "9p mount attempt {} failed rc={}".format(attempt, ret))
            time.sleep(2)
        if not mounted:
            log("Warning: 9p mount failed after retries; skipping copy-back.")
            return False
        src_root = os.path.join(mnt, vguest.lstrip("/"))
        if not os.path.isdir(src_root):
            log("Warning: {} does not exist in the guest; nothing to copy "
                "back.".format(vguest))
            return False
        try:
            os.makedirs(vhost, exist_ok=True)
        except OSError as e:
            log("Warning: cannot create host path {}: {}".format(vhost, e))
            return False
        skip = set()
        for e in (excludes or []):
            top = e.replace(os.sep, '/').strip('/').split('/')[0]
            if top:
                skip.add(top)
        failures = 0
        for entry in os.listdir(src_root):
            if entry in skip:
                debuglog(debug, "9p copy-back skipping excluded {}".format(entry))
                continue
            src = os.path.join(src_root, entry)
            dst = os.path.join(vhost, entry)
            try:
                if os.path.isdir(src):
                    _copytree_replacing(src, dst)
                else:
                    _replace_file(src, dst)
            except (OSError, shutil.Error) as e:
                log("Warning: 9p copy-back {} failed: {}".format(src, e))
                failures += 1
        ok = (failures == 0)
        if ok:
            log("9p copy-back complete.")
    finally:
        subprocess.call(sudo + ["umount", mnt], stdout=DEVNULL, stderr=DEVNULL)
        try:
            os.rmdir(mnt)
        except OSError:
            pass
    return ok


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


# ---------------------------------------------------------------------------
# tar sync: stream a directory as a ustar archive and unpack it on the other
# side. push = host -> guest at boot; pull = guest -> host after the
# passthrough command finishes (a one-shot copy, not a live mount -- the
# same semantics the vmactions copyback uses). The archive rides the guest's
# remote-exec channel: ssh when the guest has an sshd, else its telnetd
# (plan9/9front), else a raw TCP shell. Everything moves in ustar format:
# the GUEST only needs a tar that can read/write ustar on stdin/stdout
# (base-system tar everywhere we ship, including toybox tar on BlissOS and
# Sun tar on Solaris -- ustar is the dialect they all agree on, the same
# reason the vmactions copyback pipes `cpio -o -H ustar`).
#
# The HOST side prefers the system tar binary when one is on PATH (GNU tar
# on Linux, bsdtar on macOS and Windows 10+ -- both accept --format=ustar
# and -C): it is much faster than pure-Python archiving. Python's tarfile
# module is the fallback, so the sync still works on a tar-less host.
# ---------------------------------------------------------------------------

_host_tar_cache = [False]  # False = not probed yet; then a path or None


def find_host_tar():
    """Absolute path of the host tar binary, or None. Cached."""
    if _host_tar_cache[0] is False:
        _host_tar_cache[0] = shutil.which("tar")
    return _host_tar_cache[0]


def _tar_create_cmd(tar, vhost, excludes=None):
    """argv for the host tar that streams vhost as a ustar archive to
    stdout. `excludes` are /-separated paths relative to vhost."""
    if os.path.isdir(vhost):
        base, item = vhost, "."
    else:
        base, item = (os.path.dirname(vhost) or "."), os.path.basename(vhost)
    cmd = [tar, "-cf", "-", "--format=ustar"]
    for rel in (excludes or []):
        rel = rel.replace(os.sep, '/').strip('/')
        if not rel:
            continue
        # Entries are named "./<rel>/..." (the -C base + "." form). GNU tar
        # and bsdtar anchor/expand exclusion patterns differently; the pair
        # covers the directory itself and its contents on both.
        cmd.extend(["--exclude", "./" + rel, "--exclude", "./" + rel + "/*"])
    cmd.extend(["-C", base, item])
    return cmd


def _write_archive_to(writer, vhost, excludes=None, os_name=None):
    """Stream vhost as a ustar archive into writer.write(). Uses the host
    tar when available, Python tarfile otherwise. Returns True on success.
    Transport errors raised by writer.write() propagate to the caller.

    A guest with DOS naming rules takes the tarfile path even when a host
    tar exists: the filtering in _tar_write_dir is per entry, and the host
    tar has no equivalent (its --exclude works on patterns we would have to
    know in advance). The tree is the runner workspace, so the cost of
    archiving it in Python is small next to losing the whole sync."""
    tar = None if _guest_uses_dos_names(os_name) else find_host_tar()
    if tar:
        try:
            p = subprocess.Popen(_tar_create_cmd(tar, vhost, excludes),
                                 stdout=subprocess.PIPE)
        except OSError as exc:
            log("Warning: could not run the host tar ({}); falling back to "
                "the built-in archiver.".format(exc))
            p = None
        if p is not None:
            while True:
                chunk = p.stdout.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
            p.stdout.close()
            if p.wait() != 0:
                log("Warning: the host tar exited non-zero while creating "
                    "the archive.")
                return False
            return True
    tf = tarfile.open(fileobj=writer, mode='w|', format=tarfile.USTAR_FORMAT)
    _tar_write_dir(tf, vhost, excludes, os_name=os_name)
    tf.close()
    return True


def _host_tar_extract_pump(tar, reader, dest):
    """Feed bytes from reader.read() into a host `tar -xf - -C dest` child.

    The pump tracks the archive structure itself and stops at the
    end-of-archive marker (two consecutive all-zero 512-byte blocks): GNU
    tar reading a pipe blocks until it fills a whole 10240-byte record, so
    an archive whose producer pads less (busybox tar) would otherwise leave
    tar -- and this pump -- waiting on bytes that never come. Closing tar's
    stdin at the marker resolves that: tar sees EOF on a complete archive
    and exits 0. Anything the remote shell prints after the marker (prompt
    bytes, padding) is deliberately not fed to tar. True on tar rc 0."""
    try:
        p = subprocess.Popen([tar, "-xf", "-", "-C", dest],
                             stdin=subprocess.PIPE)
    except OSError as exc:
        log("Warning: could not run the host tar for extraction: {}".format(exc))
        return False
    # Belt and braces: if tar exits early (bad archive), stop the reader
    # from waiting out its quiet timeout on a stream nobody consumes.
    if hasattr(reader, 'stop_check'):
        reader.stop_check = lambda: p.poll() is not None
    pend = bytearray()
    zeros = 0
    eoa = False
    zero_block = b"\x00" * 512
    try:
        while not eoa:
            chunk = reader.read(65536)
            if not chunk:
                break
            p.stdin.write(chunk)
            pend.extend(chunk)
            while len(pend) >= 512:
                if bytes(pend[:512]) == zero_block:
                    zeros += 1
                    if zeros >= 2:
                        eoa = True
                        break
                else:
                    zeros = 0
                del pend[:512]
    except OSError:
        # tar finished the archive and exited; the broken pipe is the
        # expected end signal, and the rc below is the real verdict.
        pass
    try:
        p.stdin.close()
    except OSError:
        pass
    return p.wait() == 0


def _extract_from_reader(reader, dest):
    """Extract a tar stream arriving via reader.read() into dest, with the
    host tar when available and tarfile otherwise."""
    tar = find_host_tar()
    if tar:
        return _host_tar_extract_pump(tar, reader, dest)
    return _tar_extract_stream(reader, dest)


# Windows-family guests cannot create every name a POSIX host can archive,
# and on the streaming transports that is not a partial failure -- it takes
# the whole sync down. Reproduced locally on reactos: one file named `aux`
# in the tree makes the guest tar print
#   tar: can't open './work/aux': No such file or directory (ENOENT)
# and exit non-zero, which breaks the `&&` chain before the completion
# marker; every REMAINING archive byte is then read by cmd.exe as a command
# ("Bad command or filename - ..."), the host never sees its marker, and the
# push is declared fatal. The control run with the same tree minus that one
# name passed. So these entries are dropped from the archive with a warning,
# exactly like the ustar-limit skips below.
#
# The rule set is copied from Microsoft's "Naming Files, Paths, and
# Namespaces" (learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file),
# section "Naming Conventions", not written from memory. Verbatim, the
# reserved names are:
#
#   "CON, PRN, AUX, NUL, COM1, COM2, COM3, COM4, COM5, COM6, COM7, COM8,
#    COM9, COM<sup>1</sup>, COM<sup>2</sup>, COM<sup>3</sup>, LPT1, LPT2,
#    LPT3, LPT4, LPT5, LPT6, LPT7, LPT8, LPT9, LPT<sup>1</sup>,
#    LPT<sup>2</sup>, and LPT<sup>3</sup>. Also avoid these names followed
#    immediately by an extension; for example, NUL.txt and NUL.tar.gz are
#    both equivalent to NUL."
#
# -- hence the split('.')[0] below. The same section lists the reserved
# characters as < > : " / \ | ? * plus integer value zero and 1 through 31,
# and says "Do not end a file or directory name with a space or a period."
# Forward slash is the arcname separator here and is split on, so it is not
# in the char set; backslash is, because a POSIX host can legally put one
# INSIDE a single name component.
#
# The superscript COM/LPT forms are the ISO/IEC 8859-1 digits the same page
# calls out: "Windows recognizes the 8-bit ISO/IEC 8859-1 superscript digits
# 1, 2, and 3 as digits and treats them as valid parts of COM# and LPT#
# device names, making them reserved in every directory." They are written
# as \u escapes so this file stays pure 7-bit ASCII.
_DOS_RESERVED_STEMS = frozenset([
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    "com\u00b9", "com\u00b2", "com\u00b3",
    "lpt\u00b9", "lpt\u00b2", "lpt\u00b3",
])
_DOS_RESERVED_CHARS = '<>:"|?*\\'


def _guest_uses_dos_names(os_name):
    """True when the guest filesystem follows DOS/Windows naming rules, so
    the archive has to be filtered before it is streamed."""
    return (os_name or "").lower() in ("reactos",)


def _dos_name_problem(arcname):
    """Why a Windows-family guest cannot create `arcname`, or None if it
    can. Checked per path component, since any one of them is a real
    directory or file name on the guest."""
    for part in arcname.replace(os.sep, '/').split('/'):
        if not part or part == '.':
            continue
        for ch in part:
            if ch in _DOS_RESERVED_CHARS or ord(ch) < 32:
                return "illegal character {!r}".format(ch)
        if part[-1] in ' .':
            return "component {!r} ends in a space or period".format(part)
        if part.split('.')[0].lower() in _DOS_RESERVED_STEMS:
            return "reserved device name {!r}".format(part)
    return None


def _tar_write_dir(tf, vhost, excludes=None, os_name=None):
    """Add vhost's contents to the open TarFile `tf`, arcnames relative to
    vhost (or the basename when vhost is a single file). `excludes` are
    /-separated paths relative to vhost; a match skips that entry and
    everything below it. Entries ustar cannot represent (paths over 255
    bytes, files over 8GB) are warned about and skipped instead of aborting
    the whole stream, as are entries the guest could not create (see
    _dos_name_problem) and, on those guests, symlinks."""
    ex = set()
    for e in (excludes or []):
        ex.add(e.replace(os.sep, '/').strip('/'))

    dos = _guest_uses_dos_names(os_name)

    def add_one(path, arcname):
        if dos:
            why = _dos_name_problem(arcname)
            if why is not None:
                log("Warning: tar sync skipping {}: the guest cannot create "
                    "this name ({}).".format(path, why))
                return False
            if os.path.islink(path):
                log("Warning: tar sync skipping the symlink {}: the guest "
                    "cannot create symlinks.".format(path))
                return False
        try:
            tf.add(path, arcname=arcname, recursive=False)
            return True
        except (ValueError, OSError) as exc:
            log("Warning: tar sync skipping {}: {}".format(path, exc))
            return False

    def walk(dirpath, rel):
        try:
            entries = sorted(os.listdir(dirpath))
        except OSError as exc:
            log("Warning: tar sync cannot read {}: {}".format(dirpath, exc))
            return
        for entry in entries:
            childrel = entry if not rel else rel + '/' + entry
            if childrel in ex:
                continue
            child = os.path.join(dirpath, entry)
            if os.path.isdir(child) and not os.path.islink(child):
                # A rejected directory takes its whole subtree with it: the
                # guest cannot create the parent, so nothing under it could
                # be extracted either.
                if add_one(child, childrel):
                    walk(child, childrel)
            else:
                add_one(child, childrel)

    if os.path.isdir(vhost):
        walk(vhost, "")
    else:
        add_one(vhost, os.path.basename(vhost))


def _tar_extract_stream(fileobj, dest):
    """Extract a tar stream from `fileobj` into dest. Returns True when the
    archive's end-of-record marker was reached cleanly."""
    try:
        tf = tarfile.open(fileobj=fileobj, mode='r|*')
    except (tarfile.TarError, OSError, EOFError) as exc:
        log("Warning: tar sync could not read the archive stream: {}".format(exc))
        return False
    try:
        try:
            tf.extractall(dest, filter='tar')
        except TypeError:
            # tarfile before the extraction-filter backports (3.8.17,
            # 3.9.17, 3.10.12, 3.11.4) has no filter= parameter.
            tf.extractall(dest)
        tf.close()
        return True
    except (tarfile.TarError, OSError, EOFError) as exc:
        log("Warning: tar sync extraction failed: {}".format(exc))
        return False


# Guests whose default /usr/bin/tar is Sun tar (illumos/Solaris): function
# letters are documented WITHOUT a leading dash there ("tar xf -", illumos
# man tar(1)), and the dashless form is also accepted by GNU tar, so it is
# the safe spelling for these OSes whichever tar ends up first in PATH.
# Everything else gets the dashed form -- toybox tar (BlissOS) accepts ONLY
# that one, and GNU/bsdtar/NetBSD pax-tar all take it too.
SUN_TAR_OSES = ("solaris", "omnios", "openindiana", "tribblix")


def _guest_tar_spelling(os_name, extract):
    if os_name in SUN_TAR_OSES:
        return "tar xf -" if extract else "tar cf - ."
    return "tar -xf -" if extract else "tar -cf - ."


def _tar_push_ssh(ssh_cmd, vhost, vguest, excludes=None, os_name=None):
    """Host tar stream -> `ssh guest 'tar -xf -'`. With a host tar on PATH
    the two processes are piped directly (no Python in the data path)."""
    remote = "mkdir -p '{0}' && cd '{0}' && {1}".format(
        vguest, _guest_tar_spelling(os_name, extract=True))
    tar = find_host_tar()
    if tar:
        try:
            p_tar = subprocess.Popen(_tar_create_cmd(tar, vhost, excludes),
                                     stdout=subprocess.PIPE)
            p_ssh = subprocess.Popen(ssh_cmd + [remote], stdin=p_tar.stdout)
        except OSError as exc:
            log("Warning: could not start the tar push pipeline: {}".format(exc))
            return False
        # Drop our handle so the host tar gets EPIPE if ssh dies early.
        p_tar.stdout.close()
        rc_ssh = p_ssh.wait()
        rc_tar = p_tar.wait()
        return rc_ssh == 0 and rc_tar == 0
    try:
        p = subprocess.Popen(ssh_cmd + [remote], stdin=subprocess.PIPE)
    except OSError as exc:
        log("Warning: could not start ssh for the tar push: {}".format(exc))
        return False
    ok = True
    try:
        tf = tarfile.open(fileobj=p.stdin, mode='w|',
                          format=tarfile.USTAR_FORMAT)
        _tar_write_dir(tf, vhost, excludes, os_name=os_name)
        tf.close()
    except (OSError, tarfile.TarError) as exc:
        # A guest-side failure surfaces here as a broken pipe.
        log("Warning: tar push stream failed: {}".format(exc))
        ok = False
    try:
        p.stdin.close()
    except OSError:
        pass
    return p.wait() == 0 and ok


def _tar_pull_ssh(ssh_cmd, vhost, vguest, os_name=None):
    """`ssh guest 'tar -cf - .'` -> extract on the host. With a host tar on
    PATH the two processes are piped directly."""
    remote = "cd '{0}' && {1}".format(
        vguest, _guest_tar_spelling(os_name, extract=False))
    tar = find_host_tar()
    if tar:
        try:
            p_ssh = subprocess.Popen(ssh_cmd + [remote],
                                     stdout=subprocess.PIPE)
            p_tar = subprocess.Popen([tar, "-xf", "-", "-C", vhost],
                                     stdin=p_ssh.stdout)
        except OSError as exc:
            log("Warning: could not start the tar pull pipeline: {}".format(exc))
            return False
        p_ssh.stdout.close()
        rc_tar = p_tar.wait()
        rc_ssh = p_ssh.wait()
        if rc_ssh != 0 and rc_tar == 0:
            log("Warning: guest-side tar/ssh exited with rc={} during the "
                "pull.".format(rc_ssh))
        return rc_tar == 0
    try:
        p = subprocess.Popen(ssh_cmd + [remote], stdout=subprocess.PIPE)
    except OSError as exc:
        log("Warning: could not start ssh for the tar pull: {}".format(exc))
        return False
    ok = _tar_extract_stream(p.stdout, vhost)
    try:
        p.stdout.close()
    except OSError:
        pass
    rc = p.wait()
    if rc != 0 and ok:
        # The end-of-archive marker already proved the stream was complete;
        # a non-zero guest tar rc past that point is advisory (e.g. an
        # unreadable file it warned about).
        log("Warning: guest-side tar exited with rc={} during the pull.".format(rc))
    return ok


class _TelnetTarWriter(object):
    """write()-only file object tarfile streams into: escapes IAC bytes
    (0xFF -> 0xFF 0xFF) and counts the payload."""
    def __init__(self, sock):
        self.sock = sock
        self.sent = 0

    def write(self, data):
        data = bytes(data)
        self.sock.sendall(data.replace(b"\xff", b"\xff\xff"))
        self.sent += len(data)
        return len(data)


class _StreamTarReader(object):
    """read()-only file object feeding tarfile from a socket. In telnet mode
    the bytes pass through _telnet_eat_iac (IAC stripped, BINARY tracked);
    in raw-tcp mode they are taken verbatim. The first output line (the
    shell's echo of the tar command) is skipped before archive bytes are
    handed out.

    Two silence budgets, because the two silences mean different things.
    BEFORE the first byte the guest is walking the tree and has not written
    anything yet -- measured on Redox, `tar c` over 2002 files stayed quiet
    for 76 s and then delivered 5 MB in under 2 s, so that wait grows with
    the FILE COUNT and a small bound just kills healthy pulls (a real
    workspace of ~4600 files blew past the old flat 120 s and the host tar
    reported "This does not look like a tar archive" because nothing had
    arrived). AFTER bytes start flowing, `quiet_max` applies instead -- also
    generous, because that same measurement shows the guest going quiet for
    over a minute while doing real work. The 76 s pause landed before the
    first byte only because that is when tar happened to be walking; on a
    deeper tree the same stretch of walking falls in the MIDDLE of the
    archive, so a tight bound there would kill a healthy pull exactly the
    way the flat 120 s did. A genuinely dead guest then takes 10 min to
    notice, which is the cheaper mistake: the job timeout is the real
    backstop, while a false failure costs a red run plus the hunt for a bug
    that was never there."""
    def __init__(self, sock, telnet=False, binary=None, quiet_max=600,
                 skip_echo=True, start_max=900):
        self.sock = sock
        self.telnet = telnet
        self.binary = binary
        self.quiet_max = quiet_max
        self.start_max = start_max
        self.got_data = False
        # Half of a telnet sequence left over from the previous recv; see
        # _telnet_eat_iac. A stream reader without this loses every escaped
        # 0xFF that lands on a chunk boundary.
        self.carry = bytearray()
        self.buf = bytearray()
        self.eof = False
        # skip_echo=False for channels that do not echo the command line
        # (anyvmtd on reactos: plain pipes, no pty) -- skipping there would
        # eat archive bytes up to the first 0x0A.
        self.echo_skipped = not skip_echo
        # Optional early-out probe set by the consumer (the host-tar pump
        # sets it to "has tar exited?"): checked on every idle tick so the
        # reader stops waiting for stream bytes nobody needs anymore.
        self.stop_check = None

    def _fill(self):
        quiet = 0.0
        self.sock.settimeout(0.5)
        while not self.eof:
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                if self.stop_check is not None and self.stop_check():
                    self.eof = True
                    return
                quiet += 0.5
                limit = self.quiet_max if self.got_data else self.start_max
                if quiet >= limit:
                    log("Warning: tar pull stream {} for {}s; giving up.".format(
                        "stalled" if self.got_data else
                        "produced nothing", limit))
                    self.eof = True
                continue
            except OSError:
                self.eof = True
                return
            if not data:
                self.eof = True
                return
            self.got_data = True
            before = len(self.buf)
            if self.telnet:
                _telnet_eat_iac(self.sock, data, self.buf, binary=self.binary,
                                carry=self.carry)
            else:
                self.buf.extend(data)
            if len(self.buf) > before:
                return

    def read(self, n):
        while not self.echo_skipped:
            nl = self.buf.find(b"\n")
            if nl >= 0:
                del self.buf[:nl + 1]
                self.echo_skipped = True
                break
            if self.eof:
                self.echo_skipped = True
                break
            self._fill()
        # Return whatever is available rather than insisting on n bytes:
        # both consumers (tarfile's stream reader and the host-tar pump)
        # handle short reads, and blocking for a full buffer after the
        # archive has ended just stalls the session.
        while not self.buf and not self.eof:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


# Upper bound on a single blocking send of archive bytes into the guest.
# Generous on purpose: it exists to stop a dead guest from hanging the run
# forever, NOT to pace the transfer. The emulated agent guests consume a
# large tree slowly, and a tight value here silently turns a slow push into
# a failed one.
TAR_PUSH_SOCKET_TIMEOUT = 300


def _tar_push_telnet(host_port, vhost, vguest, excludes=None, debug=False,
                     os_name=None):
    """Push over the guest's telnetd on 127.0.0.1:host_port -- the stream
    mode for guests with no sshd (plan9/9front rc over its telnetd, reactos
    cmd.exe over the baked anyvmtd). The extract command runs on the telnet
    session and the archive bytes follow down the same connection; the
    success marker is chained onto the command itself (`&& echo ...`) so it
    only prints when the guest tar exited 0. Telnet is not naturally 8-bit
    clean: IAC bytes are escaped by doubling and BINARY mode (RFC 856) is
    requested in both directions up front; a telnetd that refuses BINARY may
    still cook control bytes in its pty, so the refusal is warned about
    loudly -- except on reactos, where anyvmtd is pipe-based (no pty), does
    the IAC IAC unescape itself, and refuses options by design."""
    IAC, WILL, DO = 255, 251, 253
    try:
        sock = socket.create_connection(("127.0.0.1", int(host_port)), 10)
    except OSError as exc:
        log("Warning: tar sync could not connect to the guest telnetd: {}".format(exc))
        return False
    binary = {'in': False, 'out': False}
    transcript = bytearray()
    drain_carry = bytearray()
    lock = threading.Lock()
    stop = threading.Event()

    # A socket timeout is per-SOCKET, not per-direction, so the reader must
    # not set a short one here: this very socket is what the archive is
    # written to, and sendall() would then inherit it. That is exactly what
    # broke the first big push -- 4600 files into a guest that could not
    # drain them fast enough, sendall blocked past the reader's 0.5s and
    # died with "tar push over telnet failed: timed out" 3s in, while every
    # small-directory test passed. The reader polls with select instead, and
    # the socket keeps a long timeout for the writer.
    sock.settimeout(TAR_PUSH_SOCKET_TIMEOUT)

    def drain():
        while not stop.is_set():
            try:
                readable, _, _ = select.select([sock], [], [], 0.5)
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            with lock:
                _telnet_eat_iac(sock, data, transcript, binary=binary,
                                carry=drain_carry)

    t = threading.Thread(target=drain)
    t.daemon = True
    t.start()
    ok = False
    try:
        sock.sendall(bytes([IAC, WILL, 0, IAC, DO, 0]))
        time.sleep(2.0)  # banner, prompt and option replies settle
        if not binary['out'] and os_name != "reactos":
            log("Warning: the guest telnetd did not acknowledge BINARY mode; "
                "the tar stream may be corrupted by pty processing.")
        # The whole session is one command line: the shell runs tar, tar
        # consumes the archive bytes that follow on the same connection,
        # and the chained echo prints the marker only when tar exited 0.
        # The marker is split ('' in rc, ^ in cmd.exe) so an echo of the
        # command line itself cannot match. Leftover archive padding after
        # tar exits is read by the shell as garbage input -- noise, but the
        # marker has printed by then.
        if os_name == "reactos":
            # cmd.exe via anyvmtd; the baked busybox-w32 provides tar.
            #
            # `& if errorlevel 1` rather than a bare `&& echo`: when the
            # guest tar dies partway (one un-creatable name is enough) the
            # chained-on-success form prints NOTHING, so the host learns
            # only that no marker arrived -- after waiting out the whole
            # deadline, and with no idea why. `if errorlevel` is evaluated
            # when the line RUNS, not when cmd parses it (unlike
            # %errorlevel%), so a failure marker comes back immediately and
            # names itself.
            cmd = ('mkdir "{0}" 2>nul & cd /d "{0}" && '
                   'C:\\anyvm\\tar.exe -xf - '
                   '& if errorlevel 1 (echo anyvm^-tar-fail) '
                   'else (echo anyvm^-tar-done)').format(vguest)
        elif os_name == "riscos":
            # anyvmd.py, not a shell: RISC OS has no `&&` and no tar, so the
            # agent parses this whole line itself, extracts with Python's
            # tarfile, and prints the marker when that returned cleanly. The
            # marker needs no split here because the agent never echoes what
            # it was sent, so an echo cannot match it.
            cmd = "mkdir -p '{0}' && cd '{0}' && tar -xf -".format(vguest)
        else:
            # rc (the plan9 shell); plan9 tar defaults to stdin for x.
            cmd = ("mkdir -p '{0}' && cd '{0}' && tar x && "
                   "echo anyvm''-tar-done").format(vguest)
        sock.sendall(cmd.encode("utf-8") + b"\r\n")
        time.sleep(1.0)
        writer = _TelnetTarWriter(sock)
        if not _write_archive_to(writer, vhost, excludes, os_name=os_name):
            log("Warning: the archive stream was incomplete; the guest may "
                "be left waiting on its tar.")
        if os_name == "reactos":
            # busybox tar does not exit at the end-of-archive blocks -- it
            # keeps reading until stdin EOF (verified live: files extracted
            # but the chained echo never ran). anyvmtd understands a
            # half-close (stdin EOF to the child while its output keeps
            # flowing back), so send one: tar exits, the chained echo
            # prints the marker, cmd.exe ends the session. plan9's telnetd
            # has no known half-close semantics, so that path keeps relying
            # on plan9 tar exiting at end-of-archive by itself.
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        deadline = time.time() + 60 + writer.sent // 50000
        failed = False
        while time.time() < deadline:
            with lock:
                if b"anyvm-tar-done" in transcript:
                    ok = True
                    break
                if b"anyvm-tar-fail" in transcript:
                    failed = True
                    break
            time.sleep(0.5)
        if failed:
            log("Warning: the guest tar exited non-zero, so the archive was "
                "not fully extracted. Its own error is the first line of the "
                "transcript below (run with --debug).")
        elif not ok:
            log("Warning: no completion marker from the guest after the tar push.")
        if debug:
            with lock:
                _t = bytes(transcript)
            # HEAD first, and always. When the push fails the diagnosis is in
            # the FIRST bytes -- the guest tar's own error line -- while the
            # tail is only the rest of the archive coming back as "Bad
            # command or filename" once the shell started reading it. The
            # old tail-only dump discarded the cause on exactly the failures
            # it existed to explain.
            debuglog(True, "tar push telnet transcript ({} bytes) head: "
                     "{!r}".format(len(_t), _t[:1500]))
            if len(_t) > 2000:
                debuglog(True, "tar push telnet transcript tail: {!r}".format(
                    _t[-500:]))
    except OSError as exc:
        log("Warning: tar push over telnet failed: {}".format(exc))
    finally:
        stop.set()
        try:
            sock.close()
        except OSError:
            pass
    return ok


def _tar_pull_telnet(host_port, vhost, vguest, debug=False, os_name=None):
    """Pull over the guest's telnetd: run `tar c` on the telnet session and
    extract the bytes that come back. Without BINARY mode the guest pty maps
    NL to CR-NL in output, which is not reversible -- the archive checksum
    catches it and the pull is reported failed rather than guessed at.
    reactos is the pty-less exception: anyvmtd moves the child's output
    through plain pipes and escapes outbound IAC itself, and cmd.exe does
    not echo commands read from a pipe, so nothing is skipped or warned."""
    IAC, WILL, DO = 255, 251, 253
    try:
        sock = socket.create_connection(("127.0.0.1", int(host_port)), 10)
    except OSError as exc:
        log("Warning: tar sync could not connect to the guest telnetd: {}".format(exc))
        return False
    binary = {'in': False, 'out': False}
    ok = False
    try:
        sock.sendall(bytes([IAC, WILL, 0, IAC, DO, 0]))
        # Drain the banner/prompt (and collect option replies) before the
        # command, so the reader below sees exactly: command echo, archive.
        pre = bytearray()
        sock.settimeout(0.5)
        end = time.time() + 2.0
        while time.time() < end:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                data = b""
            if not data:
                break
            _telnet_eat_iac(sock, data, pre, binary=binary)
        if not binary['in'] and os_name != "reactos":
            log("Warning: the guest telnetd did not acknowledge BINARY mode; "
                "the pulled tar stream may be corrupted by pty NL mapping.")
        if os_name == "reactos":
            cmd = 'cd /d "{0}" && C:\\anyvm\\tar.exe -cf - .'.format(vguest)
            skip_echo = False
        elif os_name == "riscos":
            # As on the push: anyvmd.py parses the line and streams the
            # archive back itself. skip_echo is False because the agent is
            # not a shell and never echoes the command.
            cmd = "cd '{0}' && tar -cf - .".format(vguest)
            skip_echo = False
        elif os_name == "redox":
            # Same command as the plan9 arm below, but skip_echo=False: the
            # far side is redox-builder's anyvmd, which parses the line and
            # streams the archive itself, exactly like riscos and reactos.
            # Only plan9 needs the skip -- there a real rc runs on a pty and
            # echoes the command line back before the archive starts.
            #
            # Taking the plan9 default here ate the archive's FIRST HEADER
            # BLOCK (the reader discards up to the first newline) and the host
            # tar reported "Skipping to next header / Exiting with failure
            # status". The push leg is unaffected and looks perfectly healthy,
            # so the symptom is a one-directional sync that nothing else flags.
            cmd = "cd '{0}' && tar c .".format(vguest)
            skip_echo = False
        else:
            cmd = "cd '{0}' && tar c .".format(vguest)
            skip_echo = True
        sock.sendall(cmd.encode("utf-8") + b"\r\n")
        reader = _StreamTarReader(sock, telnet=True, binary=binary,
                                  skip_echo=skip_echo)
        ok = _extract_from_reader(reader, vhost)
    except OSError as exc:
        log("Warning: tar pull over telnet failed: {}".format(exc))
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return ok


def _tar_push_tcp(host_port, vhost, vguest, excludes=None, os_name=None):
    """Push over a raw TCP shell on 127.0.0.1:host_port (guests with neither
    sshd nor telnetd, netshell-style): the connection itself is the stream,
    8-bit clean by definition. Our half-close is the end-of-input signal."""
    try:
        sock = socket.create_connection(("127.0.0.1", int(host_port)), 10)
    except OSError as exc:
        log("Warning: tar sync could not connect to the guest TCP shell: {}".format(exc))
        return False
    ok = False
    try:
        time.sleep(1.0)  # let any banner/prompt pass
        cmd = "mkdir -p '{0}' && cd '{0}' && tar -xf -\n".format(vguest)
        sock.sendall(cmd.encode("utf-8"))
        time.sleep(0.5)
        wf = sock.makefile("wb")
        ok = _write_archive_to(wf, vhost, excludes, os_name=os_name)
        # Neither tar path closes a caller-supplied fileobj; flush the
        # makefile buffer (the end-of-archive blocks) before half-closing.
        wf.flush()
        wf.close()
        if not ok:
            log("Warning: the archive stream was incomplete; the guest may "
                "be left waiting on its tar.")
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        # Give the guest a moment to finish extracting; drain whatever it
        # prints until it closes or 30s pass.
        sock.settimeout(0.5)
        end = time.time() + 30
        while time.time() < end:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
        ok = True
    except OSError as exc:
        log("Warning: tar push over TCP failed: {}".format(exc))
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return ok


def _tar_pull_tcp(host_port, vhost, vguest):
    """Pull over a raw TCP shell: run `tar -cf - .` and extract the verbatim
    byte stream that follows the command's echo line."""
    try:
        sock = socket.create_connection(("127.0.0.1", int(host_port)), 10)
    except OSError as exc:
        log("Warning: tar sync could not connect to the guest TCP shell: {}".format(exc))
        return False
    ok = False
    try:
        time.sleep(1.0)
        cmd = "cd '{0}' && tar -cf - .\n".format(vguest)
        sock.sendall(cmd.encode("utf-8"))
        reader = _StreamTarReader(sock, telnet=False)
        ok = _extract_from_reader(reader, vhost)
    except OSError as exc:
        log("Warning: tar pull over TCP failed: {}".format(exc))
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return ok


def split_vpath(vpath_str):
    """Split a -v host:guest mapping into (vhost, vguest). A plain
    rsplit(':', 1) breaks when the GUEST side is a Windows-style path
    (reactos): in "/tmp/x:C:\\work" the last colon is the guest drive
    colon. Detect the trailing ":<letter>:<path>" shape and reassemble.
    Raises ValueError on a malformed mapping."""
    if ':' not in vpath_str:
        raise ValueError(vpath_str)
    vhost, vguest = vpath_str.rsplit(':', 1)
    if (len(vhost) >= 2 and vhost[-2] == ':' and vhost[-1].isalpha()
            and (vguest.startswith('\\') or vguest.startswith('/'))):
        vguest = vhost[-1] + ':' + vguest
        vhost = vhost[:-2]
    if not vhost or not vguest:
        raise ValueError(vpath_str)
    return vhost, vguest


def sync_tar(config, ssh_cmd, vhost, vguest, excludes=None):
    """--sync tar push (host -> guest), dispatched on the guest's remote-exec
    transport: ssh when the guest has an sshd, else telnet, else raw TCP."""
    transport = config.get('transport') or "ssh"
    log("Syncing via tar ({} stream): {} -> {}".format(transport, vhost, vguest))
    if not os.path.exists(vhost):
        log("Warning: Host path {} does not exist; skipping.".format(vhost))
        return
    if transport == "ssh":
        ok = _tar_push_ssh(ssh_cmd, vhost, vguest, excludes,
                           os_name=config.get('os'))
    elif transport == "telnet":
        ok = _tar_push_telnet(config['sshport'], vhost, vguest, excludes,
                              config.get('debug'), os_name=config.get('os'))
    else:
        ok = _tar_push_tcp(config['sshport'], vhost, vguest, excludes,
                           os_name=config.get('os'))
    if not ok:
        # Fatal, not a warning. A share the caller explicitly asked for and
        # did NOT get leaves the guest without the files every later step
        # assumes: the run then does something wrong, or waits on a command
        # that can never succeed. That is exactly how a failed push turned
        # into a TWO-HOUR silent hang in CI (the guest never emitted its
        # completion marker, so the exec sat on its 7200s ceiling). Deliberate
        # skips above -- missing host path, a backend the guest cannot do --
        # stay warnings; this branch means we tried and it broke.
        fatal("tar sync push failed for {} -> {}. The guest does not have "
              "your files, so the run was stopped here.".format(vhost, vguest))


def sync_tar_pull(config, ssh_cmd, vhost, vguest):
    """--sync tar pull (guest -> host), same transport dispatch as the push."""
    transport = config.get('transport') or "ssh"
    log("Syncing back via tar ({} stream): {} -> {}".format(transport, vguest, vhost))
    if transport == "ssh":
        ok = _tar_pull_ssh(ssh_cmd, vhost, vguest,
                           os_name=config.get('os'))
    elif transport == "telnet":
        ok = _tar_pull_telnet(config['sshport'], vhost, vguest,
                              config.get('debug'), os_name=config.get('os'))
    else:
        ok = _tar_pull_tcp(config['sshport'], vhost, vguest)
    if not ok:
        log("Warning: tar sync pull failed for {}.".format(vguest))


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


def _dump_boot_debug_snapshot(config, label, serial_log_file, qmon_port, output_dir, vm_name, proc, cmd_list=None,
                              skip_monitor=False):
    """Dump diagnostic info on a boot-wait timeout. All output via debuglog so it only
    fires under --debug. Useful for triaging intermittent CI boot failures.

    skip_monitor=True keeps everything that is free -- the launch command line,
    the host CPU, the process state, the serial tail -- and drops only the
    sixteen monitor queries plus the screendump. Use it when the caller has
    ALREADY established that the monitor answers nothing (the dead-VM fast
    fail): each of those queries then sits out its own timeout, ~34 s spent
    re-proving a known fact. Dropping the whole snapshot instead was a
    mistake: the command line is the one thing a launch that never ran can
    still be compared on, and it costs nothing to print.
    """
    debug = config.get('debug')
    debuglog(debug, "===== boot-debug snapshot [{}] begin =====".format(label))

    # QEMU process status
    try:
        rc = proc.poll()
        debuglog(debug, "QEMU PID {} poll={} (None means still running)".format(proc.pid, rc))
    except Exception as e:
        debuglog(debug, "QEMU proc inspect failed: {}".format(e))

    # Host CPU identity. The deterministic first-boot panic class
    # (vmactions/netbsd-vm#21: NULL-jump SMEP panic at init exec under KVM
    # -cpu host) tracks the runner's host CPU model; recording it on every
    # timeout lets failures be correlated to a CPU generation so the
    # culprit feature can eventually be masked precisely.
    if not IS_WINDOWS:
        try:
            with open("/proc/cpuinfo") as f:
                cpuinfo = f.read()
            picked = []
            for key in ("vendor_id", "model name", "cpu family", "model",
                        "stepping", "microcode"):
                m = re.search(r"^{}\s*:\s*(.+)$".format(re.escape(key)),
                              cpuinfo, re.MULTILINE)
                if m:
                    picked.append("{}: {}".format(key, m.group(1).strip()))
            m = re.search(r"^flags\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
            if m:
                flags = set(m.group(1).split())
                sample = [fl for fl in ("hypervisor", "avx512f", "avx2",
                                        "sse4_2") if fl in flags]
                picked.append("flags(sample): {}".format(" ".join(sample)))
            debuglog(debug, "host CPU:\n{}".format("\n".join(picked)))
        except Exception as e:
            debuglog(debug, "host cpuinfo read failed: {}".format(e))
    else:
        # Same information on Windows, same key names, because the WHPX
        # launch has the same question to answer and had no way to answer it:
        # the CPU model anyvm hands WHPX is chosen from the vendor ALONE
        # (any AuthenticAMD host gets the newest entry in
        # WHPX_AMD_CPU_MODELS), so a wedged WHPX launch always raises "was
        # that model newer than this host?" -- and every Windows boot-timeout
        # snapshot so far recorded no CPU at all.
        #
        # Read the registry rather than shelling out: WMIC is gone from
        # current Windows images and a PowerShell Get-CimInstance costs a
        # second or two, while this key is a plain read.
        # HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0 holds
        # ProcessorNameString (the brand string) and Identifier
        # ("AMD64 Family 26 Model 112 Stepping 0"); PROCESSOR_IDENTIFIER is
        # the same Identifier plus the vendor, and serves as the fallback.
        try:
            picked = []
            ident = ""
            name = ""
            vendor = ""
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
                try:
                    for value, target in (("VendorIdentifier", "vendor"),
                                          ("ProcessorNameString", "name"),
                                          ("Identifier", "ident")):
                        try:
                            got = str(winreg.QueryValueEx(key, value)[0]).strip()
                        except OSError:
                            got = ""
                        if target == "vendor":
                            vendor = got
                        elif target == "name":
                            name = got
                        else:
                            ident = got
                finally:
                    winreg.CloseKey(key)
            except Exception:
                pass
            env_ident = os.environ.get("PROCESSOR_IDENTIFIER", "").strip()
            if not ident and env_ident:
                # "AMD64 Family 26 Model 112 Stepping 0, AuthenticAMD"
                parts = env_ident.rsplit(",", 1)
                ident = parts[0].strip()
                if not vendor and len(parts) == 2:
                    vendor = parts[1].strip()
            if vendor:
                picked.append("vendor_id: {}".format(vendor))
            if name:
                picked.append("model name: {}".format(name))
            # Split the Identifier into the same fields /proc/cpuinfo names,
            # so a Windows snapshot can be read next to a Linux one.
            m = re.search(r"Family\s+(\d+)\s+Model\s+(\d+)\s+Stepping\s+(\d+)",
                          ident or "")
            if m:
                picked.append("cpu family: {}".format(m.group(1)))
                picked.append("model: {}".format(m.group(2)))
                picked.append("stepping: {}".format(m.group(3)))
            elif ident:
                picked.append("identifier: {}".format(ident))
            picked.append("cpu count: {}".format(
                os.environ.get("NUMBER_OF_PROCESSORS", "?")))
            picked.append("whpx available: {}".format(whpx_available()))
            debuglog(debug, "host CPU:\n{}".format("\n".join(picked)))
        except Exception as e:
            debuglog(debug, "host CPU read failed: {}".format(e))

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
    if skip_monitor:
        debuglog(debug, "monitor queries skipped: the caller already found the "
                        "monitor unresponsive (each query would wait out its "
                        "own timeout for a known answer)")
    elif qmon_port:
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
    if skip_monitor:
        pass
    elif qmon_port and output_dir:
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
    # Route downloads through any proxy configured in the environment.
    # Done before the internal-mode dispatch because the VNC proxy child
    # process downloads cloudflared through the same helpers.
    setup_download_proxy()

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

    # Handle internal user-space nfsd watchdog mode (--sync nfs backend)
    if len(sys.argv) > 1 and sys.argv[1] == '--internal-nfsd':
        try:
            run_internal_nfsd(sys.argv[2], sys.argv[3], int(sys.argv[4]),
                              int(sys.argv[5]), sys.argv[6],
                              len(sys.argv) > 7 and sys.argv[7] == '1',
                              sys.argv[8] if len(sys.argv) > 8 else "",
                              len(sys.argv) > 9 and sys.argv[9] == '1')
        except Exception as e:
            try:
                if len(sys.argv) > 6:
                    with open(sys.argv[6], 'a') as f:
                        f.write("[nfsd-watchdog] Fatal error: {}\n".format(e))
            except:
                pass
        return

    # Handle internal "run this other Python script" mode. Only the frozen
    # build ever spawns it -- see python_argv().
    if len(sys.argv) > 1 and sys.argv[1] == '--internal-run-python':
        run_internal_python(sys.argv[2], sys.argv[3:])
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
        # --attach: operate on an already-running telnet-transport guest
        # instead of booting one; --pull-files selects the tar pull-back action.
        'attach': False,
        'pull': False,
        # Pinned host port for the 9P forward (--p9-port). Needed so a later
        # --attach --pull-files can reach the same channel.
        'p9_port': 0,
        # Names the caller does not want shared (--sync-exclude), relative to
        # each -v host path. rsync/scp callers pass their own excludes on the
        # command line; this is how the tar and 9P paths get them too.
        'sync_excludes': [],
        # Ceiling for one telnet-guest command (marker wait). Generous on
        # purpose: CI job timeouts are the real bound.
        'exec_timeout_sec': 7200,
        'vpaths': [],
        'ports': [],
        'vnc': "",
        'sync': "rsync",
        # Remote-exec channel into the guest. "ssh" everywhere except
        # plan9/9front (no sshd): "telnet" drives the guest over its
        # no-auth telnetd and moves files over exportfs 9P. Set from the
        # guest profile's "transport" key (or the plan9 os default) after
        # the profile is resolved.
        'transport': "ssh",
        'qmon': "",
        'disktype': "",
        'public': False,
        'os': "",
        'release': "",
        'arch': "",
        'builder': "",
        'whpx': False,
        'tcg': False,
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
        'enable_pmu': False,
        'firmware': "",
        'firmware_vars': ""
    }

    ssh_passthrough = []
    cpu_specified = False
    mem_user_specified = False
    sync_user_specified = False
    serial_user_specified = False
    vnc_user_specified = False
    arch_specified = False
    boot_timeout_user_specified = False


    script_home = self_home()
    working_dir = default_data_dir(script_home)

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
            # `--arch ""` (how testrun.yml passes an unset matrix arch) means
            # "not specified": leave arch_specified False so the
            # aarch64 -> x86_64 image fallback stays available when the host
            # is arm64 but the builder only publishes x86_64 images.
            if config['arch']:
                arch_specified = True
            i += 1
        elif arg == "--mem":
            config['mem'] = args[i+1]
            mem_user_specified = True
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
        elif arg == "--firmware":
            config['firmware'] = args[i+1]
            config['useefi'] = True
            i += 1
        elif arg == "--firmware-vars":
            config['firmware_vars'] = args[i+1]
            i += 1
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
            sync_user_specified = True
            if val == "mynfs":
                # Alias: nfs already means the bundled user-space nfsd.
                val = "nfs"
            if val not in ["sshfs", "nfs", "sys-nfs", "rsync", "scp", "tar", "9p", "no"]:
                 fatal("Invalid --sync mode: {}. Supported: rsync, sshfs, nfs, sys-nfs, scp, tar, 9p, no/off.".format(val))
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
        elif arg == "--tcg":
            config['tcg'] = True
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
        elif arg == "--exec-timeout-sec":
            try:
                val = int(args[i+1])
            except ValueError:
                fatal("--exec-timeout-sec requires an integer (seconds), got: {}".format(args[i+1]))
            if val <= 0:
                fatal("--exec-timeout-sec must be positive, got: {}".format(val))
            config['exec_timeout_sec'] = val
            i += 1
        elif arg == "--p9-port":
            try:
                val = int(args[i+1])
            except ValueError:
                fatal("--p9-port requires an integer (port), got: {}".format(args[i+1]))
            if val < 1 or val > 65535:
                fatal("--p9-port must be 1-65535, got: {}".format(val))
            config['p9_port'] = val
            i += 1
        elif arg == "--sync-exclude":
            val = args[i+1].strip()
            if val:
                config['sync_excludes'].append(val)
            i += 1
        elif arg == "--attach":
            config['attach'] = True
        elif arg == "--pull-files":
            config['pull'] = True
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

    # --attach: talk to an ALREADY-RUNNING guest (started earlier with
    # --detach) instead of booting one. Telnet-transport guests only: ssh
    # guests already have `ssh <vm-name>` for later commands, but the telnet
    # guests had no CLI at all for "run another command" / "pull the synced
    # tree back" until this mode. Exits before any image resolution or
    # download -- the VM is somebody else's.
    if config['attach']:
        if config['os'] not in ("plan9", "reactos", "riscos", "redox"):
            fatal("--attach only supports the telnet-transport guests "
                  "(plan9, reactos, riscos, redox); for ssh guests use "
                  "'ssh <vm-name>' directly.")
        if not config['sshport']:
            fatal("--attach requires --ssh-port <port> (the control port "
                  "the running VM was started with).")
        if config['pull']:
            if ssh_passthrough:
                fatal("--attach --pull-files takes no '-- <command>'; run the "
                      "command in a separate --attach call.")
            if not config['vpaths']:
                fatal("--attach --pull-files needs at least one -v "
                      "host_path:guest_path pair.")
            # Which channel carries the copy-back depends on how the guest
            # shares files at all: a 9P guest (plan9/9front) reopens its 9P
            # mount, everyone else streams a tar over the telnet channel.
            # `--sync 9p` selects it, and --p9-port must name the forward the
            # running VM was started with (a random one is unknowable here).
            pull_via_9p = (config.get('sync') == '9p')
            if pull_via_9p and not config.get('p9_port'):
                fatal("--attach --pull-files --sync 9p requires --p9-port "
                      "<port> (the 9P forward the running VM was started "
                      "with, e.g. anyvm ... --sync 9p --p9-port 20564).")
            attach_all_ok = True
            for vpath_str in config['vpaths']:
                try:
                    att_vhost, att_vguest = split_vpath(vpath_str)
                except ValueError:
                    fatal("Invalid format for -v. Use host_path:guest_path")
                att_vhost = os.path.abspath(att_vhost)
                if pull_via_9p:
                    if not sync_9p_pull(config['p9_port'], att_vhost,
                                        att_vguest, debug=config['debug'],
                                        excludes=config.get('sync_excludes')):
                        attach_all_ok = False
                    continue
                log("Syncing back via tar (telnet stream): {} -> {}".format(
                    att_vguest, att_vhost))
                if not _tar_pull_telnet(config['sshport'], att_vhost,
                                        att_vguest, debug=config['debug'],
                                        os_name=config['os']):
                    attach_all_ok = False
            sys.exit(0 if attach_all_ok else 1)
        if not ssh_passthrough:
            fatal("--attach needs either --pull-files or a '-- <command>' to run "
                  "in the guest.")
        attach_cmd = " ".join(ssh_passthrough)
        attach_ok, attach_text, attach_rc = telnet_exec_status(
            config['sshport'], config['os'], attach_cmd,
            timeout_sec=config.get('exec_timeout_sec', 7200),
            debug=config['debug'])
        sys.stdout.write(attach_text)
        if attach_text and not attach_text.endswith("\n"):
            sys.stdout.write("\n")
        if not attach_ok:
            log("Warning: telnet session to the guest closed early.")
            sys.exit(255)
        sys.exit(attach_rc)
    elif config['pull']:
        fatal("--pull-files only works together with --attach.")

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

    # Record WHICH x86 host this is, not just its width. The WHPX CPU-model
    # choice below is VENDOR-only -- any AuthenticAMD host gets the newest
    # name in WHPX_AMD_CPU_MODELS -- so when a WHPX launch misbehaves the
    # first question is always "was the model newer than the host?", and
    # nothing in the log could answer it. PROCESSOR_IDENTIFIER carries the
    # family/model/stepping (e.g. "AMD64 Family 26 Model 112 Stepping 0,
    # AuthenticAMD" is Zen 5), which is exactly what is needed to tell a
    # Turin host from a Milan one.
    if platform.system() == "Windows":
        debuglog(config['debug'], "Host CPU: {}".format(
            os.environ.get("PROCESSOR_IDENTIFIER", "?") or "?"))

    # Normalize arch string
    if config['arch'] in ["x86_64", "amd64"]:
        config['arch'] = ""
    if config['arch'] in ["arm", "arm64", "ARM64"]:
        config['arch'] = "aarch64"

    # ReactOS is published for 32-bit x86 only, so the host-arch default just
    # above sends an x86_64 host looking for reactos-<rel>.qcow2.zst, which no
    # release carries -- the lookup then dies in "Cannot find the image link".
    # The only existing rescue goes the other way (aarch64 -> x86_64), so
    # without this a bare `--os reactos` can never work. An explicit --arch
    # still wins, so a future 64-bit ReactOS needs no change here. Nothing
    # else is lost: the accel chain treats i386 as x86, so an i386 guest still
    # gets KVM/WHPX on an x86_64 host, exactly as `--arch i386` does today.
    if config['os'] == "reactos" and not arch_specified:
        config['arch'] = "i386"
        debuglog(config['debug'], "reactos: defaulting arch to i386 (only arch published)")

    # RISC OS is the same situation for the same reason: it is a 32-bit ARM
    # system with no 64-bit port, so the host-arch default sends an x86_64 (or
    # aarch64) host looking for an image no release carries. Note that armv7
    # is the only spelling that reaches here intact -- "arm" is rewritten to
    # aarch64 by the alias map just above, which is exactly why the builder
    # names the arch armv7 rather than arm.
    if config['os'] == "riscos" and not arch_specified:
        config['arch'] = "armv7"
        debuglog(config['debug'], "riscos: defaulting arch to armv7 (only arch published)")

    # Redox is the third guest published for exactly one arch: 0.9.0 is the
    # only release upstream ever cut and it is x86_64-only (there is no aarch64
    # port). So an aarch64 host's arch default sends it looking for
    # redox-<rel>-aarch64.qcow2.zst, which no release carries. Note the empty
    # string: "" is how this file spells x86_64 after the normalization above,
    # NOT "unset", so on an x86_64 host this is already a no-op.
    if config['os'] == "redox" and not arch_specified:
        config['arch'] = ""
        debuglog(config['debug'], "redox: defaulting arch to x86_64 (only arch published)")

    # RISC OS is Linux x86_64 only, and it is worth saying so HERE rather than
    # letting the generic path discover it: no released QEMU can boot RISC OS
    # on a raspi machine, so the guest depends on the patched build that
    # ensure_pinned_qemu() downloads -- and that only exists for Linux x86_64.
    # On any other host ensure_pinned_qemu() shrugs ("no pinned build exists
    # for this host platform; the guest may misbehave") and hands back the
    # system qemu-system-arm, which then fails much later in a way that looks
    # like a broken image rather than an unsupported host. This check runs
    # before the image download, so nobody pulls a multi-GB qcow2 first.
    if config['os'] == "riscos" and not config['qcow2']:
        if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
            fatal("RISC OS needs a patched QEMU that is only published for "
                  "Linux x86_64 hosts (this host is {} {}). No released QEMU "
                  "can boot RISC OS on a Raspberry Pi machine, so there is no "
                  "system fallback. See https://anyvm.org/docs/guests.html#riscos"
                  .format(platform.system(), platform.machine()))

    # Fail fast when host dependencies are missing: this runs BEFORE any
    # image download (images are multi-GB), so the user gets the install
    # command instead of a wasted download. The qemu-system check is skipped
    # for arches that may substitute the pinned QEMU build downloaded by
    # ensure_pinned_qemu() (riscv64/s390x/ppc64 family); the late check
    # after that substitution still covers them.
    missing_deps = []
    if config['arch'] not in ("riscv64", "s390x", "powerpc64", "powerpc64le",
                              "ppc64", "ppc64le", "loongarch64", "armv7"):
        early_bin_name = qemu_binary_name(config['arch'])
        if not find_qemu(early_bin_name):
            missing_deps.append(early_bin_name)
    for dep_tool in ("ssh", "zstd"):
        if not shutil.which(dep_tool):
            missing_deps.append(dep_tool)
    if missing_deps:
        fatal("Required tool(s) not found (searched PATH and common"
              " install locations): {}\n{}".format(
                  ", ".join(missing_deps), deps_install_hint()))

    # 2048 MB is tight for modern guests (Solaris 11.4 exhausts swap during
    # boot and sshd cannot even fork), so when the host has more than 4 GB of
    # RAM, default to 4096 MB unless the user pinned --mem.
    if not mem_user_specified:
        host_mem_mb = host_total_mem_mb()
        if host_mem_mb > 4096:
            config['mem'] = "4096"
            debuglog(config['debug'], "Host has {} MB RAM: defaulting VM memory to 4096 MB (override with --mem)".format(host_mem_mb))

    # BlissOS (Android-x86): the image is built and verified on std VGA
    # (bochs-drm KMS + HWACCEL=0 software GLES renders the Android desktop to
    # VNC at 1280x800; virtio-vga is unverified there), and Android under
    # software rendering is memory-hungry -- the builder builds/verifies with
    # 6144 MB, so default to that unless the user pinned --mem.
    if config['os'] == "blissos" and not mem_user_specified:
        config['mem'] = "6144"
        debuglog(config['debug'], "BlissOS: defaulting memory to 6144 MB (override with --mem)")
    # scp is the only sync backend a BlissOS guest supports (a static scp from
    # the dropbear tree is baked into /system/bin; there is no rsync/sshfs/nfs
    # on Android). Default to it so plain `-v dir:/path` just works.
    if config['os'] == "blissos" and not sync_user_specified:
        config['sync'] = "scp"
        debuglog(config['debug'], "BlissOS: defaulting sync mode to scp (override with --sync)")

    # netbsd sparc64: the QEMU sun4u machine boots only off the CMD646 PCI IDE,
    # whose TCG emulation loses interrupts under SUSTAINED concurrent net+disk
    # DMA. A live sshfs/nfs mount drives exactly that and wedges the guest (the
    # cmdide driver recovers ~10 s per lost IRQ, so the mount crawls), and the
    # rsync default does not even exist on the 11.0 sparc64 base image (dropped
    # over a libcrypto ABI break). A one-shot scp does not sustain the load and
    # is always present (base ssh), so default to it -- `-v dir:/path` then just
    # works. (Pinning a newer QEMU does not change the lost-interrupt rate, and
    # OpenBIOS cannot boot a SCSI disk on sun4u, so the IDE cannot be avoided.)
    if (config['os'] == "netbsd" and config['arch'] == "sparc64"
            and not sync_user_specified):
        config['sync'] = "scp"
        debuglog(config['debug'], "netbsd/sparc64: defaulting sync mode to scp (override with --sync)")

    # plan9 (9front): no ssh, so rsync/sshfs/nfs/scp (all ssh-based) can't
    # work. Folder sync is the host mounting the guest's exportfs 9P share
    # (Linux kernel v9fs). Default to it so `-v dir:/path` just works.
    if config['os'] == "plan9" and not sync_user_specified:
        config['sync'] = "9p"
        debuglog(config['debug'], "plan9: defaulting sync mode to 9p (override with --sync)")

    # reactos: the only working folder-sync backend is tar over the anyvmtd
    # telnet channel (busybox-w32 baked at C:\anyvm\tar.exe). Everything
    # else was surveyed in a running guest and is out: no sshd and no ssh
    # client (so rsync/sshfs/scp), no 9P, no SMB redirector, and the shipped
    # NFSv4.1 client's daemon service (`pnfs`) hangs in START_PENDING --
    # see reactos-builder's conf for the full survey. Defaulting to the
    # ssh-based rsync would make every `-v` run fail with a confusing ssh
    # error, so default to tar.
    if config['os'] == "reactos" and not sync_user_specified:
        config['sync'] = "tar"
        debuglog(config['debug'],
                 "reactos: defaulting sync mode to tar (override with --sync)")

    # redox: same reasoning, different missing pieces. Redox 0.9.0 ships no
    # sshd and no ssh client (out: rsync/sshfs/scp), no 9P and no NFS client,
    # so tar over the injected anyvmd telnet agent is the only backend that
    # works -- and it needs nothing baked in, because /bin/tar is already on
    # the stock image. Without this the ssh-based rsync default would make
    # every `-v` run fail with a confusing ssh error.
    if config['os'] == "redox" and not sync_user_specified:
        config['sync'] = "tar"
        debuglog(config['debug'],
                 "redox: defaulting sync mode to tar (override with --sync)")

    if not config['vga']:
        if config['os'] == "netbsd" and config['arch'] != "aarch64":
            config['vga'] = "std"
        elif config['os'] == "haiku":
            config['vga'] = "std"
        elif config['os'] == "blissos":
            config['vga'] = "std"
        elif config['os'] == "reactos":
            # ReactOS has no virtio-gpu driver; it drives QEMU's std VGA
            # through its VBE/Bochs miniport, which is what reactos-builder
            # builds and verifies on (conf VM_VGA=std). NOTE: the guest
            # profile's "vga" field is never consulted by this file -- this
            # chain is the only source -- so an OS missing from it silently
            # gets virtio no matter what its builder recorded.
            config['vga'] = "std"
        elif config['os'] == "redox":
            # Redox has no virtio-gpu driver: vesad draws on the framebuffer
            # the BOOTLOADER hands it, which on QEMU means the std VGA one
            # ("Framebuffer 1280x800 stride 1280 at FD000000" in its boot log).
            # redox-builder builds and verifies on VM_VGA=std, and its
            # profile.json records "vga": "std" -- but per the note just above
            # that field is never read here, so the guest would silently get
            # virtio-gpu and come up with no console at all.
            config['vga'] = "std"
        elif config['os'] == "openbsd" and config['arch'] != "aarch64" and config['release'] and any(
            config['release'].endswith(s)
            for s in ("-xfce", "-gnome", "-kde", "-kde6", "-mate", "-lxqt", "-lumina", "-enlightenment", "-cinnamon")
        ):
            # OpenBSD/amd64 has no DRM driver for virtio-gpu in base, so X
            # cannot get a framebuffer with the default virtio VGA: xenocara's
            # wsfb gets ENOTTY on the wsdisplay text-mode console, and
            # modesetting finds no /dev/drm0. cirrus is the one legacy VGA
            # that ships a working xenocara driver (xf86-video-cirrus) for
            # QEMU. Pair with the matching desktop hook (e.g.
            # openbsd-builder/hooks/xfce.sh) which writes
            # /etc/X11/xorg.conf.d/10-cirrus.conf and raises
            # machdep.allowaperture so the driver can map the VGA aperture.
            #
            # Skipped on aarch64: there is no cirrus_drv for OpenBSD/arm64
            # xenocara, but viogpu0 attaches wsdisplay over the default
            # virtio-gpu, and the aarch64 desktop hook (e.g.
            # openbsd-builder/hooks/xfce-aarch64.sh) pins wsfb with
            # ShadowFB off and disables xfwm4's compositor so X actually
            # pushes pixels to viogpu. So leave --vga at the default
            # (virtio) for aarch64 desktop releases.
            config['vga'] = "cirrus"
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
    # Populated from the published <vm>.profile.json when running a release
    # image (see load_guest_profile). Stays None for a local --qcow2 file or an
    # older release with no profile asset, in which case the launch falls back
    # to the built-in per-(os,arch,release) logic below.
    guest_profile = None

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
                                if config['os'] == "openeuler":
                                    # openEuler release names carry hyphens
                                    # (22.03-LTS-SP4, 24.03-LTS-SP4): taking
                                    # only the second token would truncate to
                                    # "24.03" and resolve to a nonexistent
                                    # image. Use the full remainder after the
                                    # os prefix, minus a trailing arch token.
                                    # Other OSes keep the second-token rule:
                                    # it is what excludes desktop variants
                                    # (freebsd-15.1-xfce) from auto-select.
                                    rest = filename.split('-', 1)[1]
                                    for _a in ("aarch64", "loongarch64",
                                               "riscv64", "s390x", "ppc64le"):
                                        rest = removesuffix(rest, "-" + _a)
                                    ver = rest
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
            # Two passes: an exact match always wins, then the same search
            # case-insensitively. A release name can carry upper case
            # (openEuler ships "22.03-LTS-SP4" / "24.03-LTS-SP4"), and a user
            # typing "24.03-lts-sp4" should still get the image. The asset
            # name is the authority on the spelling -- the caller adopts it
            # right after this returns, because the sidecar URLs are built
            # from <os>-<release>[-<arch>] and would 404 on the wrong case.
            for r in releases:
                for asset in r.get('assets', []):
                    u = asset.get('browser_download_url', '')
                    if u.endswith(target_zst) or u.endswith(target_xz):
                        return u
            lower_zst = target_zst.lower()
            lower_xz = target_xz.lower()
            for r in releases:
                for asset in r.get('assets', []):
                    u = asset.get('browser_download_url', '')
                    ul = u.lower()
                    if ul.endswith(lower_zst) or ul.endswith(lower_xz):
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

        # The asset name is the authority on how the release is spelled, so
        # adopt it whenever the user typed a different case (find_image_link
        # above matches case-insensitively). Everything downstream derives
        # from config['release'] -- vm_name, and with it the -host.id_rsa /
        # -id_rsa.pub / .profile.json / .qemu sidecar URLs and the local
        # state file names -- so leaving the user's spelling in place would
        # 404 every sidecar and split the state dir in two. Only a pure case
        # difference is rewritten: variant assets whose name extends the
        # release (freebsd-15.1-xfce) are left alone.
        asset_release = removesuffix(zst_link.split('/')[-1], ".qcow2.zst")
        asset_release = removesuffix(asset_release, ".qcow2.xz")
        os_prefix = config['os'] + "-"
        if asset_release.lower().startswith(os_prefix.lower()):
            asset_release = asset_release[len(os_prefix):]
            if config['arch'] and config['arch'] != "x86_64":
                asset_release = removesuffix(asset_release, "-" + config['arch'])
            if (asset_release and config['release']
                    and asset_release != config['release']
                    and asset_release.lower() == config['release'].lower()):
                log("Release '{}' published as '{}', using the published "
                    "spelling.".format(config['release'], asset_release))
                config['release'] = asset_release

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
                try:
                    shutil.copy2(cached_qcow2, qcow_name)
                except OSError as e:
                    # Drop the partial data-dir copy: a later run would take
                    # the truncated qcow2 for a fully restored image (the
                    # checks here are bare os.path.exists) and boot corrupt.
                    try:
                        os.remove(qcow_name)
                    except OSError:
                        pass
                    fatal("Copying cached image failed: {} -> {}: {} (is the "
                          "--data-dir volume large enough?)".format(cached_qcow2, qcow_name, e))
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
                        # Remove the corrupt archive (and any partial output)
                        # so the next run re-downloads instead of failing on
                        # the same bad file forever.
                        for stale in (ova_file, qcow_name):
                            try:
                                os.remove(stale)
                            except OSError:
                                pass
                        fatal("zstd extraction failed (removed corrupt download; re-run to download again)")
                elif ova_file.endswith('.xz'):
                    if not cmd_exists('xz'):
                        msg = "Error: 'xz' command not found. This is required to extract the image.\n"
                        if IS_WINDOWS:
                            msg += "Please install it via winget: winget install Tukaani.XZ\n"
                        else:
                            msg += "Please install it via your package manager (e.g. apt install xz-utils, brew install xz)\n"
                        fatal(msg)
                    xz_failed = False
                    with open(qcow_name, 'wb') as f:
                        if subprocess.call(['xz', '-d', '-c', ova_file], stdout=f) != 0:
                            xz_failed = True
                    if xz_failed:
                        for stale in (ova_file, qcow_name):
                            try:
                                os.remove(stale)
                            except OSError:
                                pass
                        fatal("xz extraction failed (removed corrupt download; re-run to download again)")
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
                    # Populate the cache. --cache-dir is always an explicit
                    # user/caller choice (there is no implicit default), so a
                    # failure here must be FATAL, not a silent fall-back to
                    # data-dir: the caller asked for a cache and would keep
                    # paying the full download+extract on every run without
                    # noticing. Clean up the partial file first, though -- a
                    # half-written qcow2 left behind would be picked up as a
                    # valid cached image by the next run (the cache-hit check
                    # is a bare os.path.exists) and boot corrupt.
                    log("Caching extracted image: {}".format(cached_qcow2))
                    if config['snapshot']:
                        # Snapshot mode never writes the backing file, so the
                        # cached copy IS the boot image: MOVE instead of
                        # copy+delete. On a same-volume cache dir the rename
                        # is instant and needs no extra space; cross-volume,
                        # shutil.move degrades to copy+delete as before.
                        debuglog(config['debug'], "Moving qcow2 to cache: {} -> {}".format(qcow_name, cached_qcow2))
                        try:
                            shutil.move(qcow_name, cached_qcow2)
                            qcow_name = cached_qcow2
                        except OSError as e:
                            try:
                                os.remove(cached_qcow2)
                            except OSError:
                                pass
                            fatal("Caching image failed: {} -> {}: {} (is the "
                                  "--cache-dir volume large enough / not "
                                  "quota-limited?)".format(qcow_name, cached_qcow2, e))
                    else:
                        # Writable mode boots (and mutates) the data-dir
                        # image, so the cache needs its own pristine COPY.
                        debuglog(config['debug'], "Copying qcow2 to cache: {} -> {}".format(qcow_name, cached_qcow2))
                        try:
                            shutil.copy2(qcow_name, cached_qcow2)
                        except OSError as e:
                            try:
                                os.remove(cached_qcow2)
                            except OSError:
                                pass
                            fatal("Caching image failed: {} -> {}: {} (is the "
                                  "--cache-dir volume large enough / not "
                                  "quota-limited?)".format(qcow_name, cached_qcow2, e))

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

        # Guest hardware profile: the single source of truth for the launch
        # (see load_guest_profile). Published beside the image and named like
        # it (<vm_name>.profile.json). Gated on check_url_exists so a release
        # that predates the profile asset never caches a 404 body; absent /
        # unreadable -> guest_profile stays None -> built-in logic.
        profile_file = os.path.join(output_dir, vm_name + ".profile.json")
        if os.path.exists(profile_file):
            guest_profile = load_guest_profile(profile_file, config['debug'])
        else:
            profile_url = "https://github.com/{}/releases/download/v{}/{}.profile.json".format(
                builder_repo, config['builder'], vm_name)
            if check_url_exists(profile_url, config['debug']):
                download_file(profile_url, profile_file, config['debug'])
                guest_profile = load_guest_profile(profile_file, config['debug'])
            else:
                debuglog(config['debug'], "No guest profile at {} (release predates it); using built-in launch logic".format(profile_url))

    # Remote-exec transport: profile "transport" key wins; otherwise plan9,
    # reactos, riscos and redox always mean telnet -- 9front has no sshd, and
    # none of ReactOS, RISC OS or Redox ships a remote-access server of any
    # kind, so their images carry a baked agent instead. Everything else = ssh.
    # The per-OS fallback matters for a local --qcow2 file, which has no
    # profile sidecar to read.
    if guest_profile and guest_profile.get('transport'):
        config['transport'] = guest_profile['transport']
    elif config['os'] in ("plan9", "reactos", "riscos", "redox"):
        config['transport'] = "telnet"

    # openbsd/sparc64 cannot cold-boot on the OpenBIOS bundled with QEMU
    # (see OPENBIOS_SPARC64_ASSET above); fetch the patched blob published
    # next to the VM images, from the image's OWN builder at the image's OWN
    # release, so firmware and image are always a version-matched pair.
    # No releases/latest fallback: that is a moving target which can hand a
    # guest firmware built for a different image (and it races a freshly-cut
    # tag, whose assets appear only after its upload jobs finish).
    # RISC OS: the ROM the raspi machine boots, from the builder release.
    riscos_rom_file = None
    if config['arch'] == "armv7":
        riscos_rom_file = os.path.join(output_dir, RISCOS_ROM_ASSET)
        if not os.path.exists(riscos_rom_file):
            if not config['builder']:
                fatal("RISC OS needs {} from its builder release, but no "
                      "builder version was resolved (pass --builder).".format(
                          RISCOS_ROM_ASSET))
            rom_url = "https://github.com/{}/releases/download/v{}/{}".format(
                builder_repo, str(config['builder']).lstrip("v"),
                RISCOS_ROM_ASSET)
            if config.get('cachedir'):
                rel_path = os.path.relpath(output_dir, working_dir)
                cache_output_dir = os.path.join(config['cachedir'], rel_path)
                if not os.path.exists(cache_output_dir):
                    os.makedirs(cache_output_dir)
                cached_rom = os.path.join(cache_output_dir, RISCOS_ROM_ASSET)
                if not os.path.exists(cached_rom):
                    debuglog(config['debug'], "RISC OS ROM not in cache, downloading to: {}".format(cached_rom))
                    download_file(rom_url, cached_rom, config['debug'])
                if os.path.exists(cached_rom):
                    shutil.copy2(cached_rom, riscos_rom_file)
            else:
                download_file(rom_url, riscos_rom_file, config['debug'])
        if not os.path.exists(riscos_rom_file):
            fatal("Could not obtain {} -- RISC OS cannot boot without its "
                  "ROM (the raspi machine has no built-in firmware).".format(
                      RISCOS_ROM_ASSET))

    sparc64_bios_file = None
    if config['os'] == "openbsd" and config['arch'] == "sparc64":
        sparc64_bios_file = os.path.join(output_dir, OPENBIOS_SPARC64_ASSET)
        if not os.path.exists(sparc64_bios_file):
            if not config['builder']:
                fatal("OpenBSD sparc64 needs {} from its builder release, but "
                      "no builder version was resolved (pass --builder).".format(
                          OPENBIOS_SPARC64_ASSET))
            bios_url = "https://github.com/{}/releases/download/v{}/{}".format(
                builder_repo, str(config['builder']).lstrip("v"),
                OPENBIOS_SPARC64_ASSET)
            if config.get('cachedir'):
                rel_path = os.path.relpath(output_dir, working_dir)
                cache_output_dir = os.path.join(config['cachedir'], rel_path)
                if not os.path.exists(cache_output_dir):
                    debuglog(config['debug'], "Creating cache directory: {}".format(cache_output_dir))
                    os.makedirs(cache_output_dir)
                cached_bios = os.path.join(cache_output_dir, OPENBIOS_SPARC64_ASSET)
                if not os.path.exists(cached_bios):
                    debuglog(config['debug'], "OpenBIOS not found in cache, downloading to: {}".format(cached_bios))
                    download_file(bios_url, cached_bios, config['debug'])
                if os.path.exists(cached_bios):
                    debuglog(config['debug'], "Copying OpenBIOS from cache to: {}".format(sparc64_bios_file))
                    shutil.copy2(cached_bios, sparc64_bios_file)
            else:
                download_file(bios_url, sparc64_bios_file, config['debug'])
        if not os.path.exists(sparc64_bios_file):
            fatal("Could not download {} from {} v{} (OpenBSD sparc64 cannot "
                  "boot on QEMU's bundled OpenBIOS).".format(
                      OPENBIOS_SPARC64_ASSET, builder_repo, config['builder']))

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
    bin_name = qemu_binary_name(config['arch'])
    qemu_bin = find_qemu(bin_name)

    # Distro QEMU 8.2 (ubuntu noble et al.) cannot run some guests reliably;
    # swap in the pinned build published by ubuntu-builder when the system
    # one is too old (no-op when it is new enough, see ensure_pinned_qemu):
    #  * ubuntu 26.04 riscv64 needs -cpu rva23s64, a CPU model QEMU grew in
    #    9.1 (and its 7.0 kernel hangs at entry on 8.2 TCG anyway);
    #  * 8.2's s390x TCG intermittently freezes guest systemd at startup
    #    ("Failed to fork off sandboxing environment ... Freezing
    #    execution.", roughly 1 in 4 boots); QEMU >= 10 is verified clean.
    #  * 8.2's pseries TCG breaks ubuntu ppc64el two separate ways, so the
    #    pin is unconditional for the whole guest family (same as s390x):
    #      - 22.04: userspace is mistranslated under -cpu power9 and
    #        python3.10 segfaults reproducibly (every cloud-init stage
    #        dies, ssh host keys are never generated on a fresh image);
    #      - 26.04: systemd as PID 1 takes a SEGV right after switch-root
    #        and freezes ("systemd[1]: Caught <SEGV>, dumped core as pid
    #        691." -> "Freezing execution."), so the guest never brings
    #        networking up and the boot probe times out. Intermittent, the
    #        same shape as the s390x freeze above: in anyvm run
    #        31881755132 the 26.04 ppc64le leg died this way while the
    #        other four sync legs booted the identical image fine.
    #    The release gate that used to limit this to 22.x is gone: 24.04
    #    has not been seen to fail, but it runs the same emulator on the
    #    same machine type, and a silently flaky leg costs far more than
    #    one pinned QEMU download.
    # Skipped entirely when the HOST is the guest's arch (real IBM Z /
    # POWER): these are TCG-only bugs (such hosts run KVM, see the accel
    # selection below), and the pinned build is an x86_64 binary that
    # could not run there anyway.
    if (config['arch'] == "riscv64" and config['os'] == "ubuntu"
            and (config['release'] or "").startswith("26.")):
        qemu_bin = ensure_pinned_qemu("riscv64", qemu_bin, (9, 1), working_dir, config['debug'],
                                      repo=builder_repo,
                                      builder_tag=config.get('builder'))
    elif config['arch'] == "s390x" and host_arch != "s390x":
        qemu_bin = ensure_pinned_qemu("s390x", qemu_bin, (10, 0), working_dir, config['debug'],
                                      repo=builder_repo,
                                      builder_tag=config.get('builder'))
    elif (config['arch'] in ("powerpc64", "powerpc64le", "ppc64", "ppc64le")
            and config['os'] == "ubuntu"
            and host_arch not in ("ppc64", "ppc64le", "powerpc64", "powerpc64le")):
        qemu_bin = ensure_pinned_qemu("ppc64le", qemu_bin, (10, 0), working_dir,
                                      config['debug'], bin_name="qemu-system-ppc64",
                                      repo=builder_repo,
                                      builder_tag=config.get('builder'))
    elif config['arch'] == "loongarch64" and host_arch != "loongarch64":
        # The loongarch virt machine needs the bundled EDK2 LoongArch
        # firmware (edk2-loongarch64-code.fd), which QEMU only ships since
        # 9.2 -- noble's stock 8.2 has the binary but not the firmware, so
        # a UEFI disk image cannot boot on it. The pinned tarball comes from
        # this guest's own builder release, like every other pinned asset.
        qemu_bin = ensure_pinned_qemu("loongarch64", qemu_bin, (9, 2),
                                      working_dir, config['debug'],
                                      repo=builder_repo,
                                      builder_tag=config.get('builder'))
    elif config['arch'] == "sparc64" and host_arch != "sparc64":
        # sun4u's sabre PCI host bridge has a single-slot IRQ-dispatch bug
        # in EVERY upstream QEMU (the PCI-INO branch clobbers an outstanding
        # OBIO request, so the guest's interrupt-clear is dropped and both
        # devices' interrupts wedge. Both sparc64 guests hit it under
        # concurrent disk+NIC DMA, from whichever side loses the race:
        # NetBSD as "cmdide0: lost interrupt" / "wm0: device timeout",
        # OpenBSD as "wd0(pciide0:0:0): timeout" / "em0: watchdog".
        # Each builder ships its own files/qemu-sabre-irq-clobber.patch and
        # publishes its own patched tarball, so this fetches from whichever
        # builder the running image came from -- never from a sibling.
        # Forced regardless of the system QEMU version: a newer stock QEMU
        # is NOT good enough, it has the same bug. Linux x86_64 only, like
        # the other pinned builds; on macOS / Windows the guest falls back
        # to system QEMU and can still wedge under heavy concurrent I/O --
        # every sparc64 image is a plain single-disk one, so nothing else
        # absorbs it.
        qemu_bin = ensure_pinned_qemu("sparc64", qemu_bin, (0, 0),
                                      working_dir, config['debug'], force=True,
                                      repo=builder_repo,
                                      builder_tag=config.get('builder'))
    elif config['arch'] == "armv7":
        # Forced, and for a stronger reason than any pin above: no released
        # QEMU can boot RISC OS on a raspi machine at all, so a newer stock
        # build is not "good enough" -- there is nothing to fall back to. The
        # patched tarball comes from the image's own builder release.
        # bin_name is explicit because QEMU spells the binary after the
        # family ("arm"), not after the arch profile.
        qemu_bin = ensure_pinned_qemu("armv7", qemu_bin, (0, 0),
                                      working_dir, config['debug'], force=True,
                                      bin_name="qemu-system-arm",
                                      repo=builder_repo,
                                      builder_tag=config.get('builder'))

    if not qemu_bin:
        fatal("QEMU binary '{}' not found (searched PATH and common "
              "install locations).\n{}".format(
                  bin_name, deps_install_hint()))

    # Log which QEMU actually got picked, AFTER every pinned-build swap above.
    # Some packagings do not carry an upstream version anywhere a log reader
    # can see it -- chocolatey's `qemu` is versioned by date (2026.7.23), and
    # neither the install output nor the package metadata names the QEMU
    # release -- so without this a Windows CI log cannot answer "which QEMU
    # was that?" at all. One cheap subprocess, once per launch.
    try:
        _qv = subprocess.check_output([qemu_bin, "--version"], stderr=DEVNULL)
        _qv = _qv.decode("utf-8", "replace").strip().splitlines()
        debuglog(config['debug'], "QEMU: {} ({})".format(
            _qv[0] if _qv else "?", qemu_bin))
    except Exception:
        debuglog(config['debug'], "QEMU: version query failed ({})".format(qemu_bin))

    # VNC Console Auto-detection logic:
    vnc_val = config.get('vnc', '')
    auto_reason = None
    
    if not vnc_val and not is_vnc_console:
        if config['os'] == "reactos":
            # ReactOS is a desktop-only guest: the GUI is the whole point, and
            # its COM1 carries kernel debug only -- there is no serial shell to
            # fall back to, so a console-mode web UI shows the user nothing.
            # Pin graphical VNC here so no generic rule below can take it away.
            debuglog(config['debug'],
                     "reactos: keeping graphical VNC (COM1 has no shell, only kernel debug)")
        elif config['os'] == "riscos":
            # Same trap as reactos, one step worse. RISC OS is a desktop system
            # -- the Wimp desktop on the BCM2835 framebuffer is the display --
            # and it writes NOTHING to the serial port: <osname>.serial.log
            # stays at 0 bytes for an entire run. (That is the same fact that
            # makes its shutdown end on a timeout instead of a halt banner.)
            # The generic non-x86 rule below would therefore replace the one
            # surface that has content with one that is permanently blank.
            debuglog(config['debug'],
                     "riscos: keeping graphical VNC (serial is never written to)")
        elif config['os'] == "openindiana":
            if "202510" in config['release']:
                # Rule for OpenIndiana: Default to 'console' if not specified.
                auto_reason = "OpenIndiana (requires console for login display)"
        elif config['arch'] not in ("", "i386"):
            # Rule for non-x86 architectures: Default to 'console' if not
            # specified. Tested on the ARCH, not on bin_name: the old
            # `"x86_64" not in bin_name` spelling also caught
            # qemu-system-i386, so every i386 guest (ReactOS, and Hurd's
            # 32-bit image) silently got the serial console in the web UI
            # instead of its framebuffer -- and ReactOS puts nothing but
            # kernel debug on COM1, so its desktop was simply never shown.
            # "" is x86_64 here (see the normalize block above).
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
    elif config['arch'] == "sparc64":
        # QEMU has no KVM (or any hardware) accelerator for sparc64 on any
        # host -- the sun4u machine is TCG-only everywhere. Pin it explicitly
        # so the generic else-branch below cannot mis-set accel="kvm" on an
        # x86 host with a usable /dev/kvm. That misclassification was harmless
        # to the QEMU launch (the sun4u machine string carries no accel=), but
        # it skipped the TCG-only boot-timeout bump (left boot on the 600s
        # default) and the TCG ssh-probe grace (left it at 5s) -- which is what
        # timed out the very slow sparc64 boots in CI.
        accel = "tcg"
    elif config['arch'] == "s390x":
        # KVM for s390x exists only on real IBM Z hosts; use it when running
        # on one (host s390x + usable /dev/kvm), TCG everywhere else.
        if host_arch == "s390x":
            if os.path.exists("/dev/kvm"):
                if os.access("/dev/kvm", os.R_OK | os.W_OK):
                    accel = "kvm"
                else:
                    log("Warning: /dev/kvm exists but is not writable. Falling back to TCG.")
    elif config['arch'] in ("powerpc64", "powerpc64le", "ppc64", "ppc64le"):
        # KVM-HV / KVM-PR is only available when the host is also ppc64
        # (real POWER8/9 hardware). On an amd64 / aarch64 host we must use
        # TCG: pseries,accel=kvm on a non-ppc host would fail at launch.
        if host_arch in ["ppc64", "ppc64le", "powerpc64", "powerpc64le"]:
            if os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK):
                accel = "kvm"
    else: # x86_64
        if host_arch in ["x86_64", "amd64"]:
            if IS_WINDOWS:
                if config['whpx']:
                    accel = "whpx"
                elif config['os'] == 'ghostbsd':
                    # GhostBSD is known-broken under WHPX: its boot path runs
                    # an SSE instruction against an MMIO region, which QEMU's
                    # WHPX instruction emulator cannot decode ("failed to
                    # decode instruction f 10" -- movups), and QEMU aborts
                    # with exit code 3 about 2 min into boot. Seen on CI run
                    # 29258342415: every ghostbsd sync variant failed this
                    # way while all other OSes passed under WHPX. Keep TCG;
                    # --whpx above still forces it for experiments.
                    debuglog(config['debug'], "GhostBSD: keeping TCG (QEMU WHPX aborts on its SSE MMIO access; pass --whpx to force)")
                elif not config['tcg'] and whpx_available():
                    # WHPX is on by default when the Windows Hypervisor
                    # Platform is actually running (real runtime probe via
                    # WHvGetCapability, not a DLL-presence heuristic).
                    # --tcg forces software emulation; --whpx is now only
                    # needed to skip this probe.
                    accel = "whpx"
                    debuglog(config['debug'], "WHPX available: enabling hardware acceleration by default (pass --tcg to force software emulation)")
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

    # Force pure software emulation (TCG) when requested (--tcg). Generic --
    # applies to any guest; useful when hardware acceleration is unavailable or
    # misbehaving.
    #
    # Note: tribblix no longer needs an Intel-specific TCG fallback. The release
    # image (>= v2.0.3, built with tribblix-builder's finalizeImage hook) ships
    # the generic, capability-neutral /lib/libc.so.1, so it boots under KVM on
    # both Intel and AMD and re-optimizes libc per-CPU at first boot. (Older
    # releases froze a vendor-specific libc_hwcap variant that crash-looped
    # cross-vendor; if you must run one of those on a mismatched CPU, pass --tcg.)
    if config['tcg'] and accel != "tcg":
        debuglog(config['debug'], "Forcing TCG software emulation (--tcg); ignoring KVM/HVF/WHPX")
        accel = "tcg"

    # An accelerator the chosen binary was not BUILT with is a hard QEMU
    # startup error, not a slow fallback: "invalid accelerator whpx", exit
    # code 1, before the guest ever runs. Windows QEMU ships WHPX in
    # qemu-system-x86_64.exe ONLY -- qemu-system-i386.exe answers
    # `-accel help` with tcg alone -- so every i386 guest (ReactOS, Hurd's
    # 32-bit image) died at launch on a Windows host.
    #
    # For i386 the fix is to run the guest on the 64-bit binary instead of
    # dropping it to TCG. The two are built from the same QEMU target
    # (target/i386) and expose the same `pc` machine and the same devices;
    # a 32-bit guest simply never enters long mode, exactly as it would not
    # on real 64-bit hardware. TCG is not an acceptable alternative here:
    # an NT kernel under software emulation does not even finish early
    # kernel init inside the 600s boot window (CI run 31238792386).
    # The substitution is logged, never silent.
    if accel != "tcg" and not qemu_has_accel(qemu_bin, accel):
        alt_bin = find_qemu("qemu-system-x86_64") if config['arch'] == "i386" else None
        if alt_bin and alt_bin != qemu_bin and qemu_has_accel(alt_bin, accel):
            log("QEMU {} has no {} accelerator; running this i386 guest on {} instead (same target, 64-bit binary).".format(
                os.path.basename(qemu_bin), accel, os.path.basename(alt_bin)))
            qemu_bin = alt_bin
        else:
            log("Warning: QEMU {} was built without the {} accelerator; falling back to TCG.".format(
                os.path.basename(qemu_bin), accel))
            accel = "tcg"

    # CPU optimization for TCG
    if not cpu_specified and accel == "tcg":
        try:
            if int(config['cpu']) > 2:
                debuglog(config['debug'], "TCG mode detected and no CPU count specified, limiting to 2 cores for performance optimization.")
                config['cpu'] = "2"
        except (ValueError, TypeError):
            pass
    elif not cpu_specified:
        # Hardware acceleration (KVM/HVF/WHPX): cap the default vCPU count
        # at 8. The old default handed the guest every host core, and on
        # big hosts (e.g. 32 threads) guest SMP bring-up gets slower, not
        # faster. Hosts with 8 or fewer cores keep their full count; an
        # explicit --cpu still grants any count.
        try:
            if int(config['cpu']) > 8:
                debuglog(config['debug'], "No CPU count specified, capping default at 8 vCPUs (pass --cpu for more).")
                config['cpu'] = "8"
        except (ValueError, TypeError):
            pass

    # sparc64 (QEMU sun4u) is uniprocessor, and its kernel's early OpenFirmware
    # pmap bootstrap panics ("Can't claim two pages of memory") with more than
    # ~2 GB of RAM. Force 1 CPU and cap memory to 2048 MB so the built image
    # boots. (The image is built on the same limits; see netbsd-builder.)
    # OpenBSD is capped tighter: OpenBIOS memory claims get flaky near the
    # 2 GB boundary and openbsd-builder builds/verifies its image at 1024 MB.
    if config['arch'] == "sparc64":
        config['cpu'] = "1"
        mem_cap = 1024 if config['os'] == "openbsd" else 2048
        try:
            mem_mb = int(str(config['mem']).rstrip("MmGg"))
            if str(config['mem']).rstrip()[-1:].lower() == "g":
                mem_mb *= 1024
            if mem_mb > mem_cap:
                config['mem'] = str(mem_cap)
        except (ValueError, TypeError, IndexError):
            config['mem'] = str(mem_cap)

    # GNU Hurd: stock gnumach is uniprocessor (SMP is an experimental add-on
    # package), so force 1 vCPU on both arches. The 32-bit i386 kernel
    # additionally cannot use big RAM; cap it at 2048 MB (hurd-builder builds
    # and verifies the image at that size too).
    if config['os'] == "hurd":
        config['cpu'] = "1"
        if config['arch'] == "i386":
            hurd_mem_cap = 2048
            try:
                mem_mb = int(str(config['mem']).rstrip("MmGg"))
                if str(config['mem']).rstrip()[-1:].lower() == "g":
                    mem_mb *= 1024
                if mem_mb > hurd_mem_cap:
                    config['mem'] = str(hurd_mem_cap)
            except (ValueError, TypeError, IndexError):
                config['mem'] = str(hurd_mem_cap)

    # powerpc64/le (QEMU pseries) under TCG: SMP bring-up on a cold boot from
    # the installed disk reproducibly wedges at the kernel's "Launching APs"
    # line (the same hang the builder pins VM_CPU=1 to avoid). TCG is
    # round-robin, so extra vCPUs give no speedup anyway. Force 1 CPU under
    # TCG; real POWER + KVM can use more.
    if (config['arch'] in ("powerpc64", "powerpc64le", "ppc64", "ppc64le")
            and accel == "tcg"):
        config['cpu'] = "1"

    # RISC OS runs on raspi2b, which models one specific board rather than a
    # configurable machine, so -smp and -m are not preferences here -- QEMU
    # refuses to start on either mismatch and the guest never runs:
    #
    #   hw/arm/raspi.c, raspi_machine_class_init():
    #       mc->default_cpus = mc->min_cpus = mc->max_cpus = cores_count(board_rev);
    #   so -smp must be EXACTLY 4 for a Pi 2 (min == max; not "at least 4"),
    #   and the error is
    #       "Invalid SMP CPUs 2. The min CPUs supported by machine 'raspi2b' is 4"
    #
    #   raspi_machine_init() rejects any other size with
    #       "Invalid RAM size, should be %s"
    #   against board_ram_size(board_rev) = 256 MiB << MEMORY_SIZE. raspi2b is
    #   board rev 0xa21041, whose MEMORY_SIZE field is 2, so 256 MiB << 2 =
    #   1024 MB.
    #
    # Both defaults were wrong: TCG had already clamped the vCPUs to 2 above,
    # and memory came in at 4096. This is pinned rather than capped, because a
    # cap cannot raise 2 up to 4, and it deliberately overrides an explicit
    # --cpu/--mem too -- there is no working value other than these. It mirrors
    # riscos-builder's conf, which pins VM_CPU=4 / VM_MEMORY=1024 for exactly
    # the same reason. (RISC OS then reports 960MB; the GPU split takes the
    # rest.)
    if config['os'] == "riscos":
        config['cpu'] = "4"
        config['mem'] = "1024"

    # KVM-accelerated x86_64-on-x86_64 guests reach the ssh probe in well
    # under a minute; when slirp has seen no guest packet for minutes the
    # guest has panicked (vmactions/netbsd-vm#21: deterministic NetBSD
    # early-boot panic on Xeon Platinum 8573C runner hosts) and waiting out
    # the full 600s only delays the conservative-CPU retry that heals it.
    # Cut the FIRST boot timeout to 180s for this case; the retry keeps the
    # full 600s window (boot_timeout_retry_sec) so a genuinely slow first
    # boot that got cut short still has every chance on attempt two. TCG
    # and emulated arches keep the default; --boot-timeout-sec always wins.
    if (not boot_timeout_user_specified and accel == "kvm"
            and config['arch'] in ("", "x86_64", "amd64")
            and platform.machine().lower() in ("x86_64", "amd64")
            and config['boot_timeout_sec'] > 180):
        config['boot_timeout_retry_sec'] = config['boot_timeout_sec']
        config['boot_timeout_sec'] = 180
        debuglog(config['debug'], "KVM x86_64-on-x86_64: first-boot timeout reduced to 180s (retry keeps {}s)".format(config['boot_timeout_retry_sec']))


    # Disk type selection. A user --disktype wins; otherwise the guest profile
    # (when present) is authoritative -- it carries the exact bus the image was
    # built on. The per-OS fallbacks below are kept for local --qcow2 files and
    # releases that predate the profile asset.
    if config['disktype']:
        disk_if = config['disktype']
    elif guest_profile and guest_profile.get('disk_if'):
        disk_if = guest_profile['disk_if']
        debuglog(config['debug'], "Disk bus from guest profile: {}".format(disk_if))
    elif config['os'] == "reactos":
        # ReactOS has no virtio-blk driver at all, so the x86 virtio default
        # gives a guest that cannot see its own boot disk: it loads the boot
        # drivers, finds nothing, and bugchecks 0x7B INACCESSIBLE_BOOT_DEVICE
        # straight into kdb. reactos-builder installs on IDE (conf
        # VM_DISK=ide) and the published profile says so; this fallback is
        # what saves the profile-less --qcow2 path.
        disk_if = "ide"
    elif config['os'] == "dragonflybsd":
        disk_if = "ide"
    elif config['os'] == "ghostbsd":
        # GhostBSD ships only a live GUI installer, so the image is built by
        # driving pc-sysinstall under SeaBIOS onto an emulated SATA/IDE disk
        # (ghostbsd-builder conf VM_DISK="ide"); the installed system enumerates
        # it as ada0 and a BIOS bootloader is written there. Boot it the same
        # way: IDE controller, no UEFI (useefi stays unset -> SeaBIOS). virtio-blk
        # would change the device name and miss the installed bootloader.
        disk_if = "ide"
    elif config['os'] == "tribblix":
        # The tribblix image is built on an AHCI SATA controller: the
        # tribblix-builder libvirt XML pins <target bus='sata'> and the disk
        # enumerates as c2t0d0 inside the guest (live_install.sh installs to
        # c2t0d0). virtio-blk puts the disk on a different controller with a
        # different device name, so the installed root pool's device paths no
        # longer match. Match the builder. Scoped to tribblix only; other
        # illumos distros are left on the virtio default.
        disk_if = "sata"
    elif config['arch'] == "sparc64":
        # QEMU sun4u only has the onboard CMD646 PCI IDE (the disk enumerates as
        # wd0); there is no virtio bus. The image is built on IDE too.
        disk_if = "ide"
    else:
        disk_if = "virtio"

    # Build Netdev Argument
    # Always include standard SSH mapping
    netdev_args = "user,id=net0,net=192.168.122.0/24,dhcpstart=192.168.122.10"
    if not config.get('enable_ipv6'):
        netdev_args += ",ipv6=off"

    # Track every hostfwd we install so we can rebind them at runtime via the
    # QEMU monitor if the VM ends up with an unexpected IP (e.g. stale DHCP lease).
    # Each entry: (proto, host_addr, host_port_str, guest_port_str)
    hostfwd_specs = []

    # plan9/9front has no sshd: the control channel is telnetd on guest port
    # 23, and folder sync mounts exportfs 9P on guest port 564. Forward
    # config['sshport'] to 23 (so `ssh <port>`-style aliases and the boot
    # wait all reach the same place) and, when 9p sync is active, pin a
    # second forward to 564.
    ctl_guest_port = "23" if config.get('transport') == "telnet" else "22"
    netdev_args += ",hostfwd=tcp:{}:{}-:{}".format(ssh_addr, config['sshport'], ctl_guest_port)
    hostfwd_specs.append(("tcp", ssh_addr, str(config['sshport']), ctl_guest_port))
    for extra_addr in ssh_extra_addrs:
        if is_port_available(extra_addr, int(config['sshport'])):
            netdev_args += ",hostfwd=tcp:{}:{}-:{}".format(extra_addr, config['sshport'], ctl_guest_port)
            hostfwd_specs.append(("tcp", extra_addr, str(config['sshport']), ctl_guest_port))
            debuglog(config['debug'], "hostfwd: CTL {}:{} -> :{} OK".format(extra_addr, config['sshport'], ctl_guest_port))
        else:
            debuglog(config['debug'], "hostfwd: CTL {}:{} -> :{} SKIPPED (port in use)".format(extra_addr, config['sshport'], ctl_guest_port))
    if config.get('transport') == "telnet" and config.get('sync') == "9p":
        # An explicit --p9-port pins the forward so a LATER process can find
        # it: `--attach --pull-files` has to reopen the same 9P channel to
        # copy the guest's work tree back, and a random port would be
        # unknowable from outside this process (nothing writes it to disk).
        p9_host_port = config.get('p9_port') or get_free_port(20564, 20999)
        if p9_host_port:
            netdev_args += ",hostfwd=tcp:{}:{}-:564".format(ssh_addr, p9_host_port)
            hostfwd_specs.append(("tcp", ssh_addr, str(p9_host_port), "564"))
            config['p9_host_port'] = p9_host_port
            debuglog(config['debug'], "hostfwd: 9P {}:{} -> :564 OK".format(ssh_addr, p9_host_port))
        else:
            log("Warning: no free host port for 9P forward; --sync 9p will be skipped.")

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
        or config['arch'] in ("powerpc64", "powerpc64le", "ppc64", "ppc64le")
        or config.get('useefi')
    )
    if disk_if == "sata":
        # AHCI controller + ide-hd, matching how illumos images are built
        # (libvirt <target bus='sata'>). The i440fx 'pc' machine already
        # carries a PIIX IDE controller, so the AHCI disk enumerates as
        # c2t0d0 in the guest -- the device name the installed system expects.
        # No bootindex in BIOS mode so we don't confuse illumos GRUB.
        args_qemu.extend([
            "-drive", "file={},format=qcow2,if=none,id=disk0,discard=unmap,detect-zeroes=unmap".format(qcow_name),
            "-device", "ich9-ahci,id=ahci0",
            "-device", "ide-hd,bus=ahci0.0,drive=disk0"
        ])
    elif disk_if == "virtio" and needs_bootindex_disk:
        args_qemu.extend([
            "-drive", "file={},format=qcow2,if=none,id=disk0,discard=unmap,detect-zeroes=unmap".format(qcow_name),
            "-device", "virtio-blk-pci,drive=disk0,bootindex=0"
        ])
    elif config['os'] == "netbsd" and config['arch'] == "riscv64":
        # NetBSD/riscv GENERIC64 has no PCI virtio driver -- a virtio-blk-pci
        # enumerates "not configured", so the kernel finds no root and hangs at
        # "boot device: <unknown>" / the root-device prompt (never reaching
        # sshd). Attach the disk on the MMIO virtio transport (virtio-blk-device)
        # like netbsd-builder does. Ubuntu riscv64 keeps the PCI shortcut (else).
        args_qemu.extend([
            "-drive", "file={},format=qcow2,if=none,id=disk0,discard=unmap,detect-zeroes=unmap".format(qcow_name),
            "-device", "virtio-blk-device,drive=disk0"
        ])
    elif config['arch'] == "armv7":
        # RISC OS on raspi2b boots off the SD interface -- the board has no
        # disk controller and no PCI bus to put one on, and SDFS is what the
        # guest reads. No discard/detect-zeroes: QEMU's SD model is not a
        # TRIM-capable transport, and every verified boot of this guest ran
        # without them.
        args_qemu.extend([
            "-drive", "file={},format=qcow2,if=sd".format(qcow_name)
        ])
    elif config['arch'] == "sparc64":
        # QEMU sun4u's CMD646 PCI IDE under TCG loses completion interrupts on
        # TRIM/UNMAP: discard=unmap + detect-zeroes=unmap turn zero-writes into
        # ATA DATA SET MANAGEMENT (TRIM) ops, and the cmd646 BMDMA path
        # mishandles their completion -> a "lost interrupt" storm that wedges the
        # guest (worst on 10.0). netbsd-builder builds the image with a PLAIN
        # `-drive if=ide,index=0` (no discard) and its boots show ZERO lost
        # interrupts; match it exactly so the runtime boot stays clean. (Thinness
        # from discard does not matter for the small 4G ephemeral sparc64 disk.)
        # The cmd646 wedge itself is fixed at its source by the patched QEMU
        # that ensure_pinned_qemu forces on Linux x86_64 hosts (each builder
        # publishes its own patched tarball).
        args_qemu.extend([
            "-drive", "file={},format=qcow2,if=ide,index=0".format(qcow_name)
        ])
    else:
        args_qemu.extend([
            "-drive", "file={},format=qcow2,if={},discard=unmap,detect-zeroes=unmap".format(qcow_name, disk_if)
        ])

    rtc_base = "utc"
    # ReactOS is an NT reimplementation and follows the Windows convention of
    # reading the CMOS clock as local time. Mirrors build.py's rtc_base.
    if config['os'] in ["windows", "reactos", "haiku"]:
        rtc_base = "localtime"
    rtc_opts = "base={},clock=host,driftfix=slew".format(rtc_base)
    if config['os'] == "riscos":
        # Same class of hazard as ReactOS below, for a simpler reason: the
        # Raspberry Pi has no mc146818 RTC for driftfix to act on, and RISC OS
        # calibrates its own delay loops from timer interrupts. Every verified
        # boot of this guest ran without driftfix; do not add it back on the
        # strength of it "probably being a no-op".
        rtc_opts = "base={},clock=host".format(rtc_base)
    if config['os'] == "reactos":
        # NO driftfix for ReactOS: it is the difference between booting and
        # not. HalpCalibrateStallExecution (hal/halx86/generic/systimer.S)
        # sizes every busy-wait by counting loop iterations between two RTC
        # (IRQ8) periodic interrupts and dividing by 125000 us. driftfix=slew
        # replays interrupts that the guest was too slow to take, so on any
        # host without full hardware virtualisation the two calibration
        # interrupts arrive back to back, the count is ~0, and the division
        # stores StallScaleFactor = 0. KeStallExecutionProcessor then does
        # `mov eax, factor; mul us; sub eax,1; jnz` -- starting from 0, which
        # wraps to 2^32 iterations, so EVERY stall (even a 1 us one) burns
        # ~8.7 s and the boot never finishes.
        # Measured on this image, pure TCG, identical otherwise:
        #   driftfix=slew  -> factor 0,    never booted (25 min+)
        #   no driftfix    -> factor 1011, booted in 68 s
        #   clock=vm       -> factor 1063, booted in 72 s
        # Under KVM the guest keeps up, no ticks are replayed, and the factor
        # comes out ~3899 either way -- which is why this only ever showed up
        # on WHPX/TCG hosts.
        rtc_opts = "base={},clock=host".format(rtc_base)
    args_qemu.extend(["-rtc", rtc_opts])

    if config['snapshot']:
        args_qemu.append("-snapshot")

    # Windows on ARM has DirectSound issues; disable audio only there.
    if IS_WINDOWS and host_arch == "aarch64":
        args_qemu.extend(["-audiodev", "none,id=snd"])

    # Network card selection. A user --nc wins; otherwise the guest profile
    # (when present) is authoritative -- it carries the exact NIC model the
    # installed guest kernel was built to drive (this is the field that drifted
    # most: a wrong model means no DHCP / no sshd). The per-OS fallback chain
    # below serves local --qcow2 files and releases without a profile asset.
    if config['nc']:
        net_card = config['nc']
    elif guest_profile and guest_profile.get('net_card'):
        net_card = guest_profile['net_card']
        if (guest_profile.get('virtio_transport') == "mmio"
                and net_card.startswith("virtio-net-pci")):
            # The profile records the MODEL (+option flags); the bus lives
            # in virtio_transport. netbsd riscv64 is the live case: its
            # profile says net_card=virtio-net-pci,ctrl_vq=off with
            # virtio_transport=mmio, and NetBSD/riscv GENERIC64 has no PCI
            # virtio driver -- taking the pci name verbatim boots a guest
            # with NO NIC at all (dhcpcd: "no valid interfaces found",
            # sshd unreachable, netbsd-vm run 30776357735). Translate to
            # the MMIO transport device, keeping the flags.
            net_card = net_card.replace("virtio-net-pci",
                                        "virtio-net-device", 1)
        debuglog(config['debug'], "Network card from guest profile: {}".format(net_card))
    else:
        if config['arch'] == "aarch64":
            net_card = "virtio-net-pci"
        elif config['arch'] == "armv7":
            # raspi2b has no PCI bus, so the e1000 default would abort QEMU at
            # launch. The board's real NIC is an SMSC LAN9512 behind the
            # on-chip USB hub, and it is also the only model RISC OS can
            # drive -- there is no choice to make here.
            net_card = "usb-net-smsc95xx"
        else:
            net_card = "e1000"
        if config['os'] == "openbsd" and config['arch'] == "sparc64":
            # sun4u has no virtio. The NIC sits on the empty secondary
            # Simba-bridge bus pciB. e1000 (-> em0 in the guest) is the
            # model openbsd-builder builds and verifies the image on:
            # ne2k_pci (-> ne0) wedges QEMU's cmd646 PCI-IDE into a
            # "lost interrupt" write-timeout storm when network and disk
            # DMA overlap at "starting network", stalling boot on a slow
            # TCG host; e1000's DMA model keeps boot clean. The published
            # image is configured for em0, so this must match it.
            net_card = "e1000"
        elif config['os'] == "openbsd" and config['release']:
            release_base = config['release'].split('-')[0]
            if release_base in OPENBSD_E1000_RELEASES:
                net_card = "e1000"
            else:
                net_card = "virtio-net-pci"
        elif config['os'] == "dragonflybsd":
            if config['release'] != "6.4.0":
                net_card = "virtio-net-pci"
        elif config['arch'] == "riscv64":
            # NetBSD/riscv GENERIC64 has no PCI virtio driver: a virtio-net-pci
            # enumerates "not configured" -> no NIC -> no DHCP/sshd. Drive the
            # NIC over the MMIO virtio transport for NetBSD (matches
            # netbsd-builder); Ubuntu riscv64 keeps PCI.
            if config['os'] == "netbsd":
                net_card = "virtio-net-device"
            else:
                net_card = "virtio-net-pci"
        elif config['arch'] == "s390x":
            # Devices on s390-ccw-virtio sit on the CCW bus, not PCI.
            net_card = "virtio-net-ccw"
        elif config['arch'] in ("powerpc64", "powerpc64le", "ppc64", "ppc64le"):
            net_card = "virtio-net-pci"
        elif config['os'] == "netbsd" and config['arch'] == "aarch64":
            net_card = "virtio-net-pci"
        elif config['os'] == "freebsd":
            net_card = "virtio-net-pci"
        elif config['os'] == "plan9":
            # plan9-builder builds and verifies the image on virtio-net
            # (conf VM_NIC=virtio; 9front's ethervirtio brings the link up as
            # ether0). The published profile already carries this; the
            # built-in default matters only for the profile-less --qcow2 path.
            net_card = "virtio-net-pci"
        elif config['os'] == "reactos":
            # ReactOS ships netkvm, but reactos-builder builds and verifies on
            # e1000 (conf VM_NIC=e1000) -- the better-worn path, and the NIC
            # the installed guest already has bound with a DHCP lease on it.
            # Same profile-less --qcow2 reasoning as plan9 above.
            net_card = "e1000"
        elif config['os'] == "ubuntu":
            # The ubuntu-builder image is built and validated on a virtio NIC
            # (conf VM_NIC=virtio / libvirt <model type='virtio'>). The baked
            # cloud-init/netplan brings DHCP up on that interface; the x86
            # default e1000 would enumerate under a different name and the
            # guest could fail to obtain a lease. Match the builder.
            net_card = "virtio-net-pci"
        elif config['os'] == "blissos":
            # blissos-builder builds and verifies on virtio-net (conf
            # VM_NIC=virtio); the BlissOS kernel drives it as eth0. Match it.
            net_card = "virtio-net-pci"

    # NetBSD + virtio-net: withdraw the control virtqueue.
    #
    # NetBSD's vioif(4) wedges FOREVER on a control-queue command that never
    # completes. if_vioif.c does:
    #
    #     mutex_enter(&ctrlq->ctrlq_wait_lock);
    #     while (ctrlq->ctrlq_inuse != DONE)
    #             cv_wait(&ctrlq->ctrlq_wait, &ctrlq->ctrlq_wait_lock);
    #
    # -- a cv_wait with NO timeout and no retry, woken only by the vq
    # interrupt. Miss that wakeup once and the interface is dead for the life
    # of the boot, with the interface lock held: dhcpcd (promisc for BPF ->
    # VIRTIO_NET_CTRL_RX_PROMISC) and mdnsd (multicast -> CTRL_MAC_TABLE_SET)
    # sleep in state D on wchan "ctrl_vq", every later network consumer piles
    # up behind them on "tstile", and even `ifconfig vioif0` never returns.
    # The guest still boots and reaches rc, so it looks like a hung kernel:
    # no DHCP lease, no address for slirp's hostfwd to reach, zero
    # guest-originated packets, and rc stops dead at whichever service touches
    # the network first (Starting ntpd. / sshd. / postfix.).
    #
    # Measured on netbsd 10.0-aarch64 v2.1.7, QEMU 8.2.2 TCG: 14 hangs in 34
    # boots (41%). anyvm CI run 30353533772 lost 3 of its 10 10.0-aarch64 jobs
    # to it in one go.
    #
    # vioif only builds that code path when BOTH CTRL_VQ and CTRL_RX are
    # negotiated:
    #
    #     if ((features & VIRTIO_NET_F_CTRL_VQ) &&
    #         (features & VIRTIO_NET_F_CTRL_RX)) { sc->sc_has_ctrl = true; ...
    #
    # so withholding CTRL_VQ makes the wedge structurally unreachable rather
    # than merely rarer. Verified: 10/10 clean boots with the knob vs 3/10
    # hangs without it in the same interleaved run, and a booted guest is
    # fully functional -- DHCP lease, default route, DNS, scp both ways, and
    # dhcpcd/mdnsd/ntpd all in normal sleep states instead of D/ctrl_vq.
    #
    # Cost: the guest can no longer program the RX filters, so the NIC runs
    # PROMISC,ALLMULTI and receives everything. On a private slirp segment
    # that changes nothing that matters.
    #
    # Two knobs that do NOT fix it, both measured, so do not "simplify" this
    # into one of them: event_idx=off (7 green/5 hang, same as baseline) and
    # -cpu cortex-a72 vs max (7/5 vs 6/6). -smp 1 makes it WORSE (7/10 hang).
    if (config['os'] == "netbsd"
            and net_card.startswith("virtio-net")
            and "ctrl_vq" not in net_card):
        net_card = net_card + ",ctrl_vq=off"
        debuglog(config['debug'],
                 "NetBSD vioif: withdrawing CTRL_VQ -> {}".format(net_card))

    # Platform specific args
    if config['arch'] == "aarch64":
        efi_path = os.path.join(output_dir, vm_name + "-QEMU_EFI.fd")
        vars_path = os.path.join(output_dir, vm_name + "-QEMU_EFI_VARS.fd")

        # Locate the CODE firmware once (also used to find the VARS template).
        # Search next to the QEMU binary first so a relocated, no-root install
        # (e.g. ~/qemu-local) is honored, then the usual system paths.
        fw_dirs = []
        if qemu_bin:
            try:
                _qpref = os.path.dirname(os.path.dirname(os.path.realpath(qemu_bin)))
                fw_dirs.append(os.path.join(_qpref, "share"))
            except Exception:
                pass
        fw_dirs += ["/usr/share", "/opt/homebrew/share", "/usr/local/share"]
        fw_rel_names = [
            os.path.join("edk2", "aarch64", "QEMU_EFI.fd"),
            os.path.join("qemu-efi-aarch64", "QEMU_EFI.fd"),
            os.path.join("AAVMF", "AAVMF_CODE.fd"),
            os.path.join("qemu", "edk2-aarch64-code.fd"),
            os.path.join("edk2", "aarch64", "QEMU_EFI-pflash.raw"),
        ]
        code_candidates = []
        if config['firmware']:
            code_candidates.append(config['firmware'])
        for d in fw_dirs:
            for rn in fw_rel_names:
                code_candidates.append(os.path.join(d, rn))
        efi_src = ""
        for c in code_candidates:
            if os.path.exists(c):
                efi_src = c
                break

        if not os.path.exists(efi_path):
            if not efi_src:
                fatal("aarch64 UEFI firmware not found (e.g. edk2-aarch64 "
                      "QEMU_EFI.fd). Install it or pass --firmware <path>.")
            debuglog(config['debug'], "Found Aarch64 EFI firmware: {}".format(efi_src))
            # ARM virt pflash is a fixed 64MB; pad the firmware into it.
            create_sized_file(efi_path, 64)
            copy_content_to_file(efi_src, efi_path)

        if config['snapshot'] and os.path.exists(vars_path):
            try: os.remove(vars_path)
            except OSError: pass

        if not os.path.exists(vars_path):
            create_sized_file(vars_path, 64)
            # Prefer the matching VARS template (auto-detected next to the CODE
            # firmware) so any preset variables are present; --firmware-vars
            # overrides. A blank 64MB store is the last-resort fallback.
            vars_src = config['firmware_vars']
            if vars_src and not os.path.exists(vars_src):
                fatal("Specified firmware vars not found: {}".format(vars_src))
            if not vars_src and efi_src:
                d = os.path.dirname(efi_src)
                base = os.path.basename(efi_src)
                guesses = []
                for a, b in [("QEMU_EFI", "QEMU_VARS"), ("_CODE", "_VARS"),
                             ("-code", "-vars"), ("CODE", "VARS")]:
                    if a in base:
                        guesses.append(os.path.join(d, base.replace(a, b)))
                guesses += [
                    os.path.join(d, "QEMU_VARS.fd"),
                    os.path.join(d, "vars-template-pflash.raw"),
                    os.path.join(d, "AAVMF_VARS.fd"),
                ]
                for g in guesses:
                    if g != efi_src and os.path.exists(g):
                        vars_src = g
                        break
            if vars_src:
                debuglog(config['debug'], "Aarch64 VARS template: {}".format(vars_src))
                copy_content_to_file(vars_src, vars_path)

        if config['cputype']:
            cpu = config['cputype']
        else:
            if accel in ["kvm", "hvf"]:
                cpu = "host"
            # Everything below is a HAND-MAINTAINED mirror of the VM_CPU_MODEL
            # pins in the builders' confs -- anyvm deliberately does not read
            # cpu_model out of the guest profile, so a conf pin and the branch
            # here must be changed together or the two sides silently disagree.
            elif config['os'] == "openbsd":
                # OpenBSD fails with "FP exception in kernel" on cpu=max
                cpu = "neoverse-n1"
            elif config['os'] in ("ubuntu", "openeuler"):
                # Two empirically verified problems with -cpu max on TCG:
                #  * Kernels that use VHE when the CPU offers it (Ubuntu
                #    26.04's 7.0, openEuler's 6.6) abort QEMU 8.2 (ubuntu
                #    noble's package) with "ERROR:target/arm/internals.h:
                #    767:regime_is_user: code should not be reached"
                #    (SIGABRT mid-boot) -- the E20 regimes were only
                #    handled in QEMU >= 9.0.
                #  * Ubuntu 26.04's shim/grub also hangs at BdsDxe under
                #    -cpu max during image builds (SVE/SME mishandling).
                # cortex-a72 (ARMv8.0, no VHE/SVE) sidesteps both and boots
                # every ubuntu/openeuler release artifact. Use --cpu-type to
                # override.
                #
                # Builder side, checked 2026-07-28: all three openeuler-aarch64
                # confs and ubuntu-26.04-aarch64 pin VM_CPU_MODEL=cortex-a72
                # too, so those agree. ubuntu 22.04/24.04-aarch64 do NOT -- they
                # BUILD on -cpu max (runner's stock QEMU 8.2) and pass, while
                # this OS-wide branch runs them on cortex-a72. It has covered
                # the whole ubuntu family since commit 4fdfc4e, whose evidence
                # was 26.04 only, so max is merely untested at run time for
                # 22.04/24.04 -- untested, not known-broken. To settle it, pin
                # VM_CPU_MODEL in those two confs and narrow this branch to
                # match; do not just drop it and hope.
                cpu = "cortex-a72"
            elif config['os'] == "netbsd" and (
                    config['release'].split('.')[0] == "9"
                    or config['release'].split('.')[:2] == ["10", "1"]):
                # NetBSD aarch64 sshd is corrupted by -cpu max under QEMU TCG on
                # 9.x and on 10.1: sshd accepts a connection and then dies in the
                # handshake, so probes get "connection closed before banner" /
                # "kex_exchange_identification: read: Connection reset by peer"
                # until the boot wait gives up. cortex-a72 (ARMv8.0, no VHE/SVE)
                # is the stable model for those releases. anyvm deliberately does
                # NOT read cpu_model from the profile, so this mirrors the
                # VM_CPU_MODEL pins in netbsd-builder's netbsd-9.x/10.1-aarch64
                # .conf and the two sides must be kept in sync BY HAND: changing
                # a conf pin alone does not change what the runtime launches.
                # Override with --cpu-type.
                #
                # The release test is deliberately EXACT, not a 10.x prefix.
                # 10.0 fails the opposite way: under cortex-a72 its guest sshd
                # refuses to start at all ("/etc/rc.d/sshd exited with code 1",
                # netbsd-builder run 30199351351) after ten consecutive green
                # builds on max. So 10.0 and 11.0 must stay on max -- the right
                # model differs per release inside 10.x and cannot be
                # generalised. Both directions were learned the hard way on
                # 2026-07-26; check a real build before adding another release.
                cpu = "cortex-a72"
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
        if config['cputype']:
            cpu_opts = config['cputype']
        elif config['os'] == "ubuntu" and (config['release'] or "").startswith("26."):
            # Ubuntu 26.04 riscv64 userspace targets the RVA23 profile
            # baseline: under plain rv64 init dies with SIGILL
            # (do_trap_insn_illegal), and its 7.0 kernel additionally hangs
            # at entry with zero output on QEMU 8.2 TCG. The rva23s64 CPU
            # model exists in QEMU >= 9.1; an older QEMU fails fast at
            # launch ("unable to find CPU definition"), which is the
            # clearest available signal that the host QEMU is too old for
            # this guest. 22.04 / 24.04 keep booting on plain rv64.
            cpu_opts = "rva23s64"
        else:
            cpu_opts = "rv64"

        args_qemu.extend([
            "-machine", machine_opts,
            "-cpu", cpu_opts,
            "-device", "qemu-xhci",
            "-device", "{},netdev=net0".format(net_card),
        ])
        # virtio-balloon-pci is a PCI device NetBSD/riscv cannot drive (it would
        # enumerate "not configured"); skip it for NetBSD. Ubuntu keeps it.
        if config['os'] != "netbsd":
            args_qemu.extend(["-device", "virtio-balloon-pci"])

        # Prefer EDK2 UEFI firmware (RISCV_VIRT_CODE.fd) located the same way
        # as the other arches (next to the QEMU binary first, so ~/qemu-local
        # works without root, then system paths). Fall back to the legacy
        # U-Boot -kernel payload when no UEFI firmware is available.
        fw_dirs = []
        if qemu_bin:
            try:
                _qpref = os.path.dirname(os.path.dirname(os.path.realpath(qemu_bin)))
                fw_dirs.append(os.path.join(_qpref, "share"))
            except Exception:
                pass
        fw_dirs += ["/usr/share", "/opt/homebrew/share", "/usr/local/share"]

        code_candidates = []
        if config['firmware']:
            code_candidates.append(config['firmware'])
        for d in fw_dirs:
            code_candidates.append(os.path.join(d, "edk2", "riscv", "RISCV_VIRT_CODE.fd"))
        code_src = ""
        for c in code_candidates:
            if os.path.exists(c):
                code_src = c
                break

        if code_src:
            # UEFI boot. The RISC-V virt flash bank is a fixed 32MB; pad the
            # firmware images into that size (same scheme as aarch64).
            efi_path = os.path.join(output_dir, vm_name + "-RISCV_VIRT_CODE.fd")
            vars_path = os.path.join(output_dir, vm_name + "-RISCV_VIRT_VARS.fd")
            if not os.path.exists(efi_path):
                create_sized_file(efi_path, 32)
                copy_content_to_file(code_src, efi_path)
            if config['snapshot'] and os.path.exists(vars_path):
                try: os.remove(vars_path)
                except OSError: pass
            if not os.path.exists(vars_path):
                create_sized_file(vars_path, 32)
                vars_src = config['firmware_vars']
                if not vars_src:
                    cand = os.path.join(os.path.dirname(code_src), "RISCV_VIRT_VARS.fd")
                    if os.path.exists(cand):
                        vars_src = cand
                if vars_src and os.path.exists(vars_src):
                    copy_content_to_file(vars_src, vars_path)
            debuglog(config['debug'], "RISC-V UEFI firmware: {}".format(code_src))
            args_qemu.extend([
                "-drive", "if=pflash,format=raw,readonly=on,file={}".format(efi_path),
                "-drive", "if=pflash,format=raw,file={},unit=1".format(vars_path)
            ])
        else:
            # Legacy fallback: U-Boot S-mode payload as the kernel.
            uboot_bin = "/usr/lib/u-boot/qemu-riscv64_smode/u-boot.bin"
            if not os.path.exists(uboot_bin):
                fatal("No RISC-V firmware found. Install edk2-riscv64 "
                      "(RISCV_VIRT_CODE.fd), pass --firmware <path>, or provide "
                      "U-Boot at {}.".format(uboot_bin))
            args_qemu.extend(["-kernel", uboot_bin])
    elif config['arch'] == "loongarch64":
        # QEMU loongarch virt machine (docs/system/loongarch/virt.rst):
        # -cpu la464 + the bundled EDK2 UEFI firmware loaded via -bios (the
        # documented boot path for this machine; NVRAM vars are not
        # persisted, the openEuler image boots via the EFI fallback path).
        # QEMU bundles edk2-loongarch64-code.fd only since 9.2;
        # ensure_pinned_qemu above swapped in the pinned 10.2.3 build when
        # the system QEMU is older, so the search below looks next to the
        # resolved QEMU binary first (the pinned tarball ships the firmware
        # in its share/qemu tree).
        machine_opts = "virt,accel=tcg"
        cpu_opts = config['cputype'] or "la464"
        args_qemu.extend([
            "-machine", machine_opts,
            "-cpu", cpu_opts,
            "-device", "{},netdev=net0".format(net_card),
        ])

        fw_dirs = []
        if qemu_bin:
            try:
                _qpref = os.path.dirname(os.path.dirname(os.path.realpath(qemu_bin)))
                fw_dirs.append(os.path.join(_qpref, "share"))
            except Exception:
                pass
        fw_dirs += ["/usr/share", "/opt/homebrew/share", "/usr/local/share"]

        code_candidates = []
        if config['firmware']:
            code_candidates.append(config['firmware'])
        for d in fw_dirs:
            code_candidates.append(os.path.join(d, "qemu", "edk2-loongarch64-code.fd"))
        code_src = ""
        for c in code_candidates:
            if os.path.exists(c):
                code_src = c
                break
        if not code_src:
            fatal("No LoongArch UEFI firmware (edk2-loongarch64-code.fd) "
                  "found. Use a QEMU >= 9.2 that bundles it, or pass "
                  "--firmware <path>.")
        debuglog(config['debug'], "LoongArch UEFI firmware: {}".format(code_src))
        args_qemu.extend(["-bios", code_src])
    elif config['arch'] == "s390x":
        # QEMU s390-ccw-virtio (IBM Z). The bundled s390-ccw.img firmware
        # reads the zipl boot map straight off the virtio disk, so no
        # external bootloader/EFI files are involved. Every device sits on
        # the CCW bus (virtio-*-ccw; net_card and the rng device below are
        # picked accordingly), the machine has no VGA and no USB, and the
        # guest console is the SCLP line console (ttysclp0), which QEMU
        # routes through -serial -- the auto-enabled VNC console mode
        # drives it like the other serial-only arches. KVM on real IBM Z
        # hosts, TCG everywhere else. CPU model default: 'host' under KVM;
        # under TCG the 'qemu' model boots the ubuntu artifacts (validated
        # by ubuntu-builder, which builds them the same way; the 'qemu'
        # model is TCG's, not meant for KVM). --cpu-type overrides both.
        # NOTE: distro QEMU 8.2 (ubuntu noble) TCG intermittently freezes
        # guest systemd at startup ("Failed to fork off sandboxing
        # environment ... Freezing execution.", roughly 1 in 4 boots) --
        # QEMU >= 10 does not; ensure_pinned_qemu() above swaps in a newer
        # one on Linux x86_64 hosts.
        if config['cputype']:
            scpu = config['cputype']
        elif accel == "kvm":
            scpu = "host"
        else:
            scpu = "qemu"
        args_qemu.extend([
            "-machine", "s390-ccw-virtio,accel={}".format(accel),
            "-cpu", scpu,
            "-device", "{},netdev=net0".format(net_card),
        ])
    elif config['arch'] == "sparc64":
        # QEMU sun4u (UltraSPARC IIi + OpenBIOS), TCG only. Console is on com0
        # serial -- the sun4u VGA only works in firmware -- so remove the VGA
        # with -vga none and OpenBIOS + the kernel fall back to ttya. The NIC
        # goes on the empty secondary Simba-bridge bus pciB (the primary bus is
        # full, so an auto-placed NIC fails). No virtio at all: no balloon, and
        # virtio-rng is skipped further down. wd0 (the IDE disk_if) boots via
        # OpenBIOS with -boot order=c.
        args_qemu.extend([
            "-machine", "sun4u",
            "-vga", "none",
            "-device", "{},netdev=net0,bus=pciB".format(net_card),
            "-boot", "order=c",
        ])
        if sparc64_bios_file:
            # openbsd: replace the bundled OpenBIOS with the patched blob
            # downloaded above (-bios overrides the machine firmware).
            args_qemu.extend(["-bios", sparc64_bios_file])
    elif config['arch'] in ("powerpc64", "powerpc64le", "ppc64", "ppc64le"):
        # QEMU pseries (sPAPR / PAPR) machine + bundled SLOF firmware
        # (auto-loaded from /usr/share/qemu/slof.bin -- no -bios, no pflash;
        # pseries uses OpenFirmware/SLOF, not UEFI). This is the FreeBSD /
        # Linux guest target on ppc64; powernv* is OPAL bare-metal and won't
        # boot a stock distro image. The published anyvm image is big-endian
        # FreeBSD/powerpc64 (ELFv1); -cpu power9 (PowerISA 3.0, POWER8+
        # baseline) drives it well under TCG and would equally serve a
        # little-endian guest since POWER8+ is bi-endian. Console is the
        # SPAPR virtual teletype (spapr-vty -> /dev/ttyu0 in FreeBSD) on the
        # -serial chardev, so no VGA device is added (console-only, like
        # riscv64). cap-cfpc/sbbc/ibs/ccf-assist are set to broken/off to
        # silence the harmless "TCG doesn't support requested feature"
        # warnings the default pseries-noble emits under TCG.
        if config['cputype']:
            cpu_opts = config['cputype']
        else:
            cpu_opts = "power9"
        machine_opts = ("pseries,accel={},usb=off,cap-cfpc=broken,"
                        "cap-sbbc=broken,cap-ibs=broken,"
                        "cap-ccf-assist=off").format(accel)
        args_qemu.extend([
            "-machine", machine_opts,
            "-cpu", cpu_opts,
            "-device", "{},netdev=net0".format(net_card),
        ])
    elif config['arch'] == "armv7":
        # RISC OS on a Raspberry Pi 2 -- a real board, not QEMU's `virt`. The
        # guest drives BCM2835 peripherals directly and knows nothing about
        # PCI, virtio or UEFI, so this branch adds almost nothing: no -cpu
        # (raspi2b fixes it at Cortex-A7), no accel option (the board models
        # are TCG-only), no VGA (video is the BCM2835 framebuffer through the
        # mailbox), no pflash. The ROM is supplied through -bios further down.
        #
        # Needs the patched QEMU riscos-builder publishes; stock QEMU cannot
        # boot RISC OS on any raspi machine. ensure_pinned_qemu() fetches it.
        #
        # The NIC is a USB device on the on-chip hub: a real Pi 2 carries an
        # SMSC LAN9512 and RISC OS has a driver for that and nothing else QEMU
        # offers, so without it the guest has no networking at all.
        args_qemu.extend([
            "-machine", "raspi2b",
            "-device", "{},netdev=net0".format(net_card),
        ])
        if riscos_rom_file:
            args_qemu.extend(["-bios", riscos_rom_file])
    else:
        # x86_64
        machine_opts = "pc,accel={},hpet=off,smm=off,graphics=on,vmport=off,usb=on".format(accel)
        if config['os'] == "hurd":
            # gnumach requires the HPET (hpet_init asserts hpet_addr != 0 and
            # panics under hpet=off). The amd64 build additionally needs the
            # q35 machine: on i440fx 'pc', rumpdisk's piix IDE DMA cannot
            # address 64-bit physical pages, so >= 3584 MB RAM fails root
            # mounting with "ext2fs: ... Input/output error" (bug-hurd
            # 2025-11 msg00017; -M q35 is the upstream-confirmed fix). i386
            # keeps pc (in-kernel gnumach IDE).
            hurd_mtype = "pc" if config['arch'] == "i386" else "q35"
            machine_opts = "{},accel={},smm=off,graphics=on,vmport=off,usb=on".format(hurd_mtype, accel)
        
        if config['cputype']:
            # Explicit --cpu-type wins (the x86_64 branch previously ignored it).
            cpu_opts = config['cputype']
        elif accel in ["kvm", "whpx", "hvf"]:
            if accel == "kvm":
                if config['os'] == 'dragonflybsd':
                    # DragonFlyBSD's early-boot init writes to MSRs that vary by
                    # runner CPU generation. -cpu host exposes too much; even
                    # pmu=off was not enough to stop intermittent #GP-in-wrmsr
                    # right after TSC calibration. Lock to a stable named model
                    # so guest CPUID is identical across all runner hardware.
                    # Note: named CPU models do NOT support the `migratable`
                    # or `host-cache-info` properties (max_x86_cpu_properties
                    # in QEMU target/i386/cpu.c -- max/host classes only).
                    # kvm/l3-cache/pmu ARE generic and would be accepted here;
                    # they are omitted simply because the defaults are fine.
                    cpu_opts = "Broadwell-v4,+hypervisor,+invtsc"
                    debuglog(config['debug'], "DragonFlyBSD: using Broadwell-v4 named CPU model (avoids -cpu host MSR variance)")
                else:
                    cpu_opts = "host,kvm=on,l3-cache=on,+hypervisor,migratable=no,+invtsc"
                    if host_nested_amd_with_avx512():
                        # Nested AMD-V (KVM inside WSL2 / Hyper-V) corrupts the
                        # guest's AVX512 XSAVE state, so any guest whose glibc
                        # uses AVX512 string/mem routines (Ubuntu 26.04+) randomly
                        # SIGSEGVs across nearly every binary. Dropping just
                        # avx512f (validated fix) makes glibc fall back to the AVX2
                        # paths; AVX2 and the rest of -cpu host stay, so the guest
                        # keeps near-native speed. Bare-metal hosts are unaffected
                        # (no 'hypervisor' flag). Override with --cpu-type.
                        cpu_opts += ",-avx512f"
                        log("Nested AMD KVM detected: dropping AVX512 from -cpu host "
                            "(works around guest SIGSEGV; pass --cpu-type to override)")
            else:
                cpu_opts = "host,+rdrand,+rdseed"
                if accel == "whpx":
                    if config['os'] in ("openeuler", "plan9"):
                        # openEuler AND 9front under WHPX + a modern named
                        # model hang on first boot on the GHA windows
                        # runners since 2026-07-23 (600s, zero guest
                        # network traffic) -- on BOTH host vendors:
                        # EPYC-Turin-v1 on AMD hosts and GraniteRapids-v2
                        # on Intel hosts (green on 07-22 with identical
                        # args/image/runner image; an Azure host/Hyper-V
                        # rollout is the only variable left). The
                        # boot-retry's conservative model boots the same
                        # images in ~20s, so go to Nehalem directly and
                        # skip the guaranteed 600s first-attempt burn.
                        # Nehalem is the smallest x86-64-v2 model (QEMU
                        # docs/system/cpu-models-x86-abi.csv) -- required
                        # by openEuler userspace, and fine for 9front
                        # (whose retry booted even on v1 qemu64).
                        # --cpu-type overrides.
                        cpu_opts = "Nehalem,+rdrand,+rdseed"
                        log("WHPX + {}: using Nehalem CPU model "
                            "(modern named models hang this guest's first "
                            "boot on GHA runners; pass --cpu-type to "
                            "override)".format(config['os']))
                    else:
                        # Use a vendor-matched named CPU model instead of
                        # -cpu host: the WHPX host-CPUID enumeration path can
                        # wedge QEMU, and the model's feature set does not
                        # matter because the guest CPUID comes from Hyper-V
                        # anyway (see WHPX_*_CPU_MODELS above). Pick the newest
                        # model this QEMU ships; --cpu-type still overrides.
                        # NESTED WHPX (the Windows host is itself a VM, e.g. a
                        # GitHub Actions runner on Azure): every feature-rich
                        # named model wedges QEMU before the guest runs -- the
                        # process lives, the serial log stays empty, the
                        # monitor answers nothing, and only a restart with a
                        # leaner model recovers it. Measured across all 43 jobs
                        # of anyvm run 30420415750, on three different runner
                        # hosts:
                        #
                        #   EPYC-Milan-v3 / Icelake-Server-v7 /
                        #   GraniteRapids-v2, -smp 4 ....... 33 of 33 wedged
                        #   Nehalem, -smp 4 ................  0 of  4 wedged
                        #   the same rich models at -smp 1 ..  0 of  3 wedged
                        #
                        # so it takes BOTH a rich model and more than one vCPU.
                        # Neither the model generation nor the vCPU count alone
                        # explains it, and a generation-matched model (Milan on
                        # a Milan host, Icelake on an Icelake host) wedges just
                        # the same. Twelve configurations on bare-metal WHPX
                        # reproduce none of it, so the trigger needs the nested
                        # setup; without a nested host to bisect on, take the
                        # arrangement the data says works.
                        #
                        # Nehalem is also strictly better than what these
                        # guests get today: the rich model wedges, the retry
                        # falls back to qemu64 (x86-64-v1), so every guest ends
                        # up on v1 anyway -- via a dead first launch. Nehalem
                        # is the smallest named model with the v2 tick, first
                        # try. --cpu-type still overrides.
                        #
                        # On BARE METAL the rich models are fine (verified on
                        # an ASUS ProArt: EPYC-Turin-v1 under WHPX starts and
                        # runs), so that path keeps the generation-aware pick.
                        vendor = windows_host_cpu_vendor()
                        nested, evidence = windows_host_is_virtual()
                        debuglog(config['debug'],
                                 "WHPX host is {}: {}".format(
                                     "VIRTUAL (nested)" if nested else "bare metal",
                                     evidence))
                        if nested:
                            cpu_opts = "Nehalem,+rdrand,+rdseed"
                            log("WHPX on a nested host ({}): using Nehalem "
                                "(richer named models wedge QEMU before the "
                                "guest starts here; pass --cpu-type to "
                                "override)".format(evidence))
                        else:
                            # ...and never a model NEWER than the host: that is
                            # its own WHPX wedge (whpx_named_model_candidates).
                            named_models, why = whpx_named_model_candidates(vendor)
                            avail = qemu_cpu_models(qemu_bin)
                            for named_model in named_models:
                                if named_model in avail:
                                    cpu_opts = named_model + ",+rdrand,+rdseed"
                                    log("WHPX on bare-metal {} host: using named "
                                        "CPU model {} [{}] (avoids -cpu host WHPX "
                                        "hang; pass --cpu-type to override)".format(
                                            vendor.upper(), named_model, why))
                                    break
        else:
            # TCG (pure software emulation): default to -cpu max, which exposes
            # every feature QEMU can emulate. The previous minimal qemu64 model
            # (~x86-64-v1) lacks SSSE3/SSE4.x/POPCNT, which the Android x86_64
            # userland (BlissOS) SIGILLs on; max is a strict superset, so any
            # guest that booted on qemu64 still boots, and modern userlands
            # that assume the x86-64-v2+ baseline now work too. Override with
            # --cpu-type for a leaner/faster named model (e.g. Nehalem/Haswell).
            #
            # ...minus avx2 on the broken QEMU range: TCG's AVX decoder sized
            # the 128-bit memory operand of VINSERTx128 as 256 bits from the
            # day AVX landed in TCG (7.2, commit 790684776861) until
            # "target/i386: fix width of third operand of VINSERTx128"
            # (feea87cd6b64, 2025-07; first mainline release with it is
            # v10.1.0, stable backports 7.2.20 / 10.0.4 -- the EOL 8.x and
            # 9.x series never get it). A legal 16-byte load whose operand
            # ends flush at a page boundary reads 16 bytes into the next
            # page and faults. FreeBSD 15.1 hits this deterministically on
            # every boot: the OpenZFS 2.4 checksum benchmark runs
            # zfs_sha256_transform_avx2 over a buffer whose end abuts an
            # unmapped page, and the guest panics ("Fatal trap 12" in zfs.ko
            # at uptime 2s) before sshd ever starts (anyvm issue #54, Google
            # Cloud Shell's distro QEMU 8.2.2; flag-bisected there:
            # -sha-ni/-vaes still panic, -avx2 boots clean; validated fixed
            # on ubuntu 26.04's QEMU 10.2.1, same image + -cpu max).
            # qemu_version() only exposes (major, minor), so the two
            # boundary branches stay masked even on their fixed micros
            # (7.2.20 / 10.0.4): masking avx2 under TCG costs nothing -- the
            # guest just picks its SSE/AVX code paths. An unparseable
            # version is treated as broken for the same reason. --cpu-type
            # overrides.
            cpu_opts = "max"
            qver = qemu_version(qemu_bin)
            if qver is None or (7, 2) <= qver <= (10, 0):
                cpu_opts = "max,-avx2"
                debuglog(config['debug'],
                         "TCG QEMU {} is in the broken VINSERTx128 range "
                         "(7.2-10.0): masking avx2 (pass --cpu-type to "
                         "override)".format(
                             "{}.{}".format(*qver) if qver else "of unknown version"))

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
        ])
        # ReactOS has no virtio-balloon driver, and an unclaimed PCI device
        # makes it raise a modal "New Hardware Wizard" over the desktop.
        # Mirrors build.py's balloon gating.
        if config['os'] != "reactos":
            args_qemu.extend(["-device", "virtio-balloon-pci"])
        args_qemu.extend(["-vga", vga_type])

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
            # Ordered list of directories to search for bundled firmware. The
            # directory derived from the QEMU binary comes first so a relocated,
            # no-root install (e.g. ~/qemu-local) is honored, followed by the
            # usual system locations.
            fw_dirs = []
            if qemu_bin:
                try:
                    _qpref = os.path.dirname(os.path.dirname(os.path.realpath(qemu_bin)))
                    fw_dirs.append(os.path.join(_qpref, "share"))
                except Exception:
                    pass
            if platform.system() == "Darwin":
                fw_dirs += ["/opt/homebrew/share", "/usr/local/share", "/usr/share"]
            elif not IS_WINDOWS:
                fw_dirs += ["/usr/share", "/usr/local/share"]

            # Relative CODE firmware names tried under each directory.
            fw_rel_names = [
                os.path.join("qemu", "edk2-x86_64-code.fd"),
                os.path.join("qemu", "OVMF.fd"),
                os.path.join("OVMF", "OVMF_CODE.fd"),
                os.path.join("ovmf", "OVMF_CODE.fd"),
                os.path.join("edk2", "ovmf", "OVMF_CODE.fd"),
                os.path.join("edk2", "x64", "OVMF_CODE.4m.fd"),
            ]

            efi_src = ""
            if config['firmware']:
                # Explicit override via --firmware <path>.
                efi_src = config['firmware']
                if not os.path.exists(efi_src):
                    fatal("Specified firmware not found: {}".format(efi_src))
            elif IS_WINDOWS:
                prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
                win_candidates = [
                    os.path.join(prog_files, "qemu", "share", "edk2-x86_64-code.fd"),
                    r"C:\msys64\ucrt64\share\qemu\edk2-x86_64-code.fd",
                ]
                for c in win_candidates:
                    if os.path.exists(c):
                        efi_src = c
                        break
                if not efi_src:
                    efi_src = win_candidates[0]  # Default fallback
            else:
                for d in fw_dirs:
                    for rn in fw_rel_names:
                        c = os.path.join(d, rn)
                        if os.path.exists(c):
                            efi_src = c
                            break
                    if efi_src:
                        break
                if not efi_src:
                    efi_src = "/usr/share/qemu/OVMF.fd"  # Default fallback
            debuglog(config['debug'], "UEFI firmware CODE: {}".format(efi_src))

            vars_path = os.path.join(output_dir, vm_name + "-OVMF_VARS.fd")
            if config['snapshot'] and os.path.exists(vars_path):
                try: os.remove(vars_path)
                except OSError: pass

            if not os.path.exists(vars_path):
                # Prefer copying the matching VARS template so its size/layout
                # pairs with the CODE firmware; only fall back to a blank store
                # when no template can be located.
                vars_template = config['firmware_vars']
                if vars_template and not os.path.exists(vars_template):
                    fatal("Specified firmware vars not found: {}".format(vars_template))
                if not vars_template:
                    base = os.path.basename(efi_src)
                    d = os.path.dirname(efi_src)
                    guesses = []
                    if "CODE" in base:
                        guesses.append(os.path.join(d, base.replace("CODE", "VARS")))
                    if "code" in base:
                        guesses.append(os.path.join(d, base.replace("code", "vars")))
                    guesses.append(os.path.join(d, "OVMF_VARS.fd"))
                    guesses.append(os.path.join(d, "edk2-i386-vars.fd"))
                    for g in guesses:
                        if g != efi_src and os.path.exists(g):
                            vars_template = g
                            break
                if vars_template:
                    debuglog(config['debug'], "UEFI firmware VARS template: {}".format(vars_template))
                    shutil.copy2(vars_template, vars_path)
                else:
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

        # Add audio support if the vnc driver is available (and not in console-only mode).
        # Skipped on sparc64: the sun4u machine has no slot for intel-hda/usb-audio.
        if not is_vnc_console and config['arch'] != "sparc64" and check_qemu_audio_backend(qemu_bin, "vnc"):
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

        # Use appropriate input devices for better VNC support. sparc64 (sun4u)
        # has no USB controller, so usb-tablet would fail to attach; skip it (the
        # console is serial anyway).
        if not is_vnc_console and config['arch'] != "sparc64":
            if config['arch'] == "aarch64":
                args_qemu.extend(["-device", "usb-kbd", "-device", "virtio-tablet-pci"])
            else:
                args_qemu.extend(["-device", "usb-tablet"])

        # Prepare info for VNC Web Proxy
        web_port = get_free_port(start=6080, end=6180)
        if web_port:
            display_arch = config['arch'] if config['arch'] else host_arch
            vm_info = "-".join(filter(None, [config['os'], config['release'], display_arch]))
            
    else:
        args_qemu.extend(["-display", "none"])

    # The QEMU monitor powers the stale-DHCP-lease hostfwd rewrite guard and the
    # boot-debug snapshot. Allocate it for ALL configs, not only when a VNC web
    # proxy is set up: headless arches (riscv64) and console-off runs use
    # -display none and previously had no monitor, so the guard could never
    # detect a mismatched guest IP or rebind hostfwd.
    if not config['qmon']:
        config['qmon'] = str(get_free_port(start=4444, end=4544))
        debuglog(config['debug'], "Auto-selected QEMU monitor port: {}".format(config['qmon']))

    if config['qmon']:
        args_qemu.extend(["-monitor", "tcp:127.0.0.1:{},server,nowait,nodelay".format(config['qmon'])])

    # Always provide RNG to guest. Use rng-builtin as a cross-platform source of entropy.
    # Skipped on sparc64: the sun4u machine has no free PCI slot for virtio-rng
    # (and NetBSD has no virtio bus there), so QEMU would abort at launch.
    # s390x has no PCI by default -- its rng is a CCW device.
    # ReactOS has no virtio-rng driver either, and an unclaimed PCI device
    # makes it raise a modal "New Hardware Wizard" over the desktop.
    # armv7 is raspi2b, which has no PCI bus at all -- a blunter version of
    # the sparc64 case: QEMU aborts at launch rather than merely confusing
    # the guest.
    if (config['os'] not in ("solaris", "reactos")
            and config['arch'] not in ("sparc64", "armv7")):
        rng_dev = "virtio-rng-ccw" if config['arch'] == "s390x" else "virtio-rng-pci"
        args_qemu.extend(["-object", "rng-builtin,id=rng0", "-device", "{},rng=rng0,max-bytes=1024,period=1000".format(rng_dev)])

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
            proxy_args = self_argv() + [
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
                open_vnc_page(web_port, config['debug'])
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
            # The retry window may be longer than the first-boot one: when the
            # KVM x86_64-on-x86_64 fast-fail above cut the first timeout to
            # 180s, boot_timeout_retry_sec preserves the original 600s so
            # attempt two is never starved. Unset means: same as attempt one.
            retry_boot_timeout_seconds = config.get('boot_timeout_retry_sec') or boot_timeout_seconds
            # The SSH handshake (KEX + pubkey auth) needs a few seconds of
            # headroom after TCP connect; 3s proved too tight, so use 5s as the
            # floor for every host/arch (Windows and aarch64 already used 5s).
            probe_timeout_sec = 5
            # TCG (pure software emulation) slows the guest 10-50x, so a single
            # SSH handshake (KEX + pubkey auth on the emulated CPU) routinely
            # takes longer than the few-second default. The TCP connect already
            # succeeded by this point (a closed port fails fast as "refused",
            # not a timeout), so a probe that hits the deadline is almost always
            # a slow-but-progressing handshake, not a stuck VM. The boot timeout
            # is already bumped for TCG above; give each probe a matching grace
            # so we don't SIGTERM a live handshake and make a ready VM look dead.
            if accel == "tcg":
                probe_timeout_sec = max(probe_timeout_sec, 15)
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

            whpx_died_early = False
            # A VM that never started at all, as opposed to one booting slowly.
            # WHPX can accept a named host CPU model on the command line and
            # then wedge QEMU before the guest executes a single instruction:
            # the process stays alive and its sockets listen, but the machine
            # never runs. Seen on every Windows CI leg with
            # -cpu EPYC-Turin-v1 under accel=whpx (anyvm run 30416930603):
            # serial.log 0 bytes, RSS 14 MB against -m 4096, 1 s of CPU time
            # after ten minutes, and all fifteen monitor queries answered
            # <no response>. The conservative-CPU retry below fixes it and
            # boots in 36 s -- but only after the full boot timeout plus the
            # guest-IP sweep plus the debug snapshot, ~12.5 min wasted on a VM
            # that was dead within a second of launch.
            #
            # Two signals, required together so a merely slow guest is never
            # mistaken for a dead one:
            #   * the serial log is still EMPTY -- any firmware, on any arch,
            #     prints something long before this deadline; and
            #   * the QEMU monitor does not answer `info version`. The monitor
            #     is served by QEMU's own main loop, so it replies as soon as
            #     the process is running, no matter how slow the GUEST is.
            # A guest can be arbitrarily slow and still fail neither test.
            vm_never_started = False
            dead_vm_checked = False
            while True:
                if proc.poll() is not None:
                    if accel == "whpx" and not config['whpx']:
                        # Auto-enabled WHPX can abort mid-boot when the guest
                        # executes something QEMU's WHPX instruction emulator
                        # cannot handle (GhostBSD's SSE store to an MMIO
                        # region: "failed to decode instruction f 10"). Every
                        # such guest worked under TCG -- that was the default
                        # before WHPX auto-enable -- so fall back to TCG via
                        # the retry path instead of failing. An explicit
                        # --whpx skips this and fails hard as before.
                        log("QEMU exited during boot (code {}) under auto-enabled WHPX; falling back to TCG...".format(proc.poll()))
                        whpx_died_early = True
                        break
                    fail_with_output("QEMU terminated during boot")

                elapsed = time.time() - boot_start_time
                if elapsed >= boot_timeout_seconds:
                    break

                if elapsed - last_boot_progress_log >= 15:
                    debuglog(config['debug'], "Boot wait: elapsed={:.1f}s/{}s, last probe={}".format(
                        elapsed, boot_timeout_seconds, last_probe_result))
                    last_boot_progress_log = elapsed

                # Dead-VM fast fail (see vm_never_started above). Checked once,
                # late enough that no real boot can still be silent on both
                # channels, early enough to save most of the timeout.
                if (not dead_vm_checked and config.get('qmon')
                        and elapsed >= DEAD_VM_CHECK_SECONDS
                        and boot_timeout_seconds > DEAD_VM_CHECK_SECONDS):
                    dead_vm_checked = True
                    serial_bytes = 0
                    try:
                        if serial_log_file and os.path.exists(serial_log_file):
                            serial_bytes = os.path.getsize(serial_log_file)
                    except OSError:
                        serial_bytes = -1
                    if serial_bytes == 0:
                        qmon_reply = _qmon_send(config['qmon'], "info version", timeout=3.0)
                        if not (qmon_reply or "").strip():
                            log("VM never started: {:.0f}s in, the serial log is empty AND the "
                                "QEMU monitor does not answer. QEMU is up but the machine is not "
                                "running -- not waiting out the remaining {:.0f}s.".format(
                                    elapsed, boot_timeout_seconds - elapsed))
                            vm_never_started = True
                            break
                        debuglog(config['debug'],
                                 "Dead-VM check: serial still empty but the monitor answers; "
                                 "the guest is merely slow, continuing to wait.")

                # Redox's bootloader stops on an interactive video-mode list
                # ("Arrow keys and enter select mode") and waits for a
                # keypress. It does NOT time out on its own: one run sat there
                # for the whole 180 s window while an otherwise identical run
                # got past it in ~30 s, so it is INTERMITTENT -- and a guest
                # stuck there looks exactly like a broken image from here, all
                # probes simply never answer.
                #
                # redox-builder's waitForLoginTag hook taps Enter for this
                # reason, which is why the builder's CI is reliably green;
                # anyvm had nothing equivalent, so the released image could
                # hang on a user's machine with nothing in either project
                # showing a fault. Enter accepts the highlighted (best) mode.
                # Redox runs on a normal PC with a PS/2 keyboard, so the
                # monitor's sendkey reaches it -- unlike RISC OS, where no
                # keypress can. Once the guest is up nothing reads the
                # console, so a late tap is harmless.
                if (config['os'] == "redox" and config.get('qmon')
                        and elapsed < 120):
                    _qmon_send(config['qmon'], "sendkey ret", timeout=2.0)

                if config.get('transport') == "telnet":
                    # plan9/9front: no sshd -- readiness is the guest's telnetd
                    # answering the marker probe through the hostfwd.
                    timed_out = False
                    if telnet_ready(config['sshport'], config['os']):
                        last_probe_result = "telnet ready"
                        success = True
                        break
                    last_probe_result = "telnet not ready"
                else:
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
                    # Prefer slirp's usernet table (outbound flows); fall back to
                    # the serial console log, which shows the lease ("bound to
                    # <ip>") even before any outbound TCP -- needed for headless
                    # arches where usernet is still empty during boot-wait.
                    actual_ip = get_vm_ip_from_monitor(config['qmon']) or get_vm_ip_from_serial(serial_log_file)
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
            
            if (not success and not whpx_died_early and not vm_never_started
                    and config.get('qmon') and hostfwd_specs):
                # Ask slirp for the guest's real address first: 'info usernet'
                # lists every guest-originated flow, so a hit means the guest
                # is up but behind a misrouted hostfwd -- rewrite the forwards
                # directly instead of brute-forcing the sweep. Zero flows
                # after a full boot timeout means the guest never brought
                # networking up at all (kernel panic / boot hang -- the
                # netbsd-vm#21 SMEP panic looks exactly like this), NOT an
                # IP/DHCP mismatch; say so explicitly for the log reader.
                swept_ip = None
                lease_ip = get_vm_ip_from_monitor(config['qmon'])
                if lease_ip:
                    log("slirp sees guest traffic from {}; rewriting hostfwd to it...".format(lease_ip))
                    if rewrite_hostfwd_target(config['qmon'], hostfwd_specs, lease_ip, debug=config['debug']):
                        ret, _swto = call_with_timeout(
                            ssh_base_cmd + ["exit"],
                            timeout_seconds=max(2, probe_timeout_sec),
                            stdout=DEVNULL,
                            stderr=DEVNULL
                        )
                        if ret == 0:
                            swept_ip = lease_ip
                else:
                    log("slirp reports no guest-originated traffic after {}s: the guest likely never brought networking up (kernel panic or boot hang), not a DHCP/IP mismatch.".format(boot_timeout_seconds))
                if not swept_ip:
                    # Fall back to the brute-force search for a booted VM that
                    # is behind a misrouted hostfwd (wrong guest IP). Cheaper
                    # than a full kill + 600s+ reboot if the VM is actually up.
                    log("Boot probe timed out; sweeping guest IPs {0}{1}-{0}254 before restart...".format(SLIRP_NETWORK_PREFIX, 10))
                    swept_ip = probe_guest_by_ip_sweep(
                        config['qmon'], hostfwd_specs, ssh_base_cmd,
                        probe_timeout=max(2, probe_timeout_sec), debug=config['debug'])
                if swept_ip:
                    success = True
                    log("VM reachable at {} after hostfwd rewrite; skipping QEMU restart.".format(swept_ip))

            if not success:
                # First timeout - dump diagnostics, then kill QEMU and retry once
                if vm_never_started:
                    # Snapshot WITHOUT the monitor queries: those would each sit
                    # out their own timeout against a monitor already known to
                    # answer nothing (~34 s to re-prove it), but the launch
                    # command line and the host identity cost nothing and are
                    # precisely what a launch that never ran can be compared on
                    # -- the CPU model turned out NOT to be the discriminator
                    # (a generation-matched model wedges too, and the newest one
                    # sometimes boots), so the next question is which other args
                    # differ, and that needs the cmd_list.
                    _dump_boot_debug_snapshot(config, "vm-never-started", serial_log_file,
                                              config.get('qmon'), output_dir, vm_name, proc,
                                              cmd_list=cmd_list, skip_monitor=True)
                    log("Killing the dead QEMU and retrying with a different CPU model...")
                elif not whpx_died_early:
                    log("Boot timed out after {} seconds. Killing QEMU and retrying...".format(boot_timeout_seconds))
                    _dump_boot_debug_snapshot(config, "first-timeout", serial_log_file, config.get('qmon'), output_dir, vm_name, proc, cmd_list=cmd_list)
                terminate_process(proc, "QEMU")
                if proxy_proc:
                    terminate_process(proxy_proc, "VNC Proxy")
                # Wait for old proxy to exit
                time.sleep(1.5)
                
                # Restart QEMU. For x86_64 guests under KVM, retry with the
                # feature-minimal qemu64 CPU model instead of -cpu host: on
                # a small subset of runner hosts a guest kernel can panic
                # deterministically under -cpu host (seen on NetBSD as a
                # NULL-jump caught by SMEP at init exec, guest sits in ddb
                # -- vmactions/netbsd-vm#21), so retrying with identical
                # args on the same host just burns another timeout. Such
                # panics track a host CPU feature that qemu64 does not pass
                # through. Only the model token is swapped; the extra -cpu
                # flags (kvm=on etc.) and every other arg stay identical --
                # EXCEPT migratable=..., which is a property that exists
                # only on "-cpu host": QEMU refuses to start with
                # "Property 'qemu64-x86_64-cpu.migratable' not found"
                # (seen live: netbsd-vm run 29134207816 on a Xeon Platinum
                # 8573C host -- the retry crashed instead of booting).
                # Scoped to qemu-system-x86_64 so an HVF/aarch64
                # "-cpu host" is never rewritten to an x86-only model.
                cmd_list_retry = list(cmd_list)
                if whpx_died_early:
                    # WHPX aborted mid-boot: retry the whole launch under TCG.
                    # Swap only the accel token and the CPU model; "max" is
                    # the established TCG default and a strict superset of
                    # every named model, and the trailing tokens (+rdrand,
                    # pmu=off, ...) are generic X86CPU properties that TCG
                    # accepts. TCG is 10-50x slower, so give the retry at
                    # least a 1800s window (an explicit larger
                    # --boot-timeout-sec still wins).
                    try:
                        mach_idx = cmd_list_retry.index("-machine")
                        cmd_list_retry[mach_idx + 1] = str(
                            cmd_list_retry[mach_idx + 1]).replace("accel=whpx", "accel=tcg")
                        cpu_idx = cmd_list_retry.index("-cpu")
                        cpu_val = str(cmd_list_retry[cpu_idx + 1])
                        cmd_list_retry[cpu_idx + 1] = ",".join(
                            ["max"] + cpu_val.split(",")[1:])
                        retry_boot_timeout_seconds = max(retry_boot_timeout_seconds, 1800)
                        log("Retrying under TCG: -cpu {} (boot timeout {}s)".format(
                            cmd_list_retry[cpu_idx + 1], retry_boot_timeout_seconds))
                    except (ValueError, IndexError):
                        pass
                elif cmd_list_retry and "qemu-system-x86_64" in str(cmd_list_retry[0]):
                    # The conservative fallback model. qemu64 is the
                    # historically validated choice, but it is baseline
                    # x86-64-v1 ONLY (QEMU docs/system/cpu-models-x86-abi.csv:
                    # qemu64-v1 has no v2 tick). openEuler userspace is built
                    # for the x86-64-v2 baseline: under qemu64 its glibc
                    # aborts init with "Fatal glibc error: CPU does not
                    # support x86-64-v2" and the kernel panic-reboots forever
                    # (seen on the Windows/WHPX CI leg). Nehalem-v1 is the
                    # smallest named model with the v2 tick in that same
                    # table; still model-based, so it keeps dodging the
                    # host-feature-passthrough panics this retry exists for.
                    conservative_cpu = "Nehalem" if config['os'] == "openeuler" else "qemu64"
                    try:
                        cpu_idx = cmd_list_retry.index("-cpu")
                        cpu_val = str(cmd_list_retry[cpu_idx + 1])
                        if cpu_val.startswith("host"):
                            retry_cpu = conservative_cpu + cpu_val[len("host"):]
                            # migratable and host-cache-info are the ONLY
                            # two properties registered on the max/host CPU
                            # classes (max_x86_cpu_properties[], QEMU
                            # target/i386/cpu.c, checked at v8.2.2); every
                            # other token anyvm uses (kvm, l3-cache, pmu,
                            # +feature flags) is generic X86CPU.
                            retry_cpu = ",".join(
                                tok for tok in retry_cpu.split(",")
                                if not tok.startswith("migratable=")
                                and not tok.startswith("host-cache-info="))
                            cmd_list_retry[cpu_idx + 1] = retry_cpu
                            log("Retrying with conservative CPU model: -cpu {}".format(cmd_list_retry[cpu_idx + 1]))
                        elif cpu_val.split(",")[0] in (WHPX_AMD_CPU_MODELS
                                                       + WHPX_INTEL_CPU_MODELS):
                            # The WHPX named model timed out too; fall back
                            # to the conservative model the same way (named
                            # models have no migratable/host-cache-info to
                            # strip, every other token is generic X86CPU).
                            cmd_list_retry[cpu_idx + 1] = ",".join(
                                [conservative_cpu] + cpu_val.split(",")[1:])
                            log("Retrying with conservative CPU model: -cpu {}".format(cmd_list_retry[cpu_idx + 1]))
                    except (ValueError, IndexError):
                        pass
                # Preserve the first attempt's serial log before the
                # restarted QEMU truncates it: losing that console output
                # made the openeuler Windows/WHPX CI failure undiagnosable
                # (only the retry's panic survived into the debug
                # artifact). The .attempt1.log suffix still matches the
                # CI debug-artifact *.log glob.
                try:
                    if serial_log_file and os.path.exists(serial_log_file):
                        shutil.copyfile(serial_log_file,
                                        serial_log_file + ".attempt1.log")
                except Exception:
                    pass
                try:
                    proc = subprocess.Popen(cmd_list_retry, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
                    proc.pid, retry_boot_timeout_seconds, probe_timeout_sec, config.get('qmon') or '<unset>', config['sshport']))

                while True:
                    if proc.poll() is not None:
                        fail_with_output("QEMU terminated during boot (retry)")

                    elapsed = time.time() - boot_start_time
                    if elapsed >= retry_boot_timeout_seconds:
                        break

                    if elapsed - last_boot_progress_log >= 15:
                        debuglog(config['debug'], "Boot wait (retry): elapsed={:.1f}s/{}s, last probe={}".format(
                            elapsed, retry_boot_timeout_seconds, last_probe_result))
                        last_boot_progress_log = elapsed

                    if config.get('transport') == "telnet":
                        # plan9/9front: no sshd -- same telnet readiness
                        # probe as the first boot attempt. Probing with ssh
                        # here can NEVER succeed (the ssh client talks SSH
                        # to the guest's telnetd, times out every cycle,
                        # and each connect spuriously logs glenda in), so
                        # before this branch existed a plan9 boot that fell
                        # into the retry path always burned the full retry
                        # timeout and failed even with the guest up
                        # (Windows CI run 30001634758).
                        timed_out = False
                        if telnet_ready(config['sshport'], config['os']):
                            last_probe_result = "telnet ready"
                            success = True
                            break
                        last_probe_result = "telnet not ready"
                    else:
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
                        actual_ip = get_vm_ip_from_monitor(config['qmon']) or get_vm_ip_from_serial(serial_log_file)
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

                # The retry succeeded: surface the FIRST attempt's console
                # tail right here in the log. The .attempt1.log copy only
                # ships in CI debug artifacts on FAILURE, so without this a
                # hang that the retry recovers from (openeuler/WHPX first
                # boot) leaves no trace of what the first boot actually did.
                attempt1_log = (serial_log_file + ".attempt1.log"
                                if serial_log_file else None)
                if attempt1_log and os.path.exists(attempt1_log):
                    try:
                        with open(attempt1_log, "rb") as f:
                            a1_data = f.read()[-4096:]
                        a1_text = a1_data.decode("utf-8", "replace")
                        a1_text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", a1_text)
                        a1_text = "".join(
                            ch for ch in a1_text
                            if ch in "\r\n\t" or ord(ch) >= 32)
                        a1_lines = [l for l in
                                    a1_text.replace("\r", "\n").split("\n")
                                    if l.strip()]
                        if a1_lines:
                            log("First boot attempt failed; its serial console tail was:")
                            for a1_line in a1_lines[-12:]:
                                log("  attempt1| " + a1_line)
                    except Exception:
                        pass
            
            qemu_elapsed = time.time() - qemu_start_time
            debuglog(config['debug'], "VM Ready! Boot took {:.2f} seconds. Connect with: ssh {}".format(qemu_elapsed, vm_name))
            
            # illumos DNS readiness + public resolver. Two issues this guards
            # against, both seen intermittently as E_COULDNT_RESOLVE_HOST (pkg)
            # and "name or service not known" (NTP):
            #   1) The SSH port opens several seconds before
            #      svc:/network/dns/client:default is online (observed ~7s gap).
            #      Any name lookup in that window fails. So wait for the service
            #      AND for a real lookup to succeed before continuing -- both
            #      sync_vm_time() below and the caller's package install need DNS.
            #   2) The DHCP-supplied resolver is slirp's built-in proxy (x.x.x.3),
            #      which drops empty AAAA (NODATA) replies for IPv4-only hosts and
            #      hangs getaddrinfo ~15s. So point resolv.conf at public DNS.
            # resolv.conf is re-asserted every iteration in case nwam rewrites it.
            if config['os'] in ('omnios', 'openindiana', 'solaris', 'tribblix'):
                debuglog(config['debug'], "[trace] waiting for dns/client and setting resolv.conf on {} ...".format(config['os']))
                dns_setup = (
                    'i=0; '
                    'while [ $i -lt 60 ]; do '
                    'st=$(svcs -H -o state svc:/network/dns/client:default 2>/dev/null); '
                    'echo "nameserver 1.1.1.1" > /etc/resolv.conf; '
                    'echo "nameserver 8.8.8.8" >> /etc/resolv.conf; '
                    'if [ "$st" = online ] && getent hosts pool.ntp.org >/dev/null 2>&1; then '
                    'echo "anyvm: dns ready after ${i}s"; break; '
                    'fi; '
                    'i=$((i+1)); sleep 1; '
                    'done\n'
                )
                p = subprocess.Popen(ssh_base_cmd + ["sh"], stdin=subprocess.PIPE)
                p.communicate(input=dns_setup.encode('utf-8'))
                p.wait()
                debuglog(config['debug'], "[trace] illumos dns setup rc={}".format(p.returncode))

            # Sync VM time with host if requested
            should_sync = config['synctime']
            if should_sync is None:
                # Default behavior: Only sync for DragonFlyBSD and Solaris family
                if config['os'] in ['dragonflybsd', 'solaris', 'omnios', 'openindiana']:
                    should_sync = True
                else:
                    should_sync = False
            
            if should_sync and config.get('transport') == "telnet":
                # plan9/9front: sync_vm_time drives the guest over ssh, which
                # doesn't exist here. Time sync on 9front would need a native
                # aux/timesync path; skip rather than fail.
                debuglog(config['debug'], "plan9: skipping --sync-time (no ssh transport).")
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
            if (config['vpaths'] and config['sync'] != 'no'
                    and config['os'] == 'blissos'
                    and config['sync'] not in ('scp', 'tar')):
                # Android/toybox has no rsync, sshfs, or NFS client. The
                # working backends are scp (a static scp from the dropbear
                # tree is baked into /system/bin as the legacy-protocol
                # receiver, builder release >= v2.0.1) and tar (toybox tar
                # reads/writes ustar fine -- the vmactions copyback already
                # relies on it). Skip other modes instead of failing one by
                # one.
                log("Warning: only --sync scp or tar works on BlissOS/Android guests; skipping {} sync.".format(config['sync']))
            elif config['vpaths'] and config['sync'] != 'no':
                sudo_cmd = []
                # sudo is only needed by the kernel-NFS path (sys-nfs).
                if config['sync'] == 'sys-nfs':
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
                        debuglog(config['debug'], "Processing -v argument: {}".format(vpath_str))
                        vhost, vguest = split_vpath(vpath_str)
                        vhost = os.path.abspath(vhost)
                        
                        excludes = []
                        # Caller-declared excludes first: a CI runner's share
                        # is usually mostly harness files the guest never
                        # needs, and on the slow agent guests shipping them
                        # is the difference between a sync that finishes and
                        # one that does not.
                        excludes.extend(config.get('sync_excludes') or [])
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
                            # Always the bundled user-space nfsd; the host
                            # kernel NFS server is used only on an explicit
                            # --sync sys-nfs. (For the v3-only BSD guests
                            # this needs the nfsd portmapper on port 111 --
                            # on Linux hosts that port usually belongs to
                            # the system rpcbind or needs root, so pass
                            # --sync sys-nfs there instead; sync_mynfs
                            # probes the port and warns.)
                            sync_mynfs(ssh_base_cmd, vhost, vguest, config['os'], output_dir, vm_name, proc.pid, config['debug'])
                        elif config['sync'] == 'sys-nfs':
                            sync_nfs(ssh_base_cmd, vhost, vguest, config['os'], sudo_cmd)
                        elif config['sync'] == 'rsync':
                            sync_rsync(ssh_base_cmd, vhost, vguest, config['os'], output_dir, vm_name, excludes=excludes)
                        elif config['sync'] == 'scp':
                            sync_scp(ssh_base_cmd, vhost, vguest, config['sshport'], hostid_file, vm_user, excludes=excludes)
                        elif config['sync'] == 'tar':
                            sync_tar(config, ssh_base_cmd, vhost, vguest, excludes=excludes)
                        elif config['sync'] == '9p':
                            p9_port = config.get('p9_host_port')
                            if p9_port:
                                sync_9p(p9_port, vhost, vguest, config['debug'],
                                        excludes=excludes)
                            else:
                                log("Warning: --sync 9p but no 9P host port was "
                                    "forwarded; skipping folder sync.")
                        else:
                            sync_sshfs(ssh_base_cmd, vhost, vguest, config['os'])

                    except ValueError:
                        log("Invalid format for -v. Use host_path:guest_path")

            if config['console']:
                 log("======================================")
                 log("")
                 if config.get('transport') == "telnet":
                     _tcmd = "telnet 127.0.0.1 " + str(config['sshport'])
                     if supports_ansi_color():
                         _tcmd = "\x1b[32m{}\x1b[0m".format(_tcmd)
                     log("Reconnect to the guest shell with:  " + _tcmd)
                 else:
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
                     if config['vnc_password']:
                         pwd_display = config['vnc_password']
                         if supports_ansi_color():
                             pwd_display = "\x1b[33m{}\x1b[0m".format(pwd_display)
                         log("VNC password: {}".format(pwd_display))
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
            # Tracks whether anything actually ran in the guest this session;
            # the tar-sync pull-back below is pointless otherwise.
            guest_cmd_ran = False
            # The status anyvm itself exits with. It mirrors the guest command
            # (or interactive shell), so `anyvm ... -- make test` fails a shell
            # script or a CI step exactly the way running the command locally
            # would. It stays 0 whenever nothing ran in the guest -- --detach,
            # a skipped final ssh, console mode -- because "no command" is not
            # a failed command.
            guest_rc = 0
            if not config['detach'] and config.get('transport') == "telnet":
                # plan9/9front: no ssh. A passthrough `-- cmd ...` runs over
                # telnet and prints the transcript. With no command, drop into
                # an interactive telnet shell when stdin is a TTY (the ssh-shell
                # analogue); in a non-TTY context (CI, piped) there is no shell
                # to attach, so just leave the VM running.
                stdin_is_tty = bool(hasattr(sys.stdin, 'isatty') and sys.stdin.isatty())
                if ssh_passthrough:
                    p9_cmd = " ".join(ssh_passthrough)
                    # Marker-based exec: waits until the guest command
                    # actually finishes (no fixed settle window to outrun)
                    # and carries its 0/1 status back where the guest shell
                    # can express one -- see _telnet_rc_lines for the per-OS
                    # dialects. riscos reports completion only.
                    ok, text, cmd_rc = telnet_exec_status(
                        config['sshport'], config['os'], p9_cmd,
                        timeout_sec=config.get('exec_timeout_sec', 7200),
                        debug=config['debug'])
                    guest_cmd_ran = True
                    sys.stdout.write(text)
                    if text and not text.endswith("\n"):
                        sys.stdout.write("\n")
                    if not ok:
                        log("Warning: telnet session to the guest closed early.")
                        # 255 is ssh's own "transport failed, the command may
                        # not have run" code, reused here so both transports
                        # signal that condition the same way.
                        guest_rc = 255
                    else:
                        guest_rc = cmd_rc
                elif stdin_is_tty:
                    interactive_telnet(config['sshport'])
                    guest_cmd_ran = True
                else:
                    debuglog(config['debug'], "plan9: no passthrough command and no TTY; leaving VM running (use the VNC console).")
            elif not config['detach']:
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
                    guest_rc = subprocess.call(ssh_cmd)
                    guest_cmd_ran = True
                    debuglog(config['debug'], "[trace] final-SSH returned rc={}".format(guest_rc))
            else:
                debuglog(config['debug'], "[trace] detach mode -- skipping final SSH")
            # tar and 9p are one-shot copies, not live mounts: pull each -v
            # tree back after the guest command/session so files created in
            # the VM reach the host (the vmactions copyback semantics).
            # 9p belongs here for exactly the same reason as tar -- its push
            # mounts the guest, copies, and unmounts again -- and leaving it
            # out meant `anyvm --os plan9 --sync 9p -v ... -- cmd` ran the
            # command and silently dropped whatever it produced, while the
            # identical tar invocation returned it.
            # Nothing to pull when no guest command ran, and in --detach
            # mode the VM keeps running for later commands, so the pull is
            # skipped there too (that is what --attach --pull-files is for).
            if (config['sync'] in ('tar', '9p') and config['vpaths']
                    and not config['detach'] and guest_cmd_ran):
                for vpath_str in config['vpaths']:
                    try:
                        vhost, vguest = split_vpath(vpath_str)
                    except ValueError:
                        continue
                    vhost = os.path.abspath(vhost)
                    if config['sync'] == '9p':
                        p9_port = config.get('p9_host_port')
                        if p9_port:
                            sync_9p_pull(p9_port, vhost, vguest,
                                         debug=config['debug'],
                                         excludes=config.get('sync_excludes'))
                        else:
                            log("Warning: no 9P forward for the copy-back; "
                                "files created in the guest stay there.")
                    else:
                        sync_tar_pull(config, ssh_base_cmd, vhost, vguest)
            # Avoid noisy banner when running as PID 1 inside a container or if QEMU already exited
            if os.getpid() != 1:
                if not config['detach']:
                    # Give a moment for QEMU to fully exit if it was powered off
                    time.sleep(1)
                if is_pid_alive_main(proc.pid):
                    log("======================================")
                    log("The VM is still running in background.")
                    if config.get('transport') == "telnet":
                        _tcmd = "telnet 127.0.0.1 " + str(config['sshport'])
                        if supports_ansi_color():
                            _tcmd = "\x1b[32m{}\x1b[0m".format(_tcmd)
                        log("Reconnect to the guest shell with:  " + _tcmd)
                    else:
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
                        if config['vnc_password']:
                            pwd_display = config['vnc_password']
                            if supports_ansi_color():
                                pwd_display = "\x1b[33m{}\x1b[0m".format(pwd_display)
                            log("VNC password: {}".format(pwd_display))
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
            return guest_rc
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

def main_installed():
    """Entry point for packaged installs (pipx/pip console scripts, Homebrew).

    Flips INSTALLED before main() runs so the defaults land in the per-user
    cache instead of beside the installed file. Running anyvm.py from a
    checkout goes through __main__ below and is unaffected.

    Returns main()'s status so the guest command's exit code survives: the
    console script packaging generates a `sys.exit(main_installed())` wrapper,
    so a value returned here becomes the process exit code.
    """
    global INSTALLED
    INSTALLED = True
    return main()

if __name__ == '__main__':
    # sys.exit() rather than a bare call: main() returns the guest command's
    # exit code, and a plain `main()` would discard it and always exit 0.
    # A None return (every path that runs no guest command) exits 0.
    sys.exit(main())

