"""Small, dependency-free helpers for conversational Discord flows.

The module intentionally does not know anything about discord.py objects.  IDs
are normalised to strings at the boundary and payloads are passed through to a
caller supplied async handler.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, TypeVar, cast

SummaryMode = Literal["brief", "detailed", "timeline", "actions"]
ConversationKey = tuple[str, str, str]
Handler = Callable[..., Any]
_MISSING = object()
_T = TypeVar("_T")

# A model concurrency permit is deliberately not a queue permit.  Keep only a
# small multiple of the configured model concurrency resident as coordinator
# tasks; callers must retry later instead of accumulating payloads in memory.
_PENDING_GENERATIONS_PER_CONCURRENCY = 4
_MIN_PENDING_GENERATIONS_PER_USER = 2
_MAX_PENDING_GENERATIONS_PER_USER = 8


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    """A deliberately narrow, already-recognised summary command.

    ``count`` is left untouched by range validation.  The application layer
    owns its product-specific limits (for example, the configured maximum
    number of messages).
    """

    count: int | None = None
    from_reply: bool = False
    mode: SummaryMode = "brief"


_SUMMARY_PREFIX = r"(?:请(?:帮我)?|帮我|麻烦(?:你)?|能否|可以|给我|来)?"
_SUMMARY_SUFFIX = r"(?:一下|下|吧|呢)?"
_SUMMARY_MODE_WORDS: tuple[tuple[SummaryMode, tuple[str, ...]], ...] = (
    ("timeline", ("时间线", "时间轴", "按时间", "按时间线", "timeline")),
    ("actions", ("行动项", "行动事项", "待办", "任务", "actions")),
    ("detailed", ("详细", "完整", "深入", "细致", "detailed")),
    ("brief", ("简要", "简短", "brief")),
)


def _normalise_command(text: str) -> str:
    normalised = unicodedata.normalize("NFKC", text).strip()
    # Mentions are useful when this helper is called directly from a Discord
    # message.  Only remove a leading mention; arbitrary text remains strict.
    normalised = re.sub(r"^(?:<@!?\d+>|@\S+)\s*", "", normalised)
    return re.sub(r"\s+", " ", normalised)


def _summary_mode(text: str) -> SummaryMode:
    for mode, words in _SUMMARY_MODE_WORDS:
        if any(word in text.lower() for word in words):
            return mode
    return "brief"


def parse_summary_request(text: str) -> SummaryRequest | None:
    """Parse explicit Chinese summary requests, returning ``None`` otherwise.

    The scope phrase is intentionally required.  Thus ordinary prose such as
    ``"我们之后总结一下今天的讨论"`` does not accidentally become a command.
    Arabic and full-width digits are accepted without imposing a range limit.
    """

    if not isinstance(text, str):
        return None
    command = _normalise_command(text)
    if not command:
        return None

    # Modes are accepted before or after the command verb, while the rest of
    # the expression remains anchored to avoid matching prose containing
    # “总结”.
    mode_words = (
        "(?:"
        + "|".join(re.escape(word) for _, words in _SUMMARY_MODE_WORDS for word in words)
        + ")?"
    )
    prefix = rf"^{_SUMMARY_PREFIX}\s*"
    suffix = rf"\s*{_SUMMARY_SUFFIX}$"

    from_here_patterns = (
        rf"{prefix}{mode_words}\s*从这里\s*总结\s*{mode_words}\s*到现在{suffix}",
        rf"{prefix}{mode_words}\s*总结\s*{mode_words}\s*从这里\s*到现在{suffix}",
    )
    if any(re.fullmatch(pattern, command, flags=re.IGNORECASE) for pattern in from_here_patterns):
        return SummaryRequest(from_reply=True, mode=_summary_mode(command))

    count_pattern = rf"{prefix}{mode_words}\s*总结\s*{mode_words}\s*最近\s*(-?[0-9]+)\s*(?:条消息|楼|条|消息)(?:对话)?{suffix}"
    count_match = re.fullmatch(count_pattern, command, flags=re.IGNORECASE)
    if count_match:
        return SummaryRequest(count=int(count_match.group(1)), mode=_summary_mode(command))

    above_patterns = (
        rf"{prefix}{mode_words}\s*总结\s*{mode_words}\s*(?:一下\s*)?(?:上面|以上|前面)(?:的?(?:内容|对话|消息))?{mode_words}{suffix}",
        rf"{prefix}{mode_words}\s*总结\s*{mode_words}\s*(?:一下\s*)?(?:上文)(?:内容|对话|消息)?{mode_words}{suffix}",
    )
    if any(re.fullmatch(pattern, command, flags=re.IGNORECASE) for pattern in above_patterns):
        return SummaryRequest(mode=_summary_mode(command))
    return None


def estimate_tokens(text: Any) -> int:
    """Return a conservative, fast token estimate without a tokenizer.

    CJK and symbol characters are counted individually.  ASCII word runs are
    rounded up at four characters per token and punctuation is counted on its
    own.  This intentionally errs high enough for deciding when to split an
    input, rather than pretending to reproduce a model's exact tokenizer.
    """

    if text is None:
        return 0
    value = text if isinstance(text, str) else str(text)
    if not value:
        return 0

    total = 0
    ascii_run = 0

    def flush_ascii() -> None:
        nonlocal ascii_run, total
        if ascii_run:
            total += math.ceil(ascii_run / 4)
            ascii_run = 0

    for char in value:
        codepoint = ord(char)
        if ("A" <= char <= "Z") or ("a" <= char <= "z") or ("0" <= char <= "9"):
            ascii_run += 1
            continue
        if codepoint < 128 and char == "_":
            ascii_run += 1
            continue
        flush_ascii()
        if char.isspace():
            continue
        total += 1
    flush_ascii()
    return max(total, 1)


def _message_content(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, Mapping) and "content" in message:
        return str(message["content"] or "")
    content = getattr(message, "content", _MISSING)
    if content is not _MISSING:
        return str(content or "")
    return str(message)


def _message_with_content(message: Any, content: str) -> Any:
    if isinstance(message, str):
        return content
    if isinstance(message, Mapping) and "content" in message:
        copied = dict(message)
        copied["content"] = content
        return copied
    if hasattr(message, "content"):
        try:
            copied = copy.copy(message)
            copied.content = content
            return copied
        except (AttributeError, TypeError):
            return content
    return content


def _split_message(message: Any, budget: int) -> list[Any]:
    content = _message_content(message)
    if estimate_tokens(content) <= budget or not content:
        return [message]

    parts: list[Any] = []
    start = 0
    while start < len(content):
        low, high = start + 1, len(content)
        best = start + 1
        while low <= high:
            middle = (low + high) // 2
            if estimate_tokens(content[start:middle]) <= budget:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        parts.append(_message_with_content(message, content[start:best]))
        start = best
    return parts


class TranscriptSplit(NamedTuple):
    """Transcript chunks and whether the configured chunk cap truncated it."""

    chunks: list[list[Any]]
    truncated: bool

    @property
    def was_truncated(self) -> bool:
        return self.truncated


def split_transcript_by_token_budget(
    messages: Iterable[Any], budget: int, max_chunks: int
) -> TranscriptSplit:
    """Pack transcript messages in order, splitting oversized messages safely.

    A message is only split when it cannot fit in one budget.  Mapping-shaped
    messages retain their metadata and receive a new ``content`` field for
    each segment.  Once ``max_chunks`` is reached, remaining input is omitted
    and ``truncated`` is set to ``True`` so callers can report that fact.
    """

    if budget <= 0:
        raise ValueError("budget must be greater than zero")
    if max_chunks <= 0:
        raise ValueError("max_chunks must be greater than zero")

    chunks: list[list[Any]] = []
    current: list[Any] = []
    current_tokens = 0
    truncated = False

    def flush() -> bool:
        nonlocal current, current_tokens
        if not current:
            return True
        if len(chunks) >= max_chunks:
            return False
        chunks.append(current)
        current = []
        current_tokens = 0
        return True

    for message in messages:
        for segment in _split_message(message, budget):
            segment_tokens = estimate_tokens(_message_content(segment))
            if current and current_tokens + segment_tokens > budget and not flush():
                truncated = True
                break
            if not current and len(chunks) >= max_chunks:
                truncated = True
                break
            current.append(segment)
            current_tokens += segment_tokens
        if truncated:
            break

    if current and len(chunks) < max_chunks:
        chunks.append(current)
    elif current:
        truncated = True
    return TranscriptSplit(chunks, truncated)


def _coerce_key(
    guild_id: str | ConversationKey, channel_id: str | None = None, user_id: str | None = None
) -> ConversationKey:
    if channel_id is None and user_id is None and isinstance(guild_id, (tuple, list)):
        if len(guild_id) != 3:
            raise ValueError("conversation key must contain guild, channel and user IDs")
        return tuple(str(value) for value in guild_id)  # type: ignore[return-value]
    if channel_id is None or user_id is None:
        raise TypeError("guild_id, channel_id and user_id are required")
    return str(guild_id), str(channel_id), str(user_id)


class BurstBuffer:
    """A bounded, expiring, process-local buffer for short message bursts.

    The buffer deliberately has no persistence API.  Callers take a snapshot
    for each generation and only clear the key once that generation is still
    current, so cancellation by a newer message cannot discard the earlier
    turns that the replacement needs to absorb.
    """

    def __init__(
        self,
        ttl_seconds: float = 30.0,
        max_keys: int = 1024,
        max_items_per_key: int = 8,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        if max_keys <= 0 or max_items_per_key <= 0:
            raise ValueError("burst buffer bounds must be greater than zero")
        self.ttl_seconds = float(ttl_seconds)
        self.max_keys = int(max_keys)
        self.max_items_per_key = int(max_items_per_key)
        self._clock = clock or time.monotonic
        self._entries: OrderedDict[ConversationKey, tuple[float, list[Any]]] = OrderedDict()

    def _purge(self, now: float) -> None:
        expired = [
            key
            for key, (updated_at, _items) in self._entries.items()
            if now - updated_at >= self.ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)

    def append(self, key: ConversationKey, value: Any) -> tuple[Any, ...]:
        now = self._clock()
        self._purge(now)
        previous = self._entries.pop(key, None)
        items = list(previous[1]) if previous is not None else []
        items.append(value)
        if len(items) > self.max_items_per_key:
            items = items[-self.max_items_per_key :]
        self._entries[key] = (now, items)
        while len(self._entries) > self.max_keys:
            self._entries.popitem(last=False)
        return tuple(items)

    def snapshot(self, key: ConversationKey) -> tuple[Any, ...]:
        now = self._clock()
        self._purge(now)
        entry = self._entries.get(key)
        if entry is None:
            return ()
        self._entries.move_to_end(key)
        return tuple(entry[1])

    def forget(self, key: ConversationKey) -> None:
        self._entries.pop(key, None)

    def forget_user(self, user_id: str) -> None:
        for key in [key for key in self._entries if key[2] == str(user_id)]:
            self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        self._purge(self._clock())
        return len(self._entries)


class ConversationCapacityError(RuntimeError):
    """Raised when a new conversation key cannot enter the bounded queue."""


class ConversationCoordinator:
    """Debounce and globally limit per-user conversation jobs.

    ``handler`` normally accepts one argument, the submitted payload.  A
    handler accepting ``(key, payload)`` is also supported for convenient
    Discord integration.  Submitting a new payload for the same key cancels
    the previous awaitable; callers awaiting that old submission receive
    ``asyncio.CancelledError`` and can safely ignore it.
    """

    def __init__(
        self,
        handler: Handler | None = None,
        *,
        debounce_seconds: float = 0.25,
        debounce: float | None = None,
        max_concurrency: int = 4,
        max_concurrent: int | None = None,
        max_pending: int | None = None,
        max_pending_per_user: int | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        if debounce is not None:
            debounce_seconds = debounce
        if max_concurrent is not None:
            max_concurrency = max_concurrent
        if debounce_seconds < 0:
            raise ValueError("debounce_seconds must be non-negative")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be greater than zero")
        if max_pending is None:
            max_pending = max_concurrency * _PENDING_GENERATIONS_PER_CONCURRENCY
        if max_pending_per_user is None:
            max_pending_per_user = min(
                max_pending,
                max(
                    _MIN_PENDING_GENERATIONS_PER_USER,
                    min(_MAX_PENDING_GENERATIONS_PER_USER, max_concurrency * 2),
                ),
            )
        if max_pending <= 0 or max_pending_per_user <= 0:
            raise ValueError("pending generation bounds must be greater than zero")
        if max_pending_per_user > max_pending:
            raise ValueError("per-user pending bound cannot exceed the global bound")
        self.handler = handler
        self.debounce_seconds = float(debounce_seconds)
        self.semaphore = semaphore or asyncio.Semaphore(max_concurrency)
        self.max_pending = int(max_pending)
        self.max_pending_per_user = int(max_pending_per_user)
        self._tasks: dict[ConversationKey, asyncio.Task[Any]] = {}
        self._versions: dict[ConversationKey, int] = {}
        self._closed = False

    @property
    def pending(self) -> int:
        self._discard_completed()
        return sum(not task.done() for task in self._tasks.values())

    def _task_done(self, key: ConversationKey, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
            self._versions.pop(key, None)

    def _discard_completed(self) -> None:
        for key, task in list(self._tasks.items()):
            if task.done():
                self._task_done(key, task)

    def _has_capacity_for(self, key: ConversationKey) -> bool:
        self._discard_completed()
        # Replacing one key transfers its existing slot to the newer payload.
        if key in self._tasks:
            return True
        if len(self._tasks) >= self.max_pending:
            return False
        return (
            sum(existing_key[2] == key[2] for existing_key in self._tasks)
            < self.max_pending_per_user
        )

    @staticmethod
    async def _invoke(handler: Handler, key: ConversationKey, payload: Any) -> Any:
        try:
            signature = inspect.signature(handler)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            accepts_varargs = any(
                parameter.kind == inspect.Parameter.VAR_POSITIONAL
                for parameter in signature.parameters.values()
            )
            if len(positional) >= 2:
                arguments = (key, payload)
            elif positional:
                arguments = (payload,)
            elif accepts_varargs:
                # AsyncMock and many generic adapters expose only *args;
                # payload-only remains the least surprising default.
                arguments = (payload,)
            else:
                arguments = ()
        except (TypeError, ValueError):
            arguments = (payload,)
        result = handler(*arguments)
        if inspect.isawaitable(result):
            return await cast(Awaitable[_T], result)
        return result

    async def _run(
        self,
        key: ConversationKey,
        payload: Any,
        handler: Handler,
        version: int,
    ) -> Any:
        if self.debounce_seconds:
            await asyncio.sleep(self.debounce_seconds)
        if self._versions.get(key) != version or self._closed:
            raise asyncio.CancelledError
        async with self.semaphore:
            if self._versions.get(key) != version or self._closed:
                raise asyncio.CancelledError
            result = await self._invoke(handler, key, payload)
            # A badly behaved handler may suppress cancellation.  Re-checking
            # the generation prevents its stale result from escaping.
            if self._versions.get(key) != version or self._closed:
                raise asyncio.CancelledError
            return result

    async def submit(
        self,
        guild_id: str | ConversationKey | None = None,
        channel_id: Any = None,
        user_id: str | None = None,
        payload: Any = _MISSING,
        *,
        key: ConversationKey | None = None,
        handler: Handler | None = None,
        callback: Handler | None = None,
    ) -> Any:
        """Submit one job, accepting either a tuple key or three IDs.

        Examples::

            await coordinator.submit((guild, channel, user), messages)
            await coordinator.submit(guild, channel, user, messages)
            await coordinator.submit(key=(guild, channel, user), payload=messages)
        """

        if self._closed:
            raise RuntimeError("conversation coordinator is closed")
        if key is not None:
            conversation_key = _coerce_key(key)
            if guild_id is not None:
                raise TypeError("use either key or guild_id, not both")
            if payload is _MISSING:
                payload = channel_id
        elif isinstance(guild_id, (tuple, list)) and user_id is None:
            conversation_key = _coerce_key(guild_id)
            if payload is _MISSING:
                if (
                    callable(channel_id)
                    and self.handler is None
                    and handler is None
                    and callback is None
                ):
                    callback = channel_id
                    payload = None
                else:
                    payload = channel_id
        else:
            if guild_id is None:
                raise TypeError("a conversation key is required")
            conversation_key = _coerce_key(guild_id, channel_id, user_id)
            if payload is _MISSING:
                payload = None

        if (
            payload is not _MISSING
            and callable(payload)
            and self.handler is None
            and handler is None
            and callback is None
        ):
            callback = payload
            payload = None
        chosen_handler = (
            callback if callback is not None else handler if handler is not None else self.handler
        )
        if chosen_handler is None:
            raise TypeError("a handler must be supplied to ConversationCoordinator")

        if not self._has_capacity_for(conversation_key):
            raise ConversationCapacityError("conversation generation queue is full")
        previous = self._tasks.get(conversation_key)
        if previous is not None and not previous.done():
            previous.cancel()
        version = self._versions.get(conversation_key, 0) + 1
        self._versions[conversation_key] = version
        task = asyncio.create_task(
            self._run(conversation_key, payload, chosen_handler, version),
            name=f"conversation:{':'.join(conversation_key)}",
        )
        self._tasks[conversation_key] = task
        task.add_done_callback(
            lambda completed, current_key=conversation_key: self._task_done(current_key, completed)
        )
        try:
            return await task
        finally:
            self._task_done(conversation_key, task)

    async def cancel(
        self,
        guild_id: str | ConversationKey,
        channel_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        key = _coerce_key(guild_id, channel_id, user_id)
        task = self._tasks.get(key)
        if task is None:
            return False
        if task.done():
            self._task_done(key, task)
            return False
        self._versions[key] = self._versions.get(key, 0) + 1
        task.cancel()
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._versions.clear()

    async def __aenter__(self) -> ConversationCoordinator:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()
