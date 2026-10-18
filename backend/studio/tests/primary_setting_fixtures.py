"""Explicit isolated authored settings; never convert existing business data."""
from studio.models import CharacterBible, SceneBible, PropBible
from studio.services.bible_variants import create_variant, revise_variant, setting_payload


def create_primary_setting(entity, identity, appearance=""):
    kind = next(key for key, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible))
        if isinstance(entity, model))
    return create_variant(actor=entity.project.owner, project_id=entity.project_id,
        entity_kind=kind, entity_id=entity.pk, appearance_role="primary",
        primary_setting_revision_id=None, primary_setting_sha256="", value={
            "name": "Primary", "reference_name": f"{entity.slug} primary", "identity_anchor": identity,
            "appearance_anchor": appearance, "basis_asset_id": None, "basis_asset_sha256": ""})


def revise_primary_setting(variant, **changes):
    variant.refresh_from_db()
    setting = variant.revisions.get(revision=variant.revision)
    return revise_variant(actor=variant.project.owner, variant=variant, expected_revision=variant.revision,
        value={**setting_payload(setting), **changes})
