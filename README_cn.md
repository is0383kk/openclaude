<table>
  <thead>
      <tr>
          <th style="text-align:center"><a href="./README.md">English</a></th>
          <th style="text-align:center">Chinese</th>
          <th style="text-align:center"><a href="./README_ja.md">日本語</a></th>
      </tr>
    </thead>
</table>

# 🦀OpenClaude — Claude Code-native personal AI assistant

基于 `claude-agent-sdk` 构建的持久化 AI 智能体系统。以 Claude Code 的 `settings.json` 为基础运行  
本项目受 [OpenClaw](https://github.com/openclaw/openclaw) 启发而创建  
以 Unix 套接字服务器形式常驻运行，接收来自 CLI 和 REST API 的消息并将其代理至 Claude

---

## 功能列表

| 功能                        | 命令 / 端点                                               |
| --------------------------- | --------------------------------------------------------- |
| 守护进程启动/停止/重启/状态 | `openclaude start/stop/restart/status`                    |
| 发送消息（流式传输）        | `openclaude -m "消息"`                                    |
| stdin / 管道输入            | `echo "问题" \| openclaude`                               |
| 查看日志                    | `openclaude logs [--tail N]`                              |
| 会话管理                    | `openclaude sessions`                                     |
| Cron 任务管理               | `openclaude cron add/list/delete/run`                     |
| HTTP REST API               | `POST /message`, `POST /message/stream`, `GET /status` 等 |
| Cron REST API               | `GET /cron`, `POST /cron`, `DELETE /cron/{id}` 等         |

---

## 安装配置

### 前提条件

- Linux / Windows（WSL2）
- Python >= 3.14
- [可使用 claude-agent-sdk 的环境](https://platform.claude.com/docs/zh-CN/agent-sdk/overview)

### 依赖包

| 包名                       | 用途                    |
| -------------------------- | ----------------------- |
| `claude-agent-sdk>=0.1.48` | Claude AI 智能体 SDK    |
| `fastapi>=0.115.0`         | REST API 框架           |
| `uvicorn>=0.30.0`          | ASGI 服务器             |
| `apscheduler>=3.10,<4`     | Cron 任务调度器（v3.x） |

### 安装步骤

```bash
git clone <repository-url> ~/.openclaude
cd ~/.openclaude
pip install -r requirements.txt
```

> **注意：** 项目必须放置在 `~/.openclaude/` 目录下。
> 由于 `src/config.py` 使用 `Path.home() / ".openclaude"` 作为基础路径，放在其他目录将无法正常运行。

---

## 使用方法

### 守护进程管理

```bash
# 启动（默认端口：28789）
openclaude start

# 指定端口启动
openclaude start --port 18789

# 停止
openclaude stop

# 重启
openclaude restart

# 查看状态
openclaude status

# 查看日志
openclaude logs           # 全部内容
openclaude logs --tail 50 # 最后50行
```

### 发送消息

```bash
# 简单发送
openclaude -m "提示词"

# 指定会话
openclaude --session-id work -m "提示词"

# stdin / 管道
echo "问题" | openclaude
cat report.txt | openclaude -m "请总结这份内容"
git diff | openclaude -m "请审查这个diff"
```

### 会话管理

```bash
# 列出会话
openclaude sessions

# 删除所有会话
openclaude sessions cleanup

# 删除指定会话
openclaude sessions delete <session-id>
```

### Cron 任务

```bash
# 添加任务（每天早上9点执行）
openclaude cron add "0 9 * * *" --name "morning" --session main -m "整理今天的任务"

# 列出任务
openclaude cron list

# 手动执行
openclaude cron run <job-id>

# 删除任务
openclaude cron delete <job-id>
```

### systemd 集成（已配置的情况下）

```bash
systemctl --user start openclaude
systemctl --user stop openclaude
systemctl --user status openclaude
```

---

## REST API

启动守护进程后，默认可通过 `http://localhost:28789` 访问。

| 方法     | 路径              | 说明                     |
| -------- | ----------------- | ------------------------ |
| `POST`   | `/message`        | 发送消息（完整响应）     |
| `POST`   | `/message/stream` | 发送消息（SSE 流式传输） |
| `GET`    | `/status`         | 守护进程状态与 PID       |
| `GET`    | `/sessions`       | 会话列表                 |
| `DELETE` | `/sessions`       | 删除所有会话             |
| `DELETE` | `/sessions/{id}`  | 删除指定会话             |
| `GET`    | `/cron`           | Cron 任务列表            |
| `POST`   | `/cron`           | 添加 Cron 任务           |
| `DELETE` | `/cron/{id}`      | 删除 Cron 任务           |
| `POST`   | `/cron/{id}/run`  | 手动执行 Cron 任务       |

---

## 架构

```
CLI (openclaude)
  └── src/cli.py
        └── 通过 Unix 套接字（~/.openclaude/openclaude.sock）与守护进程通信

守护进程 + API 服务器（同一进程）
  ├── src/daemon.py  ── Unix 套接字服务器
  ├── src/api.py     ── FastAPI + uvicorn（REST API）
  └── src/cron.py    ── 基于 apscheduler 的调度器
```

### 文件结构

```
~/.openclaude/
  ├── src/
  │   ├── config.py    # 文件路径常量与日志配置
  │   ├── daemon.py    # Unix 套接字服务器与消息处理器
  │   ├── api.py       # FastAPI REST API 服务器
  │   ├── cron.py      # Cron 任务管理（CronJob / CronScheduler）
  │   └── cli.py       # CLI 入口点
  ├── sessions/
  │   └── sessions.json         # 会话别名 → SDK 会话 ID 映射
  ├── cron/
  │   ├── jobs.json             # Cron 任务定义（持久化）
  │   └── runs/<job_id>.jsonl   # 执行历史记录
  ├── openclaude.sock           # Unix 套接字（仅运行时存在）
  ├── openclaude.pid            # PID 文件（仅运行时存在）
  └── daemon.log                # 守护进程日志
```
