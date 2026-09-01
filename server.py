#!/usr/bin/env python3
"""
语音识别 WebSocket 服务（Windows 客户端）：Sherpa KWS + fsmn-vad + 远程 FunASR。

Usage:
  python server.py
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

from engine import EngineState, SpeechEngine
from remote_asr import remote_asr_enabled
from text_postprocess import get_postprocess_config

# Windows: use Selector event loop for proper Ctrl+C delivery
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ── Config helpers ─────────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    "host": "127.0.0.1",
    "port": 8765,
    "wake_word": {
        "enabled":     True,
        "keywords":    ["小智"],
        "sensitivity": 0.5,
        "sherpa_kws": {
            "model_dir": "models/sherpa-kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
            "chunk_size": 16,
            "epoch_tag": "epoch-12-avg-2",
            "use_int8": True,
            "tokens_type": "ppinyin",
            "lexicon": "",
            "provider": "cpu",
            "num_threads": 2,
            "keywords_file": "",
            "debounce_sec": 0.8,
        },
        "pause_until_listen": False,
        "wake_repeat_cooldown_ms": 1500,
        "sensitivity": 0.5,
        "require_pre_silence_ms": 350,
        "pre_silence_min_ratio": 0.65,
        "pre_silence_level_ratio": 0.32,
        "gate_use_silero": False,
        "max_wake_utterance_ms": 1400,
    },
    "funasr": {
        "asr_model": "FunAudioLLM/Fun-ASR-Nano-2512",
        "vad_model": "fsmn-vad",
        "punc_model": "",
        "device": "cpu",
        "ncpu": 4,
        "vad_chunk_ms": 200,
        "cache_dir": "models/funasr",
        "disable_update": True,
        "hub": "ms",
        "trust_remote_code": True,
        "language": "中文",
        "itn": True,
        "hotword": "",
    },
    "asr_remote": {
        "enabled": True,
        "url": "http://127.0.0.1:8767/v1/recognize",
        "timeout_sec": 60,
        "token": "",
    },
    "whisper": {
        "language":            "zh",
        "partial_interval_ms": 0,
        "max_silence_ms":      2000,
        "max_listen_ms":       30000,
        "vad_cooldown_ms":     500,
        "vad_min_speech_ms":   200,
        "min_listen_ms":       1000,
        "min_transcribe_sec":  1.5,
        "post_wake_grace_ms":  3000,
        "output_simplified":   True,
        "domain_keywords":     [],
    },
    "postprocess": {
        "output_simplified":   True,
        "post_wake_grace_ms":  3000,
        "suppress_phrases":    ["我在", "在呢", "我在呢", "嗯", "啊", "好的"],
        "replacements":        {},
    },
    "audio": {
        "device":           None,
        "sample_rate":      16000,   # Fun-ASR-Nano is 16 kHz
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
    path = os.environ.get("SPEECH_REGO_CONFIG", path)
    abs_path = os.path.abspath(path)

    def _read_user() -> dict:
        last_err: Optional[Exception] = None
        for enc in ("utf-8-sig", "utf-8", "utf-16", "utf-16-le"):
            try:
                with open(path, encoding=enc) as f:
                    return json.load(f)
            except (FileNotFoundError, UnicodeError, json.JSONDecodeError) as exc:
                last_err = exc
                if isinstance(exc, FileNotFoundError):
                    raise
        raise json.JSONDecodeError(str(last_err), "", 0)

    try:
        user = _read_user()
        merged = _deep_merge(_DEFAULTS, user)
        f = merged.get("funasr", {})
        remote = merged.get("asr_remote") or {}
        log.info(
            "Loaded %s — vad=%s device=%s remote=%s",
            abs_path,
            f.get("vad_model", "fsmn-vad"),
            f.get("device", "cpu"),
            remote.get("url") if remote.get("enabled", True) else "(local)",
        )
        return merged
    except FileNotFoundError:
        log.warning("%s not found — using built-in defaults", abs_path)
        return dict(_DEFAULTS)
    except json.JSONDecodeError as exc:
        log.error(
            "Invalid JSON in %s (%s) — using built-in defaults. "
            "Fix the file or set SPEECH_REGO_CONFIG to another path.",
            abs_path,
            exc,
        )
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
    """
    Windows 语音客户端 WebSocket 服务：唤醒、录音、把整句交给远程 FunASR。

    默认端口 8765。
    """

    def __init__(self, config: dict):
        """
        Args:
            config: 合并默认值后的完整配置（host/port/whisper/wake_word 等）
        """
        self.config  = config
        self.clients: Set[WebSocketServerProtocol] = set()
        self.loop:    Optional[asyncio.AbstractEventLoop] = None
        self.engine  = SpeechEngine(config, self._on_engine_event)
        self.logger  = logging.getLogger("SpeechServer")
        self._no_clients_since: Optional[float] = None

    def _auto_stop_ms(self) -> int:
        """无客户端连接超过该毫秒数后自动 engine.stop()；0 表示关闭。"""
        return max(0, int(self.config.get("auto_stop_without_clients_ms", 0)))

    def _update_server_config(self, key: str, value) -> bool:
        """更新仅 server 层管理的 config 项（如 auto_stop_without_clients_ms）。"""
        if key == "auto_stop_without_clients_ms":
            self.config[key] = max(0, int(float(value)))
            self.logger.info(
                "Config updated: auto_stop_without_clients_ms = %d",
                self.config[key],
            )
            return True
        return False

    async def _tick_auto_stop_without_clients(self) -> None:
        """定时检查：无 WS 客户端且超时则 stop 引擎释放麦克风。"""
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
        """引擎线程回调：将事件投递到 asyncio 循环广播。"""
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(event), self.loop)

    async def _broadcast(self, event: dict):
        """向所有已连接 WebSocket 客户端发送 JSON 事件。"""
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
        """单客户端连接：注册、推送初始 status、循环 dispatch 命令。"""
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
        """
        解析 JSON 命令并调用 engine / 写 config。

        支持 start/stop/listen/cancel/suppress_input/status/config/check 等。
        """
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
                if (key.startswith("wake_word.") or key.startswith("funasr.")) and self.loop:
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
        """构造当前引擎与服务配置的 status 事件 dict。"""
        ww = self.config["wake_word"]
        w  = self.config.get("whisper", {})
        f  = self.config.get("funasr", {})
        _pp = get_postprocess_config(self.config)
        _pp_raw = self.config.get("postprocess") or {}
        return {
            "event":                  "status",
            "state":                  self.engine.state.value,
            "wake_word_enabled":      ww.get("enabled", True),
            "keywords":               ww.get("keywords", []),
            "mode":                   "wake_client_remote_asr" if remote_asr_enabled(self.config) else "funasr_nano",
            "asr_model":              f.get("asr_model", "FunAudioLLM/Fun-ASR-Nano-2512"),
            "asr_remote":             remote_asr_enabled(self.config),
            "asr_remote_url":         str((self.config.get("asr_remote") or {}).get("url") or ""),
            "vad_model":              f.get("vad_model", "fsmn-vad"),
            "whisper_max_silence_ms": w.get("max_silence_ms", 2500),
            "whisper_min_listen_ms":  w.get("min_listen_ms", 600),
            "whisper_max_listen_ms":  w.get("max_listen_ms", 30000),
            "post_wake_grace_ms":     int(_pp.get("post_wake_grace_ms", 1500)),
            "replacements":           _pp_raw.get("replacements") or {},
            "auto_stop_without_clients_ms": self._auto_stop_ms(),
            "ts":                     time.time(),
        }

    # ── Server lifecycle ──────────────────────────────────────────────────────

    async def run(self):
        """启动 WebSocket 服务：preload 模型、accept 连接、定时 auto-stop。"""
        self.loop = asyncio.get_running_loop()

        host     = self.config["host"]
        port     = self.config["port"]
        ww       = self.config["wake_word"]
        fcfg     = self.config.get("funasr") or {}
        remote   = self.config.get("asr_remote") or {}

        border = "=" * 56
        self.logger.info(border)
        self.logger.info("  Speech client WebSocket service")
        self.logger.info(f"  ws://{host}:{port}")
        self.logger.info(f"  Wake word  : {'enabled' if ww['enabled'] else 'disabled'}")
        if ww["enabled"]:
            self.logger.info(f"  Keywords   : {', '.join(ww.get('keywords', []))}")
        if remote_asr_enabled(self.config):
            self.logger.info(f"  ASR        : remote {remote.get('url')}")
        else:
            self.logger.info(f"  ASR        : local {fcfg.get('asr_model', 'FunAudioLLM/Fun-ASR-Nano-2512')}")
        self.logger.info(f"  VAD        : {fcfg.get('vad_model', 'fsmn-vad')} ({fcfg.get('device', 'cpu')})")
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

        # Pre-load FunASR + wake word detector before accepting connections
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
                    "Open index.html to connect.  Ctrl+C to stop."
                )
                while True:
                    await self._tick_auto_stop_without_clients()
                    await asyncio.sleep(0.5)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            self.logger.info("Shutting down engine…")
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
