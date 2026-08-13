"""Fail-closed bilingual left/right semantic comparison.

The local Qwen runtime and Django review paths both need to distinguish a
body-side fact from motion, a spatial placement, and a bilateral formation.
This module deliberately returns only normalized codes, never source prose, so
validation messages remain useful without echoing prompt content.
"""

from __future__ import annotations

from collections import Counter
import re


_ZH_BODY_PART_KEYS = {
    "手腕": "wrist", "手臂": "arm", "手": "hand", "腕": "wrist",
    "脚": "foot", "足": "foot", "腿": "leg", "膝": "knee", "肩": "shoulder",
    "眼": "eye", "眉": "brow", "脸": "face", "颊": "cheek", "耳": "ear",
}
_EN_BODY_PART_KEYS = {
    "hand": "hand", "hands": "hand", "wrist": "wrist", "wrists": "wrist",
    "arm": "arm", "arms": "arm", "foot": "foot", "feet": "foot",
    "leg": "leg", "legs": "leg", "knee": "knee", "knees": "knee",
    "shoulder": "shoulder", "shoulders": "shoulder", "eye": "eye", "eyes": "eye",
    "brow": "brow", "brows": "brow", "eyebrow": "brow", "eyebrows": "brow",
    "face": "face", "faces": "face", "cheek": "cheek", "cheeks": "cheek",
    "ear": "ear", "ears": "ear",
}
_ZH_BODY_PART_PATTERN = "|".join(
    re.escape(part) for part in sorted(_ZH_BODY_PART_KEYS, key=len, reverse=True)
)
_EN_BODY_PART_PATTERN = "|".join(
    re.escape(part) for part in sorted(_EN_BODY_PART_KEYS, key=len, reverse=True)
)
_ZH_BODY_SIDE_RE = re.compile(rf"(?P<side>左|右)(?P<part>{_ZH_BODY_PART_PATTERN})")
_EN_BODY_SIDE_RE = re.compile(
    rf"\b(?P<side>left|right)[\s-]+(?P<part>{_EN_BODY_PART_PATTERN})\b", re.IGNORECASE,
)
_EN_PAIRED_BODY_SIDE_RE = re.compile(
    rf"\b(?P<first>left|right)\s+(?:and|&)\s+(?P<second>left|right)\s+"
    rf"(?P<part>{_EN_BODY_PART_PATTERN})\b", re.IGNORECASE,
)
_ZH_SUBJECT_SPATIAL_RES = (
    re.compile(
        r"(?<![A-Za-z0-9])(?P<subject>S[1-9][0-9]*)(?![A-Za-z0-9])\s*"
        r"(?:在|位于|站在|处于|立于|停在|留在)?\s*(?P<side>左|右)(?:侧|边|方)?"
        r"(?![左右手眼眉肩臂腿脚腕])"
    ),
    re.compile(
        r"(?P<side>左|右)(?:侧|边|方)\s*(?:的)?\s*"
        r"(?P<subject>S[1-9][0-9]*)(?![A-Za-z0-9])"
    ),
)
_EN_SUBJECT_SPATIAL_RES = (
    re.compile(
        r"(?<![A-Za-z0-9])(?P<subject>S[1-9][0-9]*)(?![A-Za-z0-9])\s+"
        r"(?:(?:is|stands|waits|remains|stays|moves|appears)\s+)?"
        r"(?:on|at|to)\s+(?:the\s+)?(?P<side>left|right)(?:\s+side)?\b", re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:on|at|to)\s+(?:the\s+)?(?P<side>left|right)(?:\s+side)?"
        r"[,:]?\s+(?P<subject>S[1-9][0-9]*)(?![A-Za-z0-9])", re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?P<subject>S[1-9][0-9]*)(?![A-Za-z0-9])\s+"
        r"(?:is\s+)?(?P<side>left|right)\s+of\b", re.IGNORECASE,
    ),
)
_ZH_BILATERAL_RES = (
    re.compile(r"两(?:侧|边|旁)"), re.compile(r"一(?:左|右)一(?:左|右)"),
    re.compile(r"(?:从|自|分)?左右(?:包抄|夹击|夹攻|围攻|围拢|合围|逼近|袭来|扑来|分列|展开|散开|夹逼)"),
    re.compile(r"左右(?:两侧|两边|两旁|各)"),
)
_EN_BILATERAL_RES = (
    re.compile(r"\b(?:from|on|at|to)?\s*(?:both|opposite)\s+sides\b", re.IGNORECASE),
    # "from either side" is an alternative route, not a simultaneous two-side
    # arrangement.  Only the locative form carries bilateral placement.
    re.compile(r"\bon\s+either\s+side\b", re.IGNORECASE),
    re.compile(r"\b(?:one\s+on\s+)?each\s+side\b", re.IGNORECASE),
    re.compile(r"\b(?:left|right)\s+(?:and|&)\s+(?:right|left)\b", re.IGNORECASE),
)
_ZH_BILATERAL_MOTION_RES = (
    re.compile(r"(?:镜头)?(?:向|往|朝)?左右(?:来回)?(?:摇|移(?:动)?|转|行|走|奔|跑|冲|闪|退|进|滑|扑|掠|偏)"),
    re.compile(r"(?:从|自|由)(?:左向右|右向左|左到右|右到左)(?:摇|移(?:动)?|转|行|走|奔|跑|冲|闪|退|进|滑|扑|掠|偏)?"),
)
_EN_BILATERAL_MOTION_RES = (
    re.compile(
        r"\b(?:pan(?:s|ned|ning)?|track(?:s|ed|ing)?|truck(?:s|ed|ing)?|"
        r"dolly(?:s|ed|ing)?|move(?:s|d|ing)?|turn(?:s|ed|ing)?|shift(?:s|ed|ing)?|"
        r"sweep(?:s|t|ing)?|walk(?:s|ed|ing)?|run(?:s|ning)?|step(?:s|ped|ping)?|"
        r"slide(?:s|d|ing)?|lunge(?:s|d|ing)?|drift(?:s|ed|ing)?|veer(?:s|ed|ing)?)\s+"
        r"(?:back\s+and\s+forth\s+)?(?:left\s+(?:and|&)\s+right|right\s+(?:and|&)\s+left)\b", re.IGNORECASE,
    ),
    re.compile(r"\bfrom\s+(?:left\s+to\s+right|right\s+to\s+left)\b", re.IGNORECASE),
)
_ZH_MOTION_SIDE_RES = (
    re.compile(r"(?:向|往|朝)(?P<side>左|右)(?:摇|移|转|行|走|奔|跑|冲|闪|退|进|滑|扑|掠|偏|看|望)"),
    re.compile(r"(?P<side>左|右)(?:摇镜头|移镜头|摇|移|转|行|走|奔|跑|冲|闪|退|进|滑|扑|掠|偏)"),
)
_EN_MOTION_SIDE_RES = (
    re.compile(
        r"\b(?:pan(?:s|ned|ning)?|track(?:s|ed|ing)?|truck(?:s|ed|ing)?|"
        r"dolly(?:s|ed|ing)?|move(?:s|d|ing)?|turn(?:s|ed|ing)?|shift(?:s|ed|ing)?|"
        r"sweep(?:s|t|ing)?|walk(?:s|ed|ing)?|run(?:s|ning)?|step(?:s|ped|ping)?|"
        r"slide(?:s|d|ing)?|lunge(?:s|d|ing)?|drift(?:s|ed|ing)?|veer(?:s|ed|ing)?|"
        r"look(?:s|ed|ing)?|glance(?:s|d|ing)?)\s+(?:(?:to|toward|towards)\s+)?"
        r"(?:the\s+)?(?P<side>left|right)\b", re.IGNORECASE,
    ),
    re.compile(r"\b(?P<side>left|right)(?:ward|wards)\b", re.IGNORECASE),
)
_ZH_SPATIAL_SIDE_RE = re.compile(r"(?P<side>左|右)(?:侧|边|方)")
_EN_SPATIAL_SIDE_RES = (
    re.compile(r"\b(?:on|at|to|from)\s+(?:the\s+)?(?P<side>left|right)\s+side\b", re.IGNORECASE),
    re.compile(r"\b(?P<side>left|right)\s+side\b", re.IGNORECASE),
    re.compile(r"\b(?:on|at|to)\s+(?:the\s+)?(?P<side>left|right)\b", re.IGNORECASE),
    re.compile(r"\b(?:the\s+)?(?P<side>left|right)\s+of\b", re.IGNORECASE),
)


def _claim(value: str, claimed: list[bool], patterns, add) -> None:
    for pattern in patterns:
        for match in pattern.finditer(value):
            if any(claimed[index] for index in range(match.start(), match.end())):
                continue
            for index in range(match.start(), match.end()):
                claimed[index] = True
            add(match)


def _semantics(value: str, *, english: bool) -> dict[str, Counter[str]]:
    result = {
        "subject_spatial_side": Counter(), "body_side": Counter(),
        "motion_direction": Counter(), "spatial_side": Counter(),
        "bilateral_sides": Counter(), "bilateral_motion": Counter(),
    }
    claimed = [False] * len(value)
    if english:
        _claim(value, claimed, _EN_SUBJECT_SPATIAL_RES, lambda m: result["subject_spatial_side"].update([
            f"{m.group('subject')}:{m.group('side').casefold()}",
        ]))
        _claim(value, claimed, [_EN_PAIRED_BODY_SIDE_RE], lambda m: result["body_side"].update([
            f"{m.group('first').casefold()}_{_EN_BODY_PART_KEYS[m.group('part').casefold()]}",
            f"{m.group('second').casefold()}_{_EN_BODY_PART_KEYS[m.group('part').casefold()]}",
        ]))
        _claim(value, claimed, [_EN_BODY_SIDE_RE], lambda m: result["body_side"].update([
            f"{m.group('side').casefold()}_{_EN_BODY_PART_KEYS[m.group('part').casefold()]}",
        ]))
        _claim(value, claimed, _EN_BILATERAL_MOTION_RES, lambda _m: result["bilateral_motion"].update(["bilateral_motion"]))
        _claim(value, claimed, _EN_BILATERAL_RES, lambda _m: result["bilateral_sides"].update(["bilateral_sides"]))
        _claim(value, claimed, _EN_MOTION_SIDE_RES, lambda m: result["motion_direction"].update([m.group("side").casefold()]))
        _claim(value, claimed, _EN_SPATIAL_SIDE_RES, lambda m: result["spatial_side"].update([m.group("side").casefold()]))
    else:
        _claim(value, claimed, _ZH_SUBJECT_SPATIAL_RES, lambda m: result["subject_spatial_side"].update([
            f"{m.group('subject')}:{'left' if m.group('side') == '左' else 'right'}",
        ]))
        _claim(value, claimed, [_ZH_BODY_SIDE_RE], lambda m: result["body_side"].update([
            f"{'left' if m.group('side') == '左' else 'right'}_{_ZH_BODY_PART_KEYS[m.group('part')]}",
        ]))
        _claim(value, claimed, _ZH_BILATERAL_MOTION_RES, lambda _m: result["bilateral_motion"].update(["bilateral_motion"]))
        _claim(value, claimed, _ZH_BILATERAL_RES, lambda _m: result["bilateral_sides"].update(["bilateral_sides"]))
        _claim(value, claimed, _ZH_MOTION_SIDE_RES, lambda m: result["motion_direction"].update([
            "left" if m.group("side") == "左" else "right",
        ]))
        _claim(value, claimed, [_ZH_SPATIAL_SIDE_RE], lambda m: result["spatial_side"].update([
            "left" if m.group("side") == "左" else "right",
        ]))
    pair_count = min(result["spatial_side"]["left"], result["spatial_side"]["right"])
    if pair_count:
        result["spatial_side"]["left"] -= pair_count
        result["spatial_side"]["right"] -= pair_count
        if not result["spatial_side"]["left"]:
            del result["spatial_side"]["left"]
        if not result["spatial_side"]["right"]:
            del result["spatial_side"]["right"]
        result["bilateral_sides"]["bilateral_sides"] += pair_count
    return result


def direction_semantic_mismatches(zh: str, en: str) -> list[tuple[str, list[str], list[str]]]:
    """Return category/code mismatches using presence semantics, not counts."""
    zh_values = _semantics(zh, english=False)
    en_values = _semantics(en, english=True)
    mismatches = []
    for category in (
        "subject_spatial_side", "body_side", "motion_direction", "spatial_side",
        "bilateral_sides", "bilateral_motion",
    ):
        zh_codes, en_codes = sorted(zh_values[category]), sorted(en_values[category])
        if set(zh_codes) != set(en_codes):
            mismatches.append((category, zh_codes, en_codes))
    return mismatches


def direction_semantic_presence(value: str, *, english: bool) -> dict[str, list[str]]:
    """Expose normalized, text-free direction facts for advisory review.

    This is intentionally a read-only projection of the same parser used by
    the bilingual mismatch check.  Callers can flag a duplicated or misplaced
    bilateral fact without retaining or echoing the underlying prose.
    """
    return {
        category: sorted(counter.elements())
        for category, counter in _semantics(str(value or ""), english=english).items()
    }
