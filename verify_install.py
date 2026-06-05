#!/usr/bin/env python3
"""
Installation verification tool.
Run after install.bat to confirm everything is set up correctly.

Usage:
    .venv\Scripts\python.exe verify_install.py
"""

import json
import os
import pathlib
import platform
import struct
import sys

RESET  = "\033[0m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"

def ok(msg):   print(f"  {GREEN}[OK]{RESET}  {msg}")
def warn(msg): print(f"  {YELLOW}[!!]{RESET}  {msg}")
def fail(msg): print(f"  {RED}[XX]{RESET}  {msg}")
def info(msg): print(f"       {msg}")

# ── Enable ANSI on Windows ────────────────────────────────────────────────────
if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.SetConsoleMode(
        ctypes.windll.kernel32.GetStdHandle(-11), 7
    )

# ── Banner ────────────────────────────────────────────────────────────────────
border = "=" * 58
print(f"\n{CYAN}{BOLD}{border}{RESET}")
print(f"{CYAN}{BOLD}  语音识别服务 — 安装验证 / Install Verification{RESET}")
print(f"{CYAN}{BOLD}{border}{RESET}\n")

errors   = []
warnings = []

# ── 1. Python environment ─────────────────────────────────────────────────────
print(f"{BOLD}[1] Python environment{RESET}")

ver = sys.version_info
if ver.major == 3 and 8 <= ver.minor <= 12:
    ok(f"Python {ver.major}.{ver.minor}.{ver.micro}")
else:
    fail(f"Python {ver.major}.{ver.minor} — expected 3.8–3.12")
    errors.append("unsupported Python version")

bits = struct.calcsize("P") * 8
if bits == 64:
    ok("64-bit interpreter")
else:
    fail(f"32-bit interpreter — requires 64-bit Python")
    errors.append("32-bit Python")

ok(f"Platform: {platform.platform()}")

# ── 2. Core packages ──────────────────────────────────────────────────────────
print(f"\n{BOLD}[2] Core packages{RESET}")

def check_import(name, min_version=None):
    try:
        mod = __import__(name)
        ver_str = getattr(mod, "__version__", "?")
        if min_version and ver_str != "?":
            from packaging.version import Version
            try:
                if Version(ver_str) < Version(min_version):
                    warn(f"{name} {ver_str} (expected >= {min_version})")
                    warnings.append(f"{name} version too old")
                    return
            except Exception:
                pass
        ok(f"{name} {ver_str}")
    except ImportError as e:
        fail(f"{name} — NOT installed: {e}")
        errors.append(f"missing {name}")

check_import("websockets", "12.0")
check_import("sounddevice", "0.4.6")
check_import("numpy", "1.24.0")
check_import("faster_whisper", "1.0.0")
check_import("ctranslate2", "4.0.0")
check_import("sherpa_onnx")
check_import("sentencepiece")
check_import("silero_vad")

# ── 3. onnxruntime / GPU providers ───────────────────────────────────────────
print(f"\n{BOLD}[3] onnxruntime / GPU acceleration{RESET}")

try:
    import onnxruntime as ort
    providers = ort.get_available_providers()
    gpu = [p for p in providers if p != "CPUExecutionProvider"]
    ok(f"onnxruntime {ort.__version__}")
    if gpu:
        ok(f"GPU providers available: {', '.join(gpu)}")
    else:
        info("No GPU providers — CPU only (this is fine for most use cases)")
except ImportError as e:
    fail(f"onnxruntime not found: {e}")
    errors.append("missing onnxruntime")

# ── 4. Audio device check ─────────────────────────────────────────────────────
print(f"\n{BOLD}[4] Audio devices (microphones){RESET}")

try:
    import sounddevice as sd
    devices = sd.query_devices()
    mics = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    if mics:
        ok(f"{len(mics)} input device(s) found:")
        for idx, d in mics[:5]:
            marker = " ← default" if idx == sd.default.device[0] else ""
            info(f"  [{idx:>2}] {d['name']}{marker}")
        if len(mics) > 5:
            info(f"  ... and {len(mics)-5} more. Run list_devices.py for full list.")
    else:
        warn("No microphone detected — connect a mic and restart.")
        warnings.append("no microphone")
except Exception as e:
    fail(f"sounddevice error: {e}")
    errors.append("sounddevice failed")
    info("Possible cause: Visual C++ 2022 Redistributable not installed.")
    info("Download: https://aka.ms/vs/17/release/vc_redist.x64.exe")

# ── 5. ASR model ──────────────────────────────────────────────────────────────
print(f"\n{BOLD}[5] ASR model{RESET}")

cfg_path = pathlib.Path("config.json")
if not cfg_path.exists():
    fail("config.json not found")
    errors.append("missing config.json")
else:
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        whisper_model = cfg.get("whisper", {}).get("model", "base")
        ok(f"Whisper model configured: {whisper_model}")
        hf_home = pathlib.Path(
            os.environ.get("HF_HOME", pathlib.Path.home() / ".cache" / "huggingface")
        )
        model_dir = hf_home / "hub" / f"models--Systran--faster-whisper-{whisper_model}"
        if model_dir.exists():
            ok(f"Whisper cache found under {model_dir.parent.name}/…")
        else:
            info("Whisper weights not cached yet — first start() may download them.")
    except Exception as e:
        fail(f"Error reading config.json: {e}")
        errors.append("config read error")

# ── 6. Engine imports ─────────────────────────────────────────────────────────
print(f"\n{BOLD}[6] Engine module imports{RESET}")

try:
    from engine import SpeechEngine, EngineState  # noqa: F401
    from wake_detectors import SherpaKWSWakeWordDetector  # noqa: F401
    ok("engine + wake_detectors import OK")
except Exception as e:
    fail(f"Engine import failed: {e}")
    errors.append("engine import error")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{BOLD}{border}{RESET}")
if not errors:
    print(f"{GREEN}{BOLD}  ALL CHECKS PASSED — Installation is complete and ready!{RESET}")
    print(f"{GREEN}{BOLD}  Double-click start.bat to launch the service.{RESET}")
elif not errors and warnings:
    print(f"{YELLOW}{BOLD}  Warnings found ({len(warnings)}) — review above.{RESET}")
else:
    print(f"{RED}{BOLD}  {len(errors)} error(s) detected — please fix before running.{RESET}")
    for e in errors:
        print(f"  {RED}• {e}{RESET}")
print(f"{BOLD}{border}{RESET}\n")

sys.exit(0 if not errors else 1)
