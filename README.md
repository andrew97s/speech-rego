# Speech Recognition WebSocket Service

Windows 语音识别服务，支持语音唤醒，通过 WebSocket 与客户端交互。

## 特性

| 功能 | 实现 |
|---|---|
| 语音唤醒 | openwakeword (ONNX, 完全离线) |
| 语音识别 | Vosk (离线 ASR，支持中英文) |
| 音频采集 | sounddevice (PortAudio 已内置) |
| 通信协议 | WebSocket + JSON 事件 |
| 系统兼容 | Windows 10 / 11 (64-bit) |
| 一键安装 | install.bat |

---

## 快速开始

### 1. 安装（一次即可）

双击 **`install.bat`**

安装脚本会自动：
- 检查并安装 Python 3.11（通过 winget）
- 创建虚拟环境 `.venv`
- 安装所有 Python 依赖
- 下载 Vosk 语音模型（~42 MB）
- 预下载唤醒词模型

> 需要网络连接（仅安装时）

### 2. 启动服务

双击 **`start.bat`**

服务默认监听：`ws://127.0.0.1:8765`

---

## 配置（config.json）

```json
{
  "host": "127.0.0.1",
  "port": 8765,
  "wake_word": {
    "enabled": true,
    "keywords": ["hey_jarvis"],
    "sensitivity": 0.5
  },
  "asr": {
    "model_path": "models/vosk-model-small-cn-0.22",
    "max_silence_ms": 1500,
    "max_listen_ms": 30000
  },
  "audio": {
    "device": null,
    "sample_rate": 16000,
    "chunk_size": 4000,
    "energy_threshold": 0.02
  },
  "log_level": "INFO"
}
```

### 配置说明

| 字段 | 说明 |
|---|---|
| `wake_word.enabled` | `true` = 等待唤醒词；`false` = 检测到声音即开始识别 |
| `wake_word.keywords` | 唤醒词列表，见下方可用词表 |
| `wake_word.sensitivity` | 灵敏度 0.0–1.0，越高越灵敏（误触率也越高） |
| `asr.model_path` | Vosk 模型路径，中文或英文 |
| `asr.max_silence_ms` | 静音多久后自动结束识别（ms） |
| `asr.max_listen_ms` | 单次识别最长时间（ms） |
| `audio.device` | 麦克风设备 ID（`null` = 系统默认） |
| `audio.energy_threshold` | 仅在无唤醒词模式下有效，声音能量阈值 |

### 可用唤醒词

```
hey_jarvis   alexa   hey_mycroft   hey_rhasspy
```

### Vosk 模型选择

| 语言 | 模型名 | 大小 |
|---|---|---|
| 中文 | `vosk-model-small-cn-0.22` | ~42 MB |
| 英文 | `vosk-model-small-en-us-0.15` | ~40 MB |

---

## WebSocket API

### 客户端 → 服务端（命令）

```json
{"cmd": "start"}
{"cmd": "stop"}
{"cmd": "listen"}
{"cmd": "cancel"}
{"cmd": "status"}
{"cmd": "config", "key": "wake_word.sensitivity", "value": 0.7}
```

### 服务端 → 客户端（事件）

```json
// 连接后立即收到当前状态
{"event": "status", "state": "idle", "wake_word_enabled": true, "keywords": ["hey_jarvis"], "ts": 1234567890.1}

// 检测到唤醒词
{"event": "wake_word", "keyword": "hey_jarvis", "score": 0.92, "ts": ...}

// 开始录音
{"event": "listening_start", "trigger": "wake_word", "ts": ...}

// 中间结果（实时）
{"event": "partial", "text": "你好世", "ts": ...}

// 最终识别结果
{"event": "transcript", "text": "你好世界", "is_final": true, "ts": ...}

// 录音结束
{"event": "listening_end", "reason": "silence", "ts": ...}
// reason: silence | timeout | cancelled

// 错误
{"event": "error", "code": "mic_error", "message": "...", "ts": ...}

// 命令确认
{"event": "ack", "cmd": "listen", "ts": ...}

// 配置更新确认
{"event": "config_updated", "key": "wake_word.sensitivity", "value": 0.7, "ts": ...}
```

---

## 工具脚本

### 列出麦克风设备

```
.venv\Scripts\python.exe list_devices.py
```

输出示例：
```
 ID  Name                                           Inputs      Rate
──────────────────────────────────────────────────────────────────────
  0  Microphone Array (Intel Smart Sound Tech)          4     16000 ◄ default
  1  USB Microphone                                     2     48000
```

将设备 ID 填入 `config.json` 的 `audio.device` 字段。

---

## 常见问题

**Q: 安装时提示找不到 winget**
A: 在 Microsoft Store 中更新"应用安装程序"，或手动安装 Python 3.11+ (64-bit)。

**Q: 唤醒词检测没反应**
A: 尝试提高 `wake_word.sensitivity`（如 `0.3`），或在 `config.json` 中设置 `"enabled": false` 改为能量检测模式。

**Q: 识别结果为空**
A: 检查麦克风是否是默认设备，运行 `list_devices.py` 确认设备 ID。

**Q: 连接 WebSocket 被拒绝**
A: 确认 `start.bat` 已运行，防火墙是否阻止了 8765 端口（本地连接默认不需要开放）。

**Q: 换成英文识别**
A: 安装时选择英文模型，或手动下载并在 `config.json` 中修改 `asr.model_path`。
