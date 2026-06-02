"""唤醒词与 WakeUtteranceGate 配置解析（openWakeWord）。"""

from __future__ import annotations

from typing import Any, Dict, List


def get_wake_word_options(ww_cfg: dict) -> Dict[str, Any]:
    """
    解析 config.json 的 wake_word 段。

    Returns:
        keywords、门控参数、openWakeWord 参数等
    """
    keywords = [
        str(k).strip()
        for k in ww_cfg.get("keywords", ["hey jarvis"])
        if str(k).strip()
    ]
    return {
        "keywords": keywords,
        "sensitivity": float(ww_cfg.get("sensitivity", 0.5)),
        "require_pre_silence_ms": max(
            0, int(ww_cfg.get("require_pre_silence_ms", 350))
        ),
        "pre_silence_min_ratio": float(
            ww_cfg.get("pre_silence_min_ratio", 0.65)
        ),
        "pre_silence_level_ratio": float(
            ww_cfg.get("pre_silence_level_ratio", 0.32)
        ),
        "pre_silence_noise_floor": float(
            ww_cfg.get("pre_silence_noise_floor", 0.0035)
        ),
        "speech_energy_ratio": float(
            ww_cfg.get("speech_energy_ratio", 0.38)
        ),
        "gate_use_silero": bool(ww_cfg.get("gate_use_silero", False)),
        "gate_silero_fraction": float(
            ww_cfg.get("gate_silero_fraction", 0.28)
        ),
        "max_wake_utterance_ms": max(
            400, int(ww_cfg.get("max_wake_utterance_ms", 1400))
        ),
        "oww_models": [
            str(m).strip()
            for m in (ww_cfg.get("oww_models") or [])
            if m and str(m).strip()
        ],
        "oww_inference_framework": str(
            ww_cfg.get("oww_inference_framework", "onnx")
        ).strip().lower()
        or "onnx",
        "oww_vad_threshold": float(ww_cfg.get("oww_vad_threshold", 0.0)),
        "oww_debounce_sec": float(ww_cfg.get("oww_debounce_sec", 0.8)),
        "pause_until_listen": bool(ww_cfg.get("pause_until_listen", False)),
        "wake_repeat_cooldown_ms": max(
            0, int(ww_cfg.get("wake_repeat_cooldown_ms", 1500))
        ),
    }
