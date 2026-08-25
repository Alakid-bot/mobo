from __future__ import annotations

from app.discord_bot import AdminCommands, MoboBot, PublicCommands, image_content

PUBLIC_NAMES = {"帮助", "状态", "记住", "我的记忆", "忘记我", "隐私", "关系", "喜好"}
ADMIN_NAMES = {"管理台", "清空频道", "人设", "模型", "频道设置", "主动发言", "重载配置"}


def test_chinese_command_names_and_admin_visibility_contract(state):
    bot = MoboBot(state)
    public = {command.name: command for command in PublicCommands(bot).get_app_commands()}
    admin = {command.name: command for command in AdminCommands(bot).get_app_commands()}
    assert set(public) == PUBLIC_NAMES
    assert set(admin) == ADMIN_NAMES
    assert all(command.default_permissions is None for command in public.values())
    assert all(
        command.default_permissions is not None and command.default_permissions.administrator
        for command in admin.values()
    )
    assert all(command.checks for command in admin.values())


def test_image_content_ignores_non_images_and_caps_images():
    class Attachment:
        def __init__(self, content_type: str, url: str):
            self.content_type = content_type
            self.url = url

    assert image_content("hello", [Attachment("application/pdf", "file")], enabled=True) == "hello"
    result = image_content(
        "look",
        [Attachment("image/png", str(index)) for index in range(10)],
        enabled=True,
    )
    assert isinstance(result, list)
    assert len(result) == 5  # one text part plus at most four images


def test_discord_message_content_intent_is_enabled(state):
    bot = MoboBot(state)
    assert bot.intents.message_content
    assert bot.allowed_mentions.everyone is False
    assert bot.allowed_mentions.users is False
    assert bot.allowed_mentions.roles is False
    assert bot.allowed_mentions.replied_user is False
