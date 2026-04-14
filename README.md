# speech-rego

本地语音识别（语音转文字）服务，通过 **WebSocket** 推送状态与识别结果，并支持 **关键字唤醒**：检测到唤醒词后进入一轮聆听，也可手动触发识别。

## 功能概览

- **实时麦克风采集**（`sounddevice`），可配置输入设备与采样参数。
- **两种识别后端**（二选一启动）：
  - **Vosk**（`server.py`）：轻量、离线，适合中文/英文小模型场景。
  - **faster-whisper**（`server_whisper.py`）：精度更高，支持中英混合（可将 `language` 设为 `null` 自动检测）。
- **关键字唤醒**（可开关）：
  - **Vosk 模式**：适合中文自定义词（需对应 Vosk 模型路径）。
  - **Whisper 模式**：任意语言关键词（依赖 Whisper 推理，无需额外唤醒库）。
  - **auto**：含中日韩等 CJK 字符的关键词走 Vosk，否则走 Whisper（见 `engine_whisper.py` 说明）。
- **简易 Web 界面**：`server_whisper.py` 可在本地 HTTP 端口托管 `index.html`（由 `config.json` 的 `http_port` 控制，`0` 为关闭）。

## 环境要求

- Python 3.10+（推荐；具体以你本机已验证版本为准）
- Windows / Linux / macOS（仓库内对 Windows 事件循环、部分 DLL 加载有专门处理）
- 麦克风权限已开启
- **Vosk**：需自行下载模型放到 `config.json` 中 `asr.model_path` 指向的目录（例如 `models/vosk-model-small-cn-0.22`）。
- **Whisper**：首次运行会按 `faster-whisper` 机制下载/缓存模型；`device` 为 `cuda` 时需本机 CUDA 环境匹配。

## 安装

```bash
cd speech-rego
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

依赖见 `requirements.txt`（含 `websockets`、`sounddevice`、`numpy`、`vosk`、`faster-whisper`、`ctranslate2` 等）。

## 配置

编辑项目根目录下的 `config.json`（支持 UTF-8 BOM）。常用项：

| 区块 | 说明 |
|------|------|
| `host` / `port` | WebSocket 监听地址与端口 |
| `wake_word` | `enabled`、`mode`（`vosk` / `whisper` / `auto`）、`keywords`、`sensitivity` |
| `asr` | Vosk 模型路径、静音结束时长、单次最长聆听时间等 |
| `whisper` | 模型体量、`language`、`device`、`compute_type`、VAD/静音相关毫秒参数、`initial_prompt` 等 |
| `audio` | `device`（`null` 为默认麦克风）、`sample_rate`、`chunk_size`、`energy_threshold` |
| `log_level` | 日志级别，如 `INFO` |
| `http_port` | 静态页端口；`server_whisper.py` 下用于打开 `index.html`（`0` 关闭） |

文件中以 `_` 开头的键为说明用字段，保存运行时配置时会被剥离。

查看麦克风设备编号：

```bash
python list_devices.py
```

将输出中的 ID 填入 `"audio": { "device": <ID> }`。

## 运行

**Vosk 后端（默认端口以你当前 `config.json` 为准，常见为 8765）：**

```bash
python server.py
```

**Whisper 后端：**

```bash
python server_whisper.py
```

启动后控制台会打印 `ws://...`；若启用了 `http_port`，可浏览器打开 `http://127.0.0.1:<http_port>/index.html`（Whisper 服务端日志会提示具体地址）。若 `index.html` 里写死了 WebSocket 端口，请与 `config.json` 的 `port` 保持一致。

Whisper 服务还支持环境自检命令（见下文 WebSocket `check`）。

## WebSocket 协议摘要

连接：`ws://<host>:<port>/`

**客户端 → 服务端（JSON）：**

| 命令 | 含义 |
|------|------|
| `{"cmd": "start"}` | 启动引擎 / 打开麦克风 |
| `{"cmd": "stop"}` | 停止引擎 / 释放麦克风 |
| `{"cmd": "listen"}` | 手动开始一轮识别 |
| `{"cmd": "cancel"}` | 取消当前识别 |
| `{"cmd": "status"}` | 查询状态 |
| `{"cmd": "config", "key": "...", "value": ...}` | 运行时更新配置（成功后会写回 `config.json`） |

`server_whisper.py` 额外支持：`{"cmd": "check"}`，返回 `env_check` 事件（由 `check_env.py` 实现）。

**服务端 → 客户端（JSON 事件，节选）：**

- `status`：引擎状态 `stopped` / `no_device` / `idle` / `listening` 等  
- `wake_word`：命中唤醒词  
- `listening_start` / `listening_end`：一轮聆听开始/结束及原因  
- `partial` / `transcript`：中间结果与最终结果  
- `error` / `ack` / `config_updated`

完整字段说明见 `server.py` 与 `server_whisper.py` 文件头部注释。

## 仓库结构（核心文件）

| 文件 | 作用 |
|------|------|
| `server.py` | Vosk 流水线 WebSocket 服务 |
| `server_whisper.py` | Whisper 流水线 WebSocket 服务 + 可选 HTTP 静态页 |
| `engine.py` / `engine_whisper.py` | 音频线程、唤醒与 ASR 状态机 |
| `config.json` | 统一配置文件 |
| `index.html` | 简单调试/演示前端 |
| `list_devices.py` | 枚举输入音频设备 |
| `check_env.py` | Whisper 侧环境检查（供 `check` 命令调用） |
| `requirements.txt` | Python 依赖 |

`dist/` 等目录多为打包或离线环境产物，日常开发以仓库根目录脚本与配置为准即可。

## 常见问题

- **听不到或选错麦克风**：运行 `list_devices.py`，在 `config.json` 的 `audio.device` 指定正确 ID。  
- **唤醒不灵敏 / 误触发**：调整 `wake_word.sensitivity`、更换 `wake_word.mode`，或修改 `keywords`；Whisper 路径还可调 VAD、静音时长等参数。  
- **端口被占用**：修改 `config.json` 中的 `port`（及前端 `index.html` 中的连接端口，如有写死）。  
- **Vosk 中文唤醒词**：多字词在引擎内会按字间空格做语法匹配，详见 `engine.py` 中 `_VoskWakeWordDetector` 注释。

## 许可证

若仓库根目录未包含 `LICENSE` 文件，请由项目维护者自行补充授权条款。
