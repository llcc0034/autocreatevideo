"""Protect reserved ScriptBible provenance after the legacy parser's retirement.

No parser, renderer, entity matching or provenance-writing entry point remains.
"""
from __future__ import annotations

import json


SCRIPT_BIBLE_METADATA_KEY = "script_bible_v1"


class ScriptBibleProvenanceProtectedError(ValueError):
    """An untrusted writer attempted to change reserved provenance."""


def validate_untrusted_script_bible_metadata(
    previous_metadata: object,
    incoming_metadata: object,
) -> None:
    """Reject forged or changed reserved provenance after the old writer's retirement."""

    previous = previous_metadata if isinstance(previous_metadata, dict) else {}
    incoming = incoming_metadata if isinstance(incoming_metadata, dict) else {}
    previous_has = SCRIPT_BIBLE_METADATA_KEY in previous
    incoming_has = SCRIPT_BIBLE_METADATA_KEY in incoming
    previous_provenance = previous.get(SCRIPT_BIBLE_METADATA_KEY)
    incoming_provenance = incoming.get(SCRIPT_BIBLE_METADATA_KEY)
    same_provenance = False
    if previous_has and incoming_has:
        try:
            same_provenance = json.dumps(
                previous_provenance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) == json.dumps(
                incoming_provenance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            same_provenance = False
    if previous_has != incoming_has or (previous_has and not same_provenance):
        raise ScriptBibleProvenanceProtectedError(
            "metadata.script_bible_v1 是已停用的旧来源记录，不能注入、修改或删除。"
        )
