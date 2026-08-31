"""
LISTENING 听句结束判停用的 FunASR fsmn-vad（流式）。

本模块是项目中 **ASR 判停的主 VAD**（见 docs/VAD.md §①）：
  - engine preload 加载 fsmn-vad，打开麦克风流时 create_funasr_vad() 得到 FunASRVADSession
  - LISTENING 态每块 PCM 用 chunk_is_speech() / consume_speech_end() 判断是否仍在说话
  - FunASR 输出 [[beg,-1]] 起始、[[-1,end]] / [[beg,end]] 结束；结束即判停

与以下无关（勿混淆）：
  - WakeUtteranceGate 可选 VAD（wake_word.gate_use_silero，默认 false）
  - FunASR 流式 ASR 本身不负责切句

依赖：funasr。默认 200ms 分析窗（config funasr.vad_chunk_ms）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np

from funasr_asr import _extract_vad_value, _pcm16_to_f32

logger = logging.getLogger(__name__)


class FunASRVADSession:
    """
    流式 fsmn-vad：对 int16 PCM 按 vad_chunk_ms 窗检测语音起止。

    Attributes:
        in_speech: 当前是否处于语音段内
        sample_rate: 采样率（仅 16000 与 FunASR 官方流式示例一致）
        skip_offline_trim: 流式 VAD 不能对历史 chunk 重跑，尾裁应跳过
    """

    skip_offline_trim = True

    def __init__(
        self,
        vad_model: Any,
        sample_rate: int = 16000,
        chunk_ms: int = 200,
    ):
        self._model = vad_model
        self.sample_rate = int(sample_rate) if int(sample_rate) > 0 else 16000
        self.chunk_ms = max(60, int(chunk_ms))
        self._stride = int(self.chunk_ms * self.sample_rate / 1000)
        self._cache: dict = {}
        self._pending = np.zeros(0, dtype=np.float32)
        self.in_speech = False
        self._just_ended = False
        self._last_fraction = 0.0

    def reset(self) -> None:
        """重置 VAD cache；每次 LISTENING 开始或回 IDLE 时调用。"""
        self._cache = {}
        self._pending = np.zeros(0, dtype=np.float32)
        self.in_speech = False
        self._just_ended = False
        self._last_fraction = 0.0

    def consume_speech_end(self) -> bool:
        """若上一轮 feed 检测到语音结束则返回 True，并清除标志。"""
        flag = self._just_ended
        self._just_ended = False
        return flag

    def speech_fraction(self, pcm16_mono: bytes) -> float:
        """
        喂入本 chunk，返回当前语音占比（流式 VAD 为 0 或 1）。

        Args:
            pcm16_mono: 16-bit 单声道 PCM 字节
        """
        self._just_ended = False
        if not pcm16_mono or self._model is None:
            self._last_fraction = 1.0 if self.in_speech else 0.0
            return self._last_fraction

        self._pending = np.concatenate([self._pending, _pcm16_to_f32(pcm16_mono)])
        saw_speech = False
        while len(self._pending) >= self._stride:
            chunk = np.ascontiguousarray(self._pending[: self._stride], dtype=np.float32)
            self._pending = self._pending[self._stride :]
            self._feed_chunk(chunk, is_final=False)
            if self.in_speech:
                saw_speech = True

        if self._just_ended:
            self._last_fraction = 0.0
        elif self.in_speech or saw_speech:
            self._last_fraction = 1.0
        else:
            self._last_fraction = 0.0
        return self._last_fraction

    def flush(self) -> None:
        """冲刷不足一块的尾音频（LISTENING 结束时可选）。"""
        if self._model is None:
            return
        leftover = self._pending
        self._pending = np.zeros(0, dtype=np.float32)
        self._feed_chunk(leftover, is_final=True)

    def _feed_chunk(self, chunk: np.ndarray, *, is_final: bool) -> None:
        if chunk.size == 0 and not is_final:
            return
        try:
            res = self._model.generate(
                input=np.ascontiguousarray(chunk, dtype=np.float32),
                cache=self._cache,
                is_final=is_final,
                chunk_size=self.chunk_ms,
            )
        except Exception as exc:
            logger.warning("[VAD] FunASR generate failed (%s): %s", type(exc).__name__, exc)
            return
        self._apply_events(_extract_vad_value(res))

    def _apply_events(self, value: list) -> None:
        if not value:
            return
        for pair in value:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            beg, end = pair[0], pair[1]
            try:
                beg_i = int(beg)
                end_i = int(end)
            except (TypeError, ValueError):
                continue
            if beg_i >= 0 and end_i < 0:
                self.in_speech = True
            elif end_i >= 0:
                self.in_speech = False
                self._just_ended = True


def create_funasr_vad(
    vad_model: Any,
    sample_rate: int = 16000,
    chunk_ms: int = 200,
) -> Optional[FunASRVADSession]:
    """
    用已加载的 fsmn-vad AutoModel 创建流式会话；模型为 None 时返回 None。
    """
    if vad_model is None:
        logger.warning("[VAD] create_funasr_vad skipped: vad_model is None")
        return None
    try:
        session = FunASRVADSession(
            vad_model, sample_rate=sample_rate, chunk_ms=chunk_ms
        )
        logger.info(
            "[VAD] FunASR fsmn-vad ready (chunk=%d ms, rate=%d Hz)",
            session.chunk_ms,
            session.sample_rate,
        )
        return session
    except Exception as exc:
        logger.warning("[VAD] create_funasr_vad failed (%s)", exc)
        return None


# 兼容旧名，避免外部脚本仍调用 create_silero_vad
def create_silero_vad(*args, **kwargs):  # noqa: ARG001
    logger.warning(
        "[VAD] create_silero_vad is removed; use create_funasr_vad(vad_model, ...)"
    )
    return None


def _resolve_vad(vad: Any) -> Any:
    return vad


def speech_frame_fraction(
    pcm16_mono: bytes,
    vad: Any,
    sample_rate: int,  # noqa: ARG001
) -> float:
    """返回 chunk 内 speech 占比（委托 FunASRVADSession.speech_fraction）。"""
    session = _resolve_vad(vad)
    if isinstance(session, FunASRVADSession):
        return session.speech_fraction(pcm16_mono)
    return 0.0


def chunk_is_speech(
    pcm16_mono: bytes,
    vad: Any,
    sample_rate: int,
    min_fraction: float = 0.2,
) -> bool:
    """chunk 内 speech 占比是否 ≥ min_fraction。"""
    return speech_frame_fraction(pcm16_mono, vad, sample_rate) >= min_fraction


def trim_trailing_silence_chunks(
    buf: list,
    vad: Any,
    sample_rate: int,
    min_fraction: float = 0.15,
) -> list:
    """
    从缓冲末尾去掉连续静音 chunk。

    FunASR 流式 VAD 不能对历史块重跑，此时直接返回原缓冲（判停时已等过尾静音）。
    """
    if not buf or vad is None:
        return buf
    if getattr(vad, "skip_offline_trim", False):
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
    """兼容旧配置键；FunASR VAD 不再使用该阈值。"""
    if "silero_threshold" in w:
        return float(w["silero_threshold"])
    return 0.5


def silero_speech_fraction_from_config(w: dict) -> float:
    """兼容旧配置；流式 VAD 以 0/1 状态为主，此值仅作 gate 备用。"""
    return float(
        w.get(
            "silero_speech_fraction",
            w.get("webrtcvad_speech_fraction", 0.2),
        )
    )
