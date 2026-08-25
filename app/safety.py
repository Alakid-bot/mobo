"""Local, deterministic content safety checks.

The safety engine intentionally has no model or network dependency.  It accepts
the runtime settings object (or a plain mapping) and an optional ``Database``
instance.  Rules are loaded for each check so an admin setting or database rule
change takes effect without restarting the bot.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

Direction = Literal["input", "output"]
Action = Literal["allow", "block", "redact", "log"]
RuleAction = Literal["block", "redact", "log"]
MatchType = Literal["contains", "word", "regex"]

LOGGER = logging.getLogger(__name__)

# This response is deliberately generic.  In particular, it must not contain
# the matched text or a database rule's pattern.
SAFE_REFUSAL = "抱歉，我不能处理或继续传播这类敏感内容。"
# Public aliases make the fixed response easy to use from integrations while
# retaining the concise name used internally.
SAFETY_REFUSAL = SAFE_REFUSAL
SAFE_RESPONSE = SAFE_REFUSAL
DEFAULT_REPLACEMENT = "[已隐藏]"


@dataclass
class SafetyResult:
    """The outcome of one local safety check."""

    allowed: bool
    text: str
    action: Action
    categories: list[str]
    matched_rule_ids: list[int | str]


@dataclass(frozen=True)
class SafetyRule:
    """A normalized rule used by the local matcher."""

    rule_id: int | str
    name: str
    category: str
    direction: str
    pattern: str
    match_type: MatchType
    action: RuleAction
    replacement: str = DEFAULT_REPLACEMENT
    priority: int = 100


@dataclass(frozen=True)
class _Match:
    rule: SafetyRule
    spans: tuple[tuple[int, int], ...]


# These patterns cover the common forms users accidentally paste into a chat.
# They are intentionally conservative and are not intended to validate keys.
_SECRET_RULES: tuple[SafetyRule, ...] = (
    SafetyRule(
        "builtin:discord-token",
        "Discord bot token",
        "secret",
        "both",
        r"(?<![A-Za-z0-9_-])(?:Bot\s+)?[A-Za-z\d_-]{20,32}\.[A-Za-z\d_-]{4,8}\.[A-Za-z\d_-]{20,110}(?![A-Za-z\d_-])",
        "regex",
        "block",
    ),
    SafetyRule(
        "builtin:discord-webhook",
        "Discord webhook URL",
        "secret",
        "both",
        r"https?://(?:discord(?:app)?\.com|discord\.com)/api/webhooks/\d{15,22}/[A-Za-z0-9._-]+",
        "regex",
        "block",
    ),
    SafetyRule(
        "builtin:private-key",
        "Private key",
        "secret",
        "both",
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
        "regex",
        "block",
    ),
    SafetyRule(
        "builtin:api-key-prefix",
        "API key prefix",
        "secret",
        "both",
        r"(?<![A-Za-z0-9_-])(?:sk-(?:live|test|proj)-|sk-|pk_live_|pk_test_|rk_live_|rk_test_|ghp_|github_pat_|glpat-|xox[abprs]-|AIza)[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])",
        "regex",
        "block",
    ),
    SafetyRule(
        "builtin:cloud-access-key",
        "Cloud access key",
        "secret",
        "both",
        r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])",
        "regex",
        "block",
    ),
    SafetyRule(
        "builtin:credential-assignment",
        "Credential assignment",
        "secret",
        "both",
        r"(?<![A-Za-z0-9_])(?:bearer\s+[A-Za-z0-9._~+/=-]{20,}|(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|secret[_ -]?key|aws[_ -]?secret[_ -]?access[_ -]?key|token)\s*[:=]\s*(?:Bearer\s+)?[^\s,;]+)",
        "regex",
        "block",
    ),
    SafetyRule(
        "builtin:password-assignment",
        "Password assignment",
        "secret",
        "both",
        r"(?<![A-Za-z0-9_])(?:password|passwd|pwd|pass|密码)\s*[:=：]\s*[^\s,;]+",
        "regex",
        "block",
    ),
)

_VALID_DIRECTIONS = {"input", "output", "both"}
_VALID_MATCH_TYPES = {"contains", "word", "regex"}
_VALID_ACTIONS = {"block", "redact", "log"}


def _normalize(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    parsed = _normalize(value).strip().casefold()
    if parsed in {"1", "true", "yes", "on", "是", "开"}:
        return True
    if parsed in {"0", "false", "no", "off", "否", "关", ""}:
        return False
    return default


def _rule_id(value: Any, fallback: int | str) -> int | str:
    if isinstance(value, bool) or value is None:
        return fallback
    if isinstance(value, int):
        return value
    text = _normalize(value).strip()
    return text or fallback


class SafetyEngine:
    """Apply local settings, database rules, and secret patterns.

    ``config`` may be a plain mapping or an object exposing an async ``all``
    method, such as ``RuntimeSettings``.  ``database`` is optional; when
    present it is expected to expose the existing async ``fetchall`` and
    ``execute`` methods.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | Any | None = None,
        database: Any | None = None,
        *,
        settings: Mapping[str, Any] | Any | None = None,
        refusal: str = SAFE_REFUSAL,
        event_recorder: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> None:
        self.config = settings if settings is not None else config
        self.database = database
        # Keep the fixed response byte-for-byte stable; only user content and
        # rule fields are NFKC-normalized.
        self.refusal = str(refusal)
        self.event_recorder = event_recorder
        self.policy_prompt = ""
        self.invalid_rule_ids: list[int | str] = []
        # Cache only successfully fetched database rules.  Configuration and
        # built-in rules are rebuilt for every check, so changing either still
        # takes effect immediately while a database outage uses this LKG.
        self._database_rule_cache: dict[Direction, tuple[SafetyRule, ...]] = {}
        self.database_degraded: dict[Direction, bool] = {
            "input": False,
            "output": False,
        }
        self.database_fail_closed: dict[Direction, bool] = {
            "input": False,
            "output": False,
        }

    async def _settings(self) -> dict[str, Any]:
        source = self.config
        if source is None:
            return {}
        if isinstance(source, Mapping):
            values = dict(source)
        else:
            all_settings = getattr(source, "all", None)
            if all_settings is not None:
                values = all_settings()
                if hasattr(values, "__await__"):
                    values = await values
                values = dict(values) if isinstance(values, Mapping) else {}
            else:
                values = {}
                getter = getattr(source, "get", None)
                if getter is not None:
                    for key in (
                        "safety_policy_prompt",
                        "safety_input_terms",
                        "safety_output_terms",
                        "safety_default_action",
                        "safety_secret_detection",
                    ):
                        value = getter(key)
                        if hasattr(value, "__await__"):
                            value = await value
                        values[key] = value
        self.policy_prompt = _normalize(values.get("safety_policy_prompt", ""))
        return values

    @staticmethod
    def _terms(value: Any) -> list[str]:
        """Split an admin textarea into normalized, non-empty terms."""
        if value is None:
            return []
        # A list is convenient for callers and harmless for RuntimeSettings.
        values = value if isinstance(value, (list, tuple, set)) else _normalize(value).splitlines()
        terms: list[str] = []
        seen: set[str] = set()
        for item in values:
            term = _normalize(item).strip()
            key = term.casefold()
            if term and key not in seen:
                terms.append(term)
                seen.add(key)
        return terms

    async def _database_rules(self, direction: Direction) -> list[SafetyRule]:
        database = self.database
        if database is None:
            self.database_degraded[direction] = False
            self.database_fail_closed[direction] = False
            return []
        fetchall = getattr(database, "fetchall", None)
        if fetchall is None:
            return self._database_load_failed(direction)
        try:
            rows = fetchall(
                """SELECT id, name, category, direction, pattern, match_type,
                          action, replacement, priority
                     FROM safety_rules
                    WHERE enabled = 1 AND (direction = ? OR direction = 'both')
                    ORDER BY priority ASC, id ASC""",
                (direction,),
            )
            if hasattr(rows, "__await__"):
                rows = await rows
        except Exception:
            # Do not log the exception: database drivers may include query
            # values in exception text.  The caller only needs a safe state.
            return self._database_load_failed(direction)

        result: list[SafetyRule] = []
        try:
            for row in rows or ():
                try:
                    get = row.get if isinstance(row, Mapping) else row.__getitem__
                    raw_direction = _normalize(get("direction")).strip().casefold()
                    if raw_direction not in _VALID_DIRECTIONS or raw_direction not in {
                        direction,
                        "both",
                    }:
                        continue
                    match_type = _normalize(get("match_type")).strip().casefold()
                    if match_type not in _VALID_MATCH_TYPES:
                        continue
                    pattern = _normalize(get("pattern"))
                    if not pattern:
                        continue
                    action = _normalize(get("action")).strip().casefold()
                    if action not in _VALID_ACTIONS:
                        continue
                    priority = int(get("priority") or 100)
                    result.append(
                        SafetyRule(
                            _rule_id(get("id"), f"database:{len(result) + 1}"),
                            _normalize(get("name") or "database rule"),
                            _normalize(get("category") or "custom") or "custom",
                            raw_direction,
                            pattern,
                            match_type,  # type: ignore[arg-type]
                            action,  # type: ignore[arg-type]
                            _normalize(get("replacement") or DEFAULT_REPLACEMENT),
                            priority,
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    LOGGER.warning("Ignoring malformed safety rule")
        except Exception:
            # Iteration/parsing failures are also an unavailable database
            # policy, rather than permission to continue with an empty set.
            return self._database_load_failed(direction)

        was_degraded = self.database_degraded[direction]
        self._database_rule_cache[direction] = tuple(result)
        self.database_degraded[direction] = False
        self.database_fail_closed[direction] = False
        if was_degraded:
            LOGGER.info("Safety database rules recovered for %s", direction)
        return result

    def _database_load_failed(self, direction: Direction) -> list[SafetyRule]:
        """Return LKG rules, or activate fail-closed mode without leaking data."""
        self.database_degraded[direction] = True
        cached = self._database_rule_cache.get(direction)
        if cached is not None:
            self.database_fail_closed[direction] = False
            LOGGER.warning(
                "Safety database rules unavailable for %s; using last-known-good rules",
                direction,
            )
            return list(cached)

        self.database_fail_closed[direction] = True
        LOGGER.error(
            "Safety database rules unavailable for %s; fail-closed checks are active",
            direction,
        )
        return []

    async def load_rules(self, direction: Direction) -> list[SafetyRule]:
        """Load all active rules applicable to ``direction``."""
        if direction not in {"input", "output"}:
            raise ValueError("direction must be 'input' or 'output'")
        values = await self._settings()
        default_action = _normalize(values.get("safety_default_action", "block")).strip().casefold()
        if default_action not in _VALID_ACTIONS:
            default_action = "block"

        rules: list[SafetyRule] = []
        terms = self._terms(values.get(f"safety_{direction}_terms", ""))
        for index, term in enumerate(terms, start=1):
            rules.append(
                SafetyRule(
                    f"config:{direction}:{index}",
                    f"Configured {direction} term {index}",
                    "configured_term",
                    direction,
                    term,
                    "contains",
                    default_action,  # type: ignore[arg-type]
                    DEFAULT_REPLACEMENT,
                    100,
                )
            )

        rules.extend(await self._database_rules(direction))
        if _as_bool(values.get("safety_secret_detection", True), default=True):
            rules.extend(_SECRET_RULES)
        return sorted(rules, key=lambda rule: (rule.priority, str(rule.rule_id)))

    def _find_matches(self, text: str, rules: Sequence[SafetyRule]) -> list[_Match]:
        matches: list[_Match] = []
        self.invalid_rule_ids = []
        for rule in rules:
            pattern = rule.pattern
            if rule.match_type == "contains":
                expression = re.escape(pattern)
            elif rule.match_type == "word":
                expression = rf"(?<!\w){re.escape(pattern)}(?!\w)"
            else:
                expression = pattern
            try:
                compiled = re.compile(expression, re.IGNORECASE)
                spans = tuple((match.start(), match.end()) for match in compiled.finditer(text))
            except (re.error, TypeError, ValueError):
                self.invalid_rule_ids.append(rule.rule_id)
                # Rule identifiers are user-controlled too; keep them out of
                # logs along with the source text and rule pattern.
                LOGGER.warning("Ignoring invalid safety regex rule")
                continue
            if spans:
                matches.append(_Match(rule, spans))
        return matches

    @staticmethod
    def _redact(text: str, matches: Sequence[_Match]) -> str:
        replacements: list[tuple[int, int, str, int]] = []
        for item in matches:
            if item.rule.action != "redact":
                continue
            for start, end in item.spans:
                if start != end:
                    replacements.append((start, end, item.rule.replacement, item.rule.priority))
        # Prefer the highest-priority span when rules overlap, then keep the
        # longest span at the same position.  Each source character is emitted
        # at most once.
        replacements.sort(key=lambda item: (item[0], item[3], -(item[1] - item[0])))
        selected: list[tuple[int, int, str]] = []
        cursor = -1
        for start, end, replacement, _priority in replacements:
            if start < cursor:
                continue
            selected.append((start, end, replacement))
            cursor = end
        for start, end, replacement in reversed(selected):
            text = text[:start] + replacement + text[end:]
        return text

    async def _record(
        self,
        *,
        text_hash: str,
        direction: Direction,
        action: RuleAction,
        matches: Sequence[_Match],
        guild_id: str | None,
        channel_id: str | None,
        user_id: str | None,
    ) -> None:
        # Do not pass the source text or a rule pattern to either recorder.
        for item in matches:
            payload = {
                "rule_id": item.rule.rule_id,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "user_id": user_id,
                "direction": direction,
                "category": item.rule.category,
                "action": action,
                "content_hash": text_hash,
                "created_at": datetime.now(UTC).isoformat(),
            }
            if self.event_recorder is not None:
                try:
                    recorded = self.event_recorder(dict(payload))
                    if hasattr(recorded, "__await__"):
                        await recorded
                except Exception:
                    # Keep recorder failures generic as well; an exception
                    # message can contain source text or other secrets.
                    LOGGER.warning("Unable to record safety event")
                continue
            if self.database is None or not hasattr(self.database, "execute"):
                continue
            try:
                rule_id = item.rule.rule_id if isinstance(item.rule.rule_id, int) else None
                awaitable = self.database.execute(
                    """INSERT INTO safety_events
                       (rule_id, guild_id, channel_id, user_id, direction, category,
                        action, content_hash, created_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        rule_id,
                        guild_id,
                        channel_id,
                        user_id,
                        direction,
                        item.rule.category,
                        action,
                        text_hash,
                        payload["created_at"],
                    ),
                )
                if hasattr(awaitable, "__await__"):
                    await awaitable
            except Exception:
                LOGGER.warning("Unable to record safety event")

    async def check(
        self,
        text: str,
        direction: Direction = "input",
        *,
        guild_id: str | None = None,
        channel_id: str | None = None,
        user_id: str | None = None,
    ) -> SafetyResult:
        """Check normalized ``text`` in the input or output direction."""
        if direction not in {"input", "output"}:
            raise ValueError("direction must be 'input' or 'output'")
        normalized = _normalize(text)
        rules = await self.load_rules(direction)
        matches = self._find_matches(normalized, rules)

        categories: list[str] = []
        matched_rule_ids: list[int | str] = []
        for item in matches:
            if item.rule.category not in categories:
                categories.append(item.rule.category)
            if item.rule.rule_id not in matched_rule_ids:
                matched_rule_ids.append(item.rule.rule_id)

        if self.database_fail_closed[direction]:
            # A missing initial database policy is itself unsafe.  Keep any
            # deterministic matches for diagnostics, but never let them turn
            # this state into an allow/redact/log result.
            if "safety_database_unavailable" not in categories:
                categories.append("safety_database_unavailable")
            action: RuleAction = "block"
            result = SafetyResult(False, self.refusal, action, categories, matched_rule_ids)
        elif not matches:
            return SafetyResult(True, normalized, "allow", [], [])
        elif any(item.rule.action == "block" for item in matches):
            action: RuleAction = "block"
            result = SafetyResult(False, self.refusal, action, categories, matched_rule_ids)
        elif any(item.rule.action == "redact" for item in matches):
            action = "redact"
            result = SafetyResult(
                True, self._redact(normalized, matches), action, categories, matched_rule_ids
            )
        else:
            action = "log"
            result = SafetyResult(True, normalized, action, categories, matched_rule_ids)

        await self._record(
            text_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            direction=direction,
            action=action,
            matches=matches,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        return result

    async def check_input(self, text: str, **context: str | None) -> SafetyResult:
        return await self.check(text, "input", **context)

    async def check_output(self, text: str, **context: str | None) -> SafetyResult:
        return await self.check(text, "output", **context)

    async def inspect(
        self, text: str, direction: Direction = "input", **context: str | None
    ) -> SafetyResult:
        """Alias for integrations that use inspection terminology."""
        return await self.check(text, direction, **context)


__all__ = [
    "DEFAULT_REPLACEMENT",
    "SAFE_RESPONSE",
    "SAFE_REFUSAL",
    "SAFETY_REFUSAL",
    "SafetyEngine",
    "SafetyResult",
    "SafetyRule",
]
