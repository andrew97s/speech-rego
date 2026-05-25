#!/usr/bin/env python3
"""
Speech Recognition WebSocket Server

WebSocket API
-------------
Client -> Server (JSON):
  {"cmd": "start"}                           Start the engine / open mic
  {"cmd": "stop"}                            Stop the engine / release mic
  {"cmd": "listen"}                          Manually trigger one listen session
  {"cmd": "cancel"}                          Abort current listen session
  {"cmd": "suppress_input", "duration_ms": 1500}  Ignore mic while client plays TTS
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

# Windows: use Selector event loop for proper signal / Ctrl+C delivery
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ── Config helpers ─────────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    "host": "127.0.0.1",
    "port": 8765,
    "wake_word": {
        "enabled": True,
        "keywords": ["hey_jarvis"],
        "prefixes": ["你好", "嗨", "hi", "hey"],
        "aliases": [],
        "use_grammar": False,
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
    "postprocess": {
        "output_simplified":   True,
        "post_wake_grace_ms":  1500,
        "suppress_phrases":    ["我在", "在呢", "我在呢", "嗯", "啊", "好的"],
    },
    "log_level": "INFO",
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
    """Write runtime config back to disk so changes survive process restarts.
    Keys starting with '_' (notes/comments) are stripped from the output."""
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
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ── WebSocket server ───────────────────────────────────────────────────────────

class SpeechServer:
    def __init__(self, config: dict):
        self.config = config
        self.clients: Set[WebSocketServerProtocol] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.engine = SpeechEngine(config, self._on_engine_event)
        self.logger = logging.getLogger("SpeechServer")
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
            self.engine.trigger_listen()
            await ws.send(json.dumps({"event": "ack", "cmd": "listen", "ts": ts}))

        elif cmd == "cancel":
            self.engine.cancel_listen()
            await ws.send(json.dumps({"event": "ack", "cmd": "cancel", "ts": ts}))

        elif cmd == "suppress_input":
            duration_ms = int(msg.get("duration_ms", 1500))
            self.engine.suppress_input(duration_ms)
            await ws.send(json.dumps({
                "event": "ack", "cmd": "suppress_input", "duration_ms": duration_ms, "ts": ts,
            }))

        elif cmd == "status":
            await ws.send(json.dumps(self._status_event()))

        elif cmd == "config":
            key   = msg.get("key", "")
            value = msg.get("value")
            ok = self.engine.update_config(key, value)
            if not ok:
                ok = self._update_server_config(key, value)
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
            "auto_stop_without_clients_ms": self._auto_stop_ms(),
            "ts":                time.time(),
        }

    # ── Server lifecycle ──────────────────────────────────────────────────────

    async def run(self):
        self.loop = asyncio.get_running_loop()

        # NOTE: engine is NOT started automatically.
        # The microphone is opened only when the client sends {"cmd": "start"}.

        host = self.config["host"]
        port = self.config["port"]
        ww   = self.config["wake_word"]

        border = "=" * 54
        self.logger.info(border)
        self.logger.info("  Speech Recognition WebSocket Service")
        self.logger.info(f"  ws://{host}:{port}")
        self.logger.info(f"  Wake word : {'enabled' if ww['enabled'] else 'disabled'}")
        if ww["enabled"]:
            self.logger.info(f"  Keywords  : {', '.join(ww.get('keywords', []))}")
        self.logger.info(f"  ASR model : {self.config['asr']['model_path']}")
        idle_ms = self._auto_stop_ms()
        if idle_ms > 0:
            self.logger.info(
                f"  Auto-stop  : no WS clients for {idle_ms} ms -> release microphone"
            )
        else:
            self.logger.info("  Auto-stop  : disabled (auto_stop_without_clients_ms=0)")
        self.logger.info(border)

        try:
            async with websockets.serve(self._handle_client, host, port):
                self.logger.info(
                    "Server ready.  Open index.html to connect.  Ctrl+C to stop."
                )
                # Periodically yield so Python's signal handler can run on Windows.
                while True:
                    await self._tick_auto_stop_without_clients()
                    await asyncio.sleep(0.5)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            self.logger.info("Shutting down engine...")
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
