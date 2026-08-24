from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from app.database import iso_now
from app.llm import LLMConfigurationError, build_backend
from app.rate_limit import RateLimiter
from app.state import ApplicationState

log = logging.getLogger("mobo.discord")
MAX_DISCORD_MESSAGE = 1980


def _is_admin(interaction: discord.Interaction) -> bool:
    return bool(
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


def _guild_id(interaction: discord.Interaction) -> str:
    if interaction.guild_id is None:
        raise app_commands.NoPrivateMessage()
    return str(interaction.guild_id)


def _channel_id(interaction: discord.Interaction) -> str:
    if interaction.channel_id is None:
        raise app_commands.CheckFailure("这个命令需要在服务器频道中使用")
    return str(interaction.channel_id)


def _privacy_scope_id(interaction: discord.Interaction) -> str:
    if interaction.guild_id is not None:
        return str(interaction.guild_id)
    return f"dm:{interaction.user.id}"


def image_content(
    text: str,
    attachments: list[discord.Attachment],
    *,
    enabled: bool,
) -> str | list[dict[str, Any]]:
    images = [
        attachment
        for attachment in attachments
        if enabled and attachment.content_type and attachment.content_type.startswith("image/")
    ]
    if not images:
        return text
    content: list[dict[str, Any]] = [{"type": "text", "text": text or "请看这张图片。"}]
    for attachment in images[:4]:
        content.append({"type": "image_url", "image_url": {"url": attachment.url}})
    return content


class ChineseCommandTree(app_commands.CommandTree):
    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "这个命令只允许服务器管理员使用。"
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"操作太快了，请在 {error.retry_after:.0f} 秒后再试。"
        else:
            log.exception("slash command failed", exc_info=error)
            message = "执行失败。管理员可以在管理台的审计记录和服务日志中查看原因。"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class ForgetMeView(discord.ui.View):
    def __init__(
        self,
        state: ApplicationState,
        guild_id: str,
        user_id: str,
        scope_label: str,
    ):
        super().__init__(timeout=60)
        self.state = state
        self.guild_id = guild_id
        self.user_id = user_id
        self.scope_label = scope_label
        self.completed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("这不是你的确认按钮。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="确认删除我的数据", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.state.database.purge_user(self.guild_id, self.user_id)
        await self.state.database.audit(
            f"discord:{self.user_id}",
            "privacy.forget_me",
            target=f"guild:{self.guild_id}",
        )
        self.completed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"已删除你在{self.scope_label}中的消息记录、长期记忆和关系状态。其他数据范围没有受到影响。",
            view=self,
        )
        self.stop()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="已取消，没有删除任何数据。", view=self)
        self.stop()


class PublicCommands(commands.Cog):
    def __init__(self, bot: MoboBot):
        self.bot = bot
        self.state = bot.state

    @app_commands.command(name="帮助", description="查看你可以使用的中文命令")
    @app_commands.guild_only()
    async def help(self, interaction: discord.Interaction) -> None:
        public = "`/状态` `/记住` `/忘记` `/我的记忆` `/忘记我` `/隐私` `/关系` `/喜好`"
        admin = (
            "\n\n管理员命令\n`/管理台` `/清空频道` `/人设` `/模型` "
            "`/频道设置` `/主动发言` `/重载配置`"
            if _is_admin(interaction)
            else ""
        )
        await interaction.response.send_message(
            "可用命令\n" + public + admin + "\n\n也可以提及我或回复我的消息来聊天。",
            ephemeral=True,
        )

    @app_commands.command(name="状态", description="查看机器人当前状态和隐私摘要")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        config = await self.state.runtime.all()
        mood = await self.state.mood.current(config)
        status = self.state.bot_status
        latency = f"{round(self.bot.latency * 1000)} ms" if self.bot.is_ready() else "未连接"
        await interaction.response.send_message(
            f"**{config['bot_name']}** · {'在线' if status.ready else '启动中'}\n"
            f"模型：`{config['llm_provider']} / {config['llm_model']}`\n"
            f"延迟：{latency}\n"
            f"心情：{mood['label']}\n"
            f"主动发言：{'全局允许（仍需频道开启）' if config['proactive_global_enabled'] else '关闭'}\n"
            f"原始消息保留：{'不保存' if not config['save_raw_messages'] else str(config['raw_history_days']) + ' 天'}",
            ephemeral=True,
        )

    @app_commands.command(name="记住", description="明确告诉机器人一件要长期记住的事")
    @app_commands.describe(content="例如：我习惯晚上写代码")
    @app_commands.rename(content="内容")
    @app_commands.guild_only()
    async def remember(self, interaction: discord.Interaction, content: str) -> None:
        guild_id = _guild_id(interaction)
        memory_id = await self.state.memories.add(
            guild_id, str(interaction.user.id), content, kind="explicit"
        )
        config = await self.state.runtime.all()
        if config["relationship_enabled"]:
            await self.state.relationships.observe(
                guild_id,
                str(interaction.user.id),
                content,
                learning_rate=float(config["relationship_learning_rate"]),
                decay_days=int(config["relationship_decay_days"]),
                explicit_memory=True,
            )
        await interaction.response.send_message(
            f"记住了，编号是 `{memory_id}`。你随时可以用 `/我的记忆` 查看，或用 `/忘记` 删除。",
            ephemeral=True,
        )

    @app_commands.command(
        name="我的记忆", description="查看机器人在当前服务器或私信中记住的你的信息"
    )
    async def my_memories(self, interaction: discord.Interaction) -> None:
        guild_id = _privacy_scope_id(interaction)
        scope_label = "私信范围" if interaction.guild_id is None else "这个服务器"
        rows = await self.state.memories.list_for_user(guild_id, str(interaction.user.id), limit=25)
        if not rows:
            text = f"我还没有在{scope_label}里保存你的长期记忆。"
        else:
            labels = {
                "explicit": "你要求记住",
                "fact": "事实",
                "preference": "偏好",
                "summary": "摘要",
            }
            lines = [
                f"`#{row['id']}` · {labels.get(row['kind'], row['kind'])} · {row['content']}"
                for row in rows
            ]
            text = f"我在{scope_label}里记住了：\n" + "\n".join(lines)
            if len(text) > 1900:
                text = text[:1880] + "\n…其余请在管理台查看。"
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="忘记", description="按编号或关键词删除一条或多条自己的记忆")
    @app_commands.describe(query="记忆编号，或记忆内容中的关键词")
    @app_commands.rename(query="编号或关键词")
    async def forget(self, interaction: discord.Interaction, query: str) -> None:
        guild_id = _privacy_scope_id(interaction)
        count = await self.state.memories.forget(guild_id, str(interaction.user.id), query)
        message = f"已删除 {count} 条匹配记忆。" if count else "没有找到属于你的匹配记忆。"
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="忘记我", description="删除你在当前服务器或私信范围中的全部个人数据")
    async def forget_me(self, interaction: discord.Interaction) -> None:
        is_dm = interaction.guild_id is None
        view = ForgetMeView(
            self.state,
            _privacy_scope_id(interaction),
            str(interaction.user.id),
            "私信范围" if is_dm else "当前服务器",
        )
        await interaction.response.send_message(
            f"这会永久删除你在**{'私信范围' if is_dm else '当前服务器'}**中的原始消息、长期记忆和关系状态。此操作不能撤销。",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="隐私", description="查看机器人如何保存和隔离你的数据")
    async def privacy(self, interaction: discord.Interaction) -> None:
        config = await self.state.runtime.all()
        history = (
            "不保存聊天原文"
            if not config["save_raw_messages"]
            else (
                "聊天原文不自动过期"
                if int(config["raw_history_days"]) == 0
                else f"聊天原文最多保留 {config['raw_history_days']} 天"
            )
        )
        await interaction.response.send_message(
            f"- 记忆和关系按服务器隔离；私信是单独范围，不会互相串用。\n"
            f"- {history}。\n"
            f"- 私信处理目前{'开启' if config['dm_enabled'] else '关闭'}。\n"
            f"- 自动记忆{'开启' if config['memory_auto_extract'] else '关闭'}，只识别明确的第一人称自述。\n"
            "- `/我的记忆` 可查看，`/忘记` 可单独删除，`/忘记我` 可删除当前服务器或私信范围内全部个人数据。",
            ephemeral=True,
        )

    @app_commands.command(name="关系", description="查看机器人与你在本服务器中的关系概况")
    @app_commands.guild_only()
    async def relationship(self, interaction: discord.Interaction) -> None:
        guild_id = _guild_id(interaction)
        config = await self.state.runtime.all()
        relationship = await self.state.relationships.get(
            guild_id,
            str(interaction.user.id),
            int(config["relationship_decay_days"]),
        )
        await interaction.response.send_message(
            f"目前是：**{relationship.description}**\n"
            f"互动次数：{relationship.interaction_count}\n"
            "这些状态只用于微调语气，不会给你贴永久标签。",
            ephemeral=True,
        )

    @app_commands.command(name="喜好", description="查看机器人当前形成的主题偏好")
    @app_commands.guild_only()
    async def preferences(self, interaction: discord.Interaction) -> None:
        rows = await self.state.preferences.list(8)
        lines = [
            f"- {row['topic']} · {float(row['weight']):+.2f}{' · 已锁定' if row['locked'] else ''}"
            for row in rows
        ]
        await interaction.response.send_message(
            "我目前的主题倾向：\n" + ("\n".join(lines) if lines else "还没有形成明显偏好。"),
            ephemeral=True,
        )


class AdminCommands(commands.Cog):
    def __init__(self, bot: MoboBot):
        self.bot = bot
        self.state = bot.state

    @app_commands.command(name="管理台", description="获取私密管理控制台地址")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def console(self, interaction: discord.Interaction) -> None:
        config = await self.state.runtime.all()
        url = str(config["admin_public_url"] or self.state.bootstrap.public_base_url).rstrip("/")
        if not url:
            message = "尚未配置公网地址。请在 Zeabur 设置 PUBLIC_BASE_URL，或登录管理台填写“管理台公网地址”。"
        else:
            message = f"管理台：<{url}>\n地址仅对你可见，仍需输入管理员密码。"
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="清空频道", description="清空当前频道的对话上下文和摘要")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def clear_channel(self, interaction: discord.Interaction) -> None:
        guild_id = _guild_id(interaction)
        channel_id = _channel_id(interaction)
        count = await self.state.memories.clear_channel(guild_id, channel_id)
        await self.state.database.audit(
            f"discord:{interaction.user.id}",
            "channel.history_cleared",
            target=f"{guild_id}:{channel_id}",
            details={"deleted_messages": count},
        )
        await interaction.response.send_message(
            f"已清空当前频道的 {count} 条上下文记录和对应摘要。", ephemeral=True
        )

    @app_commands.command(name="人设", description="设置或清除本服务器的人设覆盖")
    @app_commands.describe(prompt="留空或填写“默认”可恢复全局人设")
    @app_commands.rename(prompt="提示词")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def persona(self, interaction: discord.Interaction, prompt: str) -> None:
        guild_id = _guild_id(interaction)
        if interaction.guild is None:
            raise app_commands.NoPrivateMessage()
        value = None if prompt.strip() in {"", "默认"} else prompt.strip()[:4000]
        await self.state.database.execute(
            """INSERT INTO guilds(guild_id, name, system_prompt, first_seen_at, updated_at)
               VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET name = excluded.name,
                 system_prompt = excluded.system_prompt, updated_at = excluded.updated_at""",
            (guild_id, interaction.guild.name, value, iso_now(), iso_now()),
        )
        await interaction.response.send_message(
            "已恢复使用全局人设。" if value is None else "已更新本服务器的人设覆盖。",
            ephemeral=True,
        )

    @app_commands.command(name="模型", description="切换模型提供方与模型 ID")
    @app_commands.describe(provider="模型提供方", model="准确的模型 ID")
    @app_commands.rename(provider="提供方", model="模型")
    @app_commands.choices(
        provider=[
            app_commands.Choice(name="OpenAI", value="openai"),
            app_commands.Choice(name="Anthropic", value="anthropic"),
            app_commands.Choice(name="OpenRouter", value="openrouter"),
            app_commands.Choice(name="Ollama / 兼容接口", value="ollama"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def model(
        self,
        interaction: discord.Interaction,
        provider: app_commands.Choice[str],
        model: str,
    ) -> None:
        values = await self.state.runtime.update(
            {"llm_provider": provider.value, "llm_model": model.strip()},
            actor=f"discord:{interaction.user.id}",
        )
        try:
            build_backend(values)
            notice = "配置可用。"
        except LLMConfigurationError as exc:
            notice = f"模型已切换，但还不能调用：{exc}"
        await interaction.response.send_message(
            f"已切换为 `{provider.value} / {model.strip()}`。{notice}", ephemeral=True
        )

    @app_commands.command(name="频道设置", description="设置频道监听与主动发言权限")
    @app_commands.describe(
        channel="要设置的文字频道", listen="允许读取并参与", proactive="允许主动插话"
    )
    @app_commands.rename(channel="频道", listen="监听", proactive="主动发言")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def channel_settings(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        listen: bool,
        proactive: bool,
    ) -> None:
        guild_id = _guild_id(interaction)
        if proactive and not listen:
            await interaction.response.send_message(
                "主动发言依赖频道监听，请先同时开启“监听”。", ephemeral=True
            )
            return
        await self.state.channels.set(
            guild_id,
            str(channel.id),
            channel.name,
            listen_enabled=listen,
            proactive_enabled=proactive,
        )
        await interaction.response.send_message(
            f"已更新 {channel.mention}：监听 {'开' if listen else '关'}，主动发言 {'开' if proactive else '关'}。",
            ephemeral=True,
        )

    @app_commands.command(name="主动发言", description="打开或关闭全局主动发言总开关")
    @app_commands.describe(enabled="是否允许已授权频道主动发言")
    @app_commands.rename(enabled="启用")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def proactive(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self.state.runtime.update(
            {"proactive_global_enabled": enabled},
            actor=f"discord:{interaction.user.id}",
        )
        await interaction.response.send_message(
            f"主动发言总开关已{'开启' if enabled else '关闭'}。具体频道仍由 `/频道设置` 控制。",
            ephemeral=True,
        )

    @app_commands.command(name="重载配置", description="立即清除配置缓存并刷新机器人状态")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def reload(self, interaction: discord.Interaction) -> None:
        self.state.runtime.invalidate()
        await self.bot.refresh_presence()
        await interaction.response.send_message("配置已重新读取。", ephemeral=True)


class MoboBot(commands.Bot):
    def __init__(self, state: ApplicationState):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_messages = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=ChineseCommandTree,
            allowed_mentions=discord.AllowedMentions.none(),
            help_command=None,
        )
        self.state = state
        self.rate_limiter = RateLimiter()
        self._summary_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        await self.add_cog(PublicCommands(self))
        await self.add_cog(AdminCommands(self))
        synced = await self.tree.sync()
        self.state.bot_status.commands_synced_at = iso_now()
        log.info("synced %s global Chinese commands", len(synced))
        self.maintenance.start()

    async def on_ready(self) -> None:
        self.state.bot_status.connected = True
        self.state.bot_status.ready = True
        self.state.bot_status.user_tag = str(self.user)
        self.state.bot_status.guild_count = len(self.guilds)
        self.state.bot_status.latency_ms = round(self.latency * 1000)
        self.state.bot_status.last_error = None
        await self.refresh_presence()
        for guild in self.guilds:
            await self._upsert_guild(guild)
        log.info("mobo online as %s in %s guilds", self.user, len(self.guilds))

    async def on_disconnect(self) -> None:
        self.state.bot_status.connected = False
        self.state.bot_status.ready = False

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._upsert_guild(guild)
        self.state.bot_status.guild_count = len(self.guilds)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.state.bot_status.guild_count = len(self.guilds)

    async def _upsert_guild(self, guild: discord.Guild) -> None:
        await self.state.database.execute(
            """INSERT INTO guilds(guild_id, name, system_prompt, first_seen_at, updated_at)
               VALUES(?, ?, NULL, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET name = excluded.name,
                 updated_at = excluded.updated_at""",
            (str(guild.id), guild.name, iso_now(), iso_now()),
        )

    async def refresh_presence(self) -> None:
        if not self.is_ready():
            return
        config = await self.state.runtime.all(fresh=True)
        await self.change_presence(activity=discord.CustomActivity(name=str(config["status_text"])))

    @staticmethod
    def _reply_to_bot(message: discord.Message, bot_user_id: int) -> bool:
        resolved = message.reference and message.reference.resolved
        return isinstance(resolved, discord.Message) and resolved.author.id == bot_user_id

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id or not self.user:
            return
        config = await self.state.runtime.all()
        is_dm = message.guild is None
        if is_dm and not config["dm_enabled"]:
            return
        guild_id = str(message.guild.id) if message.guild else f"dm:{message.author.id}"
        channel_id = str(message.channel.id)
        mentioned = self.user in message.mentions
        replied = self._reply_to_bot(message, self.user.id)
        direct = (mentioned and config["reply_to_mentions"]) or (
            replied and config["reply_to_replies"]
        )
        proactive_reason: str | None = None
        if not direct:
            if is_dm:
                direct = True
            else:
                decision = await self.state.proactive.decide(
                    guild_id,
                    channel_id,
                    str(message.author.id),
                    message.content,
                    config,
                )
                if not decision.should_speak:
                    return
                proactive_reason = decision.reason

        allowed, wait_seconds = self.rate_limiter.allow(
            f"{guild_id}:{message.author.id}",
            int(config["rate_limit_requests"]),
            int(config["rate_limit_window_seconds"]),
        )
        if not allowed:
            if direct:
                await message.reply(f"请求有点密集，请在 {wait_seconds} 秒后再找我。")
            return

        text = re.sub(rf"<@!?{self.user.id}>", "", message.content).strip()
        if not text and not message.attachments:
            await message.reply("我在。你想聊什么？")
            return
        content = image_content(
            text,
            list(message.attachments),
            enabled=bool(config["image_input_enabled"]),
        )
        try:
            context = await self.state.context.build(
                guild_id, channel_id, str(message.author.id), content
            )
            if config["save_raw_messages"]:
                message_id = await self.state.memories.save_message(
                    guild_id,
                    channel_id,
                    "user",
                    text or "[图片]",
                    retention_days=int(config["raw_history_days"]),
                    user_id=str(message.author.id),
                    username=str(message.author),
                )
            else:
                message_id = None
            backend = build_backend(config)
            reply = await self._stream_reply(message, backend, context)
            if config["save_raw_messages"]:
                await self.state.memories.save_message(
                    guild_id,
                    channel_id,
                    "assistant",
                    reply,
                    retention_days=int(config["raw_history_days"]),
                )
            if config["memory_auto_extract"]:
                await self.state.memories.auto_extract(
                    guild_id,
                    str(message.author.id),
                    text,
                    confidence_threshold=float(config["memory_confidence_threshold"]),
                    expires_days=int(config["memory_decay_days"]),
                    max_per_user=int(config["memory_max_per_user"]),
                    source_message_id=message_id,
                )
            if config["relationship_enabled"]:
                await self.state.relationships.observe(
                    guild_id,
                    str(message.author.id),
                    text,
                    learning_rate=float(config["relationship_learning_rate"]),
                    decay_days=int(config["relationship_decay_days"]),
                )
            if config["mood_enabled"]:
                await self.state.mood.observe(text, config)
            await self.state.preferences.interest_for(text)
            if proactive_reason:
                await self.state.proactive.record(guild_id, channel_id, proactive_reason)
            if config["save_raw_messages"]:
                asyncio.create_task(
                    self._maybe_summarize(guild_id, channel_id, config),
                    name=f"summary:{guild_id}:{channel_id}",
                )
        except LLMConfigurationError as exc:
            await message.reply(f"模型还没配置好：{exc}")
        except TimeoutError:
            await message.reply("模型响应超时了。管理员可以在管理台调大超时时间，或换一个模型。")
        except Exception as exc:
            self.state.bot_status.last_error = type(exc).__name__
            log.exception("message handling failed")
            await message.reply("这次回复失败了。错误详情只会写入服务日志，不会在频道公开。")

    async def _stream_reply(
        self,
        source: discord.Message,
        backend: Any,
        context: list[dict[str, Any]],
    ) -> str:
        sent = await source.reply("▌")
        full = ""
        current = ""
        loop = asyncio.get_running_loop()
        last_edit = loop.time()
        try:
            async with source.channel.typing():
                async for chunk in backend.stream(context):
                    full += chunk
                    current += chunk
                    while len(current) > MAX_DISCORD_MESSAGE:
                        split = current.rfind("\n", 0, MAX_DISCORD_MESSAGE)
                        split = split if split > 0 else MAX_DISCORD_MESSAGE
                        await sent.edit(content=current[:split])
                        current = current[split:].lstrip("\n")
                        sent = await source.channel.send("▌")
                        last_edit = loop.time()
                    if loop.time() - last_edit >= 1.0:
                        await sent.edit(content=(current or " ") + "▌")
                        last_edit = loop.time()
        except Exception:
            if current:
                suffix = "\n\n[回复在生成过程中中断]"
                await sent.edit(content=(current[: MAX_DISCORD_MESSAGE - len(suffix)] + suffix))
            else:
                await sent.delete()
            raise
        if current:
            await sent.edit(content=current)
        elif full:
            await sent.delete()
        else:
            await sent.edit(content="模型没有返回文字。")
        return full

    async def _maybe_summarize(
        self, guild_id: str, channel_id: str, config: dict[str, Any]
    ) -> None:
        key = (guild_id, channel_id)
        lock = self._summary_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            batch = await self.state.memories.summary_batch(
                guild_id,
                channel_id,
                trigger=int(config["summary_trigger"]),
                keep_recent=int(config["max_history_messages"]),
            )
            if not batch:
                return
            rows, through_id = batch
            existing = await self.state.memories.channel_summary(guild_id, channel_id)
            transcript = "\n".join(f"{row['role']}: {row['content']}" for row in rows)
            prompt = [
                {
                    "role": "system",
                    "content": "把频道对话压缩为简洁中文摘要。保留未解决问题、共识和后续需要的上下文；不要杜撰。",
                },
                {
                    "role": "user",
                    "content": (
                        (f"已有摘要：\n{existing['summary']}\n\n" if existing else "")
                        + "新增对话：\n"
                        + transcript
                    ),
                },
            ]
            try:
                summary = await build_backend(config).complete(prompt)
                if summary.strip():
                    await self.state.memories.store_channel_summary(
                        guild_id, channel_id, through_id, summary.strip()
                    )
            except Exception:
                log.exception("channel summary failed")

    @tasks.loop(hours=6)
    async def maintenance(self) -> None:
        deleted = await self.state.database.cleanup_expired()
        log.info("maintenance cleanup: %s", deleted)

    @maintenance.before_loop
    async def before_maintenance(self) -> None:
        await self.wait_until_ready()

    async def close(self) -> None:
        if self.maintenance.is_running():
            self.maintenance.cancel()
        self.state.bot_status.connected = False
        self.state.bot_status.ready = False
        await super().close()


def create_bot(state: ApplicationState) -> MoboBot:
    bot = MoboBot(state)
    state.discord_bot = bot
    return bot
