"""Explicit test configurations; never imported by runtime code."""
from copy import deepcopy
from importlib import import_module

FRAME_POLICY = {"fps": 24, "minimum_frames": 5, "maximum_frames": 3600, "alignment_modulus": 17, "alignment_remainder": 5}


def rendering_config():
    rendering = deepcopy(import_module("studio.migrations.0140_explicit_reference_rendering_drafts").RENDERING)
    rendering.update(task_separator=" + ", unreferenced_task="reference generation", reference_rules=[
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
