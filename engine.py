"""
Audio pipeline engine: microphone -> wake word -> ASR -> events

Wake word modes:
  vosk         -- Uses Vosk grammar/keyword-spotting (supports Chinese)
  openwakeword -- Uses openwakeword pre-trained ONNX models (English only)
  auto         -- Chinese keywords -> vosk; ASCII keywords -> openwakeword

State machine:
  STOPPED -> start() -> NO_DEVICE (no mic) or IDLE (mic ok)
  NO_DEVICE: retries every 3 s until mic appears
  IDLE -> wake word / trigger -> LISTENING -> silence/timeout/cancel -> IDLE
  IDLE/LISTENING: mic disconnected -> NO_DEVICE -> auto-retry
  any state -> stop() -> STOPPED (mic released)
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

_DEVICE_RETRY_SEC = 3.0   # seconds between mic open retries


# ── Helpers ────────────────────────────────────────────────────────────────────

def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))


def _resolve_mode(mode: str, keywords: List[str]) -> str:
    if mode != "auto":
        return mode
    return "vosk" if any(_has_cjk(kw) for kw in keywords) else "openwakeword"


# ── Wake word detectors ────────────────────────────────────────────────────────

class _VoskWakeWordDetector:
    def __init__(self, model, sample_rate: int, keywords: List[str]):
        self._model = model
        self._sample_rate = sample_rate
        self.keywords = [kw.strip() for kw in keywords]
        self._make_rec()
        logger.info(f"[WakeWord] Vosk mode -- keywords: {self.keywords}")

    @staticmethod
    def _to_grammar_phrase(kw: str) -> str:
        """Vosk Chinese models are character-level; separate each CJK char with a
        space so the recognizer can match multi-character keywords correctly."""
        if _has_cjk(kw):
            return " ".join(kw)
        return kw

    def _make_rec(self):
        import vosk
        phrases = [self._to_grammar_phrase(kw) for kw in self.keywords]
        grammar = json.dumps(phrases + ["[unk]"], ensure_ascii=False)
        self._rec = vosk.KaldiRecognizer(self._model, self._sample_rate)
        self._rec.SetGrammar(grammar)

    def process(self, audio_bytes: bytes) -> Optional[str]:
        if self._rec.AcceptWaveform(audio_bytes):
            result = json.loads(self._rec.Result())
            text = result.get("text", "").strip()
            # Vosk returns space-separated chars for Chinese; strip spaces before
            # comparing against the original (no-space) keyword strings.
            normalized = text.replace(" ", "")
            if normalized and normalized != "[unk]" and normalized in self.keywords:
                self._make_rec()
                return normalized
        return None


class _OpenWakeWordDetector:
    def __init__(self, keywords: List[str], sensitivity: float):
        from openwakeword.model import Model  # type: ignore
        self.keywords = keywords
        self.sensitivity = sensitivity
        self._model = Model(wakeword_models=keywords, inference_framework="onnx")
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
    NO_DEVICE = "no_device"   # running, waiting for microphone
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
            target=self._run_safe, daemon=True, name="SpeechEngine"
        )
        self._thread.start()
        logger.info("Engine started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._set_state(EngineState.STOPPED)
        logger.info("Engine stopped")

    def trigger_listen(self):
        self._trigger_listen.set()

    def cancel_listen(self):
        self._cancel_listen.set()

    def update_config(self, key: str, value) -> bool:
        try:
            parts = key.split(".")
            cfg = self.config
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
            logger.error(f"Engine crashed: {exc}", exc_info=True)
            self.emit({
                "event": "error", "code": "engine_crash",
                "message": str(exc), "ts": time.time(),
            })
            self.state = EngineState.STOPPED

    # ── Main audio pipeline ───────────────────────────────────────────────────

    def _run(self):
        import sounddevice as sd
        import vosk

        cfg_asr   = self.config["asr"]
        cfg_ww    = self.config["wake_word"]
        cfg_audio = self.config["audio"]

        sample_rate:      int   = cfg_audio.get("sample_rate", 16000)
        chunk_size:       int   = cfg_audio.get("chunk_size", 4000)
        max_listen_ms:    int   = cfg_asr.get("max_listen_ms", 30000)
        energy_threshold: float = cfg_audio.get("energy_threshold", 0.02)
        device                  = cfg_audio.get("device") or None

        # ── Load ASR model (once) ─────────────────────────────────────────────
        vosk.SetLogLevel(-1)
        model_path = cfg_asr["model_path"]
        logger.info(f"Loading ASR model: {model_path}")
        try:
            asr_model = vosk.Model(model_path)
        except Exception as exc:
            self.emit({
                "event": "error", "code": "model_not_found",
                "message": (
                    f"ASR model not found: '{model_path}'. "
                    "Run the installer to download models."
                ),
                "ts": time.time(),
            })
            raise
        logger.info("ASR model loaded")

        # ── Build wake word detector (once) ──────────────────────────────────
        ww_enabled:    bool      = cfg_ww.get("enabled", True)
        keywords:      List[str] = cfg_ww.get("keywords", ["你好小智"])
        sensitivity:   float     = cfg_ww.get("sensitivity", 0.5)
        mode_cfg                 = cfg_ww.get("mode", "auto")
        resolved_mode            = _resolve_mode(mode_cfg, keywords)

        ww_detector = None
        if ww_enabled and keywords:
            try:
                if resolved_mode == "vosk":
                    ww_detector = _VoskWakeWordDetector(asr_model, sample_rate, keywords)
                else:
                    try:
                        ww_detector = _OpenWakeWordDetector(keywords, sensitivity)
                    except Exception as oww_exc:
                        logger.warning(
                            f"OpenWakeWord failed ({oww_exc}), "
                            "falling back to Vosk keyword-spotting."
                        )
                        self.emit({
                            "event": "error", "code": "wake_word_fallback",
                            "message": (
                                f"openwakeword unavailable ({oww_exc}), "
                                "switched to Vosk keyword-spotting."
                            ),
                            "ts": time.time(),
                        })
                        ww_detector = _VoskWakeWordDetector(asr_model, sample_rate, keywords)
                        resolved_mode = "vosk"
            except Exception as exc:
                logger.warning(f"Wake word init failed: {exc}. Using energy-based VAD.")
                self.emit({
                    "event": "error", "code": "wake_word_unavailable",
                    "message": str(exc), "ts": time.time(),
                })
                ww_enabled = False

        logger.info(
            f"Wake word: {'enabled' if ww_enabled else 'disabled'}"
            + (f" [{resolved_mode}]" if ww_enabled else "")
        )

        def new_asr_rec():
            rec = vosk.KaldiRecognizer(asr_model, sample_rate)
            rec.SetWords(True)
            rec.SetPartialWords(True)
            return rec

        # ── Hot-plug device loop ──────────────────────────────────────────────
        while not self._stop_event.is_set():

            # Force PortAudio to re-enumerate devices so a mic plugged in after
            # startup is visible without restarting the process.
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass

            # Build a fresh audio queue for each device open attempt
            audio_q: queue.Queue = queue.Queue(maxsize=200)

            def _audio_cb(indata, frames, time_info, status,
                          _q=audio_q):  # default-arg captures current queue
                if status:
                    logger.debug(f"Audio status: {status}")
                if _q.full():
                    try:
                        _q.get_nowait()
                    except queue.Empty:
                        pass
                _q.put(bytes(indata))

            # Try to open the microphone
            try:
                stream = sd.RawInputStream(
                    samplerate=sample_rate, blocksize=chunk_size,
                    device=device, dtype="int16", channels=1,
                    callback=_audio_cb,
                )
            except Exception as exc:
                if self.state != EngineState.NO_DEVICE:
                    logger.warning(f"Cannot open microphone: {exc}")
                    self.emit({
                        "event": "error", "code": "no_device",
                        "message": (
                            f"Microphone not available: {exc}. "
                            f"Retrying every {int(_DEVICE_RETRY_SEC)} s..."
                        ),
                        "ts": time.time(),
                    })
                    self._set_state(EngineState.NO_DEVICE)
                # Wait before retrying; honours stop_event
                if self._stop_event.wait(_DEVICE_RETRY_SEC):
                    break
                continue

            # ── Per-device audio processing loop ─────────────────────────────
            device_error: Optional[str] = None
            try:
                with stream:
                    logger.info("Microphone open. Engine running.")
                    self._set_state(EngineState.IDLE)

                    asr_rec      = new_asr_rec()
                    listen_start: Optional[float] = None
                    last_partial  = ""

                    while not self._stop_event.is_set():

                        # Detect silent device removal
                        if not stream.active:
                            device_error = "Stream became inactive (device removed?)"
                            break

                        try:
                            audio_bytes = audio_q.get(timeout=0.2)
                        except queue.Empty:
                            if (self.state == EngineState.LISTENING
                                    and listen_start is not None):
                                elapsed_ms = (time.time() - listen_start) * 1000
                                if elapsed_ms >= max_listen_ms:
                                    self._finalize(asr_rec, "timeout")
                                    asr_rec      = new_asr_rec()
                                    listen_start = None
                                    last_partial = ""
                                    self._set_state(EngineState.IDLE)
                            continue

                        audio_np = np.frombuffer(audio_bytes, dtype=np.int16)

                        # ── IDLE ──────────────────────────────────────────────
                        if self.state == EngineState.IDLE:

                            if self._trigger_listen.is_set():
                                self._trigger_listen.clear()
                                asr_rec      = new_asr_rec()
                                listen_start = time.time()
                                last_partial = ""
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
                                    audio_f32 = audio_np.astype(np.float32) / 32768.0
                                    detected  = ww_detector.process(audio_f32)

                                if detected:
                                    logger.info(f"Wake word: '{detected}'")
                                    self.emit({
                                        "event": "wake_word", "keyword": detected,
                                        "score": 1.0, "ts": time.time(),
                                    })
                                    asr_rec      = new_asr_rec()
                                    listen_start = time.time()
                                    last_partial = ""
                                    self.emit({
                                        "event": "listening_start",
                                        "trigger": "wake_word", "ts": time.time(),
                                    })
                                    self._set_state(EngineState.LISTENING)
                            else:
                                # Energy-based VAD
                                rms = (float(np.sqrt(np.mean(
                                    audio_np.astype(np.float32) ** 2
                                ))) / 32768.0)
                                if rms > energy_threshold:
                                    asr_rec      = new_asr_rec()
                                    listen_start = time.time()
                                    last_partial = ""
                                    self.emit({
                                        "event": "listening_start",
                                        "trigger": "vad", "ts": time.time(),
                                    })
                                    self._set_state(EngineState.LISTENING)
                                    asr_rec.AcceptWaveform(audio_bytes)

                        # ── LISTENING ─────────────────────────────────────────
                        elif self.state == EngineState.LISTENING:

                            if self._cancel_listen.is_set():
                                self._cancel_listen.clear()
                                self.emit({
                                    "event": "listening_end",
                                    "reason": "cancelled", "ts": time.time(),
                                })
                                asr_rec      = new_asr_rec()
                                listen_start = None
                                last_partial = ""
                                self._set_state(EngineState.IDLE)
                                continue

                            if listen_start is not None:
                                elapsed_ms = (time.time() - listen_start) * 1000
                                if elapsed_ms >= max_listen_ms:
                                    self._finalize(asr_rec, "timeout")
                                    asr_rec      = new_asr_rec()
                                    listen_start = None
                                    last_partial = ""
                                    self._set_state(EngineState.IDLE)
                                    continue

                            if asr_rec.AcceptWaveform(audio_bytes):
                                result = json.loads(asr_rec.Result())
                                text   = result.get("text", "").strip()
                                if text:
                                    self.emit({
                                        "event": "transcript",
                                        "text": text, "is_final": True,
                                        "ts": time.time(),
                                    })
                                self.emit({
                                    "event": "listening_end",
                                    "reason": "silence", "ts": time.time(),
                                })
                                asr_rec      = new_asr_rec()
                                listen_start = None
                                last_partial = ""
                                self._set_state(EngineState.IDLE)
                            else:
                                partial = json.loads(
                                    asr_rec.PartialResult()
                                ).get("partial", "").strip()
                                if partial and partial != last_partial:
                                    last_partial = partial
                                    self.emit({
                                        "event": "partial",
                                        "text": partial, "ts": time.time(),
                                    })

            except Exception as exc:
                device_error = str(exc)

            # ── Device gone or stop requested ─────────────────────────────────
            if self._stop_event.is_set():
                break

            # Device error / disconnected -> NO_DEVICE -> retry
            msg = device_error or "Microphone disconnected."
            logger.warning(f"Stream ended: {msg}")
            self.emit({
                "event": "error", "code": "no_device",
                "message": (
                    f"{msg}  Retrying every {int(_DEVICE_RETRY_SEC)} s..."
                ),
                "ts": time.time(),
            })
            self._set_state(EngineState.NO_DEVICE)
            if self._stop_event.wait(_DEVICE_RETRY_SEC):
                break

        self._set_state(EngineState.STOPPED)

    def _finalize(self, rec, reason: str):
        try:
            text = json.loads(rec.FinalResult()).get("text", "").strip()
            if text:
                self.emit({
                    "event": "transcript",
                    "text": text, "is_final": True, "ts": time.time(),
                })
        except Exception as exc:
            logger.debug(f"Finalize error: {exc}")
        self.emit({"event": "listening_end", "reason": reason, "ts": time.time()})
