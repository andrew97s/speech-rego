"""
语音识别引擎：Sherpa KWS 唤醒 → FunASR fsmn-vad 判停 → 远程 Fun-ASR-Nano。

LISTENING 只缓冲音频；说完后把整段 PCM 提交给 asr_server.py。
VAD 说明见 docs/VAD.md（ASR 判停 + 唤醒门控）。
"""

import logging
import queue
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, List, Optional

import numpy as np

from funasr_asr import (
    FunASRRuntime,
    asr_language_from_config,
    funasr_cfg,
    funasr_model_key,
    hotword_list_from_config,
    load_funasr_runtime,
)
from remote_asr import RemoteAsrError, remote_asr_enabled, transcribe_remote
from speech_vad import (
    chunk_is_speech,
    create_funasr_vad,
    silero_speech_fraction_from_config,
)
from text_postprocess import get_postprocess_config, postprocess_transcript
from wake_config import get_wake_word_options
from wake_detectors import SherpaKWSWakeWordDetector
from wake_gating import WakeUtteranceGate

logger = logging.getLogger(__name__)

_DEVICE_RETRY_SEC = 3.0
_ASR_SAMPLE_RATE = 16000


# ── Helpers ────────────────────────────────────────────────────────────────────

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
    """LISTENING 判停：当前麦克风块是否仍算「在说话」。"""
    if speech_vad is None:
        return False
    return chunk_is_speech(
        audio_bytes,
        speech_vad,
        sample_rate,
        min_fraction=vad_fraction,
    )


def _reset_speech_vad(vad: Any) -> None:
    """LISTENING 开始/结束或回 IDLE 时重置 VAD cache，避免句间污染。"""
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
    """Drop leading loud chunks (speaker TTS bleed) before final ASR flush."""
    if len(buf) < 3:
        return buf
    loud = energy_threshold * 1.35
    skip = 0
    for chunk in buf[: min(len(buf), 24)]:
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


# ── Engine state ───────────────────────────────────────────────────────────────

class EngineState(Enum):
    """引擎状态枚举，随 status 事件广播给客户端。"""

    STOPPED = "stopped"
    NO_DEVICE = "no_device"
    IDLE = "idle"
    LISTENING = "listening"


# ── Main engine ────────────────────────────────────────────────────────────────

class SpeechEngine:
    """
    Windows 客户端引擎：Sherpa KWS 唤醒 + fsmn-vad 判停 + 远程句级识别。
    """

    def __init__(self, config: dict, event_callback: Callable[[dict], None]):
        """
        Args:
            config: 完整 config.json 字典
            event_callback: 引擎产生事件时调用 callback(event_dict)
        """
        self.config = config
        self.emit = event_callback
        self.state = EngineState.STOPPED

        self._stop_event = threading.Event()
        self._trigger_listen = threading.Event()
        self._cancel_listen = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._asr_runtime: Optional[FunASRRuntime] = None
        self._model_key: Optional[tuple] = None
        self._ww_detector: Optional[object] = None
        self._ww_key: Optional[tuple] = None
        self._preload_lock = threading.Lock()
        self._suppress_input_until: float = 0.0
        self._wake_paused_until_listen: bool = False
        self._last_wake_emit_at: float = 0.0

    # ── Public control API ────────────────────────────────────────────────────

    def start(self):
        """启动后台音频线程；重置 pause_until_listen 标志。"""
        if self._thread and self._thread.is_alive():
            logger.warning("Engine already running")
            return
        self._stop_event.clear()
        self._wake_paused_until_listen = False
        self._thread = threading.Thread(
            target=self._run_safe, daemon=True, name="SpeechEngine-Client"
        )
        self._thread.start()
        logger.info("Speech engine started")

    def stop(self):
        """停止线程；FunASR/唤醒模型缓存保留在内存。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._set_state(EngineState.STOPPED)
        logger.info("FunASR engine stopped")

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
        """运行时更新 config；wake_word.* / funasr.* 等会 invalidate 缓存。"""
        try:
            parts = key.split(".")
            if parts[0] == "asr_remote":
                self.config.setdefault("asr_remote", {
                    "enabled": True,
                    "url": "",
                    "timeout_sec": 60,
                    "token": "",
                })
            cfg = self.config
            for part in parts[:-1]:
                cfg = cfg[part]
            int_keys = (
                "whisper.max_silence_ms",
                "whisper.min_listen_ms",
                "whisper.max_listen_ms",
                "whisper.vad_cooldown_ms",
                "whisper.vad_min_speech_ms",
                "whisper.post_wake_grace_ms",
                "postprocess.post_wake_grace_ms",
                "funasr.ncpu",
                "funasr.vad_chunk_ms",
                "asr_remote.timeout_sec",
            )
            float_keys = (
                "whisper.silero_threshold",
                "whisper.silero_speech_fraction",
            )
            if key in int_keys and value is not None:
                value = int(float(value))
            elif key in float_keys and value is not None:
                value = float(value)
            cfg[parts[-1]] = value
            if key in ("whisper.post_wake_grace_ms", "postprocess.post_wake_grace_ms"):
                self.config.setdefault("whisper", {})["post_wake_grace_ms"] = value
                self.config.setdefault("postprocess", {})["post_wake_grace_ms"] = value
            logger.info(f"Config updated: {key} = {value!r}")
            if key.startswith("wake_word."):
                self._ww_key = None
            if key.startswith("funasr.") or key.startswith("asr_remote.") or key in (
                "whisper.domain_keywords",
                "funasr.hotword",
            ):
                self._model_key = None
            return True
        except (KeyError, TypeError):
            logger.warning(f"Invalid config key: {key!r}")
            return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _listen_cfg(self) -> dict:
        return self.config.get("whisper") or {}

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
        w = self._listen_cfg()
        _pp = get_postprocess_config(self.config)
        _pp_raw = self.config.get("postprocess") or {}
        f = funasr_cfg(self.config)
        self.emit({
            "event": "status",
            "state": state.value,
            "wake_word_enabled": ww.get("enabled", True),
            "keywords": ww.get("keywords", []),
            "mode": "wake_client_remote_asr",
            "asr_model": f.get("asr_model", "FunAudioLLM/Fun-ASR-Nano-2512"),
            "asr_remote": remote_asr_enabled(self.config),
            "vad_model": f.get("vad_model", "fsmn-vad"),
            "whisper_max_silence_ms": w.get("max_silence_ms", 2500),
            "whisper_min_listen_ms": w.get("min_listen_ms", 600),
            "whisper_max_listen_ms": w.get("max_listen_ms", 30000),
            "post_wake_grace_ms": int(_pp.get("post_wake_grace_ms", 1500)),
            "replacements": _pp_raw.get("replacements") or {},
            "ts": time.time(),
        })

    def _run_safe(self):
        """_run 的异常包装：崩溃时 emit error 并将 state 置 STOPPED。"""
        try:
            self._run()
        except Exception as exc:
            logger.error(f"Speech engine crashed: {exc}", exc_info=True)
            self.emit({
                "event": "error", "code": "engine_crash",
                "message": str(exc), "ts": time.time(),
            })
            self.state = EngineState.STOPPED

    def _postprocess_text(self, text: str) -> str:
        """调用 text_postprocess.postprocess_transcript 做后处理。"""
        cfg = get_postprocess_config(self.config)
        logger.info("post转换前识别结果:%s", text)
        result_str = postprocess_transcript(text, cfg)
        logger.info("post转换后识别结果:%s", result_str)
        return result_str

    def _finalize(
        self,
        buf: List[bytes],
        reason: str,
        *,
        speech_vad: Any = None,
    ):
        """
        LISTENING 结束：把缓冲整段交给远程 FunASR（或本地兜底）→ 后处理 → 发事件。
        """
        eth = float(self.config.get("audio", {}).get("energy_threshold", 0.02))
        w = self._listen_cfg()
        original = list(buf)
        trimmed = _trim_leading_echo(buf, eth)
        if len(trimmed) < len(original):
            logger.debug(
                "Trimmed %d leading echo chunk(s) before ASR (speaker bleed)",
                len(original) - len(trimmed),
            )
        if speech_vad is not None and hasattr(speech_vad, "flush"):
            try:
                speech_vad.flush()
            except Exception:
                pass

        dur_s = sum(len(c) for c in trimmed) / (_ASR_SAMPLE_RATE * 2)
        logger.info(
            "Listening end (%s): %.2f s audio buffered for ASR",
            reason,
            dur_s,
        )

        raw = ""
        pcm = b"".join(trimmed)
        if pcm:
            if remote_asr_enabled(self.config):
                self.emit({
                    "event": "recognizing",
                    "duration_s": round(dur_s, 2),
                    "ts": time.time(),
                })
                try:
                    raw = transcribe_remote(
                        pcm,
                        self.config,
                        language=asr_language_from_config(self.config),
                        hotwords=hotword_list_from_config(self.config),
                        sample_rate=_ASR_SAMPLE_RATE,
                    )
                except RemoteAsrError as exc:
                    logger.error("Remote ASR failed: %s", exc)
                    self.emit({
                        "event": "error",
                        "code": "asr_remote_failed",
                        "message": str(exc),
                        "ts": time.time(),
                    })
            else:
                rt = self._asr_runtime
                if rt is not None:
                    raw = rt.transcribe_pcm(pcm)

        text = self._postprocess_text(raw)
        if raw and not text:
            logger.debug(f"Transcript suppressed: {raw!r}")
        if text:
            self.emit({
                "event": "transcript",
                "text": text, "is_final": True, "ts": time.time(),
            })
        elif dur_s >= 0.35:
            lvl = _buffer_level(trimmed)
            hint = ""
            if dur_s < float(w.get("min_transcribe_sec", 1.5)):
                hint = (
                    f" — audio shorter than min_transcribe_sec="
                    f"{w.get('min_transcribe_sec', 1.5)}s"
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
        """预加载 VAD / 唤醒检测器（以及未启用远程时的本地 ASR）。"""
        self._ensure_models_loaded()

    def _ensure_models_loaded(self):
        with self._preload_lock:
            self._ensure_models_loaded_unlocked()

    def _ensure_models_loaded_unlocked(self):
        cfg_ww = self.config["wake_word"]
        remote = remote_asr_enabled(self.config)
        components = ("vad",) if remote else ("asr", "vad")
        model_key = funasr_model_key(self.config, components=components)
        if self._asr_runtime is not None and self._model_key == model_key:
            logger.info("[preload] models already loaded — skipping.")
        else:
            f = funasr_cfg(self.config)
            logger.info(
                "[preload] Loading FunASR components=%s vad=%s device=%s remote=%s …",
                ",".join(components),
                f.get("vad_model", "fsmn-vad"),
                f.get("device", "cpu"),
                remote,
            )
            try:
                runtime = load_funasr_runtime(
                    self.config,
                    base_dir=Path(__file__).resolve().parent,
                    components=components,
                )
            except Exception as exc:
                self.emit({
                    "event": "error",
                    "code": "model_not_found",
                    "message": str(exc),
                    "ts": time.time(),
                })
                raise
            self._asr_runtime = runtime
            self._model_key = model_key
            logger.info(
                "[preload] ready — asr=%s vad=%s device=%s",
                runtime.asr_name,
                runtime.vad_name,
                runtime.device,
            )

        ww_enabled = cfg_ww.get("enabled", True)
        ww_opts = get_wake_word_options(cfg_ww)
        sk = ww_opts["sherpa_kws"]
        sensitivity = cfg_ww.get("sensitivity", 0.5)
        ww_key = (
            tuple(ww_opts["keywords"]),
            sensitivity,
            sk.get("model_dir"),
            sk.get("chunk_size"),
            sk.get("use_int8"),
            sk.get("provider"),
            sk.get("keywords_file"),
            sk.get("keywords_threshold"),
            ww_opts["sherpa_debounce_sec"],
        )
        if self._ww_detector is not None and self._ww_key == ww_key:
            logger.info("[preload] Wake word detector already loaded — skipping.")
        else:
            ww_detector = None
            if ww_enabled and ww_opts["keywords"]:
                try:
                    ww_detector = SherpaKWSWakeWordDetector(
                        ww_opts["keywords"],
                        _ASR_SAMPLE_RATE,
                        sherpa_cfg=ww_opts["sherpa_kws"],
                        sensitivity=sensitivity,
                        debounce_sec=ww_opts["sherpa_debounce_sec"],
                        base_dir=Path(__file__).resolve().parent,
                    )
                except Exception as exc:
                    logger.error("[preload] Wake word init failed: %s", exc)
                    self.emit({
                        "event": "error",
                        "code": "wake_word_init_failed",
                        "message": str(exc),
                        "ts": time.time(),
                    })
            self._ww_detector = ww_detector
            self._ww_key = ww_key
            logger.info(
                "[preload] Wake word: "
                + ("enabled" if ww_enabled else "disabled")
                + (" [sherpa_kws]" if ww_enabled else "")
            )

    # ── Main audio pipeline ───────────────────────────────────────────────────

    def _run(self):
        """
        主音频循环（在后台线程运行）。

        打开麦克风 → IDLE 唤醒扫描 / LISTENING 缓冲 + FunASR VAD 判停 / 说完后整段 ASR。
        """
        import sounddevice as sd

        cfg_audio = self.config["audio"]
        sample_rate: int = _ASR_SAMPLE_RATE
        chunk_size: int = cfg_audio.get("chunk_size", 4000)
        energy_threshold: float = cfg_audio.get("energy_threshold", 0.02)
        device = cfg_audio.get("device") or None
        input_channels: int = int(cfg_audio.get("input_channels", 1))
        stereo_mode: str = str(cfg_audio.get("stereo_mode", "mix"))
        if input_channels not in (1, 2):
            input_channels = 1

        def _wparams():
            w = self._listen_cfg()
            f = funasr_cfg(self.config)
            return (
                w.get("max_silence_ms", 2000),
                w.get("max_listen_ms", 30000),
                w.get("vad_cooldown_ms", 500),
                w.get("min_listen_ms", 1000),
                float(w.get("min_transcribe_sec", 1.5)),
                silero_speech_fraction_from_config(w),
                int(f.get("vad_chunk_ms", 200)),
            )

        (
            max_silence_ms, max_listen_ms,
            vad_cooldown_ms, min_listen_ms, min_transcribe_sec,
            silero_speech_fraction, vad_chunk_ms,
        ) = _wparams()
        chunk_ms = max(1.0, chunk_size / sample_rate * 1000.0)

        self._ensure_models_loaded()
        runtime = self._asr_runtime

        while not self._stop_event.is_set():

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
                    samplerate=sample_rate,
                    blocksize=chunk_size,
                    device=device,
                    dtype="int16",
                    channels=input_channels,
                    callback=_audio_cb,
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

            device_error: Optional[str] = None

            try:
                with stream:
                    logger.info(
                        "Microphone open.  Wake/VAD client running.  "
                        f"stream={input_channels}ch->{stereo_mode if input_channels > 1 else 'mono'} "
                        f"@ {sample_rate} Hz"
                    )
                    self._set_state(EngineState.IDLE)

                    listen_buf: List[bytes] = []
                    listen_start: Optional[float] = None
                    silence_start: Optional[float] = None
                    _vad_cooldown_until: float = 0.0
                    _input_grace_until: float = 0.0
                    _listen_peak: float = 0.0
                    _listen_noise_floor: float = energy_threshold * 0.5
                    _listen_silent_chunks: int = 0
                    _listen_clock_after_grace: bool = False

                    _speech_vad = create_funasr_vad(
                        runtime.vad_model if runtime is not None else None,
                        sample_rate=sample_rate,
                        chunk_ms=vad_chunk_ms,
                    )
                    if _speech_vad is None:
                        logger.error("[VAD] FunASR fsmn-vad unavailable")
                        self.emit({
                            "event": "error",
                            "code": "funasr_vad_unavailable",
                            "message": (
                                "FunASR VAD 不可用，无法判停录音。"
                                "请安装 funasr 并确认 fsmn-vad 已加载。"
                            ),
                            "ts": time.time(),
                        })
                    _ww_gate = WakeUtteranceGate()
                    _ww_gate_cfg = get_wake_word_options(self.config["wake_word"])

                    def _end_listening(reason: str) -> None:
                        nonlocal listen_buf, listen_start, silence_start
                        nonlocal _input_grace_until, _listen_clock_after_grace
                        nonlocal _listen_silent_chunks, _listen_peak
                        nonlocal _listen_noise_floor, _vad_cooldown_until
                        self._finalize(
                            listen_buf, reason, speech_vad=_speech_vad,
                        )
                        listen_buf = []
                        listen_start = None
                        silence_start = None
                        _input_grace_until = 0.0
                        _listen_clock_after_grace = False
                        _listen_silent_chunks = 0
                        _listen_peak = 0.0
                        _listen_noise_floor = energy_threshold * 0.5
                        _vad_cooldown_until = time.time() + vad_cooldown_ms / 1000
                        _reset_speech_vad(_speech_vad)
                        self._set_state(EngineState.IDLE)

                    def _begin_listening(audio_bytes: bytes, trigger: str, level: float) -> None:
                        nonlocal listen_buf, listen_start, silence_start
                        nonlocal _vad_cooldown_until, _input_grace_until
                        nonlocal _listen_peak, _listen_noise_floor
                        listen_buf = [audio_bytes]
                        listen_start = time.time()
                        silence_start = None
                        _vad_cooldown_until = 0.0
                        _input_grace_until = 0.0
                        _listen_peak = level
                        _listen_noise_floor = min(energy_threshold * 0.5, level)
                        _reset_speech_vad(_speech_vad)
                        if _speech_vad is not None:
                            chunk_is_speech(
                                audio_bytes, _speech_vad, sample_rate, min_fraction=0.5
                            )
                        self.emit({
                            "event": "listening_start",
                            "trigger": trigger, "ts": time.time(),
                        })
                        self._set_state(EngineState.LISTENING)

                    while not self._stop_event.is_set():

                        if not stream.active:
                            device_error = "Stream became inactive (device removed?)"
                            break

                        try:
                            audio_bytes = audio_q.get(timeout=0.2)
                        except queue.Empty:
                            if (self.state == EngineState.LISTENING
                                    and listen_start is not None):
                                (
                                    max_silence_ms, max_listen_ms,
                                    vad_cooldown_ms, min_listen_ms, min_transcribe_sec,
                                    silero_speech_fraction, vad_chunk_ms,
                                ) = _wparams()
                                if (time.time() - listen_start) * 1000 >= max_listen_ms:
                                    _end_listening("timeout")
                            continue

                        (
                            max_silence_ms, max_listen_ms,
                            vad_cooldown_ms, min_listen_ms, min_transcribe_sec,
                            silero_speech_fraction, vad_chunk_ms,
                        ) = _wparams()
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
                                _begin_listening(audio_bytes, "manual", level)
                                continue

                            _ww_det = self._ww_detector
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
                                listen_buf = []
                                listen_start = None
                                silence_start = None
                                _input_grace_until = 0.0
                                _vad_cooldown_until = time.time() + vad_cooldown_ms / 1000
                                _reset_speech_vad(_speech_vad)
                                self._set_state(EngineState.IDLE)
                                continue

                            if (listen_start is not None
                                    and (time.time() - listen_start) * 1000 >= max_listen_ms):
                                _end_listening("timeout")
                                continue

                            _now = time.time()
                            if (_now < _input_grace_until
                                    or _now < self._suppress_input_until):
                                listen_buf.append(audio_bytes)
                                _listen_peak = max(_listen_peak, level)
                                silence_start = None
                                _listen_silent_chunks = 0
                                continue

                            if _listen_clock_after_grace:
                                listen_start = _now
                                _listen_clock_after_grace = False

                            _listen_peak = max(_listen_peak, level)
                            listen_buf.append(audio_bytes)
                            _listen_buf_sec = sum(
                                len(c) for c in listen_buf
                            ) / (_ASR_SAMPLE_RATE * 2)

                            still_speaking = _still_speaking_for_end(
                                audio_bytes,
                                _speech_vad,
                                sample_rate,
                                0.5,
                            )
                            vad_ended = False
                            if _speech_vad is not None and hasattr(
                                _speech_vad, "consume_speech_end"
                            ):
                                vad_ended = bool(_speech_vad.consume_speech_end())

                            can_stop = (
                                listen_start is not None
                                and (time.time() - listen_start) * 1000
                                >= min_listen_ms
                                and _listen_buf_sec >= min_transcribe_sec
                            )

                            if vad_ended and can_stop:
                                logger.debug(
                                    "Listening end (funasr-vad speech end)"
                                )
                                _end_listening("silence")
                                continue

                            if still_speaking:
                                silence_start = None
                                _listen_silent_chunks = 0
                            else:
                                if silence_start is None:
                                    silence_start = _now
                                _listen_silent_chunks += 1
                                silent_ms = (_now - silence_start) * 1000
                                if can_stop and (
                                    _listen_silent_chunks >= _silence_chunks_needed
                                    or silent_ms >= max_silence_ms
                                ):
                                    logger.debug(
                                        "Listening end (funasr-vad silence: %d chunks "
                                        "~%d ms, wall %d ms)",
                                        _listen_silent_chunks,
                                        int(_listen_silent_chunks * chunk_ms),
                                        int(silent_ms),
                                    )
                                    _end_listening("silence")
                                    continue

            except Exception as exc:
                device_error = str(exc)

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
