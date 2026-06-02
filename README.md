# speech-rego

本地语音识别 WebSocket 服务：**openWakeWord** 唤醒 → **Silero VAD** 判停 → **faster-whisper** 转写。

## 快速启动

```bat
install.bat
start.bat
```

浏览器打开 `http://127.0.0.1:8080/index.html`（WebSocket 默认 `ws://127.0.0.1:8765`）。

## 架构

| 模块 | 文件 |
|------|------|
| WebSocket 服务 | `server.py` |
| 音频引擎 | `engine.py` |
| 唤醒 | `wake_detectors.py`（openWakeWord） |
| 判停 VAD | `speech_vad.py`（Silero） |
| 后处理 | `text_postprocess.py` |
| 误唤醒门控 | `wake_gating.py` |

## 配置要点（`config.json`）

- `wake_word.keywords` / `oww_models`：唤醒词与 openWakeWord 模型名（如 `hey_jarvis`）
- `whisper.model` / `device` / `compute_type`：ASR 模型与推理设备
- `whisper.max_silence_ms`：Silero 判停静音时长
- `postprocess.replacements`：识别结果词语替换

## 依赖

见 `requirements.txt`（无 Vosk）。
