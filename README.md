# speech-rego

本地语音识别 WebSocket 服务：**Sherpa KWS** 唤醒 → **Silero VAD** 判停 → **faster-whisper** 转写。

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
| 唤醒 | `wake_detectors.py`（Sherpa-ONNX KWS） |
| 判停 VAD | `speech_vad.py`（Silero） |
| 后处理 | `text_postprocess.py` |
| 误唤醒门控 | `wake_gating.py` |

VAD 用法与调参：[docs/VAD.md](docs/VAD.md)

## 配置要点（`config.json`）

- `wake_word.keywords`：唤醒词（如 `小智`）；自动经 `sherpa-onnx-cli text2token` 生成 keywords 文件
- `wake_word.sherpa_kws.model_dir`：Sherpa KWS 模型目录（默认中英 zipformer 3M）
- `whisper.model` / `device` / `compute_type`：ASR 模型与推理设备
- `whisper.max_silence_ms`：Silero 判停静音时长
- `postprocess.replacements`：识别结果词语替换

## Sherpa KWS 模型

默认模型：[sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)

离线包构建时会自动下载并打包到 `models/sherpa-kws/`。

## 依赖

见 `requirements.txt`（`sherpa-onnx` + `silero-vad` + `faster-whisper`）。
