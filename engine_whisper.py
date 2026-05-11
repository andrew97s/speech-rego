"""
Whisper-based audio pipeline engine: microphone -> wake word -> ASR -> events

ASR backend: faster-whisper (CTranslate2), supports mixed Chinese/English
             with language=None (auto-detect per session).

Wake word detection:
  vosk    -- Vosk grammar/keyword-spotting (Chinese custom keywords)
  whisper -- Whisper-based keyword spotting (any language, no extra deps)
  auto    -- CJK keywords → vosk; other keywords → whisper

State machine:
  STOPPED -> start() -> NO_DEVICE (no mic) or IDLE (mic ok)
  NO_DEVICE: retries every 3 s until mic appears
  IDLE -> wake word / manual trigger / VAD -> LISTENING
       -> silence / timeout / cancel -> IDLE
  any state -> stop() -> STOPPED (mic released)

Key difference from Vosk engine:
  - Audio is buffered during LISTENING, then batch-transcribed by Whisper
  - Partial results come from periodic background Whisper inference
  - language=None enables automatic language detection (Chinese/English mixed)
"""

import json
import logging
import os
import queue
import re
import threading
import time
from enum import Enum
from typing import Callable, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

_DEVICE_RETRY_SEC    = 3.0
_WHISPER_SAMPLE_RATE = 16000   # Whisper only accepts 16 kHz input


# ── Helpers ────────────────────────────────────────────────────────────────────

def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))


def _resolve_mode(mode: str, keywords: List[str]) -> str:
    if mode == "auto":
        # Chinese keywords → vosk; other keywords → whisper (no extra deps)
        return "vosk" if any(_has_cjk(kw) for kw in keywords) else "whisper"
    if mode == "openwakeword":
        # openwakeword removed; fall back to whisper mode
        return "whisper"
    return mode  # vosk / whisper passed through as-is


def _normalize_for_prompt_compare(s: str) -> str:
    """Whitespace / common separators — for comparing transcript to initial_prompt."""
    if not s:
        return ""
    return re.sub(r"[\s\u3000·]+", "", s.strip())


def _text_echoes_initial_prompt(text: str, initial_prompt: Optional[str]) -> bool:
    """
    Silence/noise + initial_prompt often causes Whisper to decode the prompt itself.
    Drop obvious cases: long contiguous slice of the prompt, or long subsequence
    covering a large fraction of the prompt (not single short domain words).
    """
    if not initial_prompt or not text:
        return False
    t = _normalize_for_prompt_compare(text)
    p = _normalize_for_prompt_compare(initial_prompt)
    if len(t) < 4 or len(p) < 4:
        return False
    # 复读提示词中很长一段（典型幻听）
    if t in p and len(t) >= max(8, int(0.45 * len(p))):
        return True
    if t in p and len(p) <= 24 and len(t) >= int(0.82 * len(p)):
        return True
    # 按提示词顺序「抠」字能组成整段且占比高
    if len(t) >= 10 and len(t) >= 0.38 * len(p):
        i = 0
        for c in p:
            if i < len(t) and c == t[i]:
                i += 1
        if i == len(t):
            return True
    return False


def _buffer_to_float32(buf: List[bytes]) -> np.ndarray:
    """Concatenate int16 byte chunks and normalise to float32 [-1, 1]."""
    if not buf:
        return np.zeros(0, dtype=np.float32)
    raw = np.frombuffer(b"".join(buf), dtype=np.int16)
    return raw.astype(np.float32) / 32768.0


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


# ── Wake word detectors ────────────────────────────────────────────────────────

class _VoskWakeWordDetector:
    def __init__(self, model, sample_rate: int, keywords: List[str]):
        self._model       = model
        self._sample_rate = sample_rate
        self.keywords     = [kw.strip() for kw in keywords]
        self._make_rec()
        logger.info(f"[WakeWord] Vosk mode -- keywords: {self.keywords}")

    @staticmethod
    def _to_grammar_phrase(kw: str) -> str:
        return " ".join(kw) if _has_cjk(kw) else kw

    def _make_rec(self):
        import vosk
        phrases = [self._to_grammar_phrase(kw) for kw in self.keywords]
        grammar = json.dumps(phrases + ["[unk]"], ensure_ascii=False)
        self._rec = vosk.KaldiRecognizer(self._model, self._sample_rate)
        self._rec.SetGrammar(grammar)

    def process(self, audio_bytes: bytes) -> Optional[str]:
        if self._rec.AcceptWaveform(audio_bytes):
            result     = json.loads(self._rec.Result())
            text       = result.get("text", "").strip()
            normalized = text.replace(" ", "")
            if normalized and normalized != "[unk]" and normalized in self.keywords:
                self._make_rec()
                return normalized
        return None



class _WhisperWakeWordDetector:
    """
    Wake word detection using the already-loaded Whisper model.
    Maintains a sliding audio window; when speech energy is detected,
    runs a quick Whisper inference and checks if any keyword appears.
    No additional dependencies required — works with or without vosk.
    """
    _WINDOW_SEC   = 2.5   # sliding inference window (more context = better accuracy)
    _COOLDOWN_SEC = 0.3   # min gap between consecutive inferences

    def __init__(self, model, keywords: List[str], language: Optional[str],
                 sample_rate: int = _WHISPER_SAMPLE_RATE):
        self._model     = model
        self.keywords   = [kw.strip() for kw in keywords]
        # Normalise for matching: remove spaces, lowercase
        self._kw_norm   = [kw.replace(" ", "").lower() for kw in keywords]
        self._language  = language
        self._win_bytes = int(self._WINDOW_SEC * sample_rate) * 2  # int16 bytes
        self._buf       = bytearray()
        self._last_infer = 0.0
        # Use keywords as initial_prompt — strongly biases Whisper towards
        # transcribing the exact wake word characters (key for Chinese)
        self._prompt = "、".join(self.keywords)
        logger.info(f"[WakeWord] Whisper mode -- keywords: {self.keywords}")

    def process(self, audio_bytes: bytes, rms: float,
                energy_threshold: float) -> Optional[str]:
        self._buf.extend(audio_bytes)
        if len(self._buf) > self._win_bytes:
            self._buf = self._buf[-self._win_bytes:]

        # Use half the ASR energy threshold so soft wake-word speech isn't gated out
        if (rms < energy_threshold * 0.5
                or time.time() - self._last_infer < self._COOLDOWN_SEC
                or len(self._buf) < self._win_bytes // 4):
            return None

        self._last_infer = time.time()
        audio_f32 = (np.frombuffer(bytes(self._buf), dtype=np.int16)
                     .astype(np.float32) / 32768.0)
        try:
            segs, _ = self._model.transcribe(
                audio_f32,
                language                 = self._language,
                task                     = "transcribe",
                beam_size                = 2,      # +1 beam for better accuracy
                vad_filter               = False,
                word_timestamps          = False,
                initial_prompt           = self._prompt,  # bias towards wake word
                condition_on_previous_text = False,
            )
            # 未过滤时，静音/噪声易被 initial_prompt 诱导成「像唤醒词」的幻觉
            _WW_MAX_NS = 0.42
            parts = []
            for s in segs:
                if getattr(s, "no_speech_prob", 1.0) < _WW_MAX_NS:
                    parts.append(s.text)
            text = "".join(parts).replace(" ", "").lower()
        except Exception:
            return None

        if text:
            logger.debug(f"[WakeWord] heard: {text!r}")
        for kw, kw_n in zip(self.keywords, self._kw_norm):
            if kw_n in text:
                self._buf.clear()
                return kw
        return None


# ── Engine state ───────────────────────────────────────────────────────────────

class EngineState(Enum):
    STOPPED   = "stopped"
    NO_DEVICE = "no_device"
    IDLE      = "idle"
    LISTENING = "listening"


# ── Main engine ────────────────────────────────────────────────────────────────

class SpeechEngine:
    def __init__(self, config: dict, event_callback: Callable[[dict], None]):
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
        self._ww_resolved_mode: str              = "whisper"
        self._preload_lock      = threading.Lock()

    # ── Public control API ────────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            logger.warning("Engine already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_safe, daemon=True, name="SpeechEngine-Whisper"
        )
        self._thread.start()
        logger.info("Whisper engine started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._set_state(EngineState.STOPPED)
        logger.info("Whisper engine stopped")

    def trigger_listen(self):
        self._trigger_listen.set()

    def cancel_listen(self):
        self._cancel_listen.set()

    def update_config(self, key: str, value) -> bool:
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
            ) and value is not None:
                value = int(float(value))
            cfg[parts[-1]] = value
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
        self.state = state
        ww = self.config["wake_word"]
        w  = self.config.get("whisper", {})
        self.emit({
            "event":                 "status",
            "state":                 state.value,
            "wake_word_enabled":     ww.get("enabled", True),
            "keywords":              ww.get("keywords", []),
            "mode":                  ww.get("mode", "auto"),
            "whisper_max_silence_ms": w.get("max_silence_ms", 2500),
            "whisper_min_listen_ms":  w.get("min_listen_ms", 600),
            "whisper_max_listen_ms":  w.get("max_listen_ms", 30000),
            "ts":                    time.time(),
        })

    def _run_safe(self):
        try:
            self._run()
        except Exception as exc:
            logger.error(f"Whisper engine crashed: {exc}", exc_info=True)
            self.emit({
                "event": "error", "code": "engine_crash",
                "message": str(exc), "ts": time.time(),
            })
            self.state = EngineState.STOPPED

    def _do_transcribe(self, whisper_model, buf: List[bytes], language: Optional[str]) -> str:
        """Run Whisper on buffered audio; returns stripped transcript string."""
        if not buf:
            return ""
        audio_f32 = _buffer_to_float32(buf)
        # Skip clips shorter than 0.3 s to avoid spurious output
        if len(audio_f32) < _WHISPER_SAMPLE_RATE * 0.3:
            return ""
        # Skip nearly-silent buffers — Whisper hallucinates initial_prompt words on silence
        rms = float(np.sqrt(np.mean(audio_f32 ** 2)))
        if rms < 0.006:
            return ""
        try:
            prompt = getattr(self, "_initial_prompt", None)
            segments, _ = whisper_model.transcribe(
                audio_f32,
                language                   = language,   # None = auto-detect (mixed mode)
                task                       = "transcribe",
                beam_size                  = 5,
                vad_filter                 = False,      # we handle VAD ourselves
                word_timestamps            = False,
                initial_prompt             = prompt,
                condition_on_previous_text = False,      # prevent context bleeding between sessions
                # 内置静音判定：no_speech 高且 logprob 差时跳过片段，减轻 initial_prompt 复述
                no_speech_threshold        = 0.45,
                log_prob_threshold         = -0.85,
            )
            # no_speech_prob 高 ≈ 模型认为更像静音/噪声
            _ASR_MAX_NS = 0.48
            parts = []
            for seg in segments:
                if getattr(seg, "no_speech_prob", 0.0) < _ASR_MAX_NS:
                    parts.append(seg.text)
            merged = "".join(parts).strip()
            # 仍有少量「整段读出 initial_prompt」——多为复述提示词而非人声
            if prompt and _text_echoes_initial_prompt(merged, prompt):
                return ""
            return merged
        except Exception as exc:
            logger.warning(f"Whisper inference error: {exc}")
            return ""

    def _finalize(self, whisper_model, buf: List[bytes], language: Optional[str], reason: str):
        """Transcribe buffered audio, emit transcript + listening_end."""
        text = self._do_transcribe(whisper_model, buf, language)
        if text:
            self.emit({
                "event": "transcript",
                "text":  text, "is_final": True, "ts": time.time(),
            })
        self.emit({"event": "listening_end", "reason": reason, "ts": time.time()})

    # ── Model management ─────────────────────────────────────────────────────

    def preload(self):
        """Load the Whisper model and wake word detector now, before any start()
        call.  Safe to call from the server startup thread; idempotent — calling
        it again is a no-op if the config has not changed."""
        self._ensure_models_loaded()

    def _ensure_models_loaded(self):
        """Load (or reuse cached) Whisper model and wake word detector.
        Called by preload() at server startup and by _run() on every start().
        The second and subsequent calls are nearly instant thanks to caching."""
        with self._preload_lock:
            self._ensure_models_loaded_unlocked()

    def _ensure_models_loaded_unlocked(self):
        cfg_whisper = self.config.get("whisper", {})
        cfg_ww      = self.config["wake_word"]

        whisper_model_name: str           = cfg_whisper.get("model", "base")
        whisper_device:     str           = cfg_whisper.get("device", "cpu")
        compute_type:       str           = cfg_whisper.get("compute_type", "int8")
        language:           Optional[str] = cfg_whisper.get("language")
        keywords:           List[str]     = cfg_ww.get("keywords", ["小智"])
        sensitivity:        float         = cfg_ww.get("sensitivity", 0.5)
        resolved_mode                     = _resolve_mode(cfg_ww.get("mode", "auto"), keywords)

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
            logger.info(
                "[preload]   First run downloads the model from HuggingFace Hub "
                "(~75 MB for 'base')."
            )
            try:
                _stub_av_if_needed()
                _fix_ctranslate2_dlls()
                from faster_whisper import WhisperModel
                asr_model = WhisperModel(
                    whisper_model_name,
                    device       = whisper_device,
                    compute_type = compute_type,
                )
                _dummy = np.zeros(int(_WHISPER_SAMPLE_RATE * 0.1), dtype=np.float32)
                list(asr_model.transcribe(_dummy, beam_size=1)[0])
            except Exception as exc:
                _exc_s = str(exc).lower()
                _is_cuda_dll   = any(k in _exc_s for k in ("cublas", "cudnn", "libcuda", "cuda"))
                _is_compute    = any(k in _exc_s for k in ("compute type", "float16", "not supported"))
                if _is_cuda_dll or _is_compute:
                    # Step 1: if float16 failed but CUDA is otherwise available, retry int8 on GPU
                    if _is_compute and not _is_cuda_dll and whisper_device == "cuda":
                        logger.warning(
                            f"[preload] compute_type={compute_type!r} not supported on this GPU "
                            f"({exc}); retrying with int8 on cuda."
                        )
                        try:
                            asr_model = WhisperModel(
                                whisper_model_name, device="cuda", compute_type="int8"
                            )
                            compute_type = "int8"
                            logger.info("[preload] cuda+int8 fallback succeeded.")
                        except Exception as exc2:
                            logger.warning(f"[preload] cuda+int8 also failed ({exc2}); falling back to cpu.")
                            compute_type   = "float32"
                            whisper_device = "cpu"
                            asr_model = WhisperModel(
                                whisper_model_name, device="cpu", compute_type="float32"
                            )
                    else:
                        # CUDA DLLs missing or unrecognised compute error → CPU float32
                        logger.warning(
                            f"[preload] compute_type={compute_type!r} triggered a CUDA "
                            f"dependency ({exc}); falling back to float32 on cpu."
                        )
                        compute_type   = "float32"
                        whisper_device = "cpu"
                        asr_model = WhisperModel(
                            whisper_model_name, device="cpu", compute_type="float32"
                        )
                        logger.info("[preload] cpu+float32 fallback succeeded.")
                else:
                    self.emit({
                        "event":   "error",
                        "code":    "model_not_found",
                        "message": f"Failed to load Whisper model '{whisper_model_name}': {exc}",
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
        ww_key = (
            resolved_mode,
            tuple(keywords),
            sensitivity,
            self.config.get("asr", {}).get("model_path", ""),
        )
        if self._ww_detector is not None and self._ww_key == ww_key:
            logger.info("[preload] Wake word detector already loaded — skipping.")
        else:
            ww_detector = None
            if ww_enabled and keywords:
                if resolved_mode == "whisper":
                    ww_detector = _WhisperWakeWordDetector(
                        asr_model, keywords, language, _WHISPER_SAMPLE_RATE
                    )
                else:
                    try:
                        if resolved_mode == "vosk":
                            import vosk
                            vosk.SetLogLevel(-1)
                            ww_model_path = self.config.get("asr", {}).get(
                                "model_path", "models/vosk-model-small-cn-0.22"
                            )
                            logger.info(f"[preload] Loading Vosk model: {ww_model_path}")
                            vosk_model  = vosk.Model(ww_model_path)
                            ww_detector = _VoskWakeWordDetector(
                                vosk_model, _WHISPER_SAMPLE_RATE, keywords
                            )
                        else:
                            # Unknown mode — should not happen after _resolve_mode()
                            raise ValueError(f"Unsupported wake word mode: {resolved_mode!r}")
                    except Exception as exc:
                        logger.warning(
                            f"[preload] Wake word init failed ({exc}); "
                            "falling back to Whisper mode."
                        )
                        self.emit({
                            "event":   "error",
                            "code":    "wake_word_fallback",
                            "message": (
                                f"Primary wake-word backend unavailable ({exc}); "
                                "switched to Whisper wake-word."
                            ),
                            "ts": time.time(),
                        })
                        resolved_mode = "whisper"
                        ww_detector   = _WhisperWakeWordDetector(
                            asr_model, keywords, language, _WHISPER_SAMPLE_RATE
                        )
            self._ww_detector      = ww_detector
            self._ww_key           = ww_key
            self._ww_resolved_mode = resolved_mode
            logger.info(
                "[preload] Wake word: " + ("enabled" if ww_enabled else "disabled")
                + (f" [{resolved_mode}]" if ww_enabled else "")
            )

    # ── Main audio pipeline ───────────────────────────────────────────────────

    def _run(self):
        import sounddevice as sd

        cfg_whisper = self.config.get("whisper", {})
        cfg_ww      = self.config["wake_word"]
        cfg_audio   = self.config["audio"]

        # Audio settings – Whisper requires 16 kHz; override whatever the user set
        sample_rate:       int   = _WHISPER_SAMPLE_RATE
        chunk_size:        int   = cfg_audio.get("chunk_size", 4000)
        energy_threshold:  float = cfg_audio.get("energy_threshold", 0.02)
        device                   = cfg_audio.get("device") or None

        # Whisper-specific settings – read from self.config at each session start
        # so that runtime config updates (via WebSocket "config" command) take
        # effect immediately without restarting the engine.
        def _wparams():
            w = self.config.get("whisper", {})
            return (
                w.get("language"),
                w.get("max_silence_ms",      2500),
                w.get("max_listen_ms",       30000),
                w.get("partial_interval_ms", 2000),
                w.get("vad_cooldown_ms",     500),
                w.get("vad_min_speech_ms",   200),
                w.get("min_listen_ms",       600),
            )

        (language, max_silence_ms, max_listen_ms,
         partial_interval_ms, vad_cooldown_ms,
         vad_min_speech_ms, min_listen_ms) = _wparams()
        self._initial_prompt = cfg_whisper.get("initial_prompt")

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
                _q.put(bytes(indata))

            try:
                stream = sd.RawInputStream(
                    samplerate = sample_rate,
                    blocksize  = chunk_size,
                    device     = device,
                    dtype      = "int16",
                    channels   = 1,
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
                text = self._do_transcribe(asr_model, buf_snapshot, language)
                with _partial_lock:
                    _partial_running[0] = False
                if text and text != _last_partial_text[0]:
                    _last_partial_text[0] = text
                    self.emit({"event": "partial", "text": text, "ts": time.time()})

            try:
                with stream:
                    logger.info("Microphone open.  Whisper engine running.")
                    self._set_state(EngineState.IDLE)

                    listen_buf:    List[bytes]     = []
                    listen_start:  Optional[float] = None
                    silence_start: Optional[float] = None
                    # VAD-mode interaction state
                    _vad_speech_since:   Optional[float] = None  # when sustained speech started
                    _vad_cooldown_until: float            = 0.0  # no new VAD trigger before this

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
                                 vad_min_speech_ms, min_listen_ms) = _wparams()
                                if (time.time() - listen_start) * 1000 >= max_listen_ms:
                                    self._finalize(asr_model, listen_buf, language, "timeout")
                                    listen_buf    = []
                                    listen_start  = None
                                    silence_start = None
                                    _last_partial_text[0] = ""
                                    _last_partial_ts[0]   = 0.0
                                    _vad_speech_since   = None
                                    _vad_cooldown_until = time.time() + vad_cooldown_ms / 1000
                                    self._set_state(EngineState.IDLE)
                            continue

                        # 每帧重读 config，使 WebSocket 修改的静音/时长参数在本轮录音中立即生效
                        (language, max_silence_ms, max_listen_ms,
                         partial_interval_ms, vad_cooldown_ms,
                         vad_min_speech_ms, min_listen_ms) = _wparams()

                        audio_np = np.frombuffer(audio_bytes, dtype=np.int16)
                        rms = float(
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
                                self.emit({
                                    "event": "listening_start",
                                    "trigger": "manual", "ts": time.time(),
                                })
                                self._set_state(EngineState.LISTENING)
                                continue

                            # Read detector state fresh every chunk — picks up any
                            # changes applied by preload() running in a background thread.
                            _ww_det     = self._ww_detector
                            _ww_mode    = self._ww_resolved_mode
                            _ww_enabled = self.config["wake_word"].get("enabled", True)

                            if _ww_enabled and _ww_det is not None:
                                if _ww_mode == "vosk":
                                    detected = _ww_det.process(audio_bytes)
                                else:  # whisper
                                    detected = _ww_det.process(
                                        audio_bytes, rms, energy_threshold
                                    )

                                if detected:
                                    logger.info(f"Wake word: '{detected}'")
                                    self.emit({
                                        "event": "wake_word", "keyword": detected,
                                        "score": 1.0, "ts": time.time(),
                                    })
                                    listen_buf    = []
                                    listen_start  = time.time()
                                    silence_start = None
                                    _last_partial_text[0] = ""
                                    _last_partial_ts[0]   = 0.0
                                    self.emit({
                                        "event": "listening_start",
                                        "trigger": "wake_word", "ts": time.time(),
                                    })
                                    self._set_state(EngineState.LISTENING)
                            else:
                                # Energy-based VAD with cooldown + min-speech gate
                                now = time.time()
                                if rms > energy_threshold and now >= _vad_cooldown_until:
                                    if _vad_speech_since is None:
                                        _vad_speech_since = now
                                    elif (now - _vad_speech_since) * 1000 >= vad_min_speech_ms:
                                        _vad_speech_since   = None
                                        _vad_cooldown_until = 0.0
                                        listen_buf    = [audio_bytes]
                                        listen_start  = time.time()
                                        silence_start = None
                                        _last_partial_text[0] = ""
                                        _last_partial_ts[0]   = 0.0
                                        self.emit({
                                            "event": "listening_start",
                                            "trigger": "vad", "ts": time.time(),
                                        })
                                        self._set_state(EngineState.LISTENING)
                                else:
                                    _vad_speech_since = None  # reset on quiet/cooldown

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
                                _vad_cooldown_until = time.time() + vad_cooldown_ms / 1000
                                self._set_state(EngineState.IDLE)
                                continue

                            # Hard timeout
                            if (listen_start is not None
                                    and (time.time() - listen_start) * 1000 >= max_listen_ms):
                                self._finalize(asr_model, listen_buf, language, "timeout")
                                listen_buf    = []
                                listen_start  = None
                                silence_start = None
                                _last_partial_text[0] = ""
                                _last_partial_ts[0]   = 0.0
                                _vad_speech_since   = None
                                _vad_cooldown_until = time.time() + vad_cooldown_ms / 1000
                                self._set_state(EngineState.IDLE)
                                continue

                            # Collect audio into buffer
                            listen_buf.append(audio_bytes)

                            # Silence detection
                            if rms < energy_threshold:
                                if silence_start is None:
                                    silence_start = time.time()
                                elif ((time.time() - silence_start) * 1000 >= max_silence_ms
                                      and listen_start is not None
                                      and (time.time() - listen_start) * 1000 >= min_listen_ms):
                                    self._finalize(asr_model, listen_buf, language, "silence")
                                    listen_buf    = []
                                    listen_start  = None
                                    silence_start = None
                                    _last_partial_text[0] = ""
                                    _last_partial_ts[0]   = 0.0
                                    _vad_speech_since   = None
                                    _vad_cooldown_until = time.time() + vad_cooldown_ms / 1000
                                    self._set_state(EngineState.IDLE)
                                    continue
                            else:
                                silence_start = None   # voice activity resets silence timer

                            # Periodic partial inference (background thread)
                            now = time.time()
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
