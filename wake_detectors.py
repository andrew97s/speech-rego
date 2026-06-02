"""openWakeWord 唤醒检测器。"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_OWW_FRAME_SAMPLES = 1280
_OWW_VAD_FRAME_SAMPLES = 640


def _normalize_oww_model_name(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def _oww_score_threshold(sensitivity: float) -> float:
    s = max(0.0, min(1.0, float(sensitivity)))
    return max(0.15, min(0.85, 0.65 - s * 0.35))


class OpenWakeWordWakeWordDetector:
    """
    openWakeWord 分数型唤醒（ONNX）。

    将 PCM 缓冲到 1280 样本(80ms)再推理；命中后 debounce_sec 内不再触发。
    oww_vad_threshold 建议为 0（ASR 判停用 speech_vad.SileroVADSession）。
    """

    def __init__(
        self,
        keywords: List[str],
        sample_rate: int = _SAMPLE_RATE,
        *,
        oww_models: Optional[List[str]] = None,
        sensitivity: float = 0.5,
        inference_framework: str = "onnx",
        vad_threshold: float = 0.0,
        debounce_sec: float = 0.8,
    ):
        if sample_rate != _SAMPLE_RATE:
            raise ValueError(
                f"openWakeWord requires {_SAMPLE_RATE} Hz, got {sample_rate}"
            )
        from openwakeword.model import Model

        self.keywords = [k.strip() for k in keywords if k.strip()]
        if not self.keywords and not oww_models:
            raise ValueError("openWakeWord wake word requires keywords or oww_models")

        if oww_models:
            models = [str(m).strip() for m in oww_models if str(m).strip()]
        else:
            models = [_normalize_oww_model_name(k) for k in self.keywords]

        self._score_threshold = _oww_score_threshold(sensitivity)
        self._debounce_sec = max(0.0, float(debounce_sec))
        self._last_wake = 0.0
        self._last_score = 0.0
        self._paused = False
        self._pcm_buf = bytearray()
        self._model_to_keyword: Dict[str, str] = {}

        fw = (inference_framework or "onnx").strip().lower()
        vad = max(0.0, float(vad_threshold))
        if vad > 0:
            logger.warning(
                "[WakeWord] oww_vad_threshold=%.2f enables openWakeWord internal "
                "Silero VAD, which errors on non-640-sample tails; use 0.",
                vad,
            )
        self._model = Model(
            wakeword_models=models,
            inference_framework=fw,
            vad_threshold=vad,
        )
        loaded = list(self._model.models.keys())
        for i, mdl in enumerate(loaded):
            kw = self.keywords[i] if i < len(self.keywords) else mdl
            self._model_to_keyword[mdl] = kw
        for mdl, mapping in getattr(self._model, "class_mapping", {}).items():
            for cls in mapping.values():
                if cls not in self._model_to_keyword:
                    parent = mdl
                    idx = loaded.index(parent) if parent in loaded else -1
                    self._model_to_keyword[cls] = (
                        self.keywords[idx] if 0 <= idx < len(self.keywords) else cls
                    )

        logger.info(
            "[WakeWord] openWakeWord -- models=%s keywords=%s "
            "score_threshold=%.2f vad_threshold=%.2f framework=%s",
            models,
            self.keywords,
            self._score_threshold,
            vad,
            fw,
        )

    @property
    def last_score(self) -> float:
        return self._last_score

    def reset(self):
        self._model.reset()
        self._last_score = 0.0
        self._pcm_buf.clear()

    def pause(self):
        self._paused = True
        self.reset()

    def resume(self):
        self._paused = False
        self.reset()

    def _score_predictions(self, predictions: dict) -> Tuple[Optional[str], float]:
        best_kw: Optional[str] = None
        best_score = 0.0
        for model_key, raw_score in predictions.items():
            score = float(raw_score)
            if score < self._score_threshold:
                continue
            kw = self._model_to_keyword.get(model_key, model_key)
            if score > best_score:
                best_score = score
                best_kw = kw
        return best_kw, best_score

    def process(self, audio_bytes: bytes) -> Optional[str]:
        if self._paused:
            return None
        now = time.time()
        if self._debounce_sec > 0 and now - self._last_wake < self._debounce_sec:
            return None

        if isinstance(audio_bytes, np.ndarray):
            chunk = (audio_bytes * 32768.0).astype(np.int16)
        else:
            chunk = np.frombuffer(audio_bytes, dtype=np.int16)
        if chunk.size == 0:
            return None

        self._pcm_buf.extend(chunk.tobytes())
        max_bytes = _OWW_FRAME_SAMPLES * 5 * 2
        if len(self._pcm_buf) > max_bytes:
            self._pcm_buf = self._pcm_buf[-max_bytes:]

        n_samples = len(self._pcm_buf) // 2
        if n_samples < _OWW_FRAME_SAMPLES:
            return None

        use_n = (n_samples // _OWW_FRAME_SAMPLES) * _OWW_FRAME_SAMPLES
        pcm = np.frombuffer(bytes(self._pcm_buf[: use_n * 2]), dtype=np.int16)
        infer = pcm
        if float(getattr(self._model, "vad_threshold", 0) or 0) > 0:
            pad = (-use_n) % _OWW_VAD_FRAME_SAMPLES
            if pad:
                infer = np.pad(pcm, (0, pad), mode="constant")

        try:
            predictions = self._model.predict(infer)
        except Exception as exc:
            logger.debug("[WakeWord] openWakeWord predict error: %s", exc)
            return None

        self._pcm_buf = self._pcm_buf[use_n * 2 :]

        best_kw, best_score = self._score_predictions(predictions)
        if best_kw:
            self._last_score = best_score
            self._last_wake = now
            logger.info(
                "[WakeWord] openWakeWord matched: %r (score=%.3f)",
                best_kw,
                best_score,
            )
            self._model.reset()
            self._pcm_buf.clear()
            return best_kw
        return None

    def flush(self) -> Optional[str]:
        return None
