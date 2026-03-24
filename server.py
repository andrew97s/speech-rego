#!/usr/bin/env python3
"""
Speech Recognition WebSocket Server
Entry point: reads config, starts audio engine, serves WebSocket clients.

WebSocket API
─────────────
Client → Server commands (JSON):
  {"cmd": "start"}                         Start / resume the engine
  {"cmd": "stop"}                          Stop the engine
  {"cmd": "listen"}                        Manually trigger listening
  {"cmd": "cancel"}                        Abort current listening session
  {"cmd": "status"}                        Request current status
  {"cmd": "config", "key": "k", "value": v}  Update a config value at runtime

Server → Client events (JSON):
  {"event": "status",        "state": "idle|listening|stopped", "wake_word_enabled": bool, "keywords": [...], "ts": float}
  {"event": "wake_word",     "keyword": str, "score": float, "ts": float}
  {"event": "listening_start","trigger": "wake_word|manual|vad", "ts": float}
  {"event": "partial",       "text": str, "ts": float}
  {"event": "transcript",    "text": str, "is_final": true, "ts": float}
  {"event": "listening_end", "reason": "silence|timeout|cancelled", "ts": float}
  {"event": "error",         "code": str, "message": str, "ts": float}
  {"event": "ack",           "cmd": str, "ts": float}
  {"event": "config_updated","key": str, "value": any, "ts": float}
"""

import asyncio
import json
import logging
import sys
import time
from typing import Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

from engine import SpeechEngine, EngineState

# ── Windows asyncio policy (required for Python 3.8–3.11 with some APIs) ──────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ── Config helpers ────────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    "host": "127.0.0.1",
    "port": 8765,
    "wake_word": {
        "enabled": True,
        "keywords": ["hey_jarvis"],
        "sensitivity": 0.5,
    },
    "asr": {
        "model_path": "models/vosk-model-small-cn-0.22",
        "max_silence_ms": 1500,
        "max_listen_ms": 30000,
    },
    "audio": {
        "device": None,
        "sample_rate": 16000,
        "chunk_size": 4000,
        "energy_threshold": 0.02,
    },
    "log_level": "INFO",
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if k.startswith("_"):  # skip comment keys
            continue
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path: str = "config.json") -> dict:
    log = logging.getLogger("config")
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        return _deep_merge(_DEFAULTS, user)
    except FileNotFoundError:
        log.warning(f"'{path}' not found, using defaults")
        return dict(_DEFAULTS)
    except json.JSONDecodeError as exc:
        log.error(f"Invalid JSON in '{path}': {exc}")
        return dict(_DEFAULTS)


def setup_logging(level: str = "INFO"):
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ── WebSocket server ──────────────────────────────────────────────────────────

class SpeechServer:
    def __init__(self, config: dict):
        self.config = config
        self.clients: Set[WebSocketServerProtocol] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.engine = SpeechEngine(config, self._on_engine_event)
        self.logger = logging.getLogger("SpeechServer")

    # ── Engine → broadcast ────────────────────────────────────────────────────

    def _on_engine_event(self, event: dict):
        """Called from the engine thread; schedules a broadcast on the event loop."""
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

        # Send current status immediately on connect
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
        ts = time.time()

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
            key = msg.get("key", "")
            value = msg.get("value")
            ok = self.engine.update_config(key, value)
            await ws.send(json.dumps({
                "event": "config_updated" if ok else "error",
                "code": None if ok else "invalid_key",
                "key": key,
                "value": value,
                "ts": ts,
            }))

        else:
            await ws.send(json.dumps({
                "event": "error",
                "code": "unknown_command",
                "message": f"Unknown command: '{cmd}'",
                "ts": ts,
            }))

    def _status_event(self) -> dict:
        return {
            "event": "status",
            "state": self.engine.state.value,
            "wake_word_enabled": self.config["wake_word"]["enabled"],
            "keywords": self.config["wake_word"].get("keywords", []),
            "ts": time.time(),
        }

    # ── Server lifecycle ──────────────────────────────────────────────────────

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.engine.start()

        host = self.config["host"]
        port = self.config["port"]
        ww = self.config["wake_word"]

        border = "=" * 52
        self.logger.info(border)
        self.logger.info("  Speech Recognition WebSocket Service")
        self.logger.info(f"  ws://{host}:{port}")
        self.logger.info(f"  Wake word : {'enabled' if ww['enabled'] else 'disabled'}")
        if ww["enabled"]:
            self.logger.info(f"  Keywords  : {', '.join(ww.get('keywords', []))}")
        self.logger.info(f"  ASR model : {self.config['asr']['model_path']}")
        self.logger.info(border)

        async with websockets.serve(self._handle_client, host, port):
            self.logger.info("Server ready. Press Ctrl+C to stop.")
            await asyncio.Future()  # run forever


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    config = load_config()
    setup_logging(config.get("log_level", "INFO"))
    server = SpeechServer(config)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Server stopped by user.")


if __name__ == "__main__":
    main()
