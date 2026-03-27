#!/usr/bin/env python3
"""
环境依赖检测 / Environment dependency checker.

Standalone:  python check_env.py
Import:      from check_env import run_checks
             results = run_checks()   # list of {"name","status","detail"}

status values: "ok" | "warn" | "error" | "info"
"""

import json
import os
import pathlib
import struct
import sys
from typing import List, Dict


def run_checks(config_path: str = "config.json") -> List[Dict]:
    results: List[Dict] = []

    def add(name: str, status: str, detail: str = ""):
        results.append({"name": name, "status": status, "detail": detail})

    # ── 1. Python interpreter ─────────────────────────────────────────────────
    v = sys.version_info
    bits = struct.calcsize("P") * 8
    if v.major == 3 and 8 <= v.minor <= 12 and bits == 64:
        add("Python", "ok", f"{v.major}.{v.minor}.{v.micro}（{bits}-bit）")
    elif bits != 64:
        add("Python", "error", f"{v.major}.{v.minor} 是 32-bit；需要 64-bit Python")
    else:
        add("Python", "error",
            f"{v.major}.{v.minor} 不支持；需要 3.8–3.12")

    # ── 2. Core runtime packages ──────────────────────────────────────────────
    for pkg in ("websockets", "sounddevice", "numpy"):
        try:
            mod = __import__(pkg)
            add(pkg, "ok", getattr(mod, "__version__", "已安装"))
        except ImportError as exc:
            add(pkg, "error", str(exc))

    # ── 3. Whisper backend ────────────────────────────────────────────────────
    try:
        import faster_whisper as fw
        add("faster-whisper", "ok", getattr(fw, "__version__", "已安装"))
    except ImportError as exc:
        add("faster-whisper", "error", str(exc))

    try:
        import ctranslate2 as ct2
        ver = getattr(ct2, "__version__", "?")
        if hasattr(ct2, "StorageView"):
            add("ctranslate2", "ok", ver)
        else:
            add("ctranslate2", "warn",
                f"{ver} — C 扩展加载失败（DLL 缺失？），"
                "运行 pip install --force-reinstall ctranslate2>=4.0.0")
    except ImportError as exc:
        add("ctranslate2", "error", str(exc))

    # ── 4. Optional backends ──────────────────────────────────────────────────
    try:
        import vosk
        add("vosk", "ok", getattr(vosk, "__version__", "已安装"))
    except ImportError:
        add("vosk", "info", "未安装（仅限 Whisper 模式时可忽略）")

    try:
        import openwakeword as oww
        add("openwakeword", "ok", getattr(oww, "__version__", "已安装"))
    except ImportError:
        add("openwakeword", "info", "未安装（openwakeword 唤醒词不可用）")

    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        gpu = [p for p in providers if "CPU" not in p]
        detail = ort.__version__
        if gpu:
            detail += f"  GPU: {', '.join(gpu)}"
        add("onnxruntime", "ok", detail)
    except ImportError as exc:
        add("onnxruntime", "warn", str(exc))

    # ── 5. Microphone ─────────────────────────────────────────────────────────
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        mics = [d["name"] for d in devices if d.get("max_input_channels", 0) > 0]
        if mics:
            names = "、".join(mics[:2]) + ("…" if len(mics) > 2 else "")
            add("麦克风", "ok", f"{len(mics)} 个设备：{names}")
        else:
            add("麦克风", "warn", "未检测到录音设备，请连接麦克风后重试")
    except Exception as exc:
        add("麦克风", "error",
            f"sounddevice 错误：{exc}  "
            "（可能缺少 VC++ 2022 运行库）")

    # ── 6. VC++ Runtime (Windows only) ───────────────────────────────────────
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.CDLL("vcruntime140.dll")
            add("VC++ Runtime", "ok", "vcruntime140.dll 可用")
        except OSError:
            add("VC++ Runtime", "warn",
                "vcruntime140.dll 未找到 — sounddevice 可能无法启动，"
                "请安装 VC++ 2015-2022 x64 运行库")

    # ── 7. Model files ────────────────────────────────────────────────────────
    cfg_path = pathlib.Path(config_path)
    whisper_model = "base"   # fallback

    if not cfg_path.exists():
        add("配置文件", "error", f"未找到 {config_path}")
    else:
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))

            # Vosk model
            vosk_path = pathlib.Path(cfg.get("asr", {}).get("model_path", ""))
            if vosk_path.exists():
                size_mb = sum(
                    f.stat().st_size for f in vosk_path.rglob("*") if f.is_file()
                ) / 1e6
                add("Vosk 模型", "ok", f"{vosk_path.name}（{size_mb:.0f} MB）")
            else:
                add("Vosk 模型", "warn",
                    f"未找到：{vosk_path} — vosk 唤醒词不可用")

            # Whisper model (check local HF hub cache)
            whisper_model = cfg.get("whisper", {}).get("model", "base")
            hf_home = pathlib.Path(
                os.environ.get("HF_HOME",
                    pathlib.Path.home() / ".cache" / "huggingface")
            )
            hub_dir   = hf_home / "hub"
            model_key = f"models--Systran--faster-whisper-{whisper_model}"
            model_dir = hub_dir / model_key

            if model_dir.exists():
                snaps = list((model_dir / "snapshots").iterdir()) \
                        if (model_dir / "snapshots").exists() else []
                if snaps:
                    size_mb = sum(
                        f.stat().st_size
                        for snap in snaps
                        for f in pathlib.Path(snap).rglob("*") if f.is_file()
                    ) / 1e6
                    add(f"Whisper 模型（{whisper_model}）", "ok",
                        f"已缓存，{size_mb:.0f} MB")
                else:
                    add(f"Whisper 模型（{whisper_model}）", "warn",
                        "目录存在但没有快照，可能下载不完整")
            else:
                add(f"Whisper 模型（{whisper_model}）", "info",
                    "未缓存 — 首次启动服务时会自动下载")

        except Exception as exc:
            add("配置文件", "error", f"读取失败：{exc}")

    # ── 8. GPU acceleration check ─────────────────────────────────────────────
    _gpu_check(add)

    return results


def _gpu_check(add):
    """Detect GPU hardware and test CUDA / DirectML availability for Whisper."""

    # ── Hardware info (Windows WMI) ────────────────────────────────────────
    gpu_names: list = []
    nvidia_driver_ver: str = ""
    if sys.platform == "win32":
        try:
            import subprocess
            out = subprocess.check_output(
                ["wmic", "path", "win32_VideoController",
                 "get", "Name,DriverVersion", "/format:csv"],
                text=True, stderr=subprocess.DEVNULL, timeout=5
            )
            for line in out.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3 and parts[1]:
                    ver, name = parts[1], parts[2]
                    gpu_names.append(name)
                    if "nvidia" in name.lower() or "geforce" in name.lower() \
                            or "quadro" in name.lower() or "tesla" in name.lower():
                        nvidia_driver_ver = ver
        except Exception:
            pass

    if gpu_names:
        add("显卡", "ok", "、".join(gpu_names[:2]) + ("…" if len(gpu_names) > 2 else ""))
    else:
        add("显卡", "info", "未检测到独立显卡（将使用 CPU 推理）")

    # ── NVIDIA driver version check ────────────────────────────────────────
    if nvidia_driver_ver:
        # Windows driver version format: "31.0.15.5154" → CUDA support version
        # Driver 527.41+ supports CUDA 12; 516.94+ supports CUDA 11.8
        try:
            parts = [int(x) for x in nvidia_driver_ver.split(".")]
            # On Windows the relevant part is parts[2]*100+parts[3] mapped to cuda ver
            # Simpler: check the last two segments as a combined number
            drv_int = parts[2] * 10000 + parts[3] if len(parts) >= 4 else 0
            if drv_int >= 155154:   # ≈ 527.41 WDDM
                add("NVIDIA 驱动", "ok",
                    f"{nvidia_driver_ver} — 支持 CUDA 12 (Whisper CUDA 模式可用)")
            elif drv_int >= 151694:  # ≈ 516.94 WDDM
                add("NVIDIA 驱动", "warn",
                    f"{nvidia_driver_ver} — 仅支持 CUDA 11，建议升级驱动至 527+")
            else:
                add("NVIDIA 驱动", "warn",
                    f"{nvidia_driver_ver} — 版本较旧，建议升级驱动")
        except Exception:
            add("NVIDIA 驱动", "info", nvidia_driver_ver)

    # ── CUDA via ctranslate2 ───────────────────────────────────────────────
    try:
        import ctranslate2 as ct2
        if hasattr(ct2, "get_cuda_device_count"):
            n = ct2.get_cuda_device_count()
            if n > 0:
                add("CUDA 加速", "ok",
                    f"{n} 个设备可用 — config.json 设 whisper.device=cuda / compute_type=float16")
            else:
                add("CUDA 加速", "info",
                    "不可用（无 CUDA 设备或驱动不支持 CUDA 12）")
        else:
            add("CUDA 加速", "info", "ctranslate2 未能检测 CUDA 设备数")
    except Exception as exc:
        add("CUDA 加速", "warn", f"检测失败：{exc}")

    # ── DirectML via onnxruntime ───────────────────────────────────────────
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        if "DmlExecutionProvider" in providers:
            add("DirectML 加速", "ok",
                "可用 — openwakeword 等 ONNX 模型将自动使用 GPU")
        else:
            if any("GPU" in p or "Dml" in p or "CUDA" in p for p in providers):
                add("DirectML 加速", "ok", f"GPU provider: {providers}")
            else:
                add("DirectML 加速", "info",
                    "不可用（onnxruntime-directml 未安装，或 DirectX 12 不支持）")
    except Exception:
        pass


# ── Standalone CLI ─────────────────────────────────────────────────────────────

def _enable_ansi():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


def main():
    _enable_ansi()
    R = "\033[0m"
    G = "\033[32m";  Y = "\033[33m"
    RE = "\033[31m"; C = "\033[36m"
    DIM = "\033[90m"; B = "\033[1m"

    ICONS   = {"ok": "✓", "warn": "⚠", "error": "✗", "info": "·"}
    COLORS  = {"ok": G,   "warn": Y,   "error": RE,  "info": DIM}

    border = "=" * 60
    print(f"\n{C}{B}{border}{R}")
    print(f"{C}{B}  语音识别服务 — 环境检测{R}")
    print(f"{C}{B}{border}{R}\n")

    results = run_checks()
    errors  = sum(1 for r in results if r["status"] == "error")
    warns   = sum(1 for r in results if r["status"] == "warn")

    for r in results:
        s = r["status"]
        c = COLORS.get(s, R)
        i = ICONS.get(s, "?")
        detail = f"  {DIM}{r['detail']}{R}" if r["detail"] else ""
        print(f"  {c}{B}{i}{R}  {r['name']}{detail}")

    print(f"\n{B}{border}{R}")
    if errors == 0 and warns == 0:
        print(f"{G}{B}  所有检测通过！服务可以正常启动。{R}")
    elif errors == 0:
        print(f"{Y}{B}  {warns} 个警告，部分功能可能受限，但基本功能可用。{R}")
    else:
        print(f"{RE}{B}  发现 {errors} 个错误，请参照上方提示修复。{R}")
    print(f"{B}{border}{R}\n")
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
