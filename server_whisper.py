#!/usr/bin/env python3
"""
Whisper Speech Recognition WebSocket Server

Drop-in replacement for server.py that uses faster-whisper as the ASR backend
instead of Vosk, with native mixed Chinese/English support (language=None).

Usage:
  python server_whisper.py

WebSocket API (same as server.py)
-----------------------------------
Client -> Server (JSON):
  {"cmd": "start"}                           Start the engine / open mic
  {"cmd": "stop"}                            Stop the engine / release mic
  {"cmd": "listen"}                          Manually trigger one listen session
  {"cmd": "cancel"}                          Abort current listen session
  {"cmd": "suppress_input", "duration_ms": 1500}  Ignore mic while client plays TTS
  {"cmd": "status"}                          Request current status
  {"cmd": "config", "key": "k", "value": v}  Update a config value at runtime
                                              (e.g. auto_stop_without_clients_ms)

Server config (config.json):
  auto_stop_without_clients_ms  After last WS client disconnects, stop engine
                                and release mic if still no clients (ms); 0=off
                                              (e.g. auto_stop_without_clients_ms)

Top-level config (config.json):
  auto_stop_without_clients_ms  After last WS client disconnects, stop engine
                                and release mic if still running (0 = disabled).

Server -> Client (JSON):
  {"event": "status",          "state": "stopped|no_device|idle|listening", ...}
  {"event": "wake_word",       "keyword": str, "score": float, "ts": float}
  {"event": "listening_start", "trigger": "wake_word|manual|vad", "ts": float}
  {"event": "partial",         "text": str, "ts": float}
  {"event": "transcript",      "text": str, "is_final": true, "ts": float}
  {"event": "listening_end",   "reason": "silence|timeout|cancelled", "ts": float}
  {"event": "error",           "code": str, "message": str, "ts": float}
  {"event": "ack",             "cmd": str, "ts": float}
  {"event": "config_updated",  "key": str, "value": any, "ts": float}

Whisper config section (config.json):
  "whisper": {
    "model":               "base",    // tiny|base|small|medium|large-v3
    "language":            null,      // null=auto/mixed, "zh"=Chinese, "en"=English
    "device":              "cpu",     // cpu | cuda
    "compute_type":        "int8",    // int8 | float16 | float32
    "partial_interval_ms": 2000,      // emit partial every N ms (0 = disable)
    "max_silence_ms":      1500,      // silence duration to end listening
    "max_listen_ms":       30000      // hard cap per session
  }
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
from typing import Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

from engine_whisper import EngineState, SpeechEngine
from text_postprocess import get_postprocess_config

# Windows: use Selector event loop for proper Ctrl+C delivery
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ── Config helpers ─────────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    "host": "127.0.0.1",
    "port": 8766,                  # different port from Vosk server (8765)
    "wake_word": {
        "enabled":     True,
        "mode":        "whisper",   # whisper mode needs no extra dependencies
        "keywords":    ["小智"],
        "prefixes":    ["你好", "嗨", "hi", "hey", "喂", "哎"],
        "aliases":     [],
        "use_grammar": False,
        "sensitivity": 0.5,
        "match_partials": False,
        "partial_stable_hits": 2,
        "wake_max_extra_chars": 1,
    },
    "asr": {
        # Used only for Vosk wake-word model path (if wake_word.mode = "vosk")
        "model_path": "models/vosk-model-small-cn-0.22",
    },
    "whisper": {
        "model":               "base",
        "language":            None,   # None = auto-detect (mixed Chinese/English)
        "device":              "cpu",
        "compute_type":        "int8",
        "partial_interval_ms": 0,
        "verbatim":            True,
        "verbatim_initial_prompt": (
            "以下是普通话口语的逐字转写。请完整保留说话人的原话，"
            "不要改写、不要概括、不要省略、不要改成问句或列表。"
        ),
        "temperature":         0,
        "max_silence_ms":      2000,
        "silence_mode":        "webrtcvad",  # webrtcvad | energy
        "webrtcvad_aggressiveness": 3,
        "webrtcvad_speech_fraction": 0.2,
        "vad_rms_gate": True,
        "vad_rms_min": 0.01,
        "vad_rms_peak_ratio": 0.25,
        "vad_rms_energy_mult": 0.55,
        "vad_rms_buffer_gate": False,
        "silence_near_field_ratio": 0.25,
        "silence_speech_ratio": 0.12,
        "silence_end_ratio":    0.20,
        "max_listen_ms":       30000,
        "vad_cooldown_ms":     500,
        "vad_min_speech_ms":   200,
        "min_listen_ms":       600,    # min recording before silence-end can fire
        "initial_prompt":      None,   # e.g. "以下是普通话，包含中文、数字和英文字母。"
        "post_wake_grace_ms":  3000,   # after wake word, ignore mic (TTS echo; use longer with speakers)
        "output_simplified":   True,   # convert traditional -> simplified Chinese
    },
    "postprocess": {
        "output_simplified":   True,
        "post_wake_grace_ms":  3000,
        "suppress_phrases":    ["我在", "在呢", "我在呢", "嗯", "啊", "好的"],
    },
    "audio": {
        "device":           None,
        "sample_rate":      16000,   # ignored for Whisper (always 16 kHz)
        "chunk_size":       4000,
        "energy_threshold": 0.02,
        "input_channels":   1,      # set 2 for stereo mics; downmixed before ASR
        "stereo_mode":      "mix",  # mix | left | right
    },
    "log_level": "INFO",
    "http_port": 8080,   # 0 = disabled; serve index.html on this port
    # 无 WebSocket 客户端连接超过该时长(ms)后自动 engine.stop() 释放麦克风；0=关闭
    "auto_stop_without_clients_ms": 60000,
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if k.startswith("_"):
            continue
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path: str = "config.json") -> dict:
    log = logging.getLogger("config")
    try:
        with open(path, encoding="utf-8-sig") as f:
            user = json.load(f)
        return _deep_merge(_DEFAULTS, user)
    except FileNotFoundError:
        log.warning(f"'{path}' not found, using defaults")
        return dict(_DEFAULTS)
    except json.JSONDecodeError as exc:
        log.error(f"Invalid JSON in '{path}': {exc}")
        return dict(_DEFAULTS)


def save_config(config: dict, path: str = "config.json"):
    def _strip(d: dict) -> dict:
        return {k: _strip(v) if isinstance(v, dict) else v
                for k, v in d.items() if not k.startswith("_")}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_strip(config), f, ensure_ascii=False, indent=4)
    except Exception as exc:
        logging.getLogger("config").warning(f"Failed to save config: {exc}")


def setup_logging(level: str = "INFO"):
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level   = numeric,
        format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt = "%H:%M:%S",
        handlers = [logging.StreamHandler(sys.stdout)],
    )


# ── WebSocket server ───────────────────────────────────────────────────────────

class SpeechServer:
    def __init__(self, config: dict):
        self.config  = config
        self.clients: Set[WebSocketServerProtocol] = set()
        self.loop:    Optional[asyncio.AbstractEventLoop] = None
        self.engine  = SpeechEngine(config, self._on_engine_event)
        self.logger  = logging.getLogger("SpeechServer-Whisper")
        self._no_clients_since: Optional[float] = None

    def _auto_stop_ms(self) -> int:
        return max(0, int(self.config.get("auto_stop_without_clients_ms", 0)))

    def _update_server_config(self, key: str, value) -> bool:
        if key == "auto_stop_without_clients_ms":
            self.config[key] = max(0, int(float(value)))
            self.logger.info(
                "Config updated: auto_stop_without_clients_ms = %d",
                self.config[key],
            )
            return True
        return False

    async def _tick_auto_stop_without_clients(self) -> None:
        """Stop engine when no WS clients remain connected for configured duration."""
        ms = self._auto_stop_ms()
        if ms <= 0:
            self._no_clients_since = None
            return
        if self.clients:
            self._no_clients_since = None
            return
        if self.engine.state == EngineState.STOPPED:
            self._no_clients_since = None
            return
        now = time.time()
        if self._no_clients_since is None:
            self._no_clients_since = now
            return
        if (now - self._no_clients_since) * 1000 >= ms:
            self.logger.info(
                "No WebSocket clients for %d ms — stopping engine (microphone released)",
                ms,
            )
            self.engine.stop()
            self._no_clients_since = None

    # ── Engine -> broadcast ───────────────────────────────────────────────────

    def _on_engine_event(self, event: dict):
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(event), self.loop)

    async def _broadcast(self, event: dict):
        if not self.clients:
            return
        message = json.dumps(event, ensure_ascii=False)
        dead: Set[WebSocketServerProtocol] = set()
        for ws in self.clients.copy():
            try:
                await ws.send(message)
            except websockets.ConnectionClosed:
                dead.add(ws)
        self.clients -= dead

    # ── Client handler ────────────────────────────────────────────────────────

    async def _handle_client(self, websocket: WebSocketServerProtocol):
        addr = websocket.remote_address
        self.logger.info(f"Client connected: {addr}")
        self.clients.add(websocket)
        self._no_clients_since = None
        await websocket.send(json.dumps(self._status_event()))
        try:
            async for raw in websocket:
                await self._dispatch(websocket, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            self.logger.info(f"Client disconnected: {addr}")
            if (not self.clients
                    and self.engine.state != EngineState.STOPPED
                    and self._auto_stop_ms() > 0):
                self._no_clients_since = time.time()

    async def _dispatch(self, ws: WebSocketServerProtocol, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send(json.dumps({
                "event": "error", "code": "invalid_json",
                "message": "Message is not valid JSON", "ts": time.time(),
            }))
            return

        cmd = msg.get("cmd", "")
        ts  = time.time()

        if cmd == "start":
            self.engine.start()
            await ws.send(json.dumps({"event": "ack", "cmd": "start", "ts": ts}))

        elif cmd == "stop":
            self.engine.stop()
            await ws.send(json.dumps({"event": "ack", "cmd": "stop", "ts": ts}))

        elif cmd == "listen":
            # 判断麦克分设备是否可用
            self.engine.trigger_listen()
            await ws.send(json.dumps({"event": "ack", "cmd": "listen", "ts": ts}))
            await ws.send(json.dumps(self._status_event()))
        elif cmd == "cancel":
            self.engine.cancel_listen()
            await ws.send(json.dumps({"event": "ack", "cmd": "cancel", "ts": ts}))

        elif cmd == "suppress_input":
            duration_ms = int(msg.get("duration_ms", 1500))
            self.engine.suppress_input(duration_ms)
            await ws.send(json.dumps({
                "event": "ack", "cmd": "suppress_input", "duration_ms": duration_ms, "ts": ts,
            }))

        elif cmd == "check":
            # Run env check in a thread; results sent back as "env_check" event
            async def _run_check(ws=ws, ts=ts):
                try:
                    loop = asyncio.get_running_loop()
                    from check_env import run_checks
                    items = await loop.run_in_executor(None, run_checks)
                    await ws.send(json.dumps({
                        "event": "env_check", "items": items, "ts": time.time(),
                    }))
                except Exception as exc:
                    await ws.send(json.dumps({
                        "event": "error", "code": "check_failed",
                        "message": str(exc), "ts": time.time(),
                    }))
            asyncio.create_task(_run_check())
            await ws.send(json.dumps({"event": "ack", "cmd": "check", "ts": ts}))

        elif cmd == "status":
            await ws.send(json.dumps(self._status_event()))

        elif cmd == "config":
            key   = msg.get("key", "")
            value = msg.get("value")
            ok    = self.engine.update_config(key, value)
            if not ok:
                ok = self._update_server_config(key, value)
            if ok:
                save_config(self.config)
                # wake_word.* 变更后异步 preload（勿用 create_task(run_in_executor(..))：
                # run_in_executor 返回 Future，create_task 只接受协程，会 TypeError 导致整条连接被断开）
                if key.startswith("wake_word.") and self.loop:
                    eng = self.engine

                    async def _preload_after_wake():
                        try:
                            await asyncio.get_running_loop().run_in_executor(
                                None, eng.preload
                            )
                        except Exception as exc:
                            self.logger.exception(
                                "preload failed after wake_word config: %s", exc
                            )

                    asyncio.create_task(_preload_after_wake())
            await ws.send(json.dumps({
                "event": "config_updated" if ok else "error",
                "code":  None if ok else "invalid_key",
                "key":   key,
                "value": value,
                "ts":    ts,
            }))

        else:
            await ws.send(json.dumps({
                "event":   "error",
                "code":    "unknown_command",
                "message": f"Unknown command: '{cmd}'",
                "ts":      ts,
            }))

    def _status_event(self) -> dict:
        ww = self.config["wake_word"]
        w  = self.config.get("whisper", {})
        _pp = get_postprocess_config(self.config)
        return {
            "event":                  "status",
            "state":                  self.engine.state.value,
            "wake_word_enabled":      ww.get("enabled", True),
            "keywords":               ww.get("keywords", []),
            "mode":                   ww.get("mode", "auto"),
            "whisper_max_silence_ms": w.get("max_silence_ms", 2500),
            "whisper_min_listen_ms":  w.get("min_listen_ms", 600),
            "whisper_max_listen_ms":  w.get("max_listen_ms", 30000),
            "post_wake_grace_ms":     int(_pp.get("post_wake_grace_ms", 1500)),
            "auto_stop_without_clients_ms": self._auto_stop_ms(),
            "ts":                     time.time(),
        }

    # ── Server lifecycle ──────────────────────────────────────────────────────

    async def run(self):
        self.loop = asyncio.get_running_loop()

        host     = self.config["host"]
        port     = self.config["port"]
        ww       = self.config["wake_word"]
        wcfg     = self.config["whisper"]
        language = wcfg.get("language")
        lang_str = language if language else "auto (中文/English 混合)"

        border = "=" * 56
        self.logger.info(border)
        self.logger.info("  Whisper Speech Recognition WebSocket Service")
        self.logger.info(f"  ws://{host}:{port}")
        self.logger.info(f"  Wake word  : {'enabled' if ww['enabled'] else 'disabled'}")
        if ww["enabled"]:
            self.logger.info(f"  Keywords   : {', '.join(ww.get('keywords', []))}")
        self.logger.info(f"  Model      : {wcfg.get('model', 'base')}")
        self.logger.info(f"  Language   : {lang_str}")
        self.logger.info(f"  Device     : {wcfg.get('device', 'cpu')} / {wcfg.get('compute_type', 'int8')}")
        http_port = self.config.get("http_port", 8080)
        if http_port:
            self.logger.info(f"  UI         : http://127.0.0.1:{http_port}/index.html")
        idle_ms = self._auto_stop_ms()
        if idle_ms > 0:
            self.logger.info(
                f"  Auto-stop  : no WS clients for {idle_ms} ms -> release microphone"
            )
        else:
            self.logger.info("  Auto-stop  : disabled (auto_stop_without_clients_ms=0)")
        self.logger.info(border)

        # Pre-load Whisper model + wake word detector before accepting connections
        # so that the first start() command from the client is near-instant.
        self.logger.info("Pre-loading models (this may take a moment on first run)…")
        try:
            await self.loop.run_in_executor(None, self.engine.preload)
            self.logger.info("Models ready.  Server accepting connections.")
        except Exception as exc:
            self.logger.error(
                f"Model pre-load failed: {exc}.  "
                "start() will retry loading when called by the client."
            )

        try:
            async with websockets.serve(self._handle_client, host, port):
                self.logger.info(
                    "Open index.html (change port to 8766) to connect.  "
                    "Ctrl+C to stop."
                )
                while True:
                    await self._tick_auto_stop_without_clients()
                    await asyncio.sleep(0.5)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            self.logger.info("Shutting down Whisper engine…")
            self.engine.stop()
            self.logger.info("Server stopped.")


# ── HTTP static server ─────────────────────────────────────────────────────────

def _start_http_server(port: int, directory: str):
    """Serve *directory* over HTTP on *port* in a daemon thread.
    Silently disabled when port == 0."""
    if not port:
        return
    import http.server
    handler = http.server.SimpleHTTPRequestHandler

    class _QuietHandler(handler):
        """Suppress per-request log lines to keep the console clean."""
        def log_message(self, fmt, *args):   # noqa: ARG002
            pass
        def log_error(self, fmt, *args):
            logging.getLogger("http").warning(fmt % args)

    def _serve():
        os.chdir(directory)
        with http.server.HTTPServer(("", port), _QuietHandler) as httpd:
            logging.getLogger("http").info(
                f"HTTP server: http://127.0.0.1:{port}/index.html"
            )
            httpd.serve_forever()

    t = threading.Thread(target=_serve, daemon=True, name="HTTPServer")
    t.start()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    # Ensure CWD = script directory so relative paths in config.json
    # (e.g. "models/whisper-small") resolve correctly regardless of how
    # the server was launched.
    app_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(app_dir)
    config = load_config()
    setup_logging(config.get("log_level", "INFO"))
    http_port = config.get("http_port", 8080)
    _start_http_server(http_port, app_dir)
    server = SpeechServer(config)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
