"""Explicit renderer test facts and configurations; never imported by runtime code."""
from copy import deepcopy
from importlib import import_module

FRAME_POLICY = {"fps": 24, "minimum_frames": 5, "maximum_frames": 3600, "alignment_modulus": 17, "alignment_remainder": 5}


def generation_structure_fixture(profile, *, properties=("direction",)):
    """Publish an explicit custom input fixture, not a map from legacy columns."""
    from studio.tests.field_definition_fixtures import create_field_definition
    from studio.services.data_structures import write_data_structure, transition_data_structure

    nodes = []
    for position, (key, kind, parent) in enumerate([("root", "object", None), *[(key, "text", "root") for key in properties]]):
        field = create_field_definition(key=f"generation_{profile.pk.hex}_{key}", label=key, value_type=kind)
        nodes.append({"node_key": key, "parent_key": parent,
            "field_definition_version": str(field._fixture_version.pk),
            **({"property_name": key, "required": True, "position": position} if parent else {})})
    structure = write_data_structure(content={"name": "Explicit unit input fixture", "nodes": nodes})
    profile.input_structure_version = transition_data_structure(version_id=structure.pk, status="published")
    profile.save(update_fields=("input_structure_version",))


def profile_compilation_fixture(*, sources, reference_context=None, reviewed_values=None,
                                generation_contributions=None, source_language="Chinese", target_language="English"):
    """Only construct explicit compiler inputs; no source lookup or approval."""
    from studio.services.prompt_profile_compiler import PromptProfileCompilationFacts

    return PromptProfileCompilationFacts(
        sources=deepcopy(sources), source_language=source_language, target_language=target_language,
        reference_context=deepcopy(reference_context),
        reviewed_values=deepcopy({} if reviewed_values is None else reviewed_values),
        generation_contributions=deepcopy({} if generation_contributions is None else generation_contributions),
    )


def reference_unit_fixture(**values):
    """Empty explicit test unit; never read a Shot or infer dialogue/subjects."""
    from studio.services.reference_context_facts import ReferenceUnitFacts
    from studio.services.reference_rendering_inputs import UNIT_RENDER_SLOTS

    return ReferenceUnitFacts(**deepcopy({
        "unit_key": "fixture-unit", "duration_ms": 3500,
        "render_values": {key: "" for key in UNIT_RENDER_SLOTS},
        "subject_keys": (), "dialogue": (), "voice_profiles": (), "continuity_notes": "",
        "requires_first_frame_continuity": False, "requires_last_frame_continuity": False,
        "ambient_sound": "", **values,
    }))


def reference_context_fixture(*, ordered_units, **values):
    """Explicit renderer test data; no owner, ORM, translation or selection."""
    from studio.services.reference_context_facts import ReferenceContextFacts
    from studio.tests.factories import conditioning_contract

    return ReferenceContextFacts(**deepcopy({
        "prompt_type": "video", "mode": "T2VA", "include_audio": True, "language": "Chinese",
        "ordered_units": ordered_units, "subject_facts": (), "reference_bindings": (),
        "conditioning_mode_contract": conditioning_contract("T2VA")["modes"][0],
        "frame_policy": FRAME_POLICY, "color_script": "", "extra_direction": "",
        "overall_soundscape": "", "non_diegetic_music": "", "generation_contributions": {}, **values,
    }))


def rendering_config():
    rendering = deepcopy(import_module("studio.migrations.0140_explicit_reference_rendering_drafts").RENDERING)
    rendering.update(unit_text_bindings={}, task_separator=" + ", unreferenced_task="reference generation", reference_rules=[
        {"category": category, "purpose": purpose, "task": task, "relationship": relationship, "usage": usage}
        for category, purpose, task, relationship, usage in (
            ("character", "", "reference generation", "fully_preserved", "Preserve the referenced character identity."),
            ("scene", "", "reference generation", "fully_preserved", "Preserve the referenced environment."),
            ("prop", "", "reference generation", "fully_preserved", "Preserve the referenced prop."),
            ("continuity_frame", "", "keyframe completion", "fully_preserved", "Begin the shot from this frame."),
            ("first_frame", "", "keyframe completion", "fully_preserved", "Begin the shot from this frame."),
            ("last_frame", "", "keyframe completion", "fully_preserved", "End the shot on this frame."),
            ("video", "motion_camera_reference", "reference generation", "weak_reference", "Use the declared camera motion and timing as guidance, not a replacement story."),
            ("video", "video_editing", "video editing", "partially_preserved", "Edit this source video according to the declared changes."),
            ("video", "video_continuation", "video continuation", "fully_preserved", "Continue from the final state of this source video."),
            ("audio", "audio_reference", "audio reference", "reference", "Use the declared audio characteristics as guidance without copying the signal."),
            ("audio", "audio_reuse", "audio reuse", "partially_copy", "Reuse the selected audio in the target timeline alongside other declared sound layers."),
        )
    ])
    rendering["templates"].update({
        "entity_reference": "{subject} from {label}: {description}. {guidance}",
        "retention": "{label} (shots {shots}): {relationship} - {usage} {guidance}",
        "reference_usage": "{label}: {usage} {guidance}",
        "summary": "[{task_types}] Generate {shot_count} shots over {duration_seconds} seconds. Mode: {mode}; {reference_count} supplied references. {reference_relationships}",
        "shot_opening": "[Shot {index}]",
        "shot_transition": "[Shot {index}] At {timecode},",
    })
    rendering["templates"]["shot"] = "{shot_marker} " + rendering["templates"]["shot"].split("seconds. ", 1)[1] + "\n{reference_usage}"
    rendering["templates"]["reviewed_shot"] = "{shot_marker} Subjects: {subject_labels}.\n{description}\n{reference_usage}\nFirst-frame continuity required: {requires_first_frame_continuity}; last-frame continuity required: {requires_last_frame_continuity}.\n{dialogue}"
    return {"schema_version": 5, "primitive_options": {"reference_context_ir_v1": {
        "rendering": rendering,
    }}, "quality_advisories": {
        "schema_version": 1,
        "positive_negative_token_overlap": {"minimum_shared_tokens": 6, "minimum_overlap_ratio": 0.65},
        "short_shot_action_density": {"maximum_duration_ms": 5500, "maximum_action_characters": 420, "minimum_clause_separators": 6},
        "motion_camera_reference_guidance": {"minimum_english_words": 5, "generic_values": ["workflow smoke fixture"]},
    }}


# Explicit legacy input examples used only by migration/contract tests.
LEGACY_H3_SELECTOR_TO_PRIMITIVE = {
    "request.h3_ref2va.subject_definitions": (
        "reference_context.subject_definitions"
    ),
    "request.h3_ref2va.summary": "reference_context.summary",
    "request.h3_ref2va.retention_analysis": (
        "reference_context.retention_analysis"
    ),
    "request.h3_ref2va.detailed_description": (
        "reference_context.detailed_description"
    ),
    "request.h3_ref2va.overall_soundscape": (
        "reference_context.overall_soundscape"
    ),
    "request.h3_ref2va.non_diegetic_music": (
        "reference_context.non_diegetic_music"
    ),
}
