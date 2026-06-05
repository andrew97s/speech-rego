"""唤醒词与 WakeUtteranceGate 配置解析（Sherpa KWS）。

VAD 相关字段说明见 docs/VAD.md §②（Gate）。
"""

from __future__ import annotations

from typing import Any, Dict, List


_DEFAULT_SHERPA_KWS = {
    "model_dir": "models/sherpa-kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20",
    "chunk_size": 8,
    "use_int8": True,
    "epoch_tag": "epoch-13-avg-2",
    "provider": "cpu",
    "num_threads": 2,
    "keywords_file": "",
    "keywords_threshold": None,
    "keywords_score": 1.0,
    "num_trailing_blanks": 1,
    "max_active_paths": 4,
    "tokens_type": "phone+ppinyin",
    "lexicon": "en.phone",
    "debounce_sec": 0.8,
}


def get_wake_word_options(ww_cfg: dict) -> Dict[str, Any]:
    """
    解析 config.json 的 wake_word 段。

    Returns:
        keywords、门控参数、Sherpa KWS 参数等
    """
    keywords = [
        str(k).strip()
        for k in ww_cfg.get("keywords", ["小智"])
        if str(k).strip()
    ]

    raw_sherpa = ww_cfg.get("sherpa_kws") or {}
    if not isinstance(raw_sherpa, dict):
        raw_sherpa = {}
    sherpa_kws = {**_DEFAULT_SHERPA_KWS, **raw_sherpa}

    debounce = float(
        sherpa_kws.get("debounce_sec", ww_cfg.get("sherpa_debounce_sec", 0.8))
    )

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
        "sherpa_kws": sherpa_kws,
        "sherpa_debounce_sec": debounce,
        "pause_until_listen": bool(ww_cfg.get("pause_until_listen", False)),
        "wake_repeat_cooldown_ms": max(
            0, int(ww_cfg.get("wake_repeat_cooldown_ms", 1500))
        ),
    }
