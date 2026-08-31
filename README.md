# speech-rego

本地语音识别 WebSocket 服务：**Sherpa KWS** 唤醒 → **FunASR fsmn-vad** 判停 → **FunASR 中文流式 ASR**（paraformer-zh-streaming）。

## 快速启动

```bat
install.bat
start.bat
```

浏览器打开 `http://127.0.0.1:8080/index.html`（WebSocket 默认 `ws://127.0.0.1:8765`）。

首次启动会从 ModelScope 下载 FunASR 模型到 `models/funasr/`（需联网）。

## 架构

| 模块 | 文件 |
|------|------|
| WebSocket 服务 | `server.py` |
| 音频引擎 | `engine.py` |
| 唤醒 | `wake_detectors.py`（Sherpa-ONNX KWS） |
| 判停 VAD | `speech_vad.py`（FunASR fsmn-vad） |
| 流式 ASR | `funasr_asr.py`（paraformer-zh-streaming） |
| 后处理 | `text_postprocess.py` |
| 误唤醒门控 | `wake_gating.py` |

VAD 用法与调参：[docs/VAD.md](docs/VAD.md)

## 配置要点（`config.json`）

- `wake_word.keywords`：唤醒词（如 `小智`）；自动经 `sherpa-onnx-cli text2token` 生成 keywords 文件
- `wake_word.sherpa_kws.model_dir`：Sherpa KWS 模型目录（默认 WenetSpeech 纯中文 zipformer 3.3M）
- `funasr.asr_model`：流式 ASR，默认 `paraformer-zh-streaming`
- `funasr.vad_model`：流式 VAD，默认 `fsmn-vad`
- `funasr.device`：`cuda` 或 `cpu`
- `funasr.chunk_size`：`[0,10,5]` 为 600ms 一帧（更低延迟可用 `[0,8,4]`）
- `whisper.max_silence_ms`：判停静音时长（同时传给 fsmn-vad `max_end_silence_time`）
- `postprocess.replacements`：识别结果词语替换
- `whisper.domain_keywords`：同时作为 FunASR hotword 与后处理纠错词表

## Sherpa KWS 模型

默认模型：[sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)（WenetSpeech L 10k 小时纯中文，`tokens_type=ppinyin`）

离线包构建时会自动下载并打包到 `models/sherpa-kws/`。

## 依赖

见 `requirements.txt`（`sherpa-onnx` + `funasr` + `torch`）。
