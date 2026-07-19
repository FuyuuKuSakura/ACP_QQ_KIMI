# ACP_QQ_KIMI — QQ ↔ Kimi Code 桥接器（含 LifeOS 任务管理扩展）

用手机 QQ 遥控本机的 Kimi Code CLI，并内置一套以 Obsidian 仓库（LifeOS）为载体的
任务管理扩展：直写捕获、进度查询、agent 周结/日结、定时推送与错过补发。

## 架构

```
手机 QQ ──► QQ 客户端(LLOneBot) ──反向 WS──► ACP-QQ Bridge (nonebot2, :8080)
                                              │  src/acp_qq_bridge
                                              │  ├─ adapters/qq_bot.py   命令分流 + 下行路由
                                              │  ├─ adapters/lifeos.py   LifeOS 命令/定时/补发
                                              │  └─ adapters/agent_ws.py WS client + 断线缓冲
                                              ▼  WS (ACP/1.0 JSON, :8765)
                                       Kimi Code Bridge
                                              scripts/kimi_code_bridge.py
                                              每条消息 spawn 一次性
                                              `kimi -p <text> --output-format stream-json [-S sid]`
                                              ▼
                                       Kimi Code CLI（已验证 0.27.0）
```

- 每条 QQ 消息对应一次独立的 `kimi` 子进程调用；`-S` 续会话保持上下文。
- LifeOS 的 agent 任务走固定会话 `lifeos_main`，cwd 锁定到 vault；print 模式（`-p`）
  本身即非交互全自动（注意：`-p` 与 `-y`/`--auto` 互斥，不能追加任何批准旗标），
  回复经下行路由私聊推送给主人，且跳过 PersonaSkill 文本装饰。
- vault（LifeOS 仓库）自身的 git 历史是所有写入的兜底。

## 安装

前置：Python 3.11+；kimi CLI 0.27.0（`~/.kimi-code/bin/kimi`，升级前需回归验证
stream-json 输出格式）；QQ 客户端已登录并启用 LLOneBot
（反向 WS 指向 `ws://127.0.0.1:8080/onebot/v11/ws`）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # 按需填写
```

## 如何启动

前置：QQ 客户端已登录并启用 LLOneBot（反向 WS 指向 `ws://127.0.0.1:8080/onebot/v11/ws`），
`kimi` CLI 在 PATH 中。

```bash
scripts/start_production.sh        # 一键拉起两个进程（推荐）
```

或手动：

```bash
source .venv/bin/activate
export PATH="$HOME/.kimi-code/bin:$PATH"
python scripts/kimi_code_bridge.py &                 # WS server :8765
python -m acp_qq_bridge --config config.yaml &       # nonebot :8080
```

日志在 `logs/kimi_bridge.log` 与 `logs/acp_bridge.log`。

## Persona 系统

桥内置人格中间件：`personas/<id>.yaml`（system_prompt、语气、表情包映射）+
`corpus/<id>.txt`（few-shot 语录，Q/A 格式）。QQ 发 `/persona <id>` 为当前会话注入人格
（经 `__SET_PERSONA__` 链路拼入 kimi 提示词前缀），下行回复过 `PersonaSkill.transform`
加文本装饰。仓库自带 `kaltsit`（凯尔希，LifeOS 秘书人格，`lifeos.persona` 配置）与
`Exusiai`（能天使）两个示例。LifeOS 会话的下行消息**跳过** transform，
人格体现在 agent 成文的句风上而非后置装饰。

## 配套仓库

- [AgendaASS](https://github.com/FuyuuKuSakura/AgendaASS)：LifeOS 仓库的公开骨架
  （AGENT.md 规范、规划、STATE 模板），`lifeos.vault_path` 指向其本地克隆。
- [ACCEPTANCE_LIFEOS.md](ACCEPTANCE_LIFEOS.md)：LifeOS 扩展的实机联调验收清单。

## 常见问题（macOS 实踩过的坑）

- **nonebot 启动报 `connecting through a SOCKS proxy requires python-socks`**：
  macOS 系统代理会被 websockets/urllib 自动读取，连本地 WS 也走代理。启动前导出
  `export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"`。
- **图片下载/HTTPS 报 `CERTIFICATE_VERIFY_FAILED`**：macOS Framework Python 不携带
  CA 根证书。已声明 `certifi` 依赖（重装 `pip install -e ".[dev]"` 即含），
  图片下载走 certifi 证书库并绕过系统代理直连。
- **agent 调用报 429 / insufficient balance**：`moonshot-cn` 系模型按 token 计费，
  余额不足时充值，或把 `config.yaml` 的 `agent.model` 改为 `""` 走 kimi-code 订阅。
- **`.env` 曾误入版本库**：历史提交中仍含旧值，`ONEBOT_ACCESS_TOKEN` 若为真值请更换。

## 如何更改模型

1. `kimi provider list` 查看可用模型别名。
2. 改 `config.yaml` 的 `agent.model`（如 `"moonshot-cn/kimi-k2.6"`；留空 `""` 则用
   kimi CLI 的 `default_model`）。
3. 重启 `scripts/kimi_code_bridge.py`（桥在启动时读一次 config.yaml）。

优先级：桥 spawn 时追加的 `-m <model>` 优先于 CLI 配置里的 `default_model`。
也可用环境变量 `KIMI_MODEL` 临时覆盖。

计费差异注意：`moonshot-cn/kimi-k2.6` 走 moonshot 开放平台 API key，按 token 计费；
默认 `kimi-code/k3` 走 kimi-code 订阅（oauth）。周结/日结是分钟级长上下文任务，
选 k2.6 前请知悉成本。

## LifeOS 命令表（仅 superuser 私聊可用，非授权零响应）

| 命令 | 通道 | 说明 |
|---|---|---|
| `/记 <内容>` | 直写 | 追加 `40_收集箱/Inbox.md`，格式 `- YYYY-MM-DD HH:mm \| 内容` |
| `/打卡 <轨道> <内容>` | 直写 | 写入当日日记"推进"小节 `- [轨道] 内容` |
| `/能量 <0-10>` | 直写 | 写入当日日记 frontmatter 的 `能量` 键 |
| `/休闲 <内容>` | 直写 | 写入当日日记"休闲"小节 |
| `/总览` | 只读 | `20_进度/_总览.md` 表格转逐行纯文本 |
| `/下步` | 只读 | 聚合九个 STATE 未勾选的"下一步"，按轨道分组 |
| `/决策 <N> <内容>` | 直写 | 勾选最新周结"需要我决策的"第 N 项并追加决定 |
| `/周结` | agent | 执行 AGENT.md 周结 SOP；完成后推送 `.qq.txt` 精简版 |
| `/复活` | agent | 断档后执行复活 SOP，产出标注"（推断）"的粗摘要 |
| `/提醒 [每天] HH:mm <内容>` | 直写 | 自定义提醒；当天时间已过则顺延到明天 |
| `/提醒列表` | 只读 | 编号列出所有自定义提醒 |
| `/删提醒 <编号>` | 直写 | 按编号删除提醒 |
| 直接发图片（私聊） | 直写 | 存入 `40_收集箱/灵感草稿/assets/`，链接+附注追加 Inbox |
| `/任务`（别名 `/lifeos`） | 只读 | 显示本命令表 |

说明：

- 日记不存在时按 `00_系统/模板/tpl_日记.md` 结构自动创建。
- 每次直写后自动 `git -C <vault> add -A && git commit -m "qq: ..."`；无改动不算错误。
- `/帮助` 是桥的通用帮助（原有功能），LifeOS 命令表用 `/任务`。
- `lifeos.plain_message_mode: "inbox"` 时，主人私聊的普通消息直接进 Inbox 而不发 agent
  （默认 `"agent"`，行为不变）。

### 图片灵感

私聊直接发图片（可带文字附注）即可：每张图保存到
`40_收集箱/灵感草稿/assets/YYYY-MM-DD_HH-MM-SS_<n>.<ext>`，随后以
`- YYYY-MM-DD HH:mm | [图片] <相对路径...> <附注>` 的格式追加 Inbox 并 git commit。
图片来源优先取 segment 的 `url`（http/https，urllib 下载，10s 超时），
其次 `file` 本地路径直接复制（LLOneBot 常给本地路径）；单张失败不影响其他，
回执会注明失败张数。该 matcher 为 priority=4 的 on_message，仅命中
"私聊 + superuser + 含图片"的消息，普通文字与命令不受影响。

## 定时任务与补发

照抄 `SessionManager.start_cleanup_task` 的 asyncio 范式（算下次触发 → sleep → 执行），
由 nonebot `on_startup` 启动，零新增依赖：

| 任务 | 配置项 | 默认 | 通道 |
|---|---|---|---|
| 每日日结 + 今日建议简报 | `lifeos.daily_brief_time` | 04:00 | agent（日结 SOP，断连重试 1 次） |
| 每日未记录提醒 | `lifeos.reminder_time` | 22:30 | 直写判定（日记缺失或"推进"为空才推） |
| 周日周结提醒 | `lifeos.weekly_reminder` | 周日 10:00 | 直推 |
| 自定义提醒（/提醒） | `logs/lifeos_reminders.json` | 30s tick | 直推 `⏰ <内容>` |

自定义提醒独立一个 30 秒 tick 的 asyncio loop：扫描 `next_fire <= 今天` 且触发时刻已过的
条目推送；单次提醒推送成功后删除，每天提醒的 `next_fire` 推一天；推送失败（LLOneBot
掉线）保留不动，下个 tick 重试。关机后开机时，过去日期的条目会被立即补推一次，
正好覆盖关机/睡眠场景。持久化文件写入是原子的（tmp + replace）。

补发机制：

- 每次任务成功触发后，把 `{job: 日期}` 写入 `lifeos.state_file`（默认
  `logs/lifeos_state.json`）。
- 启动时及每 5 分钟巡检一次：某 job"上次应发时刻 > last_fired"则立即补发，
  只补最近一次，不追历史欠账。
- agent_ws 断连 / LLOneBot 离线时任务记日志并保持未触发状态，恢复后由巡检补发。
- 电脑睡眠导致的错过同样走补发兜底；想精确 04:00 送达可自行
  `sudo pmset repeat wakeorpoweron MTWRFSU 03:55:00`（手动执行）。

## 安全边界

- LifeOS 命令全部要求 superuser（`3058442393`，见 `qq_bot.py`），非授权零响应。
- vault 内所有路径拼接做 `resolve()` 越界检查；写入内容过 SecurityEngine 敏感词过滤。
- LifeOS 会话（session_id 命中 `KIMI_AUTO_APPROVE_SESSIONS`，默认 `lifeos_main`，或
  cwd == `lifeos.vault_path`）被标记为无人值守会话：cwd 锁定 vault 内，提示词为固定文本，
  vault git 全量历史兜底。kimi 的 print 模式本身非交互，不存在交互式批准环节。

## 测试

```bash
python -m pytest          # 全量（含 tests/test_lifeos.py）
ruff check src tests scripts
```
