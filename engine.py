"""
Audio pipeline engine: microphone → wake word → ASR → events

Wake word modes:
  vosk         — Uses Vosk grammar/keyword-spotting (supports ANY language, including Chinese)
  openwakeword — Uses openwakeword pre-trained ONNX models (English only)
  auto         — Chinese keywords → vosk; ASCII keywords → openwakeword (fallback to vosk)

State machine:
  STOPPED → start() → IDLE → wake word / trigger → LISTENING → silence/timeout/cancel → IDLE
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def _has_cjk(text: str) -> bool:
    """True if text contains Chinese / Japanese / Korean characters."""
    return bool(re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))


def _resolve_mode(mode: str, keywords: List[str]) -> str:
    if mode != "auto":
        return mode
    return "vosk" if any(_has_cjk(kw) for kw in keywords) else "openwakeword"


# ── Wake word detectors ────────────────────────────────────────────────────────

class _VoskWakeWordDetector:
    """
    Keyword-spotting via Vosk grammar mode.
    Works for any language supported by the loaded Vosk model (including Chinese).

    How it works:
      - A KaldiRecognizer is created with a restricted grammar (your keywords + "[unk]").
      - Audio is fed continuously; when a final result matches a keyword, wake detected.
      - A fresh recognizer is created after each detection to clear accumulated state.
    """

    def __init__(self, model, sample_rate: int, keywords: List[str]):
        import vosk  # noqa: F401 – checked at call site
        self._model = model
        self._sample_rate = sample_rate
        self.keywords = [kw.strip() for kw in keywords]
        self._make_rec()
        logger.info(f"[WakeWord] Vosk mode — keywords: {self.keywords}")

    def _make_rec(self):
        import vosk
        grammar = json.dumps(self.keywords + ["[unk]"], ensure_ascii=False)
        self._rec = vosk.KaldiRecognizer(self._model, self._sample_rate)
        self._rec.SetGrammar(grammar)

    def process(self, audio_bytes: bytes) -> Optional[str]:
        """Feed a PCM int16 audio chunk. Returns detected keyword string or None."""
        if self._rec.AcceptWaveform(audio_bytes):
            result = json.loads(self._rec.Result())
            text = result.get("text", "").strip()
            if text and text != "[unk]" and text in self.keywords:
                self._make_rec()   # reset for next detection
                return text
        return None


class _OpenWakeWordDetector:
    """
    Wake word detection via openwakeword pre-trained ONNX models (English keywords).
    Supported keywords: hey_jarvis, alexa, hey_mycroft, hey_rhasspy, …
    """

    def __init__(self, keywords: List[str], sensitivity: float):
        from openwakeword.model import Model  # type: ignore
        self.keywords = keywords
        self.sensitivity = sensitivity
        self._model = Model(wakeword_models=keywords, inference_framework="onnx")
        logger.info(f"[WakeWord] OpenWakeWord mode — keywords: {keywords}")

    def process(self, audio_f32: np.ndarray) -> Optional[str]:
        """Feed float32 audio (normalised to ±1). Returns detected keyword or None."""
        scores: dict = self._model.predict(audio_f32)
        for kw in self.keywords:
            if float(scores.get(kw, 0.0)) >= self.sensitivity:
                return kw
        return None


# ── Engine state ───────────────────────────────────────────────────────────────

class EngineState(Enum):
    STOPPED   = "stopped"
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
        """Manually start listening, bypassing wake word detection."""
        self._trigger_listen.set()

    def cancel_listen(self):
        """Abort the current listening session."""
        self._cancel_listen.set()

    def update_config(self, key: str, value) -> bool:
        """Update a config value at runtime via dot-notation key."""
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
            "event":            "status",
            "state":            state.value,
            "wake_word_enabled": ww.get("enabled", True),
            "keywords":         ww.get("keywords", []),
            "mode":             ww.get("mode", "auto"),
            "ts":               time.time(),
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

        # ── Load ASR model ────────────────────────────────────────────────────
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
                    "Run install.bat to download models."
                ),
                "ts": time.time(),
            })
            raise
        logger.info("ASR model loaded")

        # ── Build wake word detector ──────────────────────────────────────────
        ww_enabled: bool       = cfg_ww.get("enabled", True)
        keywords:   List[str]  = cfg_ww.get("keywords", ["你好小智"])
        sensitivity: float     = cfg_ww.get("sensitivity", 0.5)
        mode_cfg               = cfg_ww.get("mode", "auto")
        resolved_mode          = _resolve_mode(mode_cfg, keywords)

        ww_detector = None
        if ww_enabled and keywords:
            try:
                if resolved_mode == "vosk":
                    ww_detector = _VoskWakeWordDetector(asr_model, sample_rate, keywords)
                else:
                    # Try openwakeword; fall back to vosk on failure
                    try:
                        ww_detector = _OpenWakeWordDetector(keywords, sensitivity)
                    except Exception as oww_exc:
                        logger.warning(
                            f"OpenWakeWord failed ({oww_exc}), "
                            "falling back to Vosk keyword-spotting mode."
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

        # ── Open microphone ───────────────────────────────────────────────────
        audio_q: queue.Queue = queue.Queue(maxsize=200)

        def audio_callback(indata, frames, time_info, status):
            if status:
                logger.debug(f"Audio status: {status}")
            if audio_q.full():
                try:
                    audio_q.get_nowait()
                except queue.Empty:
                    pass
            audio_q.put(bytes(indata))

        device = cfg_audio.get("device") or None
        try:
            stream = sd.RawInputStream(
                samplerate=sample_rate, blocksize=chunk_size,
                device=device, dtype="int16", channels=1,
                callback=audio_callback,
            )
        except Exception as exc:
            self.emit({
                "event": "error", "code": "mic_error",
                "message": f"Cannot open microphone: {exc}",
                "ts": time.time(),
            })
            raise

        def new_asr_rec():
            rec = vosk.KaldiRecognizer(asr_model, sample_rate)
            rec.SetWords(True)
            rec.SetPartialWords(True)
            return rec

        # ── State machine loop ────────────────────────────────────────────────
        with stream:
            logger.info("Microphone open. Engine running.")
            self._set_state(EngineState.IDLE)

            asr_rec    = new_asr_rec()
            listen_start: Optional[float] = None
            last_partial  = ""

            while not self._stop_event.is_set():

                # Get audio chunk
                try:
                    audio_bytes = audio_q.get(timeout=0.2)
                except queue.Empty:
                    # Timeout guard while listening
                    if self.state == EngineState.LISTENING and listen_start is not None:
                        if (time.time() - listen_start) * 1000 >= max_listen_ms:
                            self._finalize(asr_rec, "timeout")
                            asr_rec      = new_asr_rec()
                            listen_start = None
                            last_partial = ""
                            self._set_state(EngineState.IDLE)
                    continue

                audio_np = np.frombuffer(audio_bytes, dtype=np.int16)

                # ── IDLE ──────────────────────────────────────────────────────
                if self.state == EngineState.IDLE:

                    # Manual trigger
                    if self._trigger_listen.is_set():
                        self._trigger_listen.clear()
                        asr_rec      = new_asr_rec()
                        listen_start = time.time()
                        last_partial = ""
                        self.emit({"event": "listening_start", "trigger": "manual", "ts": time.time()})
                        self._set_state(EngineState.LISTENING)
                        continue

                    if ww_enabled and ww_detector is not None:
                        # Wake word detection
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
                            self.emit({"event": "listening_start", "trigger": "wake_word", "ts": time.time()})
                            self._set_state(EngineState.LISTENING)
                    else:
                        # No wake word → energy-based VAD to detect speech start
                        rms = float(np.sqrt(np.mean(audio_np.astype(np.float32) ** 2))) / 32768.0
                        if rms > energy_threshold:
                            asr_rec      = new_asr_rec()
                            listen_start = time.time()
                            last_partial = ""
                            self.emit({"event": "listening_start", "trigger": "vad", "ts": time.time()})
                            self._set_state(EngineState.LISTENING)
                            asr_rec.AcceptWaveform(audio_bytes)

                # ── LISTENING ─────────────────────────────────────────────────
                elif self.state == EngineState.LISTENING:

                    # Cancel command
                    if self._cancel_listen.is_set():
                        self._cancel_listen.clear()
                        self.emit({"event": "listening_end", "reason": "cancelled", "ts": time.time()})
                        asr_rec      = new_asr_rec()
                        listen_start = None
                        last_partial = ""
                        self._set_state(EngineState.IDLE)
                        continue

                    # Hard timeout
                    if listen_start is not None:
                        if (time.time() - listen_start) * 1000 >= max_listen_ms:
                            self._finalize(asr_rec, "timeout")
                            asr_rec      = new_asr_rec()
                            listen_start = None
                            last_partial = ""
                            self._set_state(EngineState.IDLE)
                            continue

                    # Feed audio to ASR
                    if asr_rec.AcceptWaveform(audio_bytes):
                        # End of utterance
                        result = json.loads(asr_rec.Result())
                        text   = result.get("text", "").strip()
                        if text:
                            self.emit({"event": "transcript", "text": text, "is_final": True, "ts": time.time()})
                        self.emit({"event": "listening_end", "reason": "silence", "ts": time.time()})
                        asr_rec      = new_asr_rec()
                        listen_start = None
                        last_partial = ""
                        self._set_state(EngineState.IDLE)
                    else:
                        # Partial / interim result
                        partial = json.loads(asr_rec.PartialResult()).get("partial", "").strip()
                        if partial and partial != last_partial:
                            last_partial = partial
                            self.emit({"event": "partial", "text": partial, "ts": time.time()})

        self._set_state(EngineState.STOPPED)

    def _finalize(self, rec, reason: str):
        """Emit final ASR result + listening_end."""
        try:
            text = json.loads(rec.FinalResult()).get("text", "").strip()
            if text:
                self.emit({"event": "transcript", "text": text, "is_final": True, "ts": time.time()})
        except Exception as exc:
            logger.debug(f"Finalize error: {exc}")
        self.emit({"event": "listening_end", "reason": reason, "ts": time.time()})
