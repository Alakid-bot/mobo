# mobo

一个只使用官方 Discord Bot API 的中文 Discord 角色机器人。它有分层记忆、逐步变化的关系、可演化的主题偏好、会回归基线的临时情绪，以及一个完整的私密可视化管理台。

这是一个遵循 Apache License 2.0 的衍生项目，原始作品归属与修改说明详见 [NOTICE](NOTICE)。它不是 selfbot，不需要也不应使用个人 Discord Token。

## 已实现内容

- 中文原生命令名称和中文参数名称。
- 普通成员与管理员两套命令可见性；管理员命令同时做 Discord 默认权限限制和服务端权限复核。
- 服务器隔离的用户记忆与关系，不会把 A 服务器的资料带到 B 服务器。
- 显式记忆、保守的自动记忆、频道近期上下文和不删除原文的频道摘要。
- 熟悉度、信任、温暖、疲劳四维关系，以及会自然回归的愉悦度、精力、社交余量。
- 可锁定或随证据缓慢变化的机器人主题偏好。
- 双重主动发言开关、安静时段、同频道冷却、每日上限、偏好与关系联合决策。
- OpenAI、Anthropic、OpenRouter、Ollama / OpenAI 兼容接口。
- 强密码管理台、Argon2id 密码哈希、数据库会话、CSRF、登录锁定、安全响应头和操作审计。
- 模型 API Key 通过 Fernet 独立密钥加密后写入 SQLite。
- SQLite WAL、过期清理、索引、健康检查和 Zeabur `/data` 持久卷路径。
- 单容器同时运行 Discord Gateway 和 FastAPI 管理台，只占用 Zeabur 一个公开端口。

## 命令分层

### 普通成员可见

| 命令 | 用途 |
|---|---|
| `/帮助` | 查看自己当前可用的命令 |
| `/状态` | 查看模型、延迟、情绪和隐私摘要 |
| `/记住 内容` | 保存一条不会自动淘汰的显式记忆 |
| `/我的记忆` | 只查看自己在当前服务器或私信范围中的记忆 |
| `/忘记 编号或关键词` | 删除自己的一条或多条记忆 |
| `/忘记我` | 确认后删除当前服务器或私信范围内自己的消息、记忆和关系 |
| `/隐私` | 查看当前数据保留与隔离策略 |
| `/关系` | 查看与机器人的关系概况 |
| `/喜好` | 查看机器人当前主题偏好 |

### 仅服务器管理员可见

| 命令 | 用途 |
|---|---|
| `/管理台` | 私密返回管理台地址，仍需密码登录 |
| `/清空频道` | 清空当前频道上下文和摘要 |
| `/人设 提示词` | 设置服务器级人设覆盖；输入“默认”清除覆盖 |
| `/模型 提供方 模型` | 切换提供方和模型 ID |
| `/频道设置` | 设置某频道是否监听、是否允许主动发言 |
| `/主动发言` | 控制全局主动发言总开关 |
| `/重载配置` | 清理配置缓存并刷新 Discord 状态 |

Discord 的 `default_member_permissions` 会让管理员命令默认只向管理员显示；每个处理函数还带有管理员权限检查。即使 Discord 服务器以后手动改了命令权限，非管理员也无法绕过后端检查。

## 数据与行为模型

```text
Discord 消息
    ├─ 被提及 / 回复机器人 → 必定进入回复流程
    └─ 普通频道消息
         └─ 全局开关 + 频道监听 + 频道主动开关
              + 安静时段 + 冷却 + 日上限
              + 话题偏好 + 熟悉度 + 社交余量
                    → 概率性主动参与

提示词上下文
    ├─ 核心人设或服务器覆盖
    ├─ 临时情绪
    ├─ 机器人主题偏好
    ├─ 当前服务器内的用户关系
    ├─ 当前服务器内的用户长期记忆（标注为不可信数据）
    ├─ 较早频道摘要
    └─ 近期频道消息
```

默认隐私策略如下。

- 私信处理关闭。
- 原始消息最多保留 30 天。
- 显式 `/记住` 不自动过期。
- 自动提取的记忆默认 180 天过期，且每人有数量上限。
- 记忆和关系均以 `guild_id + user_id` 隔离。
- 主动发言全局关闭，具体频道也默认关闭。
- `/忘记我` 只删除请求者所在的当前服务器或私信数据范围，不影响其他服务器、其他范围或其他用户。即使管理员后来关闭了私信处理，用户仍能在私信里调用隐私查看与删除命令清理旧数据。

这些策略都能在管理台调整。

## 管理台

登录后可看到以下页面。

- **总览**：Discord 连接、数据库统计、情绪、偏好和最近审计记录。
- **全部配置**：基础人设、模型、记忆、关系与情绪、回复与主动发言、限流与安全的全部运行时参数。
- **性格与行为**：频道双开关、偏好主题和关键词、临时情绪、每服务器人设。
- **记忆库**：按服务器和用户筛选长期记忆，并可由管理员删除。
- **审计记录**：登录、配置、频道授权、密码和删除操作。
- **安全**：更换管理密码、查看活跃会话和只读启动配置状态。

有五类值不会通过网页回显或修改，因为它们必须在管理台能够安全启动之前就存在：`DISCORD_TOKEN`、`ADMIN_PASSWORD` 首次种子、`SESSION_SECRET`、`CONFIG_ENCRYPTION_KEY` 和 `DB_PATH`。管理台会显示它们是否已设置以及数据库实际路径。其他运行时配置都在网页中可视化编辑。

## Discord 应用准备

1. 打开 [Discord Developer Portal](https://discord.com/developers/applications)，创建应用并添加 Bot。
2. 在 **Bot → Privileged Gateway Intents** 打开 **Message Content Intent**。本项目不需要个人账号 Token，也不需要 Selfbot。
3. 在 **OAuth2 → URL Generator** 选择 `bot` 与 `applications.commands`。
4. 建议授予：View Channels、Send Messages、Read Message History、Use Application Commands。
5. 用生成的地址邀请机器人，把 Bot Token 保存到部署环境的 `DISCORD_TOKEN`。

全局 Slash Command 第一次同步后，Discord 客户端可能需要一点时间刷新。可以重启客户端或稍后再看。

## 本地运行

要求 Python 3.12+。

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python -m app.tools generate-secrets
```

把生成的两行复制到 `.env`，再填写：

```dotenv
DISCORD_TOKEN=你的官方机器人Token
ADMIN_USERNAME=admin
ADMIN_PASSWORD=至少16位且含大小写数字特殊字符
SESSION_SECRET=生成值
CONFIG_ENCRYPTION_KEY=生成值
DB_PATH=data/mobo.db
HOST=0.0.0.0
PORT=8080
PUBLIC_BASE_URL=http://localhost:8080
COOKIE_SECURE=false
ALLOWED_HOSTS=localhost,127.0.0.1
```

启动：

```bash
python -m app.main
```

访问 `http://localhost:8080`。首次数据库初始化后，`ADMIN_PASSWORD` 会用 Argon2id 写成哈希；之后可在安全页修改密码。环境中的 `ADMIN_PASSWORD` 只在数据库里还没有管理员时作为首次种子使用。

也可本地使用：

```bash
docker compose up --build
```

## Zeabur 部署

完整逐项操作见 [docs/ZEABUR.md](docs/ZEABUR.md)。最重要的部署约束是：

1. 使用 GitHub 仓库部署，Zeabur 会自动识别根目录 `Dockerfile`。不要把 `docker-compose.yml` 当成 Zeabur 部署文件；它只供本地使用。
2. 在服务第一次正式启动前创建 Volume，并挂载到 `/data`。
3. 设置 `DB_PATH=/data/mobo.db`，只运行 **1 个副本**。SQLite 不适合多个实例同时写同一个数据库。
4. 设置以下环境变量：

   ```dotenv
   DISCORD_TOKEN=...
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=...
   SESSION_SECRET=...
   CONFIG_ENCRYPTION_KEY=...
   DB_PATH=/data/mobo.db
   COOKIE_SECURE=true
   PUBLIC_BASE_URL=https://你的Zeabur域名
   ALLOWED_HOSTS=你的Zeabur域名
   LOG_LEVEL=INFO
   ```

   `PORT` 由 Zeabur 注入，应用会自动读取；如果平台没有注入则默认 8080。

5. 开启公开网络并把健康检查路径设为 `/healthz`。
6. 管理台首次登录后，在“全部配置 → 模型”填写模型提供方、模型 ID 和对应 API Key。

必须长期保存 `CONFIG_ENCRYPTION_KEY`。如果换掉它，数据库内已有模型密钥无法解密。对 `/data` 做快照或备份时，建议先暂停服务，避免复制到写入中的 SQLite WAL。

## 项目结构

```text
app/
├── auth.py          # Argon2id、登录锁定、数据库会话、CSRF
├── behavior.py      # 频道授权与主动发言决策
├── cognition.py     # 关系、偏好、情绪与上下文组装
├── config.py        # 只读启动配置
├── crypto.py        # 模型密钥加密
├── database.py      # SQLite schema、WAL、清理与事务
├── discord_bot.py   # Discord 事件、中文公开/管理员命令
├── llm.py           # 四类模型后端
├── main.py          # 单进程编排 Web 与 Discord Gateway
├── memory.py        # 长期记忆、历史、摘要和删除
├── runtime.py       # 全部可视化运行时配置定义
├── state.py         # 服务依赖组装
├── static/          # 无外部 CDN 的管理台 CSS/JS
├── templates/       # 中文管理台页面
└── web.py           # 受保护的 FastAPI 路由
tests/               # 权限、隐私、认证、行为与网页测试
Dockerfile
docker-compose.yml   # 仅本地
```

## 验证

```bash
pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

测试覆盖强密码、Argon2id、锁定、会话和 CSRF、密钥加密、服务器隔离、自动记忆、`忘记我`、关系边界、主动发言双开关与安静时段、中文命令集合、管理员默认权限、管理台保护和安全响应头。

## 运维边界

- 只运行一个应用实例；需要水平扩容时先把 SQLite 换成 PostgreSQL，并增加分布式锁与任务协调。
- 管理台和 Discord Gateway 在同一个进程中，任一关键组件异常会让容器退出，由 Zeabur 重启。
- Zeabur Volume 挂载会让更新出现短暂停机，这是单实例 SQLite 的预期取舍。
- 不要把 `.env`、Discord Token、模型 API Key 或数据库提交到 Git。
- 更新前备份 `/data`。升级后不要用旧容器和新容器同时挂载同一个 SQLite 文件。

## 许可证

Apache License 2.0。原项目版权与归属保留在 [LICENSE](LICENSE) 和 [NOTICE](NOTICE) 中。
