from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.database import Database, iso_now, utcnow

_SPACE = re.compile(r"\s+")
_AMBIGUOUS_VALUE = re.compile(r"(?:或者|还是|都可以|随便|之一|和|以及|/|／)")
_SENSITIVE = re.compile(
    r"(?:密码|口令|token|api[ _-]?key|私钥|助记词|银行卡|信用卡|身份证|护照号|"
    r"住址|家庭地址|手机号|电话号码|微信号|邮箱|真实姓名|病史|诊断|处方|用药|"
    r"自残|自杀|性经历|政治身份|政治立场|党派|秘密|私下|不要告诉)",
    re.I,
)


def _clean_text(value: str, limit: int) -> str:
    return _SPACE.sub(" ", value).strip(" \t\r\n，。！？,.!?：:;；'\"“”‘’")[:limit]


def _as_utc(value: datetime | None) -> datetime:
    result = value or utcnow()
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _json_object(value: Any) -> dict[str, Any]:
    try:
        result = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _bounded_number(value: int | float, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = low
    return max(low, min(high, number))


@dataclass(frozen=True)
class IntentResult:
    """A local response hint, not a diagnosis or a persistent user attribute."""

    intent: str
    confidence: float
    hint: str
    signals: tuple[str, ...] = ()

    @property
    def response_hint(self) -> str:
        return self.hint

    @property
    def category(self) -> str:
        return self.intent

    @property
    def is_crisis(self) -> bool:
        return self.intent == "危机"


class IntentService:
    """Conservative deterministic intent hints; this service never writes data."""

    _CRISIS = re.compile(
        r"(?:想死|不想活|活不下去|结束生命|自杀|轻生|伤害自己|自残|"
        r"kill myself|suicide|end my life)",
        re.I,
    )
    _LISTEN = re.compile(r"(?:听我说|听我讲|想倾诉|让我说完|陪我聊聊|只想说说)", re.I)
    _COMFORT = re.compile(
        r"(?:安慰我|抱抱|好难过|很难过|伤心|委屈|崩溃|撑不住|心情很差|失落)", re.I
    )
    _ADVICE = re.compile(r"(?:怎么办|该怎么|怎么做|给.{0,4}建议|你建议|帮我出主意)", re.I)
    _ANALYSE = re.compile(r"(?:分析一下|帮我分析|为什么|原因|怎么看|如何理解|推理)", re.I)
    _JOKE = re.compile(r"(?:讲个笑话|开个玩笑|逗我|哈哈哈+|笑死|只是玩笑|just kidding)", re.I)
    _ENDING = re.compile(r"(?:先这样|不聊了|到此为止|晚安|再见|拜拜|回头聊|结束吧)", re.I)
    _CASUAL = re.compile(r"(?:你好|嗨|早上好|下午好|晚上好|在吗|最近怎样|吃了吗)", re.I)

    _HINTS = {
        "倾听": "先接住对方的话，少追问，一次只回应一个重点。",
        "安慰": "先确认感受并温和陪伴，不夸大、不诊断，再询问对方此刻需要什么。",
        "建议": "先确认目标和限制，再给少量、可选择、可撤回的具体建议。",
        "闲聊": "自然简短地回应，避免把普通表达解读成稳定人格或心理特征。",
        "分析": "区分事实、推断和未知，给出清晰但不过度确定的分析。",
        "玩笑": "保持轻松，但不要拿隐私、疾病、创伤或安全风险开玩笑。",
        "结束": "尊重结束信号，简短收尾，不继续追问或主动留存推测。",
        "危机": "优先确认对方是否正处于即时危险，鼓励联系当地紧急服务和可信任的人；不要诊断。",
    }

    def classify(self, content: str) -> IntentResult:
        text = _clean_text(content, 2000)
        if not text:
            return IntentResult("闲聊", 0.25, self._HINTS["闲聊"])
        checks = (
            ("危机", self._CRISIS, 0.98, "明确安全风险表述"),
            ("结束", self._ENDING, 0.92, "明确结束表述"),
            ("倾听", self._LISTEN, 0.90, "明确倾诉请求"),
            ("安慰", self._COMFORT, 0.86, "明确安慰或低落表述"),
            ("建议", self._ADVICE, 0.84, "明确建议请求"),
            ("分析", self._ANALYSE, 0.78, "明确分析请求"),
            ("玩笑", self._JOKE, 0.82, "明确玩笑信号"),
            ("闲聊", self._CASUAL, 0.72, "日常寒暄"),
        )
        for intent, pattern, confidence, signal in checks:
            if pattern.search(text):
                return IntentResult(intent, confidence, self._HINTS[intent], (signal,))
        # Unknown wording is deliberately treated as low-confidence conversation,
        # rather than inferring a psychological state from it.
        return IntentResult("闲聊", 0.35, self._HINTS["闲聊"])

    detect = classify


class CorrectionService:
    _AVOID_NAME = re.compile(r"(?:别|不要)(?:再)?叫我\s*([^，。！？,.!?]{1,40})", re.I)
    _USE_NAME = re.compile(
        r"(?:(?:请|以后|今后)\s*)?(?:叫我|称呼我为)\s*([^，。！？,.!?]{1,40})", re.I
    )
    _LIKE = re.compile(
        r"你记错了\s*[，,]?\s*(?:其实\s*)?我\s*(喜欢|不喜欢|讨厌)\s*"
        r"([^，。！？,.!?]{1,80})",
        re.I,
    )
    _SHORT = re.compile(r"(?:回答|回复|说得?|讲得?)\s*(?:再)?(?:短|简短|精简)(?:一?点|些)?", re.I)
    _LONG = re.compile(r"(?:回答|回复|说得?|讲得?)\s*(?:再)?(?:长|详细|具体)(?:一?点|些)?", re.I)
    _MARKER = re.compile(r"(?:别叫我|不要叫我|请叫我|称呼我为|回答短|回复短|你记错了)", re.I)

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _value(raw: str, limit: int) -> str | None:
        value = _clean_text(raw, limit)
        if (
            not value
            or value in {"这个", "那个", "这样", "那样", "随便"}
            or _AMBIGUOUS_VALUE.search(value)
        ):
            return None
        return value

    def parse(self, content: str) -> dict[str, Any]:
        text = _clean_text(content, 1000)
        display_name: str | None = None
        avoid_names: list[str] = []
        likes: list[str] = []
        dislikes: list[str] = []
        invalid = False

        for match in self._AVOID_NAME.finditer(text):
            value = self._value(match.group(1), 40)
            if value is None:
                invalid = True
            else:
                avoid_names.append(value)

        # Do not interpret the "叫我" portion of "别叫我" as a positive name request.
        positive_text = self._AVOID_NAME.sub("", text)
        positive_matches = list(self._USE_NAME.finditer(positive_text))
        names = [self._value(match.group(1), 40) for match in positive_matches]
        if any(value is None for value in names) or len(set(names)) > 1:
            invalid = True
        elif names:
            display_name = names[0]
        if display_name and display_name in avoid_names:
            invalid = True

        preference_matches = list(self._LIKE.finditer(text))
        for match in preference_matches:
            value = self._value(match.group(2), 80)
            if value is None:
                invalid = True
            elif match.group(1) == "喜欢":
                likes.append(value)
            else:
                dislikes.append(value)

        wants_short = bool(self._SHORT.search(text))
        wants_long = bool(self._LONG.search(text))
        if wants_short and wants_long:
            invalid = True

        recognized = bool(
            avoid_names
            or display_name
            or likes
            or dislikes
            or wants_short
            or wants_long
            or self._MARKER.search(text)
        )
        changes: dict[str, Any] = {}
        if display_name:
            changes["display_name"] = display_name
        if avoid_names:
            changes["avoid_names"] = list(dict.fromkeys(avoid_names))[:20]
        if likes:
            changes["likes"] = list(dict.fromkeys(likes))[:20]
        if dislikes:
            changes["dislikes"] = list(dict.fromkeys(dislikes))[:20]
        if wants_short:
            changes["response_length"] = "short"
        elif wants_long:
            changes["response_length"] = "detailed"
        return {
            "recognized": recognized,
            "needs_confirmation": invalid or (recognized and not changes),
            "changes": changes,
        }

    async def apply(self, user_id: str, content: str) -> dict[str, Any]:
        parsed = self.parse(content)
        if not parsed["recognized"]:
            return {
                "applied": False,
                "needs_confirmation": False,
                "changes": {},
                "previous": None,
                "reason": "未发现明确纠正",
            }
        if parsed["needs_confirmation"]:
            return {
                "applied": False,
                "needs_confirmation": True,
                "changes": {},
                "previous": None,
                "reason": "纠正目标不唯一或不完整",
            }

        now = iso_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """SELECT display_name, style_json, boundaries_json
                       FROM user_profiles WHERE user_id = ?""",
                    (user_id,),
                )
                row = await cursor.fetchone()
                previous = {
                    "exists": row is not None,
                    "display_name": str(row["display_name"]) if row else "",
                    "style_json": str(row["style_json"]) if row else "{}",
                    "boundaries_json": str(row["boundaries_json"]) if row else "{}",
                }
                style = _json_object(previous["style_json"])
                boundaries = _json_object(previous["boundaries_json"])
                display_name = previous["display_name"]
                changes = parsed["changes"]

                if "display_name" in changes:
                    display_name = changes["display_name"]
                if "response_length" in changes:
                    style["response_length"] = changes["response_length"]
                for key in ("likes", "dislikes"):
                    if key in changes:
                        existing = style.get(key, [])
                        if not isinstance(existing, list):
                            existing = []
                        style[key] = list(
                            dict.fromkeys(
                                [str(item)[:80] for item in existing if isinstance(item, str)]
                                + changes[key]
                            )
                        )[:20]
                if "avoid_names" in changes:
                    existing_names = boundaries.get("avoid_names", [])
                    if not isinstance(existing_names, list):
                        existing_names = []
                    boundaries["avoid_names"] = list(
                        dict.fromkeys(
                            [str(item)[:40] for item in existing_names if isinstance(item, str)]
                            + changes["avoid_names"]
                        )
                    )[:20]

                style_json = json.dumps(style, ensure_ascii=False, separators=(",", ":"))
                boundaries_json = json.dumps(boundaries, ensure_ascii=False, separators=(",", ":"))
                await connection.execute(
                    """INSERT INTO user_profiles
                       (user_id, display_name, style_json, boundaries_json,
                        first_seen_at, last_seen_at, interaction_count)
                       VALUES(?, ?, ?, ?, ?, ?, 0)
                       ON CONFLICT(user_id) DO UPDATE SET
                         display_name = excluded.display_name,
                         style_json = excluded.style_json,
                         boundaries_json = excluded.boundaries_json""",
                    (user_id, display_name, style_json, boundaries_json, now, now),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise

        after = {
            "display_name": display_name,
            "style_json": style_json,
            "boundaries_json": boundaries_json,
        }
        changed = any(previous[key] != after[key] for key in after)
        return {
            "applied": changed,
            "needs_confirmation": False,
            "changes": parsed["changes"],
            "previous": previous,
            "after": after,
            "reason": "已应用明确纠正" if changed else "内容已是当前设置",
        }

    async def undo(self, user_id: str, correction: dict[str, Any]) -> bool:
        previous = correction.get("previous")
        after = correction.get("after")
        if not isinstance(previous, dict) or not isinstance(after, dict):
            return False
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """UPDATE user_profiles
                       SET display_name = ?, style_json = ?, boundaries_json = ?
                       WHERE user_id = ? AND display_name = ?
                         AND style_json = ? AND boundaries_json = ?""",
                    (
                        str(previous.get("display_name", ""))[:120],
                        str(previous.get("style_json", "{}")),
                        str(previous.get("boundaries_json", "{}")),
                        user_id,
                        str(after.get("display_name", "")),
                        str(after.get("style_json", "{}")),
                        str(after.get("boundaries_json", "{}")),
                    ),
                )
                changed = cursor.rowcount == 1
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return changed


@dataclass(frozen=True)
class FollowupCandidate:
    topic: str
    followup_after: datetime
    public_safe: bool = False


class FollowupService:
    _FUTURE_TOKEN = re.compile(
        r"(?:下次|改天|明天|后天|大后天|下周(?:[一二三四五六日天])?|"
        r"周[一二三四五六日天]|星期[一二三四五六日天]|"
        r"\d{1,3}\s*(?:分钟|小时|天|周)后|"
        r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}日)",
        re.I,
    )
    _NEXT_CONTINUE = re.compile(r"(?:下次|改天).{0,8}(?:继续|再聊|再说|聊|说)", re.I)
    _RELATIVE = re.compile(r"(\d{1,3})\s*(分钟|小时|天|周)后")
    _ISO_DATE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
    _CN_DATE = re.compile(r"(\d{1,2})月(\d{1,2})日")
    _WEEKDAY = re.compile(r"(下周|周|星期)([一二三四五六日天])")
    _WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

    def __init__(self, database: Database, *, max_open_per_user: int = 20):
        self.database = database
        self.max_open_per_user = max(1, min(100, int(max_open_per_user)))

    @staticmethod
    def _at_nine(value: datetime) -> datetime:
        return value.replace(hour=9, minute=0, second=0, microsecond=0)

    def _future_time(self, text: str, now: datetime) -> datetime | None:
        relative = self._RELATIVE.search(text)
        if relative:
            amount = min(365, int(relative.group(1)))
            unit = relative.group(2)
            delta = {
                "分钟": timedelta(minutes=amount),
                "小时": timedelta(hours=amount),
                "天": timedelta(days=amount),
                "周": timedelta(weeks=min(52, amount)),
            }[unit]
            return now + delta

        absolute = self._ISO_DATE.search(text)
        if absolute:
            try:
                result = datetime(
                    int(absolute.group(1)),
                    int(absolute.group(2)),
                    int(absolute.group(3)),
                    9,
                    tzinfo=UTC,
                )
            except ValueError:
                return None
            return result if result > now else None

        chinese_date = self._CN_DATE.search(text)
        if chinese_date:
            try:
                result = datetime(
                    now.year,
                    int(chinese_date.group(1)),
                    int(chinese_date.group(2)),
                    9,
                    tzinfo=UTC,
                )
                if result <= now:
                    result = result.replace(year=now.year + 1)
            except ValueError:
                return None
            return result

        weekday = self._WEEKDAY.search(text)
        if weekday:
            target = self._WEEKDAYS[weekday.group(2)]
            if weekday.group(1) == "下周":
                delta_days = 7 - now.weekday() + target
            else:
                delta_days = (target - now.weekday()) % 7 or 7
            return self._at_nine(now + timedelta(days=delta_days))

        for token, days in (("大后天", 3), ("后天", 2), ("明天", 1)):
            if token in text:
                return self._at_nine(now + timedelta(days=days))
        if "下周" in text:
            return self._at_nine(now + timedelta(days=7))
        if self._NEXT_CONTINUE.search(text):
            return self._at_nine(now + timedelta(days=1))
        return None

    def extract(self, content: str, *, now: datetime | None = None) -> FollowupCandidate | None:
        text = _clean_text(content, 500)
        if not text or _SENSITIVE.search(text):
            return None
        if not self._FUTURE_TOKEN.search(text):
            return None
        current = _as_utc(now)
        followup_after = self._future_time(text, current)
        if followup_after is None or followup_after <= current:
            return None
        topic = self._FUTURE_TOKEN.sub(" ", text)
        topic = re.sub(
            r"^(?:我们|咱们)?\s*(?:再|继续)?\s*(?:聊聊?|说说?|讨论|处理|跟进)\s*", "", topic
        )
        topic = _clean_text(topic, 200) or "继续当前话题"
        if _SENSITIVE.search(topic):
            return None
        return FollowupCandidate(topic, followup_after, False)

    async def create(
        self,
        guild_id: str,
        user_id: str,
        topic: str,
        followup_after: datetime,
        *,
        public_safe: bool = False,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> int | None:
        cleaned_topic = _clean_text(topic, 200)
        current = _as_utc(now)
        due = _as_utc(followup_after)
        if not cleaned_topic or _SENSITIVE.search(cleaned_topic) or due <= current:
            return None
        expiry = _as_utc(expires_at) if expires_at else due + timedelta(days=30)
        if expiry <= due:
            return None
        now_text = current.isoformat()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """SELECT COUNT(*) AS n FROM open_loops
                       WHERE guild_id = ? AND user_id = ? AND status = 'open'
                         AND (expires_at IS NULL OR expires_at > ?)""",
                    (guild_id, user_id, now_text),
                )
                row = await cursor.fetchone()
                if int(row["n"]) >= self.max_open_per_user:
                    await connection.rollback()
                    return None
                cursor = await connection.execute(
                    """INSERT INTO open_loops
                       (guild_id, user_id, topic, public_safe, status, followup_after,
                        expires_at, followup_count, created_at, updated_at)
                       VALUES(?, ?, ?, ?, 'open', ?, ?, 0, ?, ?)""",
                    (
                        guild_id,
                        user_id,
                        cleaned_topic,
                        int(public_safe),
                        due.isoformat(),
                        expiry.isoformat(),
                        now_text,
                        now_text,
                    ),
                )
                loop_id = int(cursor.lastrowid)
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return loop_id

    async def create_from_text(
        self,
        guild_id: str,
        user_id: str,
        content: str,
        *,
        now: datetime | None = None,
        public_safe: bool = False,
    ) -> int | None:
        candidate = self.extract(content, now=now)
        if candidate is None:
            return None
        current = _as_utc(now)
        return await self.create(
            guild_id,
            user_id,
            candidate.topic,
            candidate.followup_after,
            public_safe=public_safe,
            now=current,
        )

    async def list_due(
        self,
        *,
        now: datetime | None = None,
        guild_id: str | None = None,
        user_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        current = _as_utc(now).isoformat()
        rows = await self.database.fetchall(
            """SELECT * FROM open_loops
               WHERE status = 'open' AND followup_after IS NOT NULL
                 AND followup_after <= ? AND (expires_at IS NULL OR expires_at > ?)
                 AND (? IS NULL OR guild_id = ?) AND (? IS NULL OR user_id = ?)
               ORDER BY followup_after, id LIMIT ?""",
            (
                current,
                current,
                guild_id,
                guild_id,
                user_id,
                user_id,
                max(1, min(100, int(limit))),
            ),
        )
        for row in rows:
            row["public_safe"] = bool(row["public_safe"])
        return rows

    async def claim(self, loop_id: int, *, now: datetime | None = None) -> dict[str, Any] | None:
        current = _as_utc(now).isoformat()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """UPDATE open_loops
                       SET followup_after = NULL, followup_count = followup_count + 1,
                           updated_at = ?
                       WHERE id = ? AND status = 'open' AND followup_after IS NOT NULL
                         AND followup_after <= ?
                         AND (expires_at IS NULL OR expires_at > ?)""",
                    (current, int(loop_id), current, current),
                )
                if cursor.rowcount != 1:
                    await connection.rollback()
                    return None
                cursor = await connection.execute(
                    "SELECT * FROM open_loops WHERE id = ?", (loop_id,)
                )
                row = await cursor.fetchone()
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        result = dict(row) if row else None
        if result:
            result["public_safe"] = bool(result["public_safe"])
        return result

    async def claim_due(
        self,
        *,
        now: datetime | None = None,
        guild_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        current = _as_utc(now).isoformat()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """SELECT id FROM open_loops
                       WHERE status = 'open' AND followup_after IS NOT NULL
                         AND followup_after <= ?
                         AND (expires_at IS NULL OR expires_at > ?)
                         AND (? IS NULL OR guild_id = ?)
                         AND (? IS NULL OR user_id = ?)
                       ORDER BY followup_after, id LIMIT 1""",
                    (current, current, guild_id, guild_id, user_id, user_id),
                )
                selected = await cursor.fetchone()
                if selected is None:
                    await connection.rollback()
                    return None
                cursor = await connection.execute(
                    """UPDATE open_loops SET followup_after = NULL,
                       followup_count = followup_count + 1, updated_at = ?
                       WHERE id = ? AND status = 'open' AND followup_after IS NOT NULL
                         AND followup_after <= ?
                         AND (expires_at IS NULL OR expires_at > ?)""",
                    (current, int(selected["id"]), current, current),
                )
                if cursor.rowcount != 1:
                    await connection.rollback()
                    return None
                cursor = await connection.execute(
                    "SELECT * FROM open_loops WHERE id = ?", (int(selected["id"]),)
                )
                row = await cursor.fetchone()
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        result = dict(row) if row else None
        if result:
            result["public_safe"] = bool(result["public_safe"])
        return result

    async def close(self, loop_id: int) -> bool:
        return bool(
            await self.database.execute(
                """UPDATE open_loops SET status = 'closed', updated_at = ?
                   WHERE id = ? AND status = 'open'""",
                (iso_now(), int(loop_id)),
            )
        )

    async def reopen(
        self, loop_id: int, followup_after: datetime, *, now: datetime | None = None
    ) -> bool:
        current = _as_utc(now)
        due = _as_utc(followup_after)
        if due <= current:
            return False
        return bool(
            await self.database.execute(
                """UPDATE open_loops SET status = 'open', followup_after = ?, updated_at = ?
                   WHERE id = ? AND status IN ('closed', 'open')
                     AND (expires_at IS NULL OR expires_at > ?)""",
                (due.isoformat(), current.isoformat(), int(loop_id), current.isoformat()),
            )
        )


class FeedbackService:
    WEIGHTS = {"👍": 1.0, "👎": -1.0, "❤️": 1.0, "❤": 1.0, "😄": 0.5, "😂": 0.5}

    def __init__(self, database: Database, *, counter_limit: int = 20):
        self.database = database
        self.counter_limit = max(1, min(100, int(counter_limit)))

    async def _adjust_owner_profile(
        self, connection: Any, user_id: str, emoji: str, delta: int, now: str
    ) -> None:
        cursor = await connection.execute(
            "SELECT style_json FROM user_profiles WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        style = _json_object(row["style_json"] if row else "{}")
        feedback = style.get("feedback", {})
        if not isinstance(feedback, dict):
            feedback = {}
        key = "negative" if self.WEIGHTS[emoji] < 0 else "positive"
        current = _bounded_number(feedback.get(key, 0), 0, self.counter_limit)
        feedback[key] = max(0, min(self.counter_limit, current + delta))
        positive = _bounded_number(feedback.get("positive", 0), 0, self.counter_limit)
        negative = _bounded_number(feedback.get("negative", 0), 0, self.counter_limit)
        feedback["net"] = max(-self.counter_limit, min(self.counter_limit, positive - negative))
        style["feedback"] = feedback
        style_json = json.dumps(style, ensure_ascii=False, separators=(",", ":"))
        await connection.execute(
            """INSERT INTO user_profiles
               (user_id, display_name, style_json, boundaries_json,
                first_seen_at, last_seen_at, interaction_count)
               VALUES(?, '', ?, '{}', ?, ?, 0)
               ON CONFLICT(user_id) DO UPDATE SET style_json = excluded.style_json""",
            (user_id, style_json, now, now),
        )

    async def add(
        self,
        message_id: str,
        user_id: str,
        origin_user_id: str | None,
        guild_id: str,
        emoji: str,
    ) -> bool:
        if emoji not in self.WEIGHTS:
            return False
        now = iso_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """INSERT OR IGNORE INTO feedback_events
                       (message_id, user_id, origin_user_id, guild_id, emoji, weight, created_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(message_id)[:120],
                        str(user_id)[:120],
                        str(origin_user_id)[:120] if origin_user_id is not None else None,
                        str(guild_id)[:120],
                        emoji,
                        self.WEIGHTS[emoji],
                        now,
                    ),
                )
                inserted = cursor.rowcount == 1
                if inserted and origin_user_id is not None and str(user_id) == str(origin_user_id):
                    await self._adjust_owner_profile(connection, str(user_id), emoji, 1, now)
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return inserted

    async def remove(self, message_id: str, user_id: str, emoji: str) -> bool:
        if emoji not in self.WEIGHTS:
            return False
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """SELECT origin_user_id FROM feedback_events
                       WHERE message_id = ? AND user_id = ? AND emoji = ?""",
                    (str(message_id)[:120], str(user_id)[:120], emoji),
                )
                row = await cursor.fetchone()
                if row is None:
                    await connection.rollback()
                    return False
                await connection.execute(
                    """DELETE FROM feedback_events
                       WHERE message_id = ? AND user_id = ? AND emoji = ?""",
                    (str(message_id)[:120], str(user_id)[:120], emoji),
                )
                origin = row["origin_user_id"]
                if origin is not None and str(origin) == str(user_id):
                    await self._adjust_owner_profile(connection, str(user_id), emoji, -1, iso_now())
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return True


class UsageService:
    def __init__(self, database: Database):
        self.database = database

    async def record(
        self,
        kind: str,
        *,
        guild_id: str | None = None,
        user_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        status: str = "ok",
        error_code: str | None = None,
        created_at: datetime | None = None,
    ) -> int:
        clean_kind = _clean_text(kind, 40)
        if not clean_kind:
            raise ValueError("usage kind must not be empty")
        return await self.database.execute(
            """INSERT INTO usage_metrics
               (kind, guild_id, user_id, provider, model, input_tokens,
                output_tokens, latency_ms, status, error_code, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                clean_kind,
                str(guild_id)[:120] if guild_id is not None else None,
                str(user_id)[:120] if user_id is not None else None,
                _clean_text(provider, 80) if provider is not None else None,
                _clean_text(model, 120) if model is not None else None,
                _bounded_number(input_tokens, 0, 1_000_000_000),
                _bounded_number(output_tokens, 0, 1_000_000_000),
                _bounded_number(latency_ms, 0, 86_400_000),
                _clean_text(status, 30) or "ok",
                _clean_text(error_code, 80) if error_code is not None else None,
                _as_utc(created_at).isoformat(),
            ),
        )

    async def aggregate(
        self,
        days: int = 7,
        *,
        kind: str | None = None,
        guild_id: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        window = max(1, min(3650, int(days)))
        return await self.database.fetchall(
            """SELECT kind, provider, model, status, COUNT(*) AS calls,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      ROUND(AVG(latency_ms), 2) AS avg_latency_ms,
                      MAX(latency_ms) AS max_latency_ms
               FROM usage_metrics
               WHERE created_at >= ? AND (? IS NULL OR kind = ?)
                 AND (? IS NULL OR guild_id = ?) AND (? IS NULL OR user_id = ?)
               GROUP BY kind, provider, model, status
               ORDER BY calls DESC, kind, provider, model, status""",
            (
                (utcnow() - timedelta(days=window)).isoformat(),
                kind,
                kind,
                guild_id,
                guild_id,
                user_id,
                user_id,
            ),
        )

    async def totals(self, days: int = 7) -> dict[str, Any]:
        window = max(1, min(3650, int(days)))
        row = await self.database.fetchone(
            """SELECT COUNT(*) AS calls,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      ROUND(COALESCE(AVG(latency_ms), 0), 2) AS avg_latency_ms,
                      COALESCE(SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END), 0) AS errors
               FROM usage_metrics WHERE created_at >= ?""",
            ((utcnow() - timedelta(days=window)).isoformat(),),
        )
        return row or {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "avg_latency_ms": 0,
            "errors": 0,
        }

    async def remove(self, metric_id: int) -> bool:
        """Undo a newly recorded metric by its opaque row id."""
        return bool(
            await self.database.execute("DELETE FROM usage_metrics WHERE id = ?", (int(metric_id),))
        )


class BotExperienceService:
    def __init__(self, database: Database, *, max_per_guild: int = 50):
        self.database = database
        self.max_per_guild = max(1, min(500, int(max_per_guild)))

    async def save(
        self,
        guild_id: str | None,
        source_user_id: str | None,
        content: str,
        *,
        kind: str = "experience",
        confidence: float = 0.5,
        importance: float = 0.5,
        locked: bool = False,
        public_safe: bool = False,
        expires_at: datetime | None = None,
    ) -> int | None:
        cleaned = _clean_text(content, 300)
        if not public_safe or not cleaned or _SENSITIVE.search(cleaned):
            return None
        clean_kind = _clean_text(kind, 40) or "experience"
        bounded_confidence = max(0.0, min(1.0, float(confidence)))
        bounded_importance = max(0.0, min(1.0, float(importance)))
        now = iso_now()
        expiry_text = _as_utc(expires_at).isoformat() if expires_at else None
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """SELECT * FROM bot_experiences
                       WHERE guild_id IS ? AND kind = ?
                         AND (expires_at IS NULL OR expires_at > ?)
                       ORDER BY id""",
                    (guild_id, clean_kind, now),
                )
                rows = await cursor.fetchall()
                normalized = cleaned.casefold()
                duplicate = next(
                    (
                        row
                        for row in rows
                        if _clean_text(str(row["content"]), 300).casefold() == normalized
                    ),
                    None,
                )
                if duplicate is not None:
                    experience_id = int(duplicate["id"])
                    if not bool(duplicate["locked"]):
                        await connection.execute(
                            """UPDATE bot_experiences
                               SET confidence = MAX(confidence, ?),
                                   importance = MAX(importance, ?),
                                   evidence_count = MIN(100, evidence_count + 1),
                                   updated_at = ?, expires_at = COALESCE(?, expires_at)
                               WHERE id = ? AND locked = 0""",
                            (
                                bounded_confidence,
                                bounded_importance,
                                now,
                                expiry_text,
                                experience_id,
                            ),
                        )
                    await connection.commit()
                    return experience_id

                cursor = await connection.execute(
                    """SELECT COUNT(*) AS n FROM bot_experiences
                       WHERE guild_id IS ? AND (expires_at IS NULL OR expires_at > ?)""",
                    (guild_id, now),
                )
                count_row = await cursor.fetchone()
                if int(count_row["n"]) >= self.max_per_guild:
                    cursor = await connection.execute(
                        """SELECT id FROM bot_experiences
                           WHERE guild_id IS ? AND locked = 0
                             AND (expires_at IS NULL OR expires_at > ?)
                           ORDER BY importance, updated_at, id LIMIT 1""",
                        (guild_id, now),
                    )
                    victim = await cursor.fetchone()
                    if victim is None:
                        await connection.rollback()
                        return None
                    await connection.execute(
                        "DELETE FROM bot_experiences WHERE id = ? AND locked = 0",
                        (int(victim["id"]),),
                    )

                cursor = await connection.execute(
                    """INSERT INTO bot_experiences
                       (guild_id, source_user_id, kind, content, confidence, importance,
                        evidence_count, locked, created_at, updated_at, expires_at)
                       VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                    (
                        guild_id,
                        source_user_id,
                        clean_kind,
                        cleaned,
                        bounded_confidence,
                        bounded_importance,
                        int(locked),
                        now,
                        now,
                        expiry_text,
                    ),
                )
                experience_id = int(cursor.lastrowid)
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return experience_id

    add = save

    async def list(
        self, guild_id: str | None, *, limit: int = 20, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        rows = await self.database.fetchall(
            """SELECT * FROM bot_experiences
               WHERE guild_id IS ? AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY importance DESC, updated_at DESC, id DESC LIMIT ?""",
            (guild_id, _as_utc(now).isoformat(), max(1, min(100, int(limit)))),
        )
        for row in rows:
            row["locked"] = bool(row["locked"])
        return rows

    async def set_locked(self, experience_id: int, locked: bool) -> bool:
        return bool(
            await self.database.execute(
                "UPDATE bot_experiences SET locked = ?, updated_at = ? WHERE id = ?",
                (int(locked), iso_now(), int(experience_id)),
            )
        )

    async def remove(self, experience_id: int, *, source_user_id: str | None = None) -> bool:
        if source_user_id is None:
            return bool(
                await self.database.execute(
                    "DELETE FROM bot_experiences WHERE id = ?", (int(experience_id),)
                )
            )
        return bool(
            await self.database.execute(
                "DELETE FROM bot_experiences WHERE id = ? AND source_user_id = ?",
                (int(experience_id), source_user_id),
            )
        )
