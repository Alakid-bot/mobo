from __future__ import annotations

import asyncio

import pytest

from app.conversation import (
    BurstBuffer,
    ConversationCapacityError,
    ConversationCoordinator,
    SummaryRequest,
    estimate_tokens,
    parse_summary_request,
    split_transcript_by_token_budget,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("总结上面", SummaryRequest()),
        ("请详细总结最近 12 楼", SummaryRequest(12, mode="detailed")),
        ("总结最近３条消息", SummaryRequest(3)),
        ("总结最近0条消息", SummaryRequest(0)),
        ("总结最近-2条消息", SummaryRequest(-2)),
        ("按时间线总结最近 4 条", SummaryRequest(4, mode="timeline")),
        ("总结行动项最近 2 条消息", SummaryRequest(2, mode="actions")),
        ("从这里总结到现在", SummaryRequest(from_reply=True)),
    ],
)
def test_parse_explicit_summary_requests(text: str, expected: SummaryRequest):
    assert parse_summary_request(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "我们之后总结一下今天的讨论",
        "总结一下",
        "请总结今天发生了什么",
        "我想总结上面的原因，但还没开始",
    ],
)
def test_summary_parser_does_not_match_ordinary_prose(text: str):
    assert parse_summary_request(text) is None


def test_estimate_tokens_is_fast_and_conservative_for_mixed_text():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") >= 4
    assert estimate_tokens("hello world") >= 3
    assert estimate_tokens("hello world 你好") >= estimate_tokens("hello world")


def test_split_preserves_order_and_splits_long_mapping_messages():
    result = split_transcript_by_token_budget(
        [{"role": "user", "content": "abcdefghijk"}, {"role": "assistant", "content": "ok"}],
        budget=3,
        max_chunks=10,
    )
    assert not result.truncated
    flattened = [message for chunk in result.chunks for message in chunk]
    assert "".join(message["content"] for message in flattened) == "abcdefghijkok"
    assert all(estimate_tokens(message["content"]) <= 3 for message in flattened)
    assert all(message["role"] in {"user", "assistant"} for message in flattened)


def test_split_reports_truncation_at_chunk_cap():
    result = split_transcript_by_token_budget(["a" * 20], budget=2, max_chunks=2)
    assert result.truncated
    assert len(result.chunks) == 2
    assert "".join(part for chunk in result.chunks for part in chunk) == "a" * 16


@pytest.mark.asyncio
async def test_coordinator_replaces_stale_same_key_and_runs_different_keys_concurrently():
    started: list[str] = []
    release = asyncio.Event()

    async def handler(payload: str) -> str:
        started.append(payload)
        if payload == "first":
            await release.wait()
        return payload.upper()

    coordinator = ConversationCoordinator(handler, debounce_seconds=0, max_concurrency=2)
    old = asyncio.create_task(coordinator.submit(("g", "c", "u"), "first"))
    await asyncio.sleep(0)
    new = asyncio.create_task(coordinator.submit(("g", "c", "u"), "second"))
    assert await new == "SECOND"
    with pytest.raises(asyncio.CancelledError):
        await old
    assert started[-1] == "second"
    await coordinator.close()


@pytest.mark.asyncio
async def test_coordinator_limits_global_concurrency_and_close_cancels_pending():
    active = 0
    peak = 0
    release = asyncio.Event()

    async def handler(_: str) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return "done"

    coordinator = ConversationCoordinator(handler, debounce_seconds=0, max_concurrency=1)
    first = asyncio.create_task(coordinator.submit(("g", "c", "u1"), "one"))
    second = asyncio.create_task(coordinator.submit(("g", "c", "u2"), "two"))
    await asyncio.sleep(0.02)
    assert peak == 1
    await coordinator.close()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(asyncio.CancelledError):
        await second


@pytest.mark.asyncio
async def test_coordinator_accepts_per_submission_no_argument_callback():
    calls: list[str] = []

    async def callback() -> str:
        calls.append("called")
        return "ok"

    coordinator = ConversationCoordinator(debounce_seconds=0)
    assert await coordinator.submit(("g", "c", "u"), callback) == "ok"
    assert calls == ["called"]
    await coordinator.close()


@pytest.mark.asyncio
async def test_coordinator_bounds_distinct_keys_but_replacement_reuses_a_slot():
    started: list[str] = []
    release = asyncio.Event()

    async def handler(payload: str) -> str:
        started.append(payload)
        await release.wait()
        return payload

    coordinator = ConversationCoordinator(
        handler,
        debounce_seconds=0,
        max_concurrency=1,
        max_pending=2,
        max_pending_per_user=1,
    )
    first = asyncio.create_task(coordinator.submit(("g", "c1", "u1"), "first"))
    await asyncio.sleep(0)
    replacement = asyncio.create_task(coordinator.submit(("g", "c1", "u1"), "replacement"))
    other = asyncio.create_task(coordinator.submit(("g", "c2", "u2"), "other"))
    await asyncio.sleep(0)

    with pytest.raises(ConversationCapacityError):
        await coordinator.submit(("g", "c3", "u3"), "overflow")
    with pytest.raises(ConversationCapacityError):
        await coordinator.submit(("g", "c4", "u2"), "same-user-overflow")
    assert len(coordinator._tasks) <= coordinator.max_pending
    assert len(coordinator._versions) <= coordinator.max_pending
    with pytest.raises(asyncio.CancelledError):
        await first

    release.set()
    assert await replacement == "replacement"
    assert await other == "other"
    await asyncio.sleep(0)
    assert coordinator._tasks == {}
    assert coordinator._versions == {}
    assert started[-2:] == ["replacement", "other"]
    assert "overflow" not in started
    assert "same-user-overflow" not in started
    await coordinator.close()


def test_burst_buffer_isolated_bounded_expiring_and_forgettable():
    now = [100.0]
    buffer = BurstBuffer(
        ttl_seconds=5,
        max_keys=2,
        max_items_per_key=2,
        clock=lambda: now[0],
    )
    key = ("g", "c", "u")
    assert buffer.append(key, "one") == ("one",)
    assert buffer.append(key, "two") == ("one", "two")
    assert buffer.append(key, "three") == ("two", "three")
    assert buffer.snapshot(("g", "c", "other")) == ()
    buffer.append(("g", "other", "u"), "separate channel")
    buffer.forget_user("u")
    assert len(buffer) == 0

    buffer.append(key, "expires")
    now[0] = 106.0
    assert buffer.snapshot(key) == ()
