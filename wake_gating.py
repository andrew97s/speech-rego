"""
唤醒词门控（WakeUtteranceGate）。

在引擎判定「唤醒词命中」之前，用麦克风 RMS 历史过滤误唤醒：
要求唤醒前有一段安静，且当前语音突发不能太长（避免背景对话里误触发）。

与 openWakeWord 检测器解耦，由 engine 在 IDLE 态每块音频 push 一次。
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Deque, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 每块音频统计: (RMS 音量, 能量判为说话, Silero 判为说话)
_ChunkStat = Tuple[float, bool, bool]


class WakeUtteranceGate:
    """
    滚动麦克风历史门控：先安静 → 再说短唤醒句。

    典型流程::
        gate.push(audio, level, ...)     # 每块麦克风数据
        ok, reason = gate.may_accept_wake(cfg, chunk_ms=...)  # 检测器命中后复核

    Attributes:
        _chunks: 最近若干块的 (level, energy_talk, silero_talk)
        _noise_floor: 自适应环境噪声底（用于动态 quiet 阈值）
    """

    def __init__(self, *, maxlen: int = 80):
        """
        Args:
            maxlen: 保留的音频块数量上限（默认约 80 块，具体时长取决于 chunk_ms）
        """
        self._chunks: Deque[_ChunkStat] = deque(maxlen=maxlen)
        self._noise_floor: float = 0.006

    def reset(self) -> None:
        """清空历史；唤醒接受或长时间 IDLE 后可调用。"""
        self._chunks.clear()
        self._noise_floor = 0.006

    def _quiet_level_threshold(
        self, energy_threshold: float, ww_cfg: dict,
    ) -> float:
        """
        计算「安静」RMS 上限。

        低于该值的块视为 pre_silence；由 energy_threshold 与自适应 noise_floor 共同决定。
        """
        mult = float(ww_cfg.get("pre_silence_level_ratio", 0.32))
        floor = float(ww_cfg.get("pre_silence_noise_floor", 0.0035))
        return max(
            energy_threshold * mult,
            self._noise_floor * 2.2,
            floor,
        )

    def push(
        self,
        audio_bytes: bytes,
        level: float,
        *,
        energy_threshold: float,
        speech_vad: Any = None,
        sample_rate: int = 16000,
        speech_fraction: float = 0.2,
        ww_cfg: Optional[dict] = None,
    ) -> None:
        """
        记录一块麦克风数据的音量与是否像「在说话」。

        Args:
            audio_bytes: int16 单声道 PCM（gate_use_silero 时供 Silero 使用）
            level: 当前块 RMS，0~1
            energy_threshold: config audio.energy_threshold
            speech_vad: SileroVADSession，仅 gate_use_silero=true 时使用
            sample_rate: 采样率，默认 16000
            speech_fraction: 未直接使用，保留兼容
            ww_cfg: get_wake_word_options() 返回的门控参数字典
        """
        ww_cfg = ww_cfg or {}
        quiet_thr = self._quiet_level_threshold(energy_threshold, ww_cfg)

        if level < quiet_thr:
            self._noise_floor = min(
                self._noise_floor * 0.92 + level * 0.08,
                max(level, 1e-5),
            )

        energy_talk = level >= energy_threshold * float(
            ww_cfg.get("speech_energy_ratio", 0.38)
        )
        silero_talk = False
        use_silero = bool(ww_cfg.get("gate_use_silero", False))
        if use_silero and speech_vad is not None:
            try:
                from speech_vad import chunk_is_speech

                silero_talk = chunk_is_speech(
                    audio_bytes,
                    speech_vad,
                    sample_rate,
                    min_fraction=float(
                        ww_cfg.get("gate_silero_fraction", 0.28)
                    ),
                )
            except Exception:
                pass

        self._chunks.append((level, energy_talk, silero_talk))

    @staticmethod
    def _speech_burst_start(items: List[_ChunkStat]) -> int:
        """
        从尾部向前找当前「说话突发」的起始块索引。

        Returns:
            第一个 energy_talk 或 silero_talk 为 True 的块下标
        """
        i = len(items) - 1
        while i >= 0:
            lvl, et, st = items[i]
            if et or st:
                i -= 1
            else:
                break
        return i + 1

    def may_accept_wake(
        self,
        ww_cfg: dict,
        *,
        chunk_ms: float,
        energy_threshold: float = 0.02,
    ) -> Tuple[bool, str]:
        """
        检测器已匹配唤醒词时，复核是否应真正发出 wake_word 事件。

        Args:
            ww_cfg: get_wake_word_options() 门控字段
            chunk_ms: 每块音频时长(ms)，用于换算 require_pre_silence_ms
            energy_threshold: 音量阈值

        Returns:
            (是否接受, 原因码)
            原因码: ok | disabled | no_history | no_speech_at_wake |
                    utterance_too_long | warming_up | no_pre_silence
        """
        if not self._chunks:
            return False, "no_history"

        pre_ms = max(0, int(ww_cfg.get("require_pre_silence_ms", 350)))
        if pre_ms <= 0:
            return True, "disabled"

        pre_ratio = float(ww_cfg.get("pre_silence_min_ratio", 0.65))
        pre_ratio = max(0.4, min(pre_ratio, 1.0))
        max_utt_ms = max(400, int(ww_cfg.get("max_wake_utterance_ms", 1400)))
        quiet_thr = self._quiet_level_threshold(energy_threshold, ww_cfg)

        pre_n = max(1, int(pre_ms / max(chunk_ms, 1)))
        items = list(self._chunks)

        onset = self._speech_burst_start(items)
        if onset >= len(items):
            return False, "no_speech_at_wake"

        burst_len = len(items) - onset
        if burst_len * chunk_ms > max_utt_ms:
            return False, "utterance_too_long"

        if onset < pre_n:
            return False, "warming_up"

        pre_win = items[onset - pre_n : onset]
        quiet_count = sum(1 for lvl, _, _ in pre_win if lvl < quiet_thr)
        ratio = quiet_count / len(pre_win)
        if ratio < pre_ratio:
            levels = [round(lvl, 4) for lvl, _, _ in pre_win]
            logger.info(
                "Wake gate pre_silence: %d/%d quiet (need %.0f%%), "
                "thr=%.4f noise_floor=%.4f levels=%s",
                quiet_count,
                len(pre_win),
                pre_ratio * 100,
                quiet_thr,
                self._noise_floor,
                levels,
            )
            return False, "no_pre_silence"

        return True, "ok"
