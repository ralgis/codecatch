"""Pure code extraction — apply regex patterns from `extractor_patterns`.

Caller passes already-loaded patterns (a list of asyncpg.Record-ish dicts);
this module has no IO so it's trivial to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ExtractionResult:
    code: str | None
    platform: str | None
    matched_pattern_id: int | None
    matched_pattern_name: str | None
    candidates: list[dict[str, Any]]   # all patterns tried + whether they matched


def _matches_pattern(pattern: str | None, text: str) -> bool:
    if pattern is None or pattern == "":
        return True
    try:
        return bool(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL))
    except re.error:
        return False


def _haystack(search_in: str, subject: str, body: str) -> str:
    s = search_in.lower()
    if s == "subject":
        return subject
    if s == "body":
        return body
    return subject + "\n" + body  # 'both'


def run_extraction(
    *,
    sender: str,
    subject: str,
    body: str,
    patterns: list[Mapping[str, Any]],
) -> ExtractionResult:
    """Apply patterns by priority (already sorted by caller).
    Return first successful match + debug list of all attempts.
    """
    candidates: list[dict[str, Any]] = []
    for p in patterns:
        info = {
            "id": p["id"],
            "platform": p["platform"],
            "name": p["name"],
            "priority": p["priority"],
            "sender_match": False,
            "subject_match": False,
            "code_match": False,
            "extracted": None,
        }
        candidates.append(info)

        if not _matches_pattern(p["sender_pattern"], sender):
            continue
        info["sender_match"] = True
        if not _matches_pattern(p["subject_pattern"], subject):
            continue
        info["subject_match"] = True

        haystack = _haystack(p["search_in"], subject, body)
        try:
            m = re.search(p["code_pattern"], haystack, flags=re.IGNORECASE | re.DOTALL)
        except re.error:
            continue
        if not m:
            continue

        info["code_match"] = True
        code = m.group(1) if m.groups() else m.group(0)
        info["extracted"] = code

        return ExtractionResult(
            code=code,
            platform=p["platform"],
            matched_pattern_id=p["id"],
            matched_pattern_name=p["name"],
            candidates=candidates,
        )

    return ExtractionResult(
        code=None, platform=None,
        matched_pattern_id=None, matched_pattern_name=None,
        candidates=candidates,
    )
