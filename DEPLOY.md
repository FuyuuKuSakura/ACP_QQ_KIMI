# ACP-QQ Bridge 部署指南

## 架构概览

```
QQ 客户端  →  LLOneBot  →  NoneBot2 (ACP-QQ-Bridge)  →  Agent WebSocket
                                                     ↓
                                        ┌────────────┼────────────┐
                                        ↓            ↓            ↓
                                  CLI Bridge   VSCode Bridge   API Agent
                                        ↓            ↓            ↓
                                  Kimi CLI     Kimi VSCode     Moonshot API
```

---

## 一、环境准备

```bash
# 1. 进入项目目录
cd /Users/fuyuuku/ACP_QQ_KIMI

# 2. 激活虚拟环境
source .venv/bin/activate

# 3. 确认依赖已安装
pip install -e ".[dev]"

# 如需使用 API Agent，额外安装
pip install openai
```

---

## 二、QQ 协议端配置（LLOneBot）

### 2.1 安装 LLOneBot

1. **下载 LiteLoaderQQNT**（QQ 插件框架）
   - https://github.com/LiteLoaderQQNT/LiteLoaderQQNT/releases
   - 按 README 安装到 QQ 目录

2. **下载 LLOneBot 插件**
   - https://github.com/LLOneBot/LLOneBot/releases
   - 将插件文件夹放到 `LiteLoaderQQNT/plugins/` 目录

3. **启动 QQ**，在设置面板找到 LLOneBot

### 2.2 配置 LLOneBot

在 LLOneBot 设置中配置：

```
启用反向 WebSocket: ✅
反向 WebSocket 地址: ws://127.0.0.1:8080/onebot/v11/ws
消息上报格式: 数组
心跳间隔: 30000
```

### 2.3 配置 NoneBot2

项目根目录的 `.env` 文件已配置好：

```env
ENVIRONMENT=prod
DRIVER=~fastapi
HOST=127.0.0.1
PORT=8080
ONEBOT_ACCESS_TOKEN=
COMMAND_START=["/", "!"]
```

无需修改，保持默认即可。

---

## 三、Agent 端配置（三选一）

### 方案 A：Kimi Code CLI 桥接器 ⭐推荐

适合：希望通过 QQ 控制命令行中的 Kimi Code CLI

```bash
# 1. 确认 kimi CLI 在 PATH 中
which kimi

# 2. 启动 CLI Bridge（单例模式：所有 QQ 用户共享一个 CLI 会话）
python scripts/kimi_cli_bridge.py

# 或指定自定义命令
python scripts/kimi_cli_bridge.py --cmd "kimi chat"

# 或多例模式（每个 QQ 会话独立 CLI 进程）
python scripts/kimi_cli_bridge.py --mode multi
```

**config.yaml 对应设置：**
```yaml
agent:
  ws_url: "ws://127.0.0.1:8765"
```

### 方案 B：VSCode 扩展桥接器

适合：希望通过 QQ 控制 VSCode 中的 Kimi Code 插件

```bash
# 1. 启动 VSCode Bridge
python scripts/kimi_vscode_bridge.py

# 2. 在 VSCode 中加载扩展
#    - 打开 vscode-bridge 目录
#    - npm install && npm run compile
#    - 按 F5 启动 Extension Host
#    - 在新窗口中按 Ctrl+Shift+P → "Kimi Bridge: Enable"
```

**config.yaml 对应设置：**
```yaml
agent:
  ws_url: "ws://127.0.0.1:8766"
```

### 方案 C：OpenAI API Agent（Fallback）

适合：没有 Kimi Code CLI/VSCode，直接用 Moonshot API

```bash
# 设置 API Key
export MOONSHOT_API_KEY="sk-your-key-here"

# 启动 API Agent
python scripts/openai_agent_server.py

# 或指定自定义模型
python scripts/openai_agent_server.py --model moonshot-v1-32k
```

**config.yaml 对应设置：**
```yaml
agent:
  ws_url: "ws://127.0.0.1:8765"
```

**获取 API Key：** https://platform.moonshot.cn/

---

## 四、启动桥接器

在 **新的终端窗口** 中：

```bash
cd /Users/fuyuuku/ACP_QQ_KIMI
source .venv/bin/activate

# 启动 ACP-QQ Bridge
python -m acp_qq_bridge --config config.yaml
```

首次启动时会看到：
- `Uvicorn running on http://127.0.0.1:8080`（NoneBot2 HTTP 服务）
- `Agent WebSocket connected`（连接到 Agent 端）
- `QQ bot connected`（LLOneBot 连接成功）

---

## 五、QQ 中使用

### 基本交互

在 QQ 中 @机器人 或私聊：

```
你好，帮我分析一下这段代码
```

机器人会将消息转发到 Agent，并把 Agent 的回复返回 QQ。

### 可用命令

| 命令 | 说明 |
|---|---|
| `/stop` / `/打断` | 打断当前 Agent 任务 |
| `/status` / `/状态` | 查询当前会话状态 |
| `/persona list` | 列出可用人设 |
| `/persona set <id>` | 切换人设（assistant/sarcastic/cute/geek）|

### 人设效果

- `assistant`：标准专业助手
- `sarcastic`：毒舌吐槽风格
- `cute`：萌系颜文字风格
- `geek`：极客技术宅风格

---

## 六、常见问题

### Q: LLOneBot 连接不上 NoneBot2
A: 检查 `.env` 中的 `PORT=8080` 与 LLOneBot 配置的地址是否一致。确保防火墙允许本地连接。

### Q: Agent WebSocket 连接失败
A: 先确认 Agent 端（CLI Bridge / API Agent）已启动。检查 `config.yaml` 中的 `ws_url` 和端口是否匹配。

### Q: 如何同时接入 CLI 和 VSCode？
A: 启动两个独立的 Bridge 实例，使用不同端口，QQ 端通过不同的群/用户分别绑定。

### Q: 安全过滤太严格
A: 修改 `config.yaml` 中的 `security.allowed_commands` 和 `sensitive_patterns`。

---

## 七、完整启动流程示例

### 终端 1：启动 Agent（以 API Agent 为例）
```bash
export MOONSHOT_API_KEY="sk-xxx"
python scripts/openai_agent_server.py
```

### 终端 2：启动 ACP-QQ Bridge
```bash
python -m acp_qq_bridge --config config.yaml
```

### 终端 3：查看日志（可选）
```bash
tail -f /tmp/kimi_*.log
```

### QQ 中测试
1. 添加机器人 QQ 为好友
2. 发送消息：`你好`
3. 等待 Agent 回复
