"""
Audio pipeline engine: microphone → wake word → ASR → events

State machine:
  STOPPED   → start() →  IDLE
  IDLE      → wake word or trigger_listen() → LISTENING
  LISTENING → silence/timeout/cancel → IDLE
  IDLE/LISTENING → stop() → STOPPED
"""

import json
import logging
import queue
import threading
import time
from enum import Enum
from typing import Callable, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)


class EngineState(Enum):
    STOPPED = "stopped"
    IDLE = "idle"
    LISTENING = "listening"


class SpeechEngine:
    def __init__(self, config: dict, event_callback: Callable[[dict], None]):
        self.config = config
        self.emit = event_callback
        self.state = EngineState.STOPPED

        self._stop_event = threading.Event()
        self._trigger_listen = threading.Event()
        self._cancel_listen = threading.Event()
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
        """Manually start listening, bypassing wake word."""
        self._trigger_listen.set()

    def cancel_listen(self):
        """Abort the current listening session."""
        self._cancel_listen.set()

    def update_config(self, key: str, value) -> bool:
        """
        Update a config value at runtime.
        key uses dot notation, e.g. "wake_word.sensitivity"
        """
        try:
            parts = key.split(".")
            cfg = self.config
            for part in parts[:-1]:
                cfg = cfg[part]
            cfg[parts[-1]] = value
            logger.info(f"Config updated: {key} = {value!r}")
            return True
        except (KeyError, TypeError):
            logger.warning(f"Invalid config key: {key}")
            return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _set_state(self, state: EngineState):
        self.state = state
        self.emit(
            {
                "event": "status",
                "state": state.value,
                "wake_word_enabled": self.config["wake_word"]["enabled"],
                "keywords": self.config["wake_word"].get("keywords", []),
                "ts": time.time(),
            }
        )

    def _run_safe(self):
        try:
            self._run()
        except Exception as exc:
            logger.error(f"Engine crashed: {exc}", exc_info=True)
            self.emit(
                {
                    "event": "error",
                    "code": "engine_crash",
                    "message": str(exc),
                    "ts": time.time(),
                }
            )
            self.state = EngineState.STOPPED

    # ── Main audio pipeline ───────────────────────────────────────────────────

    def _run(self):
        import sounddevice as sd  # local import: optional at module level
        import vosk

        cfg_asr = self.config["asr"]
        cfg_ww = self.config["wake_word"]
        cfg_audio = self.config["audio"]

        sample_rate: int = cfg_audio.get("sample_rate", 16000)
        chunk_size: int = cfg_audio.get("chunk_size", 4000)
        max_listen_ms: int = cfg_asr.get("max_listen_ms", 30000)
        max_silence_ms: int = cfg_asr.get("max_silence_ms", 1500)
        energy_threshold: float = cfg_audio.get("energy_threshold", 0.02)

        # ── Load ASR model ────────────────────────────────────────────────────
        model_path = cfg_asr["model_path"]
        logger.info(f"Loading ASR model: {model_path}")
        vosk.SetLogLevel(-1)
        try:
            asr_model = vosk.Model(model_path)
        except Exception as exc:
            self.emit(
                {
                    "event": "error",
                    "code": "model_not_found",
                    "message": (
                        f"ASR model not found at '{model_path}'. "
                        "Run install.bat to download models."
                    ),
                    "ts": time.time(),
                }
            )
            raise
        logger.info("ASR model loaded")

        # ── Load wake word model ──────────────────────────────────────────────
        oww = None
        ww_enabled = cfg_ww.get("enabled", True)
        keywords = cfg_ww.get("keywords", ["hey_jarvis"])
        sensitivity: float = cfg_ww.get("sensitivity", 0.5)

        if ww_enabled:
            try:
                from openwakeword.model import Model as OWWModel  # type: ignore

                logger.info(f"Loading wake word model: {keywords}")
                oww = OWWModel(
                    wakeword_models=keywords,
                    inference_framework="onnx",
                )
                logger.info("Wake word model loaded")
            except Exception as exc:
                logger.warning(
                    f"Wake word model failed to load ({exc}). "
                    "Running in always-listening mode."
                )
                self.emit(
                    {
                        "event": "error",
                        "code": "wake_word_unavailable",
                        "message": str(exc),
                        "ts": time.time(),
                    }
                )
                ww_enabled = False

        # ── Open microphone ───────────────────────────────────────────────────
        audio_q: queue.Queue = queue.Queue(maxsize=200)

        def audio_callback(indata, frames, time_info, status):
            if status:
                logger.debug(f"Audio status: {status}")
            # Drop oldest chunk if queue is full to prevent latency buildup
            if audio_q.full():
                try:
                    audio_q.get_nowait()
                except queue.Empty:
                    pass
            audio_q.put(bytes(indata))

        device = cfg_audio.get("device") or None
        try:
            stream = sd.RawInputStream(
                samplerate=sample_rate,
                blocksize=chunk_size,
                device=device,
                dtype="int16",
                channels=1,
                callback=audio_callback,
            )
        except Exception as exc:
            self.emit(
                {
                    "event": "error",
                    "code": "mic_error",
                    "message": f"Cannot open microphone: {exc}",
                    "ts": time.time(),
                }
            )
            raise

        # ── Main state machine ────────────────────────────────────────────────
        def new_recognizer():
            rec = vosk.KaldiRecognizer(asr_model, sample_rate)
            rec.SetWords(True)
            rec.SetPartialWords(True)
            return rec

        with stream:
            logger.info("Microphone open. Engine running.")
            self._set_state(EngineState.IDLE)

            rec = new_recognizer()
            listen_start: Optional[float] = None
            last_partial = ""

            while not self._stop_event.is_set():
                # ── Get audio chunk ─────────────────────────────────────────
                try:
                    audio_bytes = audio_q.get(timeout=0.2)
                except queue.Empty:
                    # Check for listen timeout
                    if self.state == EngineState.LISTENING and listen_start is not None:
                        elapsed_ms = (time.time() - listen_start) * 1000
                        if elapsed_ms >= max_listen_ms:
                            self._finalize(rec, "timeout")
                            rec = new_recognizer()
                            self._set_state(EngineState.IDLE)
                            listen_start = None
                            last_partial = ""
                    continue

                audio_np = np.frombuffer(audio_bytes, dtype=np.int16)

                # ── IDLE ────────────────────────────────────────────────────
                if self.state == EngineState.IDLE:
                    # Manual trigger
                    if self._trigger_listen.is_set():
                        self._trigger_listen.clear()
                        rec = new_recognizer()
                        listen_start = time.time()
                        last_partial = ""
                        self.emit(
                            {
                                "event": "listening_start",
                                "trigger": "manual",
                                "ts": time.time(),
                            }
                        )
                        self._set_state(EngineState.LISTENING)
                        continue

                    if ww_enabled and oww is not None:
                        # Wake word detection
                        audio_f32 = audio_np.astype(np.float32) / 32768.0
                        try:
                            scores: Dict[str, float] = oww.predict(audio_f32)
                        except Exception as exc:
                            logger.debug(f"Wake word predict error: {exc}")
                            continue

                        for kw in keywords:
                            score = float(scores.get(kw, 0.0))
                            if score >= sensitivity:
                                logger.info(f"Wake word detected: {kw} ({score:.3f})")
                                self.emit(
                                    {
                                        "event": "wake_word",
                                        "keyword": kw,
                                        "score": round(score, 4),
                                        "ts": time.time(),
                                    }
                                )
                                rec = new_recognizer()
                                listen_start = time.time()
                                last_partial = ""
                                self.emit(
                                    {
                                        "event": "listening_start",
                                        "trigger": "wake_word",
                                        "ts": time.time(),
                                    }
                                )
                                self._set_state(EngineState.LISTENING)
                                break
                    else:
                        # No wake word: use energy-based VAD to detect speech start
                        rms = float(np.sqrt(np.mean(audio_np.astype(np.float32) ** 2))) / 32768.0
                        if rms > energy_threshold:
                            rec = new_recognizer()
                            listen_start = time.time()
                            last_partial = ""
                            self.emit(
                                {
                                    "event": "listening_start",
                                    "trigger": "vad",
                                    "ts": time.time(),
                                }
                            )
                            self._set_state(EngineState.LISTENING)
                            # Feed current chunk immediately
                            rec.AcceptWaveform(audio_bytes)

                # ── LISTENING ───────────────────────────────────────────────
                elif self.state == EngineState.LISTENING:
                    # Cancel command
                    if self._cancel_listen.is_set():
                        self._cancel_listen.clear()
                        self.emit(
                            {"event": "listening_end", "reason": "cancelled", "ts": time.time()}
                        )
                        rec = new_recognizer()
                        self._set_state(EngineState.IDLE)
                        listen_start = None
                        last_partial = ""
                        continue

                    # Hard timeout guard
                    if listen_start is not None:
                        elapsed_ms = (time.time() - listen_start) * 1000
                        if elapsed_ms >= max_listen_ms:
                            self._finalize(rec, "timeout")
                            rec = new_recognizer()
                            self._set_state(EngineState.IDLE)
                            listen_start = None
                            last_partial = ""
                            continue

                    # Feed to ASR
                    if rec.AcceptWaveform(audio_bytes):
                        # End of utterance (Vosk detected silence)
                        result = json.loads(rec.Result())
                        text = result.get("text", "").strip()
                        if text:
                            self.emit(
                                {
                                    "event": "transcript",
                                    "text": text,
                                    "is_final": True,
                                    "ts": time.time(),
                                }
                            )
                        self.emit(
                            {"event": "listening_end", "reason": "silence", "ts": time.time()}
                        )
                        rec = new_recognizer()
                        self._set_state(EngineState.IDLE)
                        listen_start = None
                        last_partial = ""
                    else:
                        # Partial / interim result
                        partial_result = json.loads(rec.PartialResult())
                        partial_text = partial_result.get("partial", "").strip()
                        if partial_text and partial_text != last_partial:
                            last_partial = partial_text
                            self.emit(
                                {
                                    "event": "partial",
                                    "text": partial_text,
                                    "ts": time.time(),
                                }
                            )

        self._set_state(EngineState.STOPPED)

    def _finalize(self, rec, reason: str):
        """Emit the final ASR result and listening_end event."""
        try:
            result = json.loads(rec.FinalResult())
            text = result.get("text", "").strip()
            if text:
                self.emit(
                    {
                        "event": "transcript",
                        "text": text,
                        "is_final": True,
                        "ts": time.time(),
                    }
                )
        except Exception as exc:
            logger.debug(f"Finalize error: {exc}")
        self.emit({"event": "listening_end", "reason": reason, "ts": time.time()})
