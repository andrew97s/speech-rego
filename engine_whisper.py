"""
Whisper-based audio pipeline engine: microphone -> wake word -> ASR -> events

ASR backend: faster-whisper (CTranslate2), supports mixed Chinese/English
             with language=None (auto-detect per session).

Wake word detection (unchanged from Vosk engine):
  vosk         -- Vosk grammar/keyword-spotting (supports Chinese)
  openwakeword -- openwakeword pre-trained ONNX models (English only)
  auto         -- Chinese keywords -> vosk; ASCII keywords -> openwakeword

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
    if mode != "auto":
        return mode
    return "vosk" if any(_has_cjk(kw) for kw in keywords) else "openwakeword"


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
    ctranslate2 bundles MKL/OpenMP DLLs next to its C extension.
    On some Windows environments the DLL loader cannot find them,
    causing the C extension to load silently incomplete (StorageView
    and other C-side symbols are missing).

    Strategy (applied in order):
    1. Add ctranslate2's own directory to the OS DLL search path.
    2. Evict any already-imported broken copy from sys.modules so
       the next import re-runs __init__.py with the new path.
    3. Re-import ctranslate2.
    4. If StorageView is still absent after the reload, inject a
       minimal stub class.  faster-whisper uses StorageView only as
       an Optional type-annotation default (= None); the stub is
       never instantiated during normal numpy-array transcription.
    """
    import sys, os, importlib.util

    # ── Step 1: add DLL directory (Windows only) ──────────────────
    if hasattr(os, "add_dll_directory"):
        spec = importlib.util.find_spec("ctranslate2")
        if spec and spec.submodule_search_locations:
            ct2_dir = str(list(spec.submodule_search_locations)[0])
            try:
                os.add_dll_directory(ct2_dir)
                logger.debug(f"Added ctranslate2 DLL dir: {ct2_dir}")
            except OSError as exc:
                logger.debug(f"add_dll_directory skipped: {exc}")

    # ── Step 2: evict broken module if already cached ─────────────
    ct2 = sys.modules.get("ctranslate2")
    if ct2 is not None and not hasattr(ct2, "StorageView"):
        stale = [k for k in sys.modules
                 if k == "ctranslate2" or k.startswith("ctranslate2.")]
        for k in stale:
            del sys.modules[k]

    # ── Step 3: fresh import ──────────────────────────────────────
    import ctranslate2

    # ── Step 4: stub missing symbols used only as type annotations ─
    if not hasattr(ctranslate2, "StorageView"):
        ctranslate2.StorageView = type("StorageView", (), {})
        logger.warning(
            "ctranslate2.StorageView still missing after reload; "
            "injected a stub so faster-whisper can be imported.  "
            "Transcription will work if ctranslate2 inference classes "
            "are functional.  To fully fix: "
            "pip install --force-reinstall ctranslate2>=4.0.0"
        )


# ── Wake word detectors (identical to engine.py) ──────────────────────────────

class _VoskWakeWordDetector:
    def __init__(self, model, sample_rate: int, keywords: List[str]):
        self._model       = model
        self._sample_rate = sample_rate
        self.keywords     = [kw.strip() for kw in keywords]
        self._make_rec()
        logger.info(f"[WakeWord] Vosk mode -- keywords: {self.keywords}")

    @staticmethod
    def _to_grammar_phrase(kw: str) -> str:
        """CJK keywords must be space-separated characters for Vosk."""
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


class _OpenWakeWordDetector:
    def __init__(self, keywords: List[str], sensitivity: float):
        from openwakeword.model import Model   # type: ignore
        self.keywords   = keywords
        self.sensitivity = sensitivity
        self._model     = Model(wakeword_models=keywords, inference_framework="onnx")
        logger.info(f"[WakeWord] OpenWakeWord mode -- keywords: {keywords}")

    def process(self, audio_f32: np.ndarray) -> Optional[str]:
        scores: dict = self._model.predict(audio_f32)
        for kw in self.keywords:
            if float(scores.get(kw, 0.0)) >= self.sensitivity:
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
            cfg[parts[-1]] = value
            logger.info(f"Config updated: {key} = {value!r}")
            return True
        except (KeyError, TypeError):
            logger.warning(f"Invalid config key: {key!r}")
            return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _set_state(self, state: EngineState):
        self.state = state
        ww = self.config["wake_word"]
        self.emit({
            "event":             "status",
            "state":             state.value,
            "wake_word_enabled": ww.get("enabled", True),
            "keywords":          ww.get("keywords", []),
            "mode":              ww.get("mode", "auto"),
            "ts":                time.time(),
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
        try:
            segments, _ = whisper_model.transcribe(
                audio_f32,
                language      = language,   # None = auto-detect (mixed mode)
                task          = "transcribe",
                beam_size     = 5,
                vad_filter    = False,      # we handle VAD ourselves
                word_timestamps = False,
            )
            return "".join(seg.text for seg in segments).strip()
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

        # Whisper-specific settings
        whisper_model_name: str            = cfg_whisper.get("model", "base")
        whisper_device:     str            = cfg_whisper.get("device", "cpu")
        compute_type:       str            = cfg_whisper.get("compute_type", "int8")
        language:           Optional[str]  = cfg_whisper.get("language")   # None = auto/mixed
        max_silence_ms:     int            = cfg_whisper.get("max_silence_ms", 1500)
        max_listen_ms:      int            = cfg_whisper.get("max_listen_ms", 30000)
        partial_interval_ms: int           = cfg_whisper.get("partial_interval_ms", 2000)

        # ── Load Whisper model (once per engine start) ────────────────────────
        logger.info(
            f"Loading Whisper model '{whisper_model_name}' "
            f"(device={whisper_device}, compute_type={compute_type}) …"
        )
        logger.info(
            "  First run will download the model from HuggingFace Hub (~75 MB for 'base')."
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
        except Exception as exc:
            self.emit({
                "event": "error", "code": "model_not_found",
                "message": (
                    f"Failed to load Whisper model '{whisper_model_name}': {exc}.  "
                    "Install faster-whisper: pip install faster-whisper"
                ),
                "ts": time.time(),
            })
            raise

        lang_desc = language if language else "auto/mixed (Chinese + English)"
        logger.info(f"Whisper model ready.  Language: {lang_desc}")

        # ── Build wake word detector (once) ──────────────────────────────────
        ww_enabled:    bool      = cfg_ww.get("enabled", True)
        keywords:      List[str] = cfg_ww.get("keywords", ["小智"])
        sensitivity:   float     = cfg_ww.get("sensitivity", 0.5)
        mode_cfg                 = cfg_ww.get("mode", "auto")
        resolved_mode            = _resolve_mode(mode_cfg, keywords)

        ww_detector = None
        if ww_enabled and keywords:
            try:
                if resolved_mode == "vosk":
                    import vosk
                    vosk.SetLogLevel(-1)
                    ww_model_path = self.config.get("asr", {}).get(
                        "model_path", "models/vosk-model-small-cn-0.22"
                    )
                    logger.info(f"Loading Vosk wake-word model: {ww_model_path}")
                    vosk_model  = vosk.Model(ww_model_path)
                    ww_detector = _VoskWakeWordDetector(vosk_model, sample_rate, keywords)
                else:
                    try:
                        ww_detector = _OpenWakeWordDetector(keywords, sensitivity)
                    except Exception as oww_exc:
                        logger.warning(
                            f"OpenWakeWord failed ({oww_exc}), falling back to energy VAD."
                        )
                        self.emit({
                            "event": "error", "code": "wake_word_fallback",
                            "message": (
                                f"openwakeword unavailable ({oww_exc}), "
                                "switched to energy-based VAD."
                            ),
                            "ts": time.time(),
                        })
                        ww_enabled = False
            except Exception as exc:
                logger.warning(f"Wake word init failed: {exc}. Using energy-based VAD.")
                self.emit({
                    "event": "error", "code": "wake_word_unavailable",
                    "message": str(exc), "ts": time.time(),
                })
                ww_enabled = False

        logger.info(
            "Wake word: " + ("enabled" if ww_enabled else "disabled")
            + (f" [{resolved_mode}]" if ww_enabled else "")
        )

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

                    listen_buf:   List[bytes]      = []
                    listen_start: Optional[float]  = None
                    silence_start: Optional[float] = None

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
                                if (time.time() - listen_start) * 1000 >= max_listen_ms:
                                    self._finalize(asr_model, listen_buf, language, "timeout")
                                    listen_buf    = []
                                    listen_start  = None
                                    silence_start = None
                                    _last_partial_text[0] = ""
                                    _last_partial_ts[0]   = 0.0
                                    self._set_state(EngineState.IDLE)
                            continue

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
                                self.emit({
                                    "event": "listening_start",
                                    "trigger": "manual", "ts": time.time(),
                                })
                                self._set_state(EngineState.LISTENING)
                                continue

                            if ww_enabled and ww_detector is not None:
                                if resolved_mode == "vosk":
                                    detected = ww_detector.process(audio_bytes)
                                else:
                                    detected = ww_detector.process(
                                        audio_np.astype(np.float32) / 32768.0
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
                                # Energy-based VAD
                                if rms > energy_threshold:
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
                                self._set_state(EngineState.IDLE)
                                continue

                            # Collect audio into buffer
                            listen_buf.append(audio_bytes)

                            # Silence detection
                            if rms < energy_threshold:
                                if silence_start is None:
                                    silence_start = time.time()
                                elif (time.time() - silence_start) * 1000 >= max_silence_ms:
                                    self._finalize(asr_model, listen_buf, language, "silence")
                                    listen_buf    = []
                                    listen_start  = None
                                    silence_start = None
                                    _last_partial_text[0] = ""
                                    _last_partial_ts[0]   = 0.0
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
