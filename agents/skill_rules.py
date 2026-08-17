"""Checkable rules derived from a skill, shared by every arm of a paired evaluation.

The same rule set is applied to `no_skill`, `current_champion` and `candidate`
responses so a comparison measures behaviour, not a different grading standard.
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

SideQuery = Callable[[str, str], Awaitable[str]]

_RE_URL = re.compile(r"https?://\S+")
_RE_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\((https?://[^)]+)\)")
_RE_SOURCE_LABEL = re.compile(r"(?im)^\s*(source|sources|reference|references|来源|参考)\s*[:：]")
_RE_JSON_PREFIX = re.compile(r"^\s*[\{\[]")
_RE_CONCLUSION = re.compile(r"(?i)\b(tl;dr|answer|conclusion|bottom line)\b|结论|先说结论")
_RE_PARAGRAPH_LIMIT = re.compile(r"(不超过|少于|最多|within|less than|at most)\s*(\d+)\s*(段|paragraph)")

MAX_RULES = 8


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def paragraph_limit(text: str) -> int:
    for match in _RE_PARAGRAPH_LIMIT.finditer(str(text or "")):
        try:
            return max(1, int(match.group(2)))
        except ValueError:
            continue
    low = normalize_text(text)
    if "少于 3 段" in low or "不超过 3 段" in low or "3 paragraphs" in low:
        return 3
    return 0


def skill_alignment_requirement(skill: dict[str, Any], *, max_chars: int = 3200) -> str:
    """Render the skill into a requirement a blinded judge can score a response against."""
    parts: list[str] = []
    for label, key in (("Skill name", "name"), ("Description", "description"), ("When to use", "when_to_use")):
        value = str(skill.get(key) or "").strip()
        if value:
            parts.append(f"{label}: {value}")
    tags = [str(tag).strip() for tag in skill.get("tags", []) if str(tag).strip()]
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    instructions = str(skill.get("instructions") or "").strip()
    if instructions:
        parts.append("Instructions:\n" + instructions)

    body = "\n\n".join(parts).strip()
    if not body:
        return ""
    if len(body) > max_chars:
        body = body[: max_chars - 80].rstrip() + "\n\n[Truncated to fit judge context.]"
    return (
        "Evaluate whether the assistant response follows these observable requirements. "
        "Judge only the visible response for the given user message; do not require hidden tool calls. "
        "Return false when the response clearly violates important instructions, misses the requested "
        "style or format, or ignores the intended behaviour.\n\n" + body
    )


def compile_eval_rules(skill: dict[str, Any], *, include_llm_rules: bool = False) -> list[dict[str, Any]]:
    """Derive rules from a skill snapshot. Hard rules are blocking failures."""
    corpus = "\n".join(
        [
            str(skill.get("name") or ""),
            str(skill.get("description") or ""),
            str(skill.get("when_to_use") or ""),
            str(skill.get("instructions") or ""),
            " ".join(str(tag) for tag in skill.get("tags", []) if str(tag).strip()),
        ]
    )
    low = normalize_text(corpus)
    rules: list[dict[str, Any]] = [
        {
            "rule_id": "response_nonempty",
            "label": "Non-empty response",
            "kind": "programmatic",
            "hard": True,
            "params": {"mode": "nonempty"},
            "provenance": {"source": "baseline"},
        }
    ]

    requirement = skill_alignment_requirement(skill)
    if include_llm_rules and requirement:
        rules.append(
            {
                "rule_id": "skill_instruction_alignment",
                "label": "Follows skill instructions",
                "kind": "llm_binary",
                "hard": False,
                "params": {"mode": "requirement", "requirement_text": requirement},
                "provenance": {"source": "skill_text"},
            }
        )

    if any(
        key in low
        for key in (
            "引用来源",
            "标注来源",
            "注明来源",
            "cite sources",
            "with sources",
            "provide sources",
            "source-backed",
        )
    ):
        rules.append(
            {
                "rule_id": "must_cite_sources",
                "label": "Cite sources",
                "kind": "programmatic",
                "hard": True,
                "params": {"mode": "mentions_sources"},
                "provenance": {"source": "skill_text"},
            }
        )

    limit = paragraph_limit(corpus)
    if limit:
        rules.append(
            {
                "rule_id": "paragraph_limit",
                "label": f"At most {limit} paragraphs",
                "kind": "programmatic",
                "hard": True,
                "params": {"mode": "max_paragraphs", "max_paragraphs": limit},
                "provenance": {"source": "skill_text"},
            }
        )

    if any(
        key in low
        for key in ("先给结论", "结论在前", "先说结论", "answer first", "lead with the conclusion", "bottom line first")
    ):
        rules.append(
            {
                "rule_id": "lead_with_conclusion",
                "label": "Lead with conclusion",
                "kind": "programmatic",
                "hard": False,
                "params": {"mode": "lead_with_conclusion"},
                "provenance": {"source": "skill_text"},
            }
        )

    if "json" in low or "结构化输出" in low:
        rules.append(
            {
                "rule_id": "json_parseable",
                "label": "Valid JSON output",
                "kind": "programmatic",
                "hard": True,
                "params": {"mode": "json_parseable"},
                "provenance": {"source": "skill_text"},
            }
        )

    if "markdown table" in low or "表格" in low:
        rules.append(
            {
                "rule_id": "markdown_table",
                "label": "Markdown table present",
                "kind": "programmatic",
                "hard": False,
                "params": {"mode": "markdown_table"},
                "provenance": {"source": "skill_text"},
            }
        )

    hallucination_keys = (
        "不要幻觉",
        "不要编造",
        "不确定就说",
        "do not hallucinate",
        "don't hallucinate",
        "avoid hallucination",
        "if unsure",
    )
    if any(key in low for key in hallucination_keys):
        if include_llm_rules:
            rules.append(
                {
                    "rule_id": "no_unfounded_claims",
                    "label": "Avoid unfounded claims",
                    "kind": "llm_binary",
                    "hard": True,
                    "params": {
                        "mode": "requirement",
                        "requirement_text": "Avoid unfounded claims and state uncertainty when needed.",
                    },
                    "provenance": {"source": "skill_text"},
                }
            )
        else:
            rules.append(
                {
                    "rule_id": "uncertainty_marked",
                    "label": "Mark uncertainty",
                    "kind": "programmatic",
                    "hard": False,
                    "params": {"mode": "uncertainty_marked"},
                    "provenance": {"source": "skill_text"},
                }
            )

    return rules[:MAX_RULES]


def evaluate_rule(rule: dict[str, Any], response_text: str) -> dict[str, Any]:
    params = rule.get("params") if isinstance(rule.get("params"), dict) else {}
    mode = str(params.get("mode") or "").strip()
    text = str(response_text or "")
    stripped = text.strip()
    passed = False
    details: dict[str, Any] = {}

    if mode == "nonempty":
        passed = bool(stripped)
        details = {"length": len(stripped)}
    elif mode == "json_parseable":
        if not _RE_JSON_PREFIX.search(stripped):
            details = {"reason": "missing_json_prefix"}
        else:
            try:
                json.loads(stripped)
                passed = True
            except ValueError as exc:
                details = {"reason": "json_parse_failed", "error": str(exc)}
    elif mode == "mentions_sources":
        passed = bool(_RE_URL.search(text) or _RE_MARKDOWN_LINK.search(text) or _RE_SOURCE_LABEL.search(text))
        details = {"has_url": bool(_RE_URL.search(text))}
    elif mode == "lead_with_conclusion":
        first_block = stripped.split("\n\n", 1)[0].strip()
        passed = bool(_RE_CONCLUSION.search(first_block)) or len(first_block) <= 100
        details = {"first_block": first_block[:160]}
    elif mode == "max_paragraphs":
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", stripped) if part.strip()]
        limit = max(1, int(params.get("max_paragraphs", 3) or 3))
        passed = len(paragraphs) <= limit
        details = {"paragraph_count": len(paragraphs), "limit": limit}
    elif mode == "markdown_table":
        lines = [line for line in text.splitlines() if "|" in line]
        passed = len(lines) >= 2 and any("---" in line for line in lines)
        details = {"table_line_count": len(lines)}
    elif mode == "uncertainty_marked":
        markers = (
            "不确定",
            "无法确认",
            "需要验证",
            "uncertain",
            "not sure",
            "cannot verify",
            "可能",
            "假设",
            "assuming",
        )
        passed = any(marker in stripped.lower() for marker in markers)
        details = {"heuristic": "uncertainty_marker"}
    else:
        details = {"error": f"unsupported programmatic mode: {mode}"}

    return {
        "rule_id": rule.get("rule_id", ""),
        "label": rule.get("label", ""),
        "hard": bool(rule.get("hard")),
        "passed": bool(passed),
        "details": details,
    }


async def evaluate_rule_async(
    rule: dict[str, Any],
    response_text: str,
    *,
    user_message: str = "",
    judge: SideQuery | None = None,
) -> dict[str, Any]:
    """Score one rule. LLM rules never see which arm produced the response."""
    kind = str(rule.get("kind") or "programmatic").strip()
    if kind == "programmatic":
        return evaluate_rule(rule, response_text)

    base = {
        "rule_id": rule.get("rule_id", ""),
        "label": rule.get("label", ""),
        "hard": bool(rule.get("hard")),
    }
    if kind != "llm_binary":
        return {**base, "passed": False, "details": {"error": f"unsupported rule kind: {kind}"}}

    requirement = str((rule.get("params") or {}).get("requirement_text") or "").strip()
    if not requirement:
        return {**base, "passed": False, "details": {"error": "missing_requirement_text"}}
    if judge is None:
        return {**base, "passed": False, "skipped": True, "details": {"reason": "judge_llm_not_configured"}}

    system = (
        "You are a strict binary evaluator.\n"
        "Output ONLY strict JSON parseable by json.loads.\n"
        'Schema: {"pass": true|false, "reason": "short reason"}\n'
        "Judge only against the requirement provided. The response is anonymous; "
        "you are not told which system produced it.\n"
        "Prefer false if the requirement is not clearly satisfied.\n"
    )
    payload = {
        "requirement": requirement,
        "latest_user_message": user_message,
        "response": str(response_text or ""),
    }
    try:
        raw = await judge(system, json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # judge failures must not crash the run
        return {**base, "passed": False, "details": {"reason": f"judge failed: {exc}"[:500]}}
    parsed = parse_json_object(raw)
    reason = str(parsed.get("reason") or "").strip() or "judge returned no reason"
    return {**base, "passed": bool(parsed.get("pass", False)), "details": {"reason": reason[:500]}}


async def score_response(
    *,
    rules: list[dict[str, Any]],
    response_text: str,
    user_message: str = "",
    judge: SideQuery | None = None,
) -> dict[str, Any]:
    """Apply the full rule set to one response and derive pass / hard-failure flags."""
    outcomes: list[dict[str, Any]] = []
    for rule in rules:
        outcomes.append(
            await evaluate_rule_async(rule, response_text, user_message=user_message, judge=judge)
        )
    hard_failures = [item for item in outcomes if item.get("hard") and not item.get("passed")]
    soft_total = len([item for item in outcomes if not item.get("hard")])
    soft_passed = len([item for item in outcomes if not item.get("hard") and item.get("passed")])
    return {
        "rules": outcomes,
        "passed": not hard_failures and all(item.get("passed") for item in outcomes),
        "hard_failed": bool(hard_failures),
        "hard_failures": [item.get("rule_id") for item in hard_failures],
        "soft_pass_rate": (soft_passed / soft_total) if soft_total else 1.0,
    }
