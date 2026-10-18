"""Carry the common M5 reader's exact use into the original execution facts.

This is not a selection, approval or current-version lookup. The preparation
producer still owns review/currentness and its original write transaction.
Frozen consumers validate this evidence without reading today's library.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.services.production_reference_mapping import reference_use_records, reviewed_reference_item_key


def _is_shot_use(scope, item_key):
    return isinstance(scope, dict) and scope.get("kind") == "shot" and item_key.startswith("reviewed:")


def resolve_execution_shot_reference(item, *, spec, owner, project_id, asset_id, scope, media_facts, cache):
    if not _is_shot_use(scope, item["item_key"]):
        return None
    from studio.services.shot_reference_sources import resolve_shot_reference_sources
    cache_key = ("shot_uses", owner.pk, str(project_id), scope["id"])
    if cache_key not in cache:
        resolved = resolve_shot_reference_sources(actor=owner, project_id=project_id, shot_ids=[scope["id"]])
        uses = resolved.value["shots"][0]["uses"]
        cache[cache_key] = list(zip(uses, reference_use_records(uses)))
    matches = [(use, record) for use, record in cache[cache_key] if reviewed_reference_item_key(
        input_key=spec["key"], shot_id=scope["id"], reference_key=record["reference_key"]) == item["item_key"]]
    if (len(matches) != 1 or matches[0][1]["kind"] != spec["kind"]
            or matches[0][1]["source"] != {"kind": "asset", "asset_id": asset_id}
            or matches[0][1]["subject_ref"] != item.get("subject_ref")
            or matches[0][0]["asset"] != {"asset_id": asset_id,
                "sha256": media_facts["sha256"], "mime_type": media_facts["mime_type"]}):
        raise ValidationError({f"inputs.{spec['key']}":
            "请求主体及素材与本镜确切用途选择不一致，请重新读取；不能用主图或其他用途替代"})
    return deepcopy(matches[0][0])


def validate_frozen_shot_reference(row):
    """Check shape and cross-links, not authority from a caller-rehashed object."""
    def fail():
        raise ValidationError({"workflow_inputs": "冻结分镜素材来源缺失或与本次使用不一致；请重新准备，不读取当前版本补齐"})

    def identity(value):
        try:
            valid = isinstance(value, str) and str(UUID(value)) == value
        except ValueError:
            valid = False
        if not valid:
            fail()

    def digest(value):
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            fail()

    expected = _is_shot_use(row.get("scope_ref"), row["item_key"])
    if not expected:
        if "shot_reference" in row:
            fail()
        return
    use = row.get("shot_reference")
    if (row.get("origin") != {"kind": "asset", "asset_id": row.get("asset_id")}
            or not isinstance(use, dict) or set(use) != {"use_id", "use_key", "position", "media_kind",
                "purpose", "setting", "source_mode", "source", "asset"}):
        fail()
    identity(use["use_id"])
    if (not isinstance(use["use_key"], str) or not use["use_key"]
            or type(use["position"]) is not int or use["position"] < 0
            or use["media_kind"] != row["kind"] or not isinstance(use["purpose"], str)
            or use["source_mode"] not in ("existing", "upload", "generated")):
        fail()
    if reviewed_reference_item_key(input_key=row["input_key"], shot_id=row["scope_ref"].get("id"),
            reference_key="shot-use:" + use["use_key"]) != row["item_key"]:
        fail()
    identity(row["asset_id"])
    identity(row["scope_ref"].get("id"))
    if use["asset"] != {"asset_id": row["asset_id"], "sha256": row["media_facts"].get("sha256"),
            "mime_type": row["media_facts"].get("mime_type")}:
        fail()
    digest(use["asset"]["sha256"])
    source = use["source"]
    if not isinstance(source, dict):
        fail()
    if source.get("kind") == "asset":
        if source != {"kind": "asset", "asset_id": row["asset_id"]} or use["source_mode"] == "generated":
            fail()
    elif source.get("kind") == "reference_version":
        if (set(source) != {"kind", "reference_version_id", "series_id", "version", "acquisition_source"}
                or type(source["version"]) is not int or source["version"] < 1
                or source["acquisition_source"] not in ("existing", "upload", "generated")
                or (use["source_mode"] == "generated" and source["acquisition_source"] != "generated")):
            fail()
        identity(source["reference_version_id"])
        identity(source["series_id"])
    else:
        fail()
    setting, subject = use["setting"], row["subject"]
    if setting is None:
        if subject is not None:
            fail()
        return
    if (not isinstance(setting, dict) or set(setting) != {"appearance_id", "setting_revision_id", "revision",
            "content_sha256", "entity_kind", "entity_id", "appearance_role", "reference_name"}
            or type(setting["revision"]) is not int or setting["revision"] < 1
            or setting["entity_kind"] not in ("character", "scene", "prop")
            or setting["appearance_role"] not in ("primary", "variant")
            or not isinstance(setting["reference_name"], str) or not setting["reference_name"].strip()
            or subject is None or (setting["entity_kind"], setting["entity_id"]) != (subject["kind"], subject["id"])):
        fail()
    for field in ("appearance_id", "setting_revision_id", "entity_id"):
        identity(setting[field])
    digest(setting["content_sha256"])
