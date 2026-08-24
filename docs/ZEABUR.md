# Zeabur 部署清单

这份清单对应当前项目的单服务、单端口、单 SQLite 实例架构。Zeabur 使用根目录 `Dockerfile` 构建，不使用 `docker-compose.yml`。

相关官方说明：

- [Dockerfile 自动部署](https://zeabur.com/docs/en-US/deploy/methods/dockerfile)
- [环境变量](https://zeabur.com/docs/en-US/deploy/config/environment-variables)
- [公开网络与 PORT](https://zeabur.com/docs/en-US/deploy/networking/public-networking)
- [持久卷](https://zeabur.com/docs/en-US/operations/data/volumes)
- [健康检查](https://zeabur.com/docs/en-US/operations/monitoring/health-checks)
- [备份与恢复](https://zeabur.com/docs/en-US/operations/data/backup-restore)

## 0. Discord 侧先准备

在 [Discord Developer Portal](https://discord.com/developers/applications) 完成：

1. 创建 Application 和 Bot。
2. 开启 **Message Content Intent**。
3. OAuth2 scopes 选择 `bot` 和 `applications.commands`。
4. 邀请权限至少包含 View Channels、Send Messages、Read Message History、Use Application Commands。
5. 保存 Bot Token，只把它放进 Zeabur Secret/Variable，绝不提交到 Git。

## 1. 生成启动密钥

在本地项目根目录执行：

```bash
python -m app.tools generate-secrets
```

会输出两项：

```dotenv
SESSION_SECRET=...
CONFIG_ENCRYPTION_KEY=...
```

另外准备一个至少 16 位、包含大写字母、小写字母、数字和特殊字符的 `ADMIN_PASSWORD`。示例格式只能用于理解，不要照抄：

```text
四个随机单词 + 大小写变化 + 两个数字 + 两个符号
```

把三项保存在密码管理器。尤其不能丢失 `CONFIG_ENCRYPTION_KEY`。

## 2. 从 GitHub 创建服务

1. 把本目录作为你自己的 GitHub 仓库内容推送。
2. 在 Zeabur 新建 Project，选择 **Deploy New Service → Git**。
3. 连接仓库并选择根目录。
4. Zeabur 应识别为 Dockerfile 服务。

初始环境不完整时容器会明确报错并退出，这是预期的安全失败。完成下面配置后再重新部署。

## 3. 先挂载持久卷

在该服务的 Volume 设置中创建 Volume，挂载路径必须是：

```text
/data
```

数据库环境变量必须是：

```dotenv
DB_PATH=/data/mobo.db
```

不要挂载单个文件，要挂载目录 `/data`，这样 SQLite 的 `mobo.db-wal` 和 `mobo.db-shm` 也能与主文件一起持久化。

服务副本数保持 **1**。不要让两个容器同时写这个 SQLite 文件。

## 4. 设置环境变量

在 Zeabur 服务的 Variables 中填写：

| 变量 | 必填值 / 说明 |
|---|---|
| `DISCORD_TOKEN` | Discord Developer Portal 的官方 Bot Token |
| `ADMIN_USERNAME` | 建议 `admin` |
| `ADMIN_PASSWORD` | 首次建库用的 16 位以上复杂密码 |
| `SESSION_SECRET` | 上一步生成值，至少 32 字符 |
| `CONFIG_ENCRYPTION_KEY` | 上一步生成的 Fernet 密钥 |
| `DB_PATH` | `/data/mobo.db` |
| `COOKIE_SECURE` | `true` |
| `PUBLIC_BASE_URL` | 最终公网地址，例如 `https://bot.example.zeabur.app` |
| `ALLOWED_HOSTS` | 只写域名，不带 `https://` 和路径；多个用逗号分隔 |
| `LOG_LEVEL` | 建议 `INFO` |

`PORT` 通常由 Zeabur 自动注入。应用优先读取 `PORT`，缺省值为 8080，不需要手工写死。

`ADMIN_PASSWORD` 只在数据库里不存在管理员时使用。首次成功建库后，密码以 Argon2id 哈希保存；以后在管理台修改密码不会要求同步修改该环境变量。只要 `/data` 未丢失，重启也不会重新套用旧环境密码。

## 5. 公开网络与健康检查

1. 为服务添加 Zeabur Domain 或绑定自定义域名。
2. 把最终 HTTPS 地址写入 `PUBLIC_BASE_URL`。
3. 把最终域名写入 `ALLOWED_HOSTS`。
4. 健康检查路径设为：

   ```text
   /healthz
   ```

5. 重新部署。

`/healthz` 只公开数据库可用性与 Discord 的 `starting/ready` 状态，不公开配置、频道或记忆。

## 6. 第一次登录

打开最终域名，使用 `ADMIN_USERNAME` 与首次密码登录。

按顺序完成：

1. **全部配置 → 模型**：选择提供方、模型 ID，填写对应 API Key。
2. **基础与人设**：确认管理台公网地址、时区、状态文字和核心人设。
3. **记忆**：确认 30 天原始消息保留和 180 天自动记忆过期是否合适。
4. **性格与行为 → 频道授权**：只打开确实允许机器人监听的频道。
5. 如果要主动发言，先为频道打开“监听 + 主动”，再到全部配置打开全局总开关。
6. **安全**：如需更换首次密码，在这里修改。修改会注销全部管理会话。

## 7. Discord 验收

用普通成员账号检查：

- 可以看到 `/帮助`、`/状态`、`/记住`、`/忘记`、`/我的记忆`、`/忘记我`、`/隐私`、`/关系`、`/喜好`。
- 不应看到管理员命令。

用有 Administrator 权限的账号检查：

- 除公开命令外，还能看到 `/管理台`、`/清空频道`、`/人设`、`/模型`、`/频道设置`、`/主动发言`、`/重载配置`。
- `/管理台` 的响应只对调用者可见。

如果命令列表尚未刷新，等待 Discord 全局命令同步并重启客户端。不要反复创建多个 Bot 应用。

## 8. 备份与升级

### 备份

SQLite 使用 WAL。最稳妥的卷级备份流程是：

1. 暂停服务。
2. 对 `/data` Volume 执行 Zeabur 备份或快照。
3. 完成后恢复服务。

如果平台提供 Volume Backup，可以按 Zeabur 官方备份文档操作。恢复时必须同时保持原来的 `CONFIG_ENCRYPTION_KEY`，否则数据库中的模型密钥无法读取。

### 升级

1. 先备份 `/data`。
2. 合并新代码并重新部署。
3. 观察 `/healthz` 和服务日志。
4. 登录管理台检查总览和审计记录。

Volume 服务更新可能有短暂停机。不要为了消除停机而临时扩到两个副本；那会破坏 SQLite 的单写入者边界。

## 故障排查

| 现象 | 优先检查 |
|---|---|
| 服务不断重启 | 日志中的“启动配置不完整”；五个必需启动变量是否填写 |
| 管理台能开但登录后马上回到登录页 | `COOKIE_SECURE=true` 且正在使用 HTTPS；域名是否在 `ALLOWED_HOSTS` |
| 机器人在线但看不到消息内容 | Discord Portal 是否开启 Message Content Intent |
| 频道里提及机器人没有回复 | 模型 API Key、提供方和模型 ID；机器人 Send Messages / Read Message History 权限 |
| `/管理台` 没有地址 | `PUBLIC_BASE_URL` 或管理台中的“管理台公网地址” |
| 重启后数据消失 | Volume 是否真正挂载到 `/data`；`DB_PATH` 是否为 `/data/mobo.db` |
| 日志提示无法解密配置 | 当前 `CONFIG_ENCRYPTION_KEY` 与首次保存模型密钥时不同 |
| 非管理员仍能看到旧命令 | 等待 Discord 缓存刷新；确认不是服务器管理员；执行时后端仍会拒绝 |
| 升级时出现数据库锁 | 确认副本数为 1，没有第二个旧容器同时挂载同一 Volume |
