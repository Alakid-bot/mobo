from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from app.config import settings
from app.discord_bot import create_bot
from app.instance_lock import InstanceLock
from app.state import create_state
from app.web import create_web_app


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


async def run() -> None:
    configure_logging()
    log = logging.getLogger("mobo")
    instance_lock = InstanceLock(Path(settings.db_path).with_name("mobo.instance.lock"))
    instance_lock.acquire()
    state = await create_state(settings)
    web_app = create_web_app(state)
    bot = create_bot(state)
    server = uvicorn.Server(
        uvicorn.Config(
            web_app,
            host=settings.web_host,
            port=settings.web_port,
            log_level=settings.log_level.lower(),
            access_log=False,
            # No trusted proxy ranges are configured, so forwarded client and
            # scheme headers must not influence the ASGI scope.
            proxy_headers=False,
            forwarded_allow_ips="",
        )
    )
    # The parent coroutine owns signal handling so bot and web stop together.
    server.install_signal_handlers = lambda: None
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        stop_event.set()

    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, request_stop)
        except NotImplementedError:
            signal.signal(signame, lambda *_: loop.call_soon_threadsafe(request_stop))

    async def run_bot() -> None:
        try:
            async with bot:
                await bot.start(settings.discord_token.get_secret_value())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep the private console alive so a Discord token/configuration problem is diagnosable.
            state.bot_status.connected = False
            state.bot_status.ready = False
            state.bot_status.last_error = type(exc).__name__
            log.exception("Discord 连接失败；管理台将保持可用，修正配置后请重新部署")
            await stop_event.wait()
        else:
            state.bot_status.connected = False
            state.bot_status.ready = False
            state.bot_status.last_error = "DiscordStopped"
            await stop_event.wait()

    web_task = asyncio.create_task(server.serve(), name="mobo-web")
    bot_task = asyncio.create_task(run_bot(), name="mobo-discord")
    stop_task = asyncio.create_task(stop_event.wait(), name="mobo-stop")
    log.info("mobo 管理台正在 %s:%s 启动", settings.web_host, settings.web_port)

    done, _ = await asyncio.wait(
        {web_task, bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    failure: BaseException | None = None
    for task in done:
        if task is stop_task:
            continue
        if not task.cancelled():
            try:
                task.result()
            except BaseException as exc:
                failure = exc
    server.should_exit = True
    if not bot.is_closed():
        await bot.close()
    for task in (web_task, bot_task, stop_task):
        if not task.done():
            task.cancel()
    await asyncio.gather(web_task, bot_task, stop_task, return_exceptions=True)
    await state.llm.close()
    await state.database.close()
    instance_lock.release()
    if failure:
        raise failure
    log.info("mobo 已安全停止")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
