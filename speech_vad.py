"""
LISTENING 听句结束判停用的 Silero VAD（ONNX）。

本模块是项目中 **ASR 判停的主 VAD**（见 docs/VAD.md §①）：
  - engine 在打开麦克风流时 create_silero_vad() 得到 SileroVADSession
  - LISTENING 态每块 PCM 用 chunk_is_speech() 判断是否仍在说话
  - 连续静音达到 max_silence_ms 后 _finalize()，转写前 trim_trailing_silence_chunks()

与以下无关（勿混淆）：
  - WakeUtteranceGate 可选 Silero（wake_word.gate_use_silero，默认 false）
  - faster-whisper transcribe(vad_filter=...)（引擎已关闭）

依赖：silero-vad + onnxruntime。16kHz 下分析窗 512 样本（约 32ms）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Union

import numpy as np

logger = logging.getLogger(__name__)

_WINDOW_BY_RATE = {8000: 256, 16000: 512}
_VALID_RATES = (8000, 16000)


class SileroVADSession:
    """
    流式 Silero VAD：对 int16 PCM 按 512 样本窗计算语音概率。

    Attributes:
        threshold: 单窗概率 ≥ 此值计为 speech（0.05~0.95）
        sample_rate: 8000 或 16000
    """

    def __init__(self, threshold: float = 0.5, sample_rate: int = 16000):
        self.threshold = max(0.05, min(0.95, float(threshold)))
        self.sample_rate = 16000 if int(sample_rate) not in _VALID_RATES else int(sample_rate)
        self._window = _WINDOW_BY_RATE[self.sample_rate]
        self._model = None
        self._load_model()

    def _load_model(self) -> None:
        try:
            from silero_vad import load_silero_vad

            self._model = load_silero_vad(onnx=True)
            logger.info(
                "[VAD] Silero ready (threshold=%.2f, rate=%d Hz)",
                self.threshold,
                self.sample_rate,
            )
        except Exception as exc:
            logger.warning("[VAD] Silero init failed (%s)", exc)
            self._model = None

    def reset(self) -> None:
        """重置 Silero 内部 RNN 状态；每次 LISTENING 开始或回 IDLE 时调用。"""
        if self._model is not None:
            self._model.reset_states()

    def speech_fraction(self, pcm16_mono: bytes) -> float:
        """
        计算本 chunk 中被判为 speech 的 Silero 窗占比（0.0~1.0）。

        Args:
            pcm16_mono: 16-bit 单声道 PCM 字节
        """
        if not pcm16_mono or self._model is None:
            return 0.0
        import torch

        audio = (
            np.frombuffer(pcm16_mono, dtype=np.int16).astype(np.float32) / 32768.0
        )
        n = self._window
        if len(audio) < n:
            return 0.0
        speech = 0
        total = 0
        for i in range(0, len(audio) - n + 1, n):
            x = torch.from_numpy(audio[i : i + n]).unsqueeze(0)
            prob = float(self._model(x, self.sample_rate).item())
            total += 1
            if prob >= self.threshold:
                speech += 1
        return speech / total if total else 0.0


def create_silero_vad(
    threshold: float = 0.5,
    sample_rate: int = 16000,
) -> Optional[SileroVADSession]:
    """
    创建 SileroVADSession；包或模型不可用时返回 None。

    Args:
        threshold: 语音概率阈值
        sample_rate: 8000 或 16000
    """
    try:
        session = SileroVADSession(threshold=threshold, sample_rate=sample_rate)
        if session._model is None:
            return None
        return session
    except Exception as exc:
        logger.warning("[VAD] create_silero_vad failed (%s)", exc)
        return None


def _resolve_vad(vad: Any) -> Any:
    if isinstance(vad, SileroVADSession):
        return vad
    return vad


def speech_frame_fraction(
    pcm16_mono: bytes,
    vad: Any,
    sample_rate: int,
) -> float:
    """返回 chunk 内 speech 窗占比（委托 SileroVADSession.speech_fraction）。"""
    session = _resolve_vad(vad)
    if isinstance(session, SileroVADSession):
        return session.speech_fraction(pcm16_mono)
    return 0.0


def chunk_is_speech(
    pcm16_mono: bytes,
    vad: Any,
    sample_rate: int,
    min_fraction: float = 0.2,
) -> bool:
    """chunk 内 speech 窗占比是否 ≥ min_fraction。"""
    return speech_frame_fraction(pcm16_mono, vad, sample_rate) >= min_fraction


def trim_trailing_silence_chunks(
    buf: list,
    vad: Any,
    sample_rate: int,
    min_fraction: float = 0.15,
) -> list:
    """
    从缓冲末尾去掉连续静音 chunk（Whisper 转写前裁剪尾静音）。

    若裁剪后总长低于约 0.35s 则保留原缓冲，避免过短。
    """
    if not buf or vad is None:
        return buf
    min_bytes = int(sample_rate * 0.35) * 2
    keep = len(buf)
    for i in range(len(buf) - 1, -1, -1):
        if chunk_is_speech(buf[i], vad, sample_rate, min_fraction=min_fraction):
            break
        keep = i
    if keep <= 0:
        return buf
    if keep < len(buf):
        trimmed = buf[:keep]
        if sum(len(c) for c in trimmed) < min_bytes:
            return buf
        return trimmed
    return buf


def silero_threshold_from_config(w: dict) -> float:
    """
    从 whisper 配置读取 silero_threshold。

    若无则按 legacy webrtcvad_aggressiveness(0~3) 映射到 0.35~0.575。
    """
    if "silero_threshold" in w:
        return float(w["silero_threshold"])
    if "webrtcvad_aggressiveness" in w:
        agg = max(0, min(3, int(w["webrtcvad_aggressiveness"])))
        return 0.35 + agg * 0.075
    return 0.5


def silero_speech_fraction_from_config(w: dict) -> float:
    """从 whisper 配置读取 silero_speech_fraction（兼容 webrtcvad_speech_fraction）。"""
    return float(
        w.get(
            "silero_speech_fraction",
            w.get("webrtcvad_speech_fraction", 0.2),
        )
    )
