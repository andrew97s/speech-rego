# speech-rego

两段式部署：

1. **Windows 客户端**（`server.py`）：麦克风 + Sherpa 唤醒 + fsmn-vad 判停，对外提供 WebSocket。
2. **GPU 识别服务**（`asr_server.py`）：只跑 Fun-ASR-Nano，接收一整句音频并返回文本。

客户端说完一句后再把 PCM 提交给服务器，**不实时出字**。

## 架构

```
外部程序 / 浏览器
    ↕ WebSocket  ws://windows-host:8766
Windows  python server.py
    ├─ Sherpa KWS 唤醒
    ├─ fsmn-vad 判停 + 缓冲 PCM
    └─ POST http://gpu-server:8767/v1/recognize
Ubuntu  python asr_server.py
    └─ Fun-ASR-Nano generate → JSON {text}
```

| 模块 | 跑在哪 | 文件 |
|------|--------|------|
| 对外 WebSocket | Windows | `server.py` |
| 唤醒 / 录音 / 判停 | Windows | `engine.py` `wake_detectors.py` `speech_vad.py` |
| 远程识别客户端 | Windows | `remote_asr.py` |
| Fun-ASR-Nano HTTP | GPU 服务器 | `asr_server.py` |
| 后处理（错词替换等） | Windows | `text_postprocess.py` |

## 1. GPU 服务器（Ubuntu）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-asr-server.txt
# 按机器 CUDA 安装对应的 torch 轮子
python asr_server.py
```

默认监听 `0.0.0.0:8767`。配置见 `asr_server.json`（`funasr.device` 用 `auto` 选空闲显存最多的卡，或写成 `cuda:1`）。

防火墙放行 8767。可用 `token` 字段开启 `X-ASR-Token` 鉴权。

探测：

```bash
curl http://127.0.0.1:8767/v1/health
```

识别接口：`POST /v1/recognize`

```json
{
  "audio_b64": "<base64 16kHz s16le PCM>",
  "encoding": "pcm_s16le",
  "sample_rate": 16000,
  "language": "中文",
  "hotwords": ["勤务", "巡检"]
}
```

返回：`{"ok": true, "text": "...", "duration_s": 2.1, "elapsed_s": 0.4}`

首次启动会从 ModelScope 下载 Fun-ASR-Nano 到 `models/funasr/`（体积较大）。

funasr 1.4.11 已内置 `FunASRNano` 类。不要用官方示例里的 `remote_code="./model.py"`：ModelScope 权重目录里没有这个文件，会报 `No module named 'model'`。若启动时报无法 import FunASRNano，在服务器上执行：

```bash
pip install tiktoken huggingface_hub transformers
```

## 2. Windows 客户端

开发机：

```bat
install.bat
start.bat
```

打成安装包（后台常驻 Windows 服务）：

```bat
build_client_installer.bat
```

产出 `dist\SpeechReco-Client-Setup.exe`。安装后：

- 服务名 `SpeechRecoClient`，开机自启
- Web 控制台 `http://127.0.0.1:9400/index.html`
- WebSocket `ws://127.0.0.1:8766`

编辑安装目录或源码里的 `config.json`：

- `asr_remote.enabled`: `true`
- `asr_remote.url`: `http://<GPU服务器IP>:8767/v1/recognize`
- `asr_remote.token`: 与 `asr_server.json` 的 `token` 一致（可空）
- `funasr.device`: `cpu`（本机只跑小 VAD，不必用独显）
- `port`: `8766`（WebSocket）
- `http_port`: `9400`（Web UI）

构建安装包需要 [Inno Setup 6](https://jrsoftware.org/isdl.php)。已有离线目录时可：`.\build_client_installer.ps1 -SkipOffline`。

对外 WebSocket 默认 `ws://127.0.0.1:8766`（以 `config.json` 的 `host`/`port` 为准）。  
控制台：`http://127.0.0.1:9400/index.html`。

外部程序协议与原来相同：`start` / `listen` / `cancel` 等命令，事件 `wake_word`、`transcript`。说完后会先有 `recognizing`，再收到 `transcript`。识别服务不可达时事件 `error`，`code=asr_remote_failed`。

本机仍会下载 **fsmn-vad**（很小）。不要在 Windows 上再加载 Nano。

把 `asr_remote.enabled` 设为 `false` 可退回本机识别（需本机 GPU 与 Nano 权重）。

## 配置要点

- `wake_word.keywords`：唤醒词
- `whisper.max_silence_ms` / `min_listen_ms` / `max_listen_ms`：本机判停
- `whisper.domain_keywords`：本机后处理纠错词，不会当作 FunASR hotwords
- `funasr.hotword`：才传给 FunASR；留空则不启用热词
- `postprocess.replacements`：Windows 侧错词替换

VAD 说明：[docs/VAD.md](docs/VAD.md)
