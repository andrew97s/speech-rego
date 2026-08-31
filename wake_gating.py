"""
唤醒词门控（WakeUtteranceGate）。

## 为什么需要门控？

Sherpa KWS 等检测器在 **IDLE** 态对麦克风持续扫描，背景里有人说话、电视声、
连续对话中的某个音节，都可能被模型误当成唤醒词。门控不替换 KWS，而是在
**检测器已经命中** 之后做二次复核：这次命中是否像「用户有意喊了一句短唤醒词」。

## 与 engine 的配合（IDLE 态）

    每块 PCM chunk
        │
        ├─ gate.push(...)          ← 先记录 RMS / 可选 Silero 标记
        └─ detector.process(...)   ← KWS 推理
                │
                └─ 若命中 → gate.may_accept_wake(...)
                        ├─ False → 丢弃，reset 检测器
                        └─ True  → emit wake_word，gate.reset()

门控与 KWS 解耦：engine 每块音频先 push 再 process；只有 process 返回 keyword
时才调用 may_accept_wake。

## 核心假设（默认策略）

真实唤醒通常是：

    [安静或较静] → [短促说话，含唤醒词] → （随后进入 LISTENING）

误唤醒常见于：

    - 背景里一直在说话，模型从长句中「抠」出类似唤醒词的片段
    - 没有明显「先安静再开口」的前奏

因此默认检查三件事：

1. **pre_silence**：说话突发开始前，有足够长的「安静窗口」
2. **utterance 长度**：当前说话突发不能太长（否则像连续对话而非喊唤醒词）
3. **warming_up**：历史 deque 还没攒够 pre_silence 所需块数时先拒绝（避免刚启动就误放行）

## 与 ASR 判停 VAD 的区别

| | WakeUtteranceGate | speech_vad（LISTENING 判停） |
|--|-------------------|------------------------------|
| 何时 | IDLE，KWS 命中后 | LISTENING，用户说完一句 |
| 默认 | 只看 RMS 历史 | Silero 流式 VAD |
| 可选 | gate_use_silero=true 叠加 Silero | 必用 |

两套 VAD 实例与阈值互不替代。详见 docs/VAD.md §②。
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Deque, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 每块音频的摘要统计，供 may_accept_wake 回溯历史：
#   level       — 块 RMS，约 0~1（engine 从 int16 PCM 算出）
#   energy_talk — 是否达到「像在说话」的能量（level >= energy_threshold * speech_energy_ratio）
#   silero_talk — gate_use_silero 时，Silero 判该块为 speech 则为 True
_ChunkStat = Tuple[float, bool, bool]


class WakeUtteranceGate:
    """
    基于滚动麦克风历史的唤醒二次门控。

    内部维护一个固定长度的 deque（默认 80 块），每块对应 engine 里一次
    `audio_q.get()` 的 chunk（时长由 config audio.chunk_size / sample_rate 决定，
    默认 4000 样本 @16kHz ≈ 250ms/块，80 块 ≈ 20s 历史）。

    ## 典型时序

        t0  t1  t2  t3  t4  t5  t6  t7  t8
        ──  ──  ──  ──  ──  ──  ──  ──  ──
        静  静  静  说  说  说  静  静  静
                    └─ speech burst ─┘
              └ pre_win ─┘
                    ↑ onset（burst 起始下标）

    KWS 在 t5 命中「小智」时，may_accept_wake 会：

    - 用 `_speech_burst_start` 从 deque 尾部向前找 onset（第一个 energy_talk/silero_talk 块）
    - 检查 onset 前 pre_n 块里「安静」占比是否 ≥ pre_silence_min_ratio
    - 检查 burst 总时长是否 ≤ max_wake_utterance_ms

    ## 配置项（wake_word.*，由 get_wake_word_options 解析）

    | 键 | 默认 | 作用 |
    |----|------|------|
    | require_pre_silence_ms | 350 | 需要的唤醒前静音时长；0 则关闭门控（直接 ok） |
    | pre_silence_min_ratio | 0.65 | pre 窗口内 RMS < quiet_thr 的块占比下限 |
    | pre_silence_level_ratio | 0.32 | quiet_thr = max(energy_threshold×此值, …) |
    | pre_silence_noise_floor | 0.0035 | quiet_thr 绝对下限 |
    | speech_energy_ratio | 0.38 | 判定 energy_talk 时相对 energy_threshold 的比例 |
    | max_wake_utterance_ms | 1400 | 当前 speech burst 允许的最长毫秒数 |
    | gate_use_silero | false | true 时用 Silero 辅助标记 silero_talk |
    | gate_silero_fraction | 0.28 | Silero 块内 speech 窗占比阈值 |

    ## may_accept_wake 返回值 reason

    | reason | 含义 |
    |--------|------|
    | ok | 通过门控，可发 wake_word |
    | disabled | require_pre_silence_ms=0，门控关闭 |
    | no_history | deque 为空（不应在正常使用中出现） |
    | no_speech_at_wake | 跳过尾静音后仍找不到 energy_talk/silero_talk 块 |
    | utterance_too_long | 从 onset 到当前的 burst 超过 max_wake_utterance_ms |
    | warming_up | deque 长度不足以覆盖 pre_silence 所需块数 |
    | no_pre_silence | onset 前的 pre 窗口安静占比不足 |
    """

    def __init__(self, *, maxlen: int = 80):
        """
        Args:
            maxlen: deque 最多保留的块数。engine 在麦克风循环开始时创建一次，
                直到 stream 结束。块数 × chunk_ms ≈ 可回溯的时间窗。
        """
        self._chunks: Deque[_ChunkStat] = deque(maxlen=maxlen)
        # 自适应环境噪声底：在「安静块」上指数滑动更新，用于动态抬高 quiet_thr，
        # 避免在略吵的环境里把持续背景声当成 pre_silence。
        self._noise_floor: float = 0.006

    def reset(self) -> None:
        """
        清空块历史并将 noise_floor 恢复初值。

        engine 在成功 emit wake_word 后调用，避免上一次唤醒的 burst
        污染下一次 may_accept_wake 的 pre 窗口计算。
        """
        self._chunks.clear()
        self._noise_floor = 0.006

    def _quiet_level_threshold(
        self, energy_threshold: float, ww_cfg: dict,
    ) -> float:
        """
        判定「安静块」的 RMS 上限 quiet_thr。

        某块 level < quiet_thr 则计入 pre_silence 的 quiet_count。

        取三者最大值：
        - energy_threshold × pre_silence_level_ratio（相对麦克风灵敏度）
        - noise_floor × 2.2（随环境自适应）
        - pre_silence_noise_floor（绝对下限，极静环境仍有效）

        push() 与 may_accept_wake() 必须使用同一公式，保证标记与判定一致。
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
        记录一块麦克风数据到滚动历史（IDLE 态每 chunk 调用一次）。

        不决定是否唤醒；只积累 (level, energy_talk, silero_talk) 供
        may_accept_wake 在 KWS 命中后回溯。

        Args:
            audio_bytes: int16 单声道 PCM；仅 gate_use_silero 时需要
            level: 当前块 RMS，0~1，engine 用 sqrt(mean(sample^2))/32768 计算
            energy_threshold: config audio.energy_threshold，环境「有声」基准
            speech_vad: engine 的 SileroVADSession；gate_use_silero=false 时可忽略
            sample_rate: 采样率，默认 16000
            speech_fraction: 保留参数，当前实现未使用（Silero 用 gate_silero_fraction）
            ww_cfg: get_wake_word_options() 的门控字段子集

        副作用:
            - 若 level < quiet_thr，用 EMA 更新 _noise_floor（系数 0.92/0.08）
            - 追加一条 _ChunkStat 到 deque；满 maxlen 时丢弃最旧块
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
        # 短唤醒词常被 250ms 块里的静音稀释，略低于 energy_threshold 仍算开口
        if not energy_talk:
            min_talk = float(ww_cfg.get("speech_level_floor", 0.004))
            energy_talk = level >= max(min_talk, self._noise_floor * 2.5)
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
    def _speech_burst_start(
        items: List[_ChunkStat], max_trail_quiet: int = 3,
    ) -> int:
        """
        从 deque 尾部向前扫描，定位「当前说话突发」的起始块下标 onset。

        Sherpa KWS 常在词尾 1～3 块偏静的 PCM 上才解码出 keyword（trailing blanks），
        因此先跳过最多 max_trail_quiet 块尾静音，再沿 energy_talk/silero_talk 往前找 onset。

        若跳过尾静音后仍看不到说话块，返回 len(items) → no_speech_at_wake。

        示例 items（et=energy_talk）::

            idx:  0    1    2    3    4    5
            et:   F    F    T    T    T    F     ← KWS 在 t5 命中
                              ↑ onset=2（跳过尾块 5）
        """
        i = len(items) - 1
        skipped = 0
        while i >= 0 and skipped < max(0, int(max_trail_quiet)):
            _lvl, et, st = items[i]
            if et or st:
                break
            i -= 1
            skipped += 1
        if i < 0:
            return len(items)
        _lvl, et, st = items[i]
        if not (et or st):
            return len(items)
        while i >= 0:
            _lvl, et, st = items[i]
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
        KWS 已返回 keyword 时，决定是否真正发出 wake_word 事件。

        调用时机：engine IDLE 态，`detector.process()` 非 None 之后立即调用。
        若返回 False，engine 会 log 并 reset 检测器，不 emit wake_word。

        Args:
            ww_cfg: get_wake_word_options() 的门控相关字段
            chunk_ms: 单块音频毫秒数（engine: chunk_size/sample_rate*1000）
            energy_threshold: config audio.energy_threshold

        Returns:
            (accepted, reason) — reason 见类文档表格

        判定顺序（短路）:
            1. no_history — deque 空
            2. disabled — require_pre_silence_ms <= 0
            3. no_speech_at_wake — 尾部无 burst
            4. utterance_too_long — burst 过长
            5. warming_up — onset < pre_n（历史不够长）
            6. no_pre_silence — pre 窗口安静占比不足
            7. ok
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

        # 唤醒前至少需要多少块「pre 窗口」
        pre_n = max(1, int(pre_ms / max(chunk_ms, 1)))
        # ~500ms 尾静音：KWS 解码滞后于能量峰值
        max_trail = max(1, int(500.0 / max(chunk_ms, 1.0)))
        items = list(self._chunks)

        onset = self._speech_burst_start(items, max_trail_quiet=max_trail)
        if onset >= len(items):
            tail = [
                round(lvl, 4) for lvl, _, _ in items[-max(4, max_trail) :]
            ]
            logger.info(
                "Wake gate no_speech_at_wake: last_levels=%s "
                "energy_thr=%.4f (need energy_talk on/near match chunk)",
                tail,
                energy_threshold * float(ww_cfg.get("speech_energy_ratio", 0.38)),
            )
            return False, "no_speech_at_wake"

        last_talk = len(items) - 1
        while last_talk > onset:
            _lvl, et, st = items[last_talk]
            if et or st:
                break
            last_talk -= 1
        burst_len = last_talk - onset + 1
        if burst_len * chunk_ms > max_utt_ms:
            return False, "utterance_too_long"

        # deque 还没积累够 pre_n 块时 onset 会偏小，拒绝以免刚启动就误放行
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
