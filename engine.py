"""
语音识别引擎：openWakeWord 唤醒 → Silero VAD 判停 → faster-whisper ASR。

详见 docs/CORE_API.md
"""

import json
import logging
import os
import queue
import re
import threading
import time
from enum import Enum
from typing import Any, Callable, List, Optional

import numpy as np

from speech_vad import (
    chunk_is_speech,
    create_silero_vad,
    silero_speech_fraction_from_config,
    silero_threshold_from_config,
    trim_trailing_silence_chunks,
)
from text_postprocess import get_postprocess_config, postprocess_transcript
from wake_detectors import OpenWakeWordWakeWordDetector
from wake_gating import WakeUtteranceGate
from wake_config import get_wake_word_options
from whisper_local import (
    is_hub_offline,
    offline_model_error_message,
    pick_whisper_model,
    read_bundled_model_manifest,
)

logger = logging.getLogger(__name__)

_DEVICE_RETRY_SEC    = 3.0
_WHISPER_SAMPLE_RATE = 16000   # Whisper only accepts 16 kHz input


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize_for_prompt_compare(s: str) -> str:
    """Whitespace / common separators — for comparing transcript to initial_prompt."""
    if not s:
        return ""
    return re.sub(r"[\s\u3000·]+", "", s.strip())


def _prompt_vocab_tokens(prompt: str) -> set:
    """Space-separated domain words + CJK runs from initial_prompt."""
    if not prompt:
        return set()
    toks = set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}", prompt))
    for part in re.split(r"[\s\u3000,，、]+", prompt.strip()):
        part = part.strip()
        if len(part) >= 2:
            toks.add(part)
    return {t for t in toks if t}


def _has_looped_unit(text: str, min_unit: int = 2, max_unit: int = 12, min_repeat: int = 3) -> bool:
    """Same short phrase repeated many times (处置类型处置类型… / 区域区域…)."""
    if len(text) < min_unit * min_repeat:
        return False
    for n in range(min_unit, min(max_unit, len(text) // min_repeat) + 1):
        pat = rf"(.{{{n}}})\1{{{min_repeat - 1},}}"
        if re.search(pat, text):
            return True
    return False


def _text_echoes_initial_prompt(text: str, initial_prompt: Optional[str]) -> bool:
    """
    Silence/noise + keyword-style initial_prompt makes Whisper repeat prompt tokens.
    """
    if not initial_prompt or not text:
        return False
    t = _normalize_for_prompt_compare(text)
    p = _normalize_for_prompt_compare(initial_prompt)
    if len(t) < 4 or len(p) < 4:
        return False

    vocab = _prompt_vocab_tokens(initial_prompt)

    # Repeated domain fragment (most common user report)
    if _has_looped_unit(t):
        if not vocab:
            return True
        for n in range(2, 13):
            pat_loop = r"(.{" + str(n) + r"})\1{2,}"
            for m in re.finditer(pat_loop, t):
                unit = m.group(1)
                if unit in vocab or unit in p:
                    return True
                if any(unit in w or w in unit for w in vocab if len(w) >= 2):
                    return True

    # Keyword-soup hallucination: few exact domain words tiled with little other text.
    # Do NOT use loose substring token match — normal sentences like「今日告警有多少」
    # would be dropped when the whole line is one regex token.
    if len(t) >= 8 and vocab and _has_looped_unit(t):
        exact_hits = [w for w in vocab if w in t]
        if exact_hits and len("".join(exact_hits)) >= 0.55 * len(t):
            return True

    # Long contiguous slice of the prompt
    if t in p and len(t) >= max(8, int(0.45 * len(p))):
        return True
    if t in p and len(p) <= 24 and len(t) >= int(0.82 * len(p)):
        return True
    # Ordered walk through prompt chars — only when text is almost entirely from
    # the keyword list (not a normal sentence that merely mentions 告警/火警).
    if len(t) >= 12 and len(t) >= 0.55 * len(p):
        i = 0
        for c in p:
            if i < len(t) and c == t[i]:
                i += 1
        if i == len(t):
            exact_hits = [w for w in vocab if w in t] if vocab else []
            if not exact_hits or len("".join(exact_hits)) >= 0.5 * len(t):
                return True
    return False


_DEFAULT_VERBATIM_PROMPT = (
    "以下是普通话口语的逐字转写。只输出音频里实际说出的词句，"
    "不要编造、不要续写未说出的内容，不要改写或概括。"
)
_DEFAULT_DOMAIN_STYLE_PROMPT = (
    "以下是普通话口语，可能包含消防物联网、楼栋楼层、告警处置等术语。"
)
_MAX_ASR_INITIAL_PROMPT_CHARS = 120


def _domain_vocab_text(w: dict) -> str:
    """
    Full fire-domain vocabulary for echo filtering (not necessarily sent to Whisper).
    Maintain as domain_keywords[] and/or domain_initial_prompt / initial_prompt.
    """
    parts: List[str] = []
    kws = w.get("domain_keywords")
    if isinstance(kws, list):
        parts.extend(str(k).strip() for k in kws if k and str(k).strip())
    elif isinstance(kws, str) and kws.strip():
        parts.append(kws.strip())
    for key in ("domain_initial_prompt", "initial_prompt"):
        v = w.get(key)
        if v and str(v).strip():
            parts.append(str(v).strip())
    return " ".join(parts)


def _resolve_asr_initial_prompt(w: dict) -> Optional[str]:
    """
    What we pass to Whisper as initial_prompt.

    Do NOT pass long keyword lists — they cause loop hallucinations on silence.
    """
    verbatim = bool(w.get("verbatim", True))
    if verbatim:
        if not w.get("verbatim_use_prompt", False):
            return None
        raw = w.get("verbatim_initial_prompt")
        if raw is None:
            return None
        s = str(raw).strip()
        return s if s else None

    if not w.get("use_domain_vocab_prompt", False):
        short = w.get("domain_style_prompt") or _DEFAULT_DOMAIN_STYLE_PROMPT
        if short and len(short) > _MAX_ASR_INITIAL_PROMPT_CHARS:
            short = short[:_MAX_ASR_INITIAL_PROMPT_CHARS].rstrip()
        return short

    raw = (w.get("initial_prompt") or w.get("domain_initial_prompt") or "").strip()
    if not raw:
        return _DEFAULT_DOMAIN_STYLE_PROMPT
    max_chars = int(w.get("max_initial_prompt_chars", _MAX_ASR_INITIAL_PROMPT_CHARS))
    max_chars = max(40, min(max_chars, 200))
    if len(raw) > max_chars:
        logger.warning(
            "initial_prompt truncated %d -> %d chars for ASR "
            "(set whisper.use_domain_vocab_prompt=false to use short style prompt only)",
            len(raw),
            max_chars,
        )
        return raw[:max_chars].rstrip()
    return raw


def _filter_whisper_segments(segs_list: List, w: dict) -> List:
    """Drop segments Whisper flags as repetitive / low-confidence hallucination."""
    cr_max = float(w.get("compression_ratio_threshold", 2.0))
    lp_min = float(w.get("min_avg_logprob", -1.05))
    kept: List = []
    for seg in segs_list:
        txt = (seg.text or "").strip()
        if not txt:
            continue
        cr = getattr(seg, "compression_ratio", None)
        if cr is not None and float(cr) > cr_max:
            logger.debug(
                "ASR skip segment compression_ratio=%.2f: %r",
                cr,
                txt[:48],
            )
            continue
        lp = getattr(seg, "avg_logprob", None)
        if lp is not None and float(lp) < lp_min:
            logger.debug(
                "ASR skip segment avg_logprob=%.2f: %r",
                lp,
                txt[:48],
            )
            continue
        kept.append(seg)
    return kept


def _apply_short_audio_runon_guard(
    merged: str,
    segs_list: List,
    audio_sec: float,
    w: dict,
) -> str:
    """
    Short clips often get a correct first phrase + invented tail
    (e.g. 今日勤务 -> 今日志务给我按过去半年统计一下数量).
    """
    if not merged:
        return merged
    guard_sec = float(w.get("short_utterance_guard_sec", 6.0))
    if audio_sec > guard_sec:
        return merged
    max_cps = float(w.get("max_chars_per_second", 7))
    budget = max(12, int(audio_sec * max_cps) + 6)
    first = ""
    if segs_list:
        first = (segs_list[0].text or "").strip()
    if first and len(merged) > len(first) * 1.35 and len(first) <= budget:
        logger.info(
            "ASR short audio (%.2fs): trimmed run-on hallucination %r -> %r",
            audio_sec,
            merged[:56] + ("…" if len(merged) > 56 else ""),
            first,
        )
        return first
    if len(merged) > budget:
        for sep in "。！？；?.!;":
            pos = merged.find(sep)
            if 0 < pos < budget:
                trimmed = merged[: pos + 1]
                logger.info(
                    "ASR short audio (%.2fs): trimmed to first sentence %r",
                    audio_sec,
                    trimmed,
                )
                return trimmed
        trimmed = merged[:budget]
        logger.info(
            "ASR short audio (%.2fs): trimmed by char budget %r",
            audio_sec,
            trimmed,
        )
        return trimmed
    return merged


def _chunk_level(audio_bytes: bytes) -> float:
    audio_np = np.frombuffer(audio_bytes, dtype=np.int16)
    return float(np.sqrt(np.mean(audio_np.astype(np.float32) ** 2))) / 32768.0


def _to_mono_pcm16(
    indata: bytes,
    input_channels: int,
    stereo_mode: str = "mix",
) -> bytes:
    """Convert interleaved int16 PCM to mono (for 2-ch mics where ch0/ch1 differ)."""
    if input_channels <= 1:
        return bytes(indata)
    arr = np.frombuffer(indata, dtype=np.int16).reshape(-1, input_channels)
    mode = (stereo_mode or "mix").strip().lower()
    if mode == "left":
        mono = arr[:, 0]
    elif mode == "right":
        mono = arr[:, 1] if input_channels > 1 else arr[:, 0]
    else:
        mono = arr.astype(np.int32).mean(axis=1).astype(np.int16)
    return mono.tobytes()


def _buffer_to_float32(buf: List[bytes]) -> np.ndarray:
    """Concatenate int16 byte chunks and normalise to float32 [-1, 1]."""
    if not buf:
        return np.zeros(0, dtype=np.float32)
    raw = np.frombuffer(b"".join(buf), dtype=np.int16)
    return raw.astype(np.float32) / 32768.0


def _still_speaking_for_end(
    audio_bytes: bytes,
    speech_vad: Any,
    sample_rate: int,
    vad_fraction: float,
) -> bool:
    """End-of-utterance: Silero speech fraction in chunk."""
    if speech_vad is None:
        return False
    return chunk_is_speech(
        audio_bytes,
        speech_vad,
        sample_rate,
        min_fraction=vad_fraction,
    )


def _reset_speech_vad(vad: Any) -> None:
    if vad is not None and hasattr(vad, "reset"):
        vad.reset()


def _buffer_level(buf: List[bytes]) -> float:
    if not buf:
        return 0.0
    audio_f32 = _buffer_to_float32(buf)
    if len(audio_f32) == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio_f32 ** 2)))


def _trim_leading_echo(buf: List[bytes], energy_threshold: float) -> List[bytes]:
    """Drop leading loud chunks (speaker TTS bleed) before Whisper ASR."""
    if len(buf) < 3:
        return buf
    loud = energy_threshold * 1.35
    skip = 0
    for chunk in buf[: min(len(buf), 24)]:  # ~2.4 s at 100 ms/chunk
        if _chunk_level(chunk) >= loud:
            skip += 1
        else:
            break
    if skip <= 0:
        return buf
    min_keep = max(3, len(buf) // 5)
    if skip >= len(buf) - min_keep:
        skip = max(0, len(buf) - min_keep)
    return buf[skip:]


def _stub_av_if_needed():
    """
    faster-whisper imports ``av`` (PyAV) at module level for audio-file
    decoding.  We always pass numpy arrays so ``av`` is never actually
    called at runtime.  On Windows, if av's bundled FFmpeg DLLs are
    missing, inject a minimal in-memory stub so the import succeeds.
    """
    try:
        import av  # noqa: F401
        return     # av is healthy, nothing to do
    except (ImportError, OSError):
        pass

    import sys
    import types

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    av_mod        = _mod("av")
    core_mod      = _mod("av._core")
    audio_mod     = _mod("av.audio")
    resampler_mod = _mod("av.audio.resampler")

    # av/__init__.py does: from av._core import time_base, library_versions, ...
    core_mod.time_base           = 0
    core_mod.library_versions    = lambda: {}
    core_mod.ffmpeg_version_info = (0, 0, 0)

    av_mod._core        = core_mod
    av_mod.audio        = audio_mod
    audio_mod.resampler = resampler_mod

    logger.warning(
        "av (PyAV) DLL failed to load; injected a stub module so "
        "faster-whisper can start.  numpy-array input works normally; "
        "audio file paths would not work."
    )


def _fix_ctranslate2_dlls():
    """
    ctranslate2's C extension sometimes fails to load on Windows
    (missing MKL/OpenMP DLLs, ABI mismatch, etc.), leaving the Python
    module importable but without its C-side classes.

    Strategy:
    1. Add ctranslate2's own directory to the OS DLL search path.
    2. Add every site-packages/nvidia/*/bin directory so that CUDA DLLs
       installed via pip (nvidia-cublas-cu12, nvidia-cuda-runtime-cu12,
       nvidia-cudnn-cu12 …) are visible to the Windows DLL loader.
    3. Evict any already-cached broken copy from sys.modules.
    4. Re-import ctranslate2 with the corrected search path.
    5. Scan faster_whisper/transcribe.py for every ctranslate2.Xxx and
       ctranslate2.models.Xxx reference; stub any that are still missing.
       These symbols appear only as type annotations (never instantiated
       during numpy-array transcription), so stubs are safe at runtime.
    """
    import sys, os, re, types, importlib.util

    # ── Step 1 & 2: register CUDA DLL directories ─────────────────────
    # ctranslate2 loads CUDA libs via LoadLibraryEx which may not respect
    # os.add_dll_directory(). The most reliable fix on Windows is to
    # prepend the directories to PATH so every DLL loader can find them.
    # We also call os.add_dll_directory() as a belt-and-suspenders measure.
    if not hasattr(_fix_ctranslate2_dlls, "_dll_handles"):
        _fix_ctranslate2_dlls._dll_handles = []

    def _add_dll(path):
        # 1. Prepend to PATH (works with all Windows DLL loaders)
        current_path = os.environ.get("PATH", "")
        if path not in current_path:
            os.environ["PATH"] = path + os.pathsep + current_path
        # 2. Also register via add_dll_directory (keep handle alive)
        if hasattr(os, "add_dll_directory"):
            try:
                h = os.add_dll_directory(path)
                _fix_ctranslate2_dlls._dll_handles.append(h)
            except OSError:
                pass
        logger.debug(f"[DLL] registered {path}")

    # ctranslate2 package dir
    spec = importlib.util.find_spec("ctranslate2")
    if spec and spec.submodule_search_locations:
        ct2_dir = str(list(spec.submodule_search_locations)[0])
        _add_dll(ct2_dir)

    # nvidia pip packages: site-packages/nvidia/<pkg>/bin/
    import site
    _nvidia_dirs_added = 0
    for sp in site.getsitepackages():
        nvidia_root = os.path.join(sp, "nvidia")
        if not os.path.isdir(nvidia_root):
            continue
        for entry in os.scandir(nvidia_root):
            if not entry.is_dir():
                continue
            bin_dir = os.path.join(entry.path, "bin")
            if os.path.isdir(bin_dir):
                _add_dll(bin_dir)
                _nvidia_dirs_added += 1
    if _nvidia_dirs_added == 0:
        logger.warning(
            "[DLL] site-packages/nvidia/ not found — CUDA DLLs not bundled. "
            "CUDA mode needs CUDA Toolkit installed on this machine, "
            "or re-run build_offline.ps1 -GPU cuda to bundle them."
        )

    # ── Step 2: evict broken cached module ────────────────────────
    ct2 = sys.modules.get("ctranslate2")
    if ct2 is not None and not hasattr(ct2, "StorageView"):
        for k in [k for k in sys.modules
                  if k == "ctranslate2" or k.startswith("ctranslate2.")]:
            del sys.modules[k]

    # ── Step 3: fresh import ──────────────────────────────────────
    import ctranslate2

    # Ensure ctranslate2.models sub-module exists
    if not hasattr(ctranslate2, "models"):
        ctranslate2.models = types.ModuleType("ctranslate2.models")
        sys.modules["ctranslate2.models"] = ctranslate2.models

    # ── Step 4: scan transcribe.py and stub every missing symbol ──
    fw_spec = importlib.util.find_spec("faster_whisper")
    source = ""
    if fw_spec and fw_spec.submodule_search_locations:
        tp = os.path.join(str(list(fw_spec.submodule_search_locations)[0]),
                          "transcribe.py")
        try:
            with open(tp, encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            pass

    _CT2_BROKEN_MSG = (
        "ctranslate2 C extension failed to load on this system.  "
        "Fix options:\n"
        "  1. pip install --force-reinstall ctranslate2>=4.0.0\n"
        "  2. Install VC++ Redistributable 2022 (x64) and retry:\n"
        "     https://aka.ms/vs/17/release/vc_redist.x64.exe"
    )

    def _make_ct2_stub(full_name: str):
        """Return a stub class that raises a helpful error when instantiated."""
        msg = _CT2_BROKEN_MSG
        fname = full_name

        class _Stub:
            def __init__(self, *a, **kw):
                raise RuntimeError(
                    f"{fname} could not be loaded — {msg}"
                )
            def __class_getitem__(cls, item):   # satisfy Optional[Stub]
                return cls

        _Stub.__name__ = full_name.split(".")[-1]
        _Stub.__qualname__ = full_name
        return _Stub

    stubbed: list = []
    # ctranslate2.models.Xxx
    for name in set(re.findall(r"ctranslate2\.models\.(\w+)", source)):
        if not hasattr(ctranslate2.models, name):
            setattr(ctranslate2.models, name, _make_ct2_stub(f"ctranslate2.models.{name}"))
            stubbed.append(f"ctranslate2.models.{name}")
    # ctranslate2.Xxx  (exclude the "models" sub-module itself)
    for name in set(re.findall(r"ctranslate2\.(?!models\b)(\w+)", source)):
        if not hasattr(ctranslate2, name):
            setattr(ctranslate2, name, _make_ct2_stub(f"ctranslate2.{name}"))
            stubbed.append(f"ctranslate2.{name}")

    if stubbed:
        logger.warning(
            "ctranslate2 C extension did not expose %d symbol(s); "
            "injected stubs for type annotations: %s.  "
            "Transcription works if ctranslate2 inference classes loaded correctly.  "
            "To fully fix: pip install --force-reinstall ctranslate2>=4.0.0",
            len(stubbed), ", ".join(sorted(stubbed)),
        )


# ── Engine state ───────────────────────────────────────────────────────────────

class EngineState(Enum):
    """引擎状态枚举，随 status 事件广播给客户端。"""

    STOPPED   = "stopped"
    NO_DEVICE = "no_device"
    IDLE      = "idle"
    LISTENING = "listening"


# ── Main engine ────────────────────────────────────────────────────────────────

class SpeechEngine:
    """
    Whisper 批量 ASR 引擎：openWakeWord 唤醒 + Silero VAD 判停 + faster-whisper。
    """

    def __init__(self, config: dict, event_callback: Callable[[dict], None]):
        """
        Args:
            config: 完整 config.json 字典
            event_callback: 引擎产生事件时调用 callback(event_dict)
        """
        self.config = config
        self.emit   = event_callback
        self.state  = EngineState.STOPPED

        self._stop_event     = threading.Event()
        self._trigger_listen = threading.Event()
        self._cancel_listen  = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Model cache — persists across stop/start so restart is instant
        self._asr_model:        Optional[object] = None
        self._model_key:        Optional[tuple]  = None   # (name, device, compute_type)
        self._ww_detector:      Optional[object] = None
        self._ww_key:           Optional[tuple]  = None
        self._preload_lock      = threading.Lock()
        self._suppress_input_until: float        = 0.0
        # After wake_word: optionally pause scan until client sends listen.
        self._wake_paused_until_listen: bool     = False
        self._last_wake_emit_at: float           = 0.0

    # ── Public control API ────────────────────────────────────────────────────

    def start(self):
        """启动后台音频线程；重置 pause_until_listen 标志。"""
        if self._thread and self._thread.is_alive():
            logger.warning("Engine already running")
            return
        self._stop_event.clear()
        self._wake_paused_until_listen = False
        self._thread = threading.Thread(
            target=self._run_safe, daemon=True, name="SpeechEngine-Whisper"
        )
        self._thread.start()
        logger.info("Whisper engine started")

    def stop(self):
        """停止线程；Whisper/唤醒模型缓存保留在内存。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._set_state(EngineState.STOPPED)
        logger.info("Whisper engine stopped")

    def trigger_listen(self):
        """进入 LISTENING：清除唤醒暂停、resume 检测器、开始录音识别。"""
        self._wake_paused_until_listen = False
        det = self._ww_detector
        if det is not None and hasattr(det, "resume"):
            det.resume()
        self._trigger_listen.set()

    def cancel_listen(self):
        """取消当前 LISTENING，回到 IDLE。"""
        self._cancel_listen.set()

    def suppress_input(self, duration_ms: int = 1500):
        """在 duration_ms 内忽略麦克风输入（TTS 回声防护）。"""
        ms = max(0, int(duration_ms))
        self._suppress_input_until = time.time() + ms / 1000.0
        logger.debug(f"Input suppressed for {ms} ms")

    def update_config(self, key: str, value) -> bool:
        """运行时更新 config；wake_word.* / whisper.model 等会 invalidate 缓存。"""
        try:
            parts = key.split(".")
            cfg   = self.config
            for part in parts[:-1]:
                cfg = cfg[part]
            if key in (
                "whisper.max_silence_ms",
                "whisper.min_listen_ms",
                "whisper.max_listen_ms",
                "whisper.partial_interval_ms",
                "whisper.vad_cooldown_ms",
                "whisper.vad_min_speech_ms",
                "whisper.post_wake_grace_ms",
                "postprocess.post_wake_grace_ms",
                "whisper.silero_threshold",
                "whisper.silero_speech_fraction",
            ) and value is not None:
                if key in (
                    "whisper.silero_threshold",
                    "whisper.silero_speech_fraction",
                ):
                    value = float(value)
                else:
                    value = int(float(value))
            cfg[parts[-1]] = value
            if key in ("whisper.post_wake_grace_ms", "postprocess.post_wake_grace_ms"):
                self.config.setdefault("whisper", {})["post_wake_grace_ms"] = value
                self.config.setdefault("postprocess", {})["post_wake_grace_ms"] = value
            if key == "whisper.verbatim" and value is not None:
                value = bool(value)
            logger.info(f"Config updated: {key} = {value!r}")
            # Invalidate cached wake word detector so it rebuilds on next start
            if key.startswith("wake_word."):
                self._ww_key = None
            # Whisper model params that need a full model reload
            if key in ("whisper.model", "whisper.device", "whisper.compute_type"):
                self._model_key = None   # force reload on next start()
                self._ww_key    = None   # wake word detector references the model
            # Sync initial_prompt immediately so in-flight sessions pick it up
            if key == "whisper.initial_prompt":
                self._initial_prompt = value
            return True
        except (KeyError, TypeError):
            logger.warning(f"Invalid config key: {key!r}")
            return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _set_state(self, state: EngineState):
        """切换状态并广播 status；从 LISTENING 回 IDLE 时 reset 唤醒检测器。"""
        prev = self.state
        self.state = state
        if state == EngineState.IDLE and prev == EngineState.LISTENING:
            det = self._ww_detector
            if det is not None and hasattr(det, "reset"):
                det.reset()
                logger.debug("[WakeWord] detector reset (back to idle after listening)")
        ww = self.config["wake_word"]
        w  = self.config.get("whisper", {})
        _pp = get_postprocess_config(self.config)
        self.emit({
            "event":                 "status",
            "state":                 state.value,
            "wake_word_enabled":     ww.get("enabled", True),
            "keywords":              ww.get("keywords", []),
            "mode":                  "openwakeword",
            "whisper_max_silence_ms": w.get("max_silence_ms", 2500),
            "whisper_min_listen_ms":  w.get("min_listen_ms", 600),
            "whisper_max_listen_ms":  w.get("max_listen_ms", 30000),
            "post_wake_grace_ms":     int(_pp.get("post_wake_grace_ms", 1500)),
            "ts":                    time.time(),
        })

    def _run_safe(self):
        """_run 的异常包装：崩溃时 emit error 并将 state 置 STOPPED。"""
        try:
            self._run()
        except Exception as exc:
            logger.error(f"Whisper engine crashed: {exc}", exc_info=True)
            self.emit({
                "event": "error", "code": "engine_crash",
                "message": str(exc), "ts": time.time(),
            })
            self.state = EngineState.STOPPED

    def _transcribe_options(self) -> dict:
        """从 config 组装 Whisper transcribe() 参数字典（verbatim / domain prompt）。"""
        w = self.config.get("whisper", {})
        verbatim = bool(w.get("verbatim", True))
        prompt = _resolve_asr_initial_prompt(w)
        temperature = 0.0 if verbatim else float(w.get("temperature", 0.0))

        return {
            "task":                       "transcribe",
            "beam_size":                  int(w.get("beam_size", 5)),
            "vad_filter":                 False,
            "word_timestamps":            False,
            "initial_prompt":             prompt,
            "condition_on_previous_text": False,
            "temperature":                temperature,
            "no_speech_threshold":        float(w.get("no_speech_threshold", 0.5)),
            "log_prob_threshold":           float(w.get("log_prob_threshold", -0.9)),
            "compression_ratio_threshold":  float(w.get("compression_ratio_threshold", 2.0)),
            "hallucination_silence_threshold": float(
                w.get("hallucination_silence_threshold", 1.5)
            ),
        }

    def _do_transcribe(
        self,
        whisper_model,
        buf: List[bytes],
        language: Optional[str],
    ) -> str:
        """对缓冲音频跑 Whisper，返回 strip 后的转写文本（空串表示无有效结果）。"""
        if not buf:
            return ""
        audio_f32 = _buffer_to_float32(buf)
        # Skip clips shorter than 0.3 s to avoid spurious output
        if len(audio_f32) < _WHISPER_SAMPLE_RATE * 0.3:
            return ""
        # Skip nearly-silent buffers — Whisper hallucinates on silence
        level = float(np.sqrt(np.mean(audio_f32 ** 2)))
        if level < 0.003:
            logger.warning(
                "Whisper skip: buffer too quiet (level=%.4f, chunks=%d)",
                level,
                len(buf),
            )
            return ""
        try:
            opts = self._transcribe_options()
            w = self.config.get("whisper", {})
            lang = language if language is not None else w.get("language")
            if lang in ("", "null", "None"):
                lang = None
            prompt = opts.get("initial_prompt")
            segments, _ = whisper_model.transcribe(
                audio_f32,
                language=lang,
                **opts,
            )
            segs_list = list(segments)
            audio_sec = len(audio_f32) / float(_WHISPER_SAMPLE_RATE)
            segs_list = _filter_whisper_segments(segs_list, w)
            # no_speech_prob 高 ≈ 模型认为更像静音/噪声（过严会导致 raw=''）
            _ASR_MAX_NS = float(w.get("asr_max_no_speech_prob", 0.58))
            parts = []
            dropped: List[tuple] = []
            for seg in segs_list:
                ns = float(getattr(seg, "no_speech_prob", 0.0))
                txt = (seg.text or "").strip()
                if ns < _ASR_MAX_NS:
                    parts.append(seg.text)
                elif txt:
                    dropped.append((txt[:40], ns))
            merged = "".join(parts).strip()
            merged = _apply_short_audio_runon_guard(merged, segs_list, audio_sec, w)
            if not merged and segs_list:
                best = min(
                    segs_list,
                    key=lambda s: float(getattr(s, "no_speech_prob", 1.0)),
                )
                best_ns = float(getattr(best, "no_speech_prob", 1.0))
                best_txt = (best.text or "").strip()
                _FB_MAX_NS = 0.72
                if best_txt and best_ns < _FB_MAX_NS:
                    merged = best_txt
                    logger.debug(
                        "ASR fallback segment (no_speech_prob=%.2f): %r",
                        best_ns,
                        best_txt[:80],
                    )
            if not merged and segs_list:
                logger.debug(
                    "Whisper %d segment(s) all empty/filtered (level=%.4f), "
                    "dropped_sample=%s",
                    len(segs_list),
                    level,
                    dropped[:3],
                )
            # Filter against full domain keyword list (even if ASR used short prompt only)
            raw_kw = _domain_vocab_text(w)
            echo_prompt = raw_kw or prompt
            if echo_prompt and merged and _text_echoes_initial_prompt(merged, echo_prompt):
                logger.info(
                    "Dropped prompt-echo hallucination (not real speech): %r",
                    merged[:80] + ("…" if len(merged) > 80 else ""),
                )
                return ""
            return merged
        except Exception as exc:
            logger.warning(
                "Whisper inference error (%s): %s",
                type(exc).__name__,
                exc,
            )
            return ""

    def _postprocess_text(self, text: str) -> str:
        """调用 text_postprocess.postprocess_transcript 做后处理。"""
        cfg = get_postprocess_config(self.config)
        logger.info("post转换前识别结果:%s" , text)
        result_str = postprocess_transcript(text, cfg)
        logger.info("post转换前识别结果:%s" , result_str)
        return result_str

    def _create_whisper_model(
        self, model_name: str, device: str, compute_type: str
    ):
        """加载 Whisper 模型；离线时优先本地 HF 快照，避免联网下载。"""
        _stub_av_if_needed()
        _fix_ctranslate2_dlls()
        from faster_whisper import WhisperModel

        manifest = read_bundled_model_manifest()
        if manifest and manifest != model_name:
            logger.warning(
                "config whisper.model=%r does not match bundled model %r; using bundled.",
                model_name,
                manifest,
            )
            model_name = manifest

        name, local_path = pick_whisper_model(model_name)
        kw = dict(device=device, compute_type=compute_type)
        if local_path is not None:
            logger.info(f"[preload] Local Whisper weights: {local_path}")
            return WhisperModel(str(local_path), local_files_only=True, **kw)
        if is_hub_offline():
            raise RuntimeError(offline_model_error_message(name))
        logger.info(
            "[preload] No local snapshot under HF_HOME; may download from HuggingFace Hub."
        )
        return WhisperModel(name, local_files_only=False, **kw)

    def _finalize(
        self,
        whisper_model,
        buf: List[bytes],
        language: Optional[str],
        reason: str,
        *,
        speech_vad: Any = None,
    ):
        """
        LISTENING 结束：裁剪首尾静音/回声 → Whisper 转写 → 后处理 → 发事件。

        依次 emit transcript（若有）或 transcript_empty，最后 listening_end。
        """
        eth = float(self.config.get("audio", {}).get("energy_threshold", 0.02))
        w = self.config.get("whisper", {})
        vad_frac = silero_speech_fraction_from_config(w)
        original = list(buf)
        trimmed = _trim_leading_echo(buf, eth)
        after_leading = trimmed
        if len(trimmed) < len(buf):
            logger.debug(
                "Trimmed %d leading echo chunk(s) before ASR (speaker bleed)",
                len(buf) - len(trimmed),
            )
        if speech_vad is not None:
            before = len(trimmed)
            trimmed = trim_trailing_silence_chunks(
                trimmed,
                speech_vad,
                _WHISPER_SAMPLE_RATE,
                min_fraction=vad_frac * 0.75,
            )
            if len(trimmed) < before:
                logger.debug(
                    "Trimmed %d trailing silent chunk(s) before ASR",
                    before - len(trimmed),
                )
        l_trim = _buffer_level(trimmed)
        l_lead = _buffer_level(after_leading)
        l_orig = _buffer_level(original)
        if l_trim < 0.003 and l_lead >= 0.003:
            logger.debug(
                "ASR trim revert: trailing VAD left silence (%.4f -> %.4f)",
                l_trim,
                l_lead,
            )
            trimmed = after_leading
        if _buffer_level(trimmed) < 0.003 and l_orig >= 0.003:
            logger.debug(
                "ASR trim revert: using full buffer (%.4f -> %.4f)",
                _buffer_level(trimmed),
                l_orig,
            )
            trimmed = original
        dur_s = sum(len(c) for c in trimmed) / (_WHISPER_SAMPLE_RATE * 2)
        logger.info(
            "Listening end (%s): %.2f s audio buffered for Whisper",
            reason,
            dur_s,
        )
        raw = self._do_transcribe(whisper_model, trimmed, language)
        text = self._postprocess_text(raw)
        if raw and not text:
            logger.debug(f"Transcript suppressed: {raw!r}")
        if text:
            self.emit({
                "event": "transcript",
                "text":  text, "is_final": True, "ts": time.time(),
            })
        elif dur_s >= 0.35:
            lvl = _buffer_level(trimmed)
            hint = ""
            if dur_s < float(w.get("min_transcribe_sec", 1.5)):
                hint = (
                    f" — audio shorter than min_transcribe_sec="
                    f"{w.get('min_transcribe_sec', 1.5)}s; speak longer or lower Silero strictness"
                )
            elif lvl < 0.008:
                hint = " — audio very quiet; check mic gain / energy_threshold"
            logger.warning(
                "Listening end (%s): no transcript (%.2f s, level=%.4f, raw=%r)%s",
                reason,
                dur_s,
                lvl,
                (raw[:120] + "…") if raw and len(raw) > 120 else raw,
                hint,
            )
            self.emit({
                "event": "transcript_empty",
                "reason": reason,
                "duration_s": round(dur_s, 2),
                "ts": time.time(),
            })
        self.emit({"event": "listening_end", "reason": reason, "ts": time.time()})

    # ── Model management ─────────────────────────────────────────────────────

    def preload(self):
        """
        预加载 Whisper 与唤醒检测器（server 启动时在后台线程调用）。

        幂等：config 未变时再次调用几乎无开销。
        """
        self._ensure_models_loaded()

    def _ensure_models_loaded(self):
        """带锁的模型加载入口；供 preload() 与 _run() 调用。"""
        with self._preload_lock:
            self._ensure_models_loaded_unlocked()

    def _ensure_models_loaded_unlocked(self):
        """
        实际加载/复用 Whisper 与唤醒检测器。

        按 (model, device, compute_type) 与 wake_word 配置做缓存；
        config 变更时对应 invalidate 并重建。
        """
        cfg_whisper = self.config.get("whisper", {})
        cfg_ww      = self.config["wake_word"]

        whisper_model_name: str           = cfg_whisper.get("model", "base")
        whisper_device:     str           = cfg_whisper.get("device", "cpu")
        compute_type:       str           = cfg_whisper.get("compute_type", "int8")
        language:           Optional[str] = cfg_whisper.get("language")
        keywords:           List[str]     = cfg_ww.get("keywords", ["hey jarvis"])
        sensitivity:        float         = cfg_ww.get("sensitivity", 0.5)

        # ── Whisper model ──────────────────────────────────────────────────
        model_key = (whisper_model_name, whisper_device, compute_type)
        if self._asr_model is not None and self._model_key == model_key:
            logger.info(
                f"[preload] Whisper model '{whisper_model_name}' already loaded — skipping."
            )
            asr_model = self._asr_model
        else:
            logger.info(
                f"[preload] Loading Whisper model '{whisper_model_name}' "
                f"(device={whisper_device}, compute_type={compute_type}) …"
            )
            try:
                asr_model = self._create_whisper_model(
                    whisper_model_name, whisper_device, compute_type
                )
                _dummy = np.zeros(int(_WHISPER_SAMPLE_RATE * 0.1), dtype=np.float32)
                list(asr_model.transcribe(_dummy, beam_size=1)[0])
            except Exception as exc:
                _exc_s = str(exc).lower()
                _is_cuda_dll   = any(k in _exc_s for k in ("cublas", "cudnn", "libcuda", "cuda"))
                _is_compute    = any(k in _exc_s for k in ("compute type", "float16", "not supported"))
                if _is_cuda_dll or _is_compute:
                    if _is_compute and not _is_cuda_dll and whisper_device == "cuda":
                        logger.warning(
                            f"[preload] compute_type={compute_type!r} not supported on this GPU "
                            f"({exc}); retrying with int8 on cuda."
                        )
                        try:
                            asr_model = self._create_whisper_model(
                                whisper_model_name, "cuda", "int8"
                            )
                            compute_type = "int8"
                            logger.info("[preload] cuda+int8 fallback succeeded.")
                        except Exception as exc2:
                            logger.warning(f"[preload] cuda+int8 also failed ({exc2}); falling back to cpu.")
                            compute_type   = "float32"
                            whisper_device = "cpu"
                            asr_model = self._create_whisper_model(
                                whisper_model_name, "cpu", "float32"
                            )
                    else:
                        logger.warning(
                            f"[preload] compute_type={compute_type!r} triggered a CUDA "
                            f"dependency ({exc}); falling back to float32 on cpu."
                        )
                        compute_type   = "float32"
                        whisper_device = "cpu"
                        asr_model = self._create_whisper_model(
                            whisper_model_name, "cpu", "float32"
                        )
                        logger.info("[preload] cpu+float32 fallback succeeded.")
                else:
                    self.emit({
                        "event":   "error",
                        "code":    "model_not_found",
                        "message": str(exc),
                        "ts":      time.time(),
                    })
                    raise
            self._asr_model   = asr_model
            self._model_key   = (whisper_model_name, whisper_device, compute_type)
            self._ww_detector = None   # model changed → force WW rebuild
            lang_desc = language if language else "auto/mixed (Chinese + English)"
            logger.info(
                f"[preload] Whisper model ready — "
                f"device={whisper_device} compute_type={compute_type}  language={lang_desc}"
            )

        # ── Wake word detector ─────────────────────────────────────────────
        ww_enabled = cfg_ww.get("enabled", True)
        ww_opts    = get_wake_word_options(cfg_ww)
        ww_key = (
            tuple(ww_opts["keywords"]),
            sensitivity,
            tuple(ww_opts["oww_models"]),
            ww_opts["oww_inference_framework"],
            ww_opts["oww_vad_threshold"],
            ww_opts["oww_debounce_sec"],
        )
        if self._ww_detector is not None and self._ww_key == ww_key:
            logger.info("[preload] Wake word detector already loaded — skipping.")
        else:
            ww_detector = None
            if ww_enabled and ww_opts["keywords"]:
                kw = ww_opts["keywords"]
                try:
                    oww_models = ww_opts["oww_models"] or None
                    ww_detector = OpenWakeWordWakeWordDetector(
                        kw,
                        _WHISPER_SAMPLE_RATE,
                        oww_models=oww_models,
                        sensitivity=sensitivity,
                        inference_framework=ww_opts["oww_inference_framework"],
                        vad_threshold=ww_opts["oww_vad_threshold"],
                        debounce_sec=ww_opts["oww_debounce_sec"],
                    )
                except Exception as exc:
                    logger.error("[preload] Wake word init failed: %s", exc)
                    self.emit({
                        "event":   "error",
                        "code":    "wake_word_init_failed",
                        "message": str(exc),
                        "ts":      time.time(),
                    })
            self._ww_detector = ww_detector
            self._ww_key      = ww_key
            logger.info(
                "[preload] Wake word: " + ("enabled" if ww_enabled else "disabled")
                + (" [openwakeword]" if ww_enabled else "")
            )

    # ── Main audio pipeline ───────────────────────────────────────────────────

    def _run(self):
        """
        主音频循环（在后台线程运行）。

        打开麦克风 → IDLE 唤醒扫描 / LISTENING 缓冲与 Silero 判停 →
        热插拔重试、partial 后台线程、wake gating 等。
        """
        import sounddevice as sd

        cfg_whisper = self.config.get("whisper", {})
        cfg_ww      = self.config["wake_word"]
        cfg_audio   = self.config["audio"]

        # Audio settings – Whisper requires 16 kHz; override whatever the user set
        sample_rate:       int   = _WHISPER_SAMPLE_RATE
        chunk_size:        int   = cfg_audio.get("chunk_size", 4000)
        energy_threshold:  float = cfg_audio.get("energy_threshold", 0.02)
        device                   = cfg_audio.get("device") or None
        input_channels:    int   = int(cfg_audio.get("input_channels", 1))
        stereo_mode:       str   = str(cfg_audio.get("stereo_mode", "mix"))
        if input_channels not in (1, 2):
            input_channels = 1

        # Whisper-specific settings – read from self.config at each session start
        # so that runtime config updates (via WebSocket "config" command) take
        # effect immediately without restarting the engine.
        def _wparams():
            w = self.config.get("whisper", {})
            partial = int(w.get("partial_interval_ms", 0))
            if w.get("verbatim", True):
                partial = 0
            return (
                w.get("language"),
                w.get("max_silence_ms",      2000),
                w.get("max_listen_ms",       30000),
                partial,
                w.get("vad_cooldown_ms",     500),
                w.get("vad_min_speech_ms",   200),
                w.get("min_listen_ms",       1000),
                float(w.get("min_transcribe_sec", 1.5)),
                silero_threshold_from_config(w),
                silero_speech_fraction_from_config(w),
            )

        (language, max_silence_ms, max_listen_ms,
         partial_interval_ms, vad_cooldown_ms,
         vad_min_speech_ms, min_listen_ms, min_transcribe_sec,
         silero_threshold,
         silero_speech_fraction) = _wparams()
        chunk_ms = max(1.0, chunk_size / sample_rate * 1000.0)

        # ── Load / reuse cached models ────────────────────────────────────────
        # (preload() at server startup means this is a no-op on start() calls)
        self._ensure_models_loaded()
        asr_model = self._asr_model
        # NOTE: ww_detector / resolved_mode / ww_enabled are read dynamically from
        # self in the inner IDLE loop so that runtime config changes (keyword update,
        # mode change, enable/disable) take effect immediately without engine restart.

        # ── Hot-plug device loop ──────────────────────────────────────────────
        while not self._stop_event.is_set():

            # Re-enumerate audio devices so hot-plugged mics are visible
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass

            audio_q: queue.Queue = queue.Queue(maxsize=200)

            def _audio_cb(indata, frames, time_info, status, _q=audio_q):
                if status:
                    logger.debug(f"Audio status: {status}")
                if _q.full():
                    try:
                        _q.get_nowait()
                    except queue.Empty:
                        pass
                pcm = _to_mono_pcm16(bytes(indata), input_channels, stereo_mode)
                _q.put(pcm)

            try:
                stream = sd.RawInputStream(
                    samplerate = sample_rate,
                    blocksize  = chunk_size,
                    device     = device,
                    dtype      = "int16",
                    channels   = input_channels,
                    callback   = _audio_cb,
                )
            except Exception as exc:
                if self.state != EngineState.NO_DEVICE:
                    logger.warning(f"Cannot open microphone: {exc}")
                    self.emit({
                        "event": "error", "code": "no_device",
                        "message": (
                            f"Microphone not available: {exc}.  "
                            f"Retrying every {int(_DEVICE_RETRY_SEC)} s…"
                        ),
                        "ts": time.time(),
                    })
                    self._set_state(EngineState.NO_DEVICE)
                if self._stop_event.wait(_DEVICE_RETRY_SEC):
                    break
                continue

            # ── Per-device audio processing loop ─────────────────────────────
            device_error: Optional[str] = None

            # Partial inference coordination
            _partial_lock       = threading.Lock()
            _partial_running    = [False]
            _last_partial_text  = [""]
            _last_partial_ts    = [0.0]

            def _run_partial_bg(buf_snapshot: List[bytes]):
                raw = self._do_transcribe(asr_model, buf_snapshot, language)
                text = self._postprocess_text(raw)
                with _partial_lock:
                    _partial_running[0] = False
                if text and text != _last_partial_text[0]:
                    _last_partial_text[0] = text
                    self.emit({"event": "partial", "text": text, "ts": time.time()})

            try:
                with stream:
                    logger.info(
                        "Microphone open.  Whisper engine running.  "
                        f"stream={input_channels}ch->{stereo_mode if input_channels > 1 else 'mono'} "
                        f"@ {sample_rate} Hz"
                    )
                    self._set_state(EngineState.IDLE)

                    listen_buf:    List[bytes]     = []
                    listen_start:  Optional[float] = None
                    silence_start: Optional[float] = None
                    # VAD-mode interaction state
                    _vad_speech_since:   Optional[float] = None  # when sustained speech started
                    _vad_cooldown_until: float            = 0.0  # no new VAD trigger before this
                    _input_grace_until:  float            = 0.0  # skip buffering (TTS echo after wake)
                    _listen_peak:        float            = 0.0
                    _listen_noise_floor: float            = energy_threshold * 0.5
                    _listen_silent_chunks: int           = 0
                    _listen_clock_after_grace: bool        = False
                    _speech_vad = create_silero_vad(
                        silero_threshold, sample_rate=sample_rate
                    )
                    if _speech_vad is None:
                        logger.error(
                            "[VAD] Silero unavailable; install silero-vad + onnxruntime"
                        )
                        self.emit({
                            "event": "error",
                            "code": "silero_vad_unavailable",
                            "message": (
                                "Silero VAD 不可用，无法判停录音。"
                                "请安装 silero-vad 与 onnxruntime。"
                            ),
                            "ts": time.time(),
                        })
                    _ww_gate = WakeUtteranceGate()
                    _ww_gate_cfg = get_wake_word_options(
                        self.config["wake_word"]
                    )

                    while not self._stop_event.is_set():

                        if not stream.active:
                            device_error = "Stream became inactive (device removed?)"
                            break

                        try:
                            audio_bytes = audio_q.get(timeout=0.2)
                        except queue.Empty:
                            # Check timeout even when queue is empty
                            if (self.state == EngineState.LISTENING
                                    and listen_start is not None):
                                (language, max_silence_ms, max_listen_ms,
                                 partial_interval_ms, vad_cooldown_ms,
                                 vad_min_speech_ms, min_listen_ms, min_transcribe_sec,
                                 silero_threshold,
                                 silero_speech_fraction) = _wparams()
                                if (time.time() - listen_start) * 1000 >= max_listen_ms:
                                    self._finalize(
                                        asr_model, listen_buf, language, "timeout",
                                        speech_vad=_speech_vad,
                                    )
                                    listen_buf    = []
                                    listen_start  = None
                                    silence_start = None
                                    _last_partial_text[0] = ""
                                    _last_partial_ts[0]   = 0.0
                                    _vad_speech_since   = None
                                    _listen_peak    = 0.0
                                    _listen_noise_floor = energy_threshold * 0.5
                                    _vad_cooldown_until = time.time() + vad_cooldown_ms / 1000
                                    _reset_speech_vad(_speech_vad)
                                    self._set_state(EngineState.IDLE)
                            continue

                        # 每帧重读 config，使 WebSocket 修改的静音/时长参数在本轮录音中立即生效
                        (language, max_silence_ms, max_listen_ms,
                         partial_interval_ms, vad_cooldown_ms,
                         vad_min_speech_ms, min_listen_ms, min_transcribe_sec,
                         silero_threshold,
                         silero_speech_fraction) = _wparams()
                        _silence_chunks_needed = max(
                            3, int(max_silence_ms / chunk_ms)
                        )

                        audio_np = np.frombuffer(audio_bytes, dtype=np.int16)
                        level = float(
                            np.sqrt(np.mean(audio_np.astype(np.float32) ** 2))
                        ) / 32768.0

                        # ── IDLE ──────────────────────────────────────────────
                        if self.state == EngineState.IDLE:

                            if self._trigger_listen.is_set():
                                self._trigger_listen.clear()
                                listen_buf    = [audio_bytes]
                                listen_start  = time.time()
                                silence_start = None
                                _last_partial_text[0] = ""
                                _last_partial_ts[0]   = 0.0
                                _vad_speech_since   = None
                                _vad_cooldown_until = 0.0
                                _input_grace_until  = 0.0
                                _listen_peak    = level
                                _listen_noise_floor = min(energy_threshold * 0.5, level)
                                self.emit({
                                    "event": "listening_start",
                                    "trigger": "manual", "ts": time.time(),
                                })
                                _reset_speech_vad(_speech_vad)
                                self._set_state(EngineState.LISTENING)
                                continue

                            # Read detector state fresh every chunk — picks up any
                            # changes applied by preload() running in a background thread.
                            _ww_det     = self._ww_detector
                            _ww_enabled = self.config["wake_word"].get("enabled", True)

                            if _ww_enabled and not self._wake_paused_until_listen:
                                _ww_gate.push(
                                    audio_bytes,
                                    level,
                                    energy_threshold=energy_threshold,
                                    speech_vad=_speech_vad,
                                    sample_rate=sample_rate,
                                    speech_fraction=silero_speech_fraction,
                                    ww_cfg=_ww_gate_cfg,
                                )

                            if (
                                _ww_enabled
                                and _ww_det is not None
                                and not self._wake_paused_until_listen
                            ):
                                detected = _ww_det.process(audio_bytes)
                                logger.info(f"try process wake_word : bytes {len(audio_bytes)}   ,result: {detected}")

                                if detected:
                                    ok, why = _ww_gate.may_accept_wake(
                                        _ww_gate_cfg,
                                        chunk_ms=chunk_ms,
                                        energy_threshold=energy_threshold,
                                    )
                                    if not ok:
                                        logger.info(
                                            "Wake phrase matched but ignored "
                                            "(%s): %r",
                                            why,
                                            detected,
                                        )
                                        if hasattr(_ww_det, "reset"):
                                            _ww_det.reset()
                                    else:
                                        ww_opts_now = get_wake_word_options(
                                            self.config["wake_word"]
                                        )
                                        cooldown_ms = int(
                                            ww_opts_now.get(
                                                "wake_repeat_cooldown_ms", 1500
                                            )
                                        )
                                        if cooldown_ms > 0:
                                            since = time.time() - self._last_wake_emit_at
                                            if since < cooldown_ms / 1000.0:
                                                if hasattr(_ww_det, "reset"):
                                                    _ww_det.reset()
                                                continue
                                        logger.info(f"Wake word: '{detected}'")
                                        self.emit({
                                            "event": "wake_word",
                                            "keyword": detected,
                                            "score": float(
                                                getattr(_ww_det, "last_score", 1.0)
                                            ),
                                            "ts": time.time(),
                                        })
                                        self._last_wake_emit_at = time.time()
                                        if ww_opts_now.get("pause_until_listen", False):
                                            self._wake_paused_until_listen = True
                                            if hasattr(_ww_det, "pause"):
                                                _ww_det.pause()
                                            logger.info(
                                                "Wake detected — wake-word scan "
                                                "paused (send cmd listen to resume)."
                                            )
                                        else:
                                            logger.info(
                                                "Wake detected — wake-word scan "
                                                "continues (repeat wake enabled)."
                                            )
                                        _ww_gate.reset()

                        # ── LISTENING ─────────────────────────────────────────
                        elif self.state == EngineState.LISTENING:

                            if self._cancel_listen.is_set():
                                self._cancel_listen.clear()
                                self.emit({
                                    "event": "listening_end",
                                    "reason": "cancelled", "ts": time.time(),
                                })
                                listen_buf    = []
                                listen_start  = None
                                silence_start = None
                                _last_partial_text[0] = ""
                                _last_partial_ts[0]   = 0.0
                                _vad_speech_since   = None
                                _input_grace_until  = 0.0
                                _vad_cooldown_until = time.time() + vad_cooldown_ms / 1000
                                _reset_speech_vad(_speech_vad)
                                self._set_state(EngineState.IDLE)
                                continue

                            # Hard timeout
                            if (listen_start is not None
                                    and (time.time() - listen_start) * 1000 >= max_listen_ms):
                                self._finalize(
                                    asr_model, listen_buf, language, "timeout",
                                    speech_vad=_speech_vad,
                                )
                                listen_buf    = []
                                listen_start  = None
                                silence_start = None
                                _last_partial_text[0] = ""
                                _last_partial_ts[0]   = 0.0
                                _vad_speech_since   = None
                                _input_grace_until  = 0.0
                                _listen_clock_after_grace = False
                                _listen_silent_chunks = 0
                                _listen_peak    = 0.0
                                _listen_noise_floor = energy_threshold * 0.5
                                _vad_cooldown_until = time.time() + vad_cooldown_ms / 1000
                                _reset_speech_vad(_speech_vad)
                                self._set_state(EngineState.IDLE)
                                continue

                            # Skip buffering during post-wake / client TTS (e.g.「我在」)
                            _now = time.time()
                            if (_now < _input_grace_until
                                    or _now < self._suppress_input_until):
                                # Still record audio (wake后马上说话)，仅跳过 VAD 判停
                                listen_buf.append(audio_bytes)
                                _listen_peak = max(_listen_peak, level)
                                silence_start = None
                                _listen_silent_chunks = 0
                                continue

                            if _listen_clock_after_grace:
                                listen_start = _now
                                _listen_clock_after_grace = False

                            w_cfg = self.config.get("whisper", {})
                            silero_end_frac = float(
                                w_cfg.get(
                                    "silero_end_speech_fraction",
                                    silero_speech_fraction * 0.65,
                                )
                            )
                            silero_end_frac = max(
                                0.08,
                                min(silero_end_frac, silero_speech_fraction),
                            )
                            _listen_peak = max(_listen_peak, level)
                            listen_buf.append(audio_bytes)
                            _listen_buf_sec = sum(
                                len(c) for c in listen_buf
                            ) / (_WHISPER_SAMPLE_RATE * 2)
                            if level < _listen_peak * 0.55:
                                _listen_noise_floor = min(
                                    _listen_noise_floor,
                                    level * 0.85 + _listen_noise_floor * 0.15,
                                )
                            still_speaking = _still_speaking_for_end(
                                audio_bytes,
                                _speech_vad,
                                sample_rate,
                                silero_end_frac,
                            )

                            if still_speaking:
                                silence_start = None
                                _listen_silent_chunks = 0
                            else:
                                if silence_start is None:
                                    silence_start = _now
                                _listen_silent_chunks += 1
                                silent_ms = (_now - silence_start) * 1000
                                if (listen_start is not None
                                        and (time.time() - listen_start) * 1000
                                        >= min_listen_ms
                                        and _listen_buf_sec >= min_transcribe_sec
                                        and (
                                            _listen_silent_chunks
                                            >= _silence_chunks_needed
                                            or silent_ms >= max_silence_ms
                                        )):
                                    logger.debug(
                                        "Listening end (silero: %d chunks "
                                        "~%d ms, wall %d ms)",
                                        _listen_silent_chunks,
                                        int(_listen_silent_chunks * chunk_ms),
                                        int(silent_ms),
                                    )
                                    self._finalize(
                                        asr_model, listen_buf, language, "silence",
                                        speech_vad=_speech_vad,
                                    )
                                    listen_buf    = []
                                    listen_start  = None
                                    silence_start = None
                                    _last_partial_text[0] = ""
                                    _last_partial_ts[0]   = 0.0
                                    _vad_speech_since   = None
                                    _input_grace_until  = 0.0
                                    _listen_clock_after_grace = False
                                    _listen_silent_chunks = 0
                                    _listen_peak    = 0.0
                                    _listen_noise_floor = energy_threshold * 0.5
                                    _vad_cooldown_until = time.time() + vad_cooldown_ms / 1000
                                    _reset_speech_vad(_speech_vad)
                                    self._set_state(EngineState.IDLE)
                                    continue

                            # Periodic partial inference (background thread)
                            now = _now
                            if (partial_interval_ms > 0 and listen_buf
                                    and (now - _last_partial_ts[0]) * 1000 >= partial_interval_ms):
                                with _partial_lock:
                                    if not _partial_running[0]:
                                        _partial_running[0] = True
                                        _last_partial_ts[0] = now
                                        buf_snap = list(listen_buf)
                                        threading.Thread(
                                            target  = _run_partial_bg,
                                            args    = (buf_snap,),
                                            daemon  = True,
                                            name    = "WhisperPartial",
                                        ).start()

            except Exception as exc:
                device_error = str(exc)

            # ── Device gone or stop requested ─────────────────────────────────
            if self._stop_event.is_set():
                break

            msg = device_error or "Microphone disconnected."
            logger.warning(f"Stream ended: {msg}")
            self.emit({
                "event": "error", "code": "no_device",
                "message": f"{msg}  Retrying every {int(_DEVICE_RETRY_SEC)} s…",
                "ts": time.time(),
            })
            self._set_state(EngineState.NO_DEVICE)
            if self._stop_event.wait(_DEVICE_RETRY_SEC):
                break

        self._set_state(EngineState.STOPPED)
