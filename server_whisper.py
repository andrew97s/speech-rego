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
  {"cmd": "status"}                          Request current status
  {"cmd": "config", "key": "k", "value": v}  Update a config value at runtime

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
import sys
import time
from typing import Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

from engine_whisper import SpeechEngine, EngineState

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
        "sensitivity": 0.5,
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
        "partial_interval_ms": 2000,
        "max_silence_ms":      2500,   # ms of silence to end session (2500 = natural pause)
        "max_listen_ms":       30000,
        "vad_cooldown_ms":     500,
        "vad_min_speech_ms":   200,
        "min_listen_ms":       600,    # min recording before silence-end can fire
        "initial_prompt":      None,   # e.g. "以下是普通话，包含中文、数字和英文字母。"
    },
    "audio": {
        "device":           None,
        "sample_rate":      16000,   # ignored for Whisper (always 16 kHz)
        "chunk_size":       4000,
        "energy_threshold": 0.02,
    },
    "log_level": "INFO",
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
        await websocket.send(json.dumps(self._status_event()))
        try:
            async for raw in websocket:
                await self._dispatch(websocket, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            self.logger.info(f"Client disconnected: {addr}")

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
            self.engine.trigger_listen()
            await ws.send(json.dumps({"event": "ack", "cmd": "listen", "ts": ts}))

        elif cmd == "cancel":
            self.engine.cancel_listen()
            await ws.send(json.dumps({"event": "ack", "cmd": "cancel", "ts": ts}))

        elif cmd == "status":
            await ws.send(json.dumps(self._status_event()))

        elif cmd == "config":
            key   = msg.get("key", "")
            value = msg.get("value")
            ok    = self.engine.update_config(key, value)
            if ok:
                save_config(self.config)
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
        return {
            "event":             "status",
            "state":             self.engine.state.value,
            "wake_word_enabled": self.config["wake_word"]["enabled"],
            "keywords":          self.config["wake_word"].get("keywords", []),
            "ts":                time.time(),
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
        self.logger.info(border)

        try:
            async with websockets.serve(self._handle_client, host, port):
                self.logger.info(
                    "Server ready.  Open index.html (change port to 8766) to connect.  "
                    "Ctrl+C to stop."
                )
                while True:
                    await asyncio.sleep(0.5)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            self.logger.info("Shutting down Whisper engine…")
            self.engine.stop()
            self.logger.info("Server stopped.")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    config = load_config()
    setup_logging(config.get("log_level", "INFO"))
    server = SpeechServer(config)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
