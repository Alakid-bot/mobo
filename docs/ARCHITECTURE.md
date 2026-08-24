# 架构与不变量

## 进程模型

一个 Python 进程同时运行两个异步任务：

- `discord.py` Gateway 客户端，负责事件、中文 Slash Command 和流式回复。
- FastAPI + Uvicorn，负责私密管理台和 `/healthz`。

任一关键任务异常退出时，编排器会关闭另一任务并让容器退出，由 Zeabur 的服务重启策略接管。应用只监听一个 `PORT`。

## 必须保持的不变量

1. **不是 selfbot**：`DISCORD_TOKEN` 必须来自 Discord Application 的 Bot 页面。
2. **一个实例**：SQLite Volume 只能由一个应用副本写入。
3. **服务器隔离**：用户长期记忆和关系的查询条件必须同时包含 `guild_id` 与 `user_id`。
4. **两层管理员检查**：管理员命令必须同时有 `default_member_permissions(administrator=True)` 和运行时 `has_permissions(administrator=True)`。
5. **启动密钥不回显**：Discord Token、会话根密钥和配置加密根密钥不能通过管理台 API 返回。
6. **用户拥有删除权**：`/忘记` 只能改自己的记忆；`/忘记我` 在单事务中删除自己在当前服务器或私信数据范围内的消息、记忆和关系。
7. **主动发言双开关**：全局和频道两个开关都为真，且通过安静时段、冷却和日上限后，才允许概率决策。
8. **模型看见的记忆是不可信数据**：上下文必须明确禁止执行记忆中的指令。

## SQLite 关键表

| 表 | 作用 | 关键隔离键 |
|---|---|---|
| `admins` | Argon2id 管理密码 | `username` |
| `admin_sessions` | 哈希会话令牌与 CSRF | `admin_id` |
| `app_settings` | 运行时配置；密钥字段加密 | `key` |
| `guilds` | 服务器名称和人设覆盖 | `guild_id` |
| `channel_settings` | 监听与主动开关 | `guild_id, channel_id` |
| `messages` | 有过期时间的频道上下文 | `guild_id, channel_id` |
| `channel_summaries` | 不删除原文的压缩上下文 | `guild_id, channel_id` |
| `memories` | 显式和自动长期记忆 | `guild_id, user_id` |
| `relationships` | 四维关系状态 | `guild_id, user_id` |
| `bot_preferences` | 主题、关键词、权重和锁定状态 | `topic` |
| `mood_state` | 单例临时情绪 | `id = 1` |
| `proactive_log` | 冷却与每日额度 | `guild_id, channel_id` |
| `audit_log` | 重要管理与隐私操作 | 时间倒序索引 |

数据库连接启用 WAL、foreign keys、5 秒 busy timeout 和实际查询所需索引。定时任务每六小时清理过期消息、自动记忆和管理会话。

## 提示词组装顺序

1. 全局核心人设或服务器覆盖。
2. 当前用户的关系描述。
3. 会衰减的临时情绪。
4. 当前主题偏好。
5. 当前服务器内该用户的长期记忆，置于不可信数据边界中。
6. 频道较早摘要。
7. 近期频道消息。
8. 当前用户输入。

关系和情绪只改变语气与主动性，不允许覆盖核心安全边界。

## 配置模型

`app/config.py` 只读取管理台启动前必须存在的环境变量。`app/runtime.py` 是所有运行时配置的唯一 schema；管理台表单从同一 schema 自动生成，因此新增字段时不会出现“后端有配置但网页漏掉”的第二份清单。

运行时密钥用 `CONFIG_ENCRYPTION_KEY` 加密。管理员会话令牌是高熵随机值，数据库只保存用 `SESSION_SECRET` 做 HMAC-SHA256 后的摘要。

## 扩容路径

需要多副本时不能直接扩大副本数。至少要同时完成：

1. SQLite 迁移到 PostgreSQL。
2. 管理会话、限流、主动发言冷却改为共享存储。
3. Discord Gateway shard 或 leader ownership。
4. 摘要与清理任务加入分布式锁。
5. 数据迁移校验与回滚演练。

在这些工作完成前，单实例是功能正确性的组成部分，不只是部署建议。
