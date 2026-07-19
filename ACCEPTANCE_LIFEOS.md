# LifeOS Phase 3 联调验收清单

> 前置条件：① moonshot 开放平台已充值，`kimi -p "测试" -m moonshot-cn/kimi-k2.6` 不再报 429；
> ② LLOneBot/NapCat 已登录在线；③ 在仓库根目录操作（/Users/fuyuuku/ACP_QQ_KIMI）。

## 0. 启动桥

```bash
# 方式一（推荐）：一键拉起 kimi bridge + nonebot
bash scripts/start_production.sh
# 方式二：分两个终端
.venv/bin/python scripts/kimi_code_bridge.py
.venv/bin/python -m acp_qq_bridge
```

启动日志应看到：kimi binary 版本 0.27.0、model = moonshot-cn/kimi-k2.6、auto-approve 会话 = lifeos_main、LifeOS 定时任务已启动。

## 1. 手机 QQ 逐条验收（私聊机器人）

| 步骤 | 发送 | 预期 |
|---|---|---|
| 1 | /任务 | 返回 LifeOS 命令表（注意：不是 /帮助，/帮助被现有聊天功能占用） |
| 2 | /记 联调测试第一条 | 一行回执；仓库 40_收集箱/Inbox.md 出现该条；vault 里 git log 有新 commit |
| 3 | /打卡 拉丁语 ch1 练习1-5 | 当日日记"推进"节追加 `- [拉丁语] ch1 练习1-5`；有 commit |
| 4 | /能量 7 | 当日日记 frontmatter `能量: 7` |
| 5 | /休闲 散步30min | 当日日记"休闲"节追加 |
| 6 | /总览 | 九条轨道逐行纯文本（无表格竖线） |
| 7 | /下步 | 按轨道分组的未勾选项清单 |
| 8 | /周结 | 立即收到"收到，在做"；几分钟后收到 ≤10 行纯文本汇报（凯尔希口吻，无 markdown 表格/加粗）；30_日志/周结/ 出现本周 .md + 同名 .qq.txt + commit；随后收到 .qq.txt 推送（待决策项已编号） |
| 9 | /决策 1 同意 | 最新周结"需要我决策的"第 1 项被勾选并记下"同意"（需步骤 8 先生成周结） |
| 10 | /复活 | 收到一份标注"（推断）"的粗摘要 |
| 11 | 用另一个 QQ 号发 /总览 | 零响应（鉴权生效） |
| 12 | /提醒 23:55 测试提醒 | 一行回执（编号+触发时间）；logs/lifeos_reminders.json 出现该条；到点收到"⏰ 测试提醒"；单次提醒触发后从 json 消失。再发 /提醒 每天 07:50 吃药 → /提醒列表 有两条 → /删提醒 1 → 列表只剩每天那条 |
| 13 | 私聊发一张图片（可带一句附注） | 回执"已存 1 张图到灵感草稿"；40_收集箱/灵感草稿/assets/ 出现该图；Inbox 追加 `- ... \| [图片] <路径> <附注>`；有 commit |

## 2. 安全演练

1. 让 agent 改坏一个 STATE（如 /周结 后手动改乱再让 agent 处理，或直接编辑）→
   vault 里 `git checkout -- 20_进度/xxx_STATE.md` 或 `git revert` 恢复成功。
2. 构造越界写入（在 Inbox 里写 `../../etc/passwd` 类路径让分拣 agent 处理）→ 被拒绝。

## 3. 定时推送验证

1. 临时把 config.yaml 的 `lifeos.reminder_time` 改成当前时间+2 分钟，重启桥 →
   到点收到提醒（若当日日记已有"推进"内容则不会发，属正常判定）。
2. 验证完改回 `"22:30"` 并重启。

## 4. 补发演练（关机/断连边界）

1. 编辑 logs/lifeos_state.json，把某个 job 的 last_fired 改成昨天日期。
2. 重启桥 → 5 分钟内（启动即巡检一次）应收到该 job 的补发推送。
3. 04:00 日结同理可验证：把 daily_brief 的 last_fired 改昨天，重启 → 收到"昨日日结+今日建议"。

## 5. 收尾

- [ ] 恢复所有正式时间配置并重启桥。
- [ ] 勾选 Dionysus_STATE.md 中 B1–B5 对应进展（周结时让 agent 做也行）。
- [ ] （可选，让电脑 3:55 自动醒来发日结）`sudo pmset repeat wakeorpoweron MTWRFSU 03:55:00`
- [ ] 桥接仓库的 git 提交由你自行决定（仓库里还有你自己的未提交改动，agent 未动 git）。

## 常见问题

- **/周结 报 429/余额不足**：moonshot 未充值，或临时把 agent.model 改为 "" 走 kimi-code 订阅。
- **推送没收到但日志正常**：LLOneBot 反向 WS 掉了，看 nonebot 日志；恢复后 5 分钟巡检会补发。
- **QQ 收到带 ** 加粗或表格的汇报**：说明消息没走 LifeOS 会话路由，检查 kimi_code_bridge 日志里该会话是否带 lifeos 标记。
