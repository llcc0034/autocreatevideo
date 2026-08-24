from uuid import uuid4

from django.contrib.auth import get_user_model
from django.utils import timezone

from studio.models import (
    Asset,
    CharacterBible,
    Episode,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelProfileVersion,
    ProviderProfile,
    RuntimeHost,
    SceneBible,
    SeriesProject,
    Shot,
    Style,
    PromptFieldDefinition,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    WorkflowTemplate,
)
from studio.services.deployment_smoke import record_deployment_capability_snapshot
from studio.services.h3_prompts import SDXL_BASE_MODEL_ID
from studio.services.json_contracts import assign_json_contract
from studio.services.production_pipeline import (
    BIBLE_IMAGE_MODEL_CHECKPOINT,
    BIBLE_IMAGE_PROMPT_TEMPLATE_KEY,
    BIBLE_IMAGE_TEMPLATE_SLUG,
)
from studio.services.prompt_normalization_contract import PROMPT_NORMALIZE_OPERATION
from studio.services.system_workflow_contracts import approved_minimax_reference_image_definition
from studio.services.prompt_profiles import (
    model_family_for_profile,
    publish_prompt_profile,
    refresh_prompt_profile_contract,
)


def create_test_prompt_profile_binding(
    *,
    actor,
    model_profile_version,
    operation: str,
    mode: str = "",
) -> PromptProfileBinding:
    """Create one explicit reviewed PromptProfile for a production test route."""

    family = model_family_for_profile(model_profile_version)
    suffix = f"{model_profile_version.id.hex[:12]}-{operation.replace('.', '-')}-{mode or 'default'}"
    definition = PromptFieldDefinition.objects.create(
        key=f"test_policy_{suffix}"[:191],
        label="Reviewed test policy",
        category="test",
        description="Explicit test-only policy required by the production PromptProfile gate.",
        impact="Affects the frozen model request in this test only.",
        value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        default_value="Use the strict fixture contract without adding unsupported facts.",
    )
    profile = PromptProfileVersion.objects.create(
        profile_key=f"test.{suffix}"[:191],
        version=1,
        name=f"Test {operation} {mode or 'default'}",
        model_family=family,
        operation=operation,
        mode=mode,
        review_state=PromptProfileVersion.ReviewState.READY,
    )
    PromptProfileFieldRule.objects.create(
        profile_version=profile,
        field_definition=definition,
        source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
        channel=(
            PromptProfileFieldRule.Channel.POSITIVE_PROMPT
            if operation in {"image.generate", "video.generate"}
            else PromptProfileFieldRule.Channel.SYSTEM_INSTRUCTION
        ),
        position=1,
        max_chars=4000,
    )
    refresh_prompt_profile_contract(profile)
    profile = publish_prompt_profile(profile, actor=actor)
    return PromptProfileBinding.objects.create(
        key=f"test-binding-{suffix}"[:191],
        name=f"Test binding {operation} {mode or 'default'}",
        operation=operation,
        model_family=family,
        mode=mode,
        model_profile_version=model_profile_version,
        prompt_profile_version=profile,
        is_active=True,
    )


def create_ready_prompt_normalization_test_deployment() -> ModelDeployment:
    """Create one exact, selectable prompt.normalize route for API tests.

    Tests that exercise translation orchestration mock the actual model call, but
    their configuration still has to reference a real active deployment with a
    published capability contract and fresh smoke evidence.  Keeping that
    control-plane state realistic prevents a random UUID from bypassing the
    production resource-reference gate.
    """

    suffix = uuid4().hex[:12]
    host = RuntimeHost.objects.create(
        key=f"test-normalizer-host-{suffix}",
        name="Prompt normalization test host",
    )
    provider = ProviderProfile(
        key=f"test-normalizer-provider-{suffix}",
        name="Prompt normalization test provider",
        driver_type=ProviderProfile.DriverType.LOCAL_QWEN,
        modalities=ModelProfileVersion.Modality.TEXT,
        operations=PROMPT_NORMALIZE_OPERATION,
        is_active=True,
    )
    assign_json_contract(provider, "driver_config", {
        "runtime_source": "openai_compatible_endpoint",
        "base_url": "http://127.0.0.1:9123",
        "chat_path": "/v1/chat/completions",
        "timeout_seconds": 10,
    })
    provider.save()
    profile = ModelProfileVersion(
        profile_key=f"test-normalizer-profile-{suffix}",
        version=1,
        provider=provider,
        model_identifier=f"test-normalizer-{suffix}",
        modality=ModelProfileVersion.Modality.TEXT,
        status=ModelProfileVersion.Status.PUBLISHED,
        published_at=timezone.now(),
    )
    assign_json_contract(profile, "specification", {
        "schema_version": 1,
        "capabilities": [PROMPT_NORMALIZE_OPERATION],
    })
    profile.save()
    deployment = ModelDeployment(
        key=f"test-normalizer-{suffix}",
        name="Prompt normalization test deployment",
        profile_version=profile,
        runtime_host=host,
        status=ModelDeployment.Status.ACTIVE,
    )
    assign_json_contract(deployment, "runtime_config", {})
    deployment.save()
    record_deployment_capability_snapshot(
        deployment,
        probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
        status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
        operations=[PROMPT_NORMALIZE_OPERATION],
        evidence={
            "schema_version": 1,
            "execution_submitted": True,
            "test_fixture": True,
        },
    )
    return deployment


def activate_reviewed_bible_image_template() -> WorkflowTemplate:
    """Install the explicit reviewed fixture required by candidate pipeline tests."""

    template, _ = WorkflowTemplate.objects.update_or_create(
        owner=None,
        slug=BIBLE_IMAGE_TEMPLATE_SLUG,
        version=1,
        defaults={
            "name": "Reviewed Bible reference image fixture",
            "provider": "comfyui",
            "output_kind": WorkflowTemplate.OutputKind.IMAGE,
            "model": BIBLE_IMAGE_MODEL_CHECKPOINT,
            "definition": approved_minimax_reference_image_definition(),
            "input_schema": {
                "schema_version": 1,
                "workflow_mode": "text_to_image",
                "conditioning_mode": "text_and_bible_anchors_only",
                "pixel_reference_inputs": False,
                "fixed_model": True,
                "model_id": SDXL_BASE_MODEL_ID,
                "prompt_template_keys": [BIBLE_IMAGE_PROMPT_TEMPLATE_KEY],
                "allowed_overrides": [
                    "SEED", "RESOLUTION_PROFILE", "RESOLUTION_MULTIPLE",
                    "WIDTH", "HEIGHT",
                ],
                "required_dimension_multiple": 8,
                "recommended_resolution_profile": "0.98",
                "recommended_megapixels_min": 0.8,
                "low_resolution_smoke_profiles": ["0.1", "0.2", "0.3"],
                "model_file": {
                    "filename": BIBLE_IMAGE_MODEL_CHECKPOINT,
                    "sha256": "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b",
                },
            },
            "is_system": True,
            "is_active": True,
        },
    )
    return template


def build_story(username="alice"):
    user = get_user_model().objects.create_user(username=username, password="StrongPass123!")
    style = Style.objects.create(
        slug=f"noir-{username}", name="黑色侦探", category=Style.Category.DETECTIVE,
        visual_prompt="rainy neo-noir, amber practical lights",
        negative_prompt="face drift, costume drift",
    )
    project = SeriesProject.objects.create(
        owner=user, title="雨夜证词", style=style, continuity_seed=42,
        logline="侦探在停电前找出说谎的人。",
    )
    portrait = Asset.objects.create(
        owner=user, style=style, kind=Asset.Kind.CHARACTER, name="林默标准像",
        local_path="C:/fixtures/lin.png", checksum="fixture-lin", mime_type="image/png",
    )
    scene_reference = Asset.objects.create(
        owner=user, style=style, kind=Asset.Kind.SCENE, name="档案室标准景",
        local_path="C:/fixtures/archive.png", checksum="fixture-archive", mime_type="image/png",
    )
    character = CharacterBible.objects.create(
        project=project, slug="lin", name="林默", stable_speaker_id="S1",
        visual_identity="三十岁短发女侦探，左眼下小痣", wardrobe="炭灰风衣",
        voice_profile="冷静低声", face_identity="高颧骨，深棕眼睛",
        anchor_asset=portrait,
    )
    scene = SceneBible.objects.create(
        project=project, slug="archive", name="旧档案室",
        visual_identity="狭窄高架档案室，尘埃漂浮", lighting="绿色台灯和窗外闪电",
        ambient_sound="雨打玻璃，老钟滴答",
        anchor_asset=scene_reference,
    )
    episode = Episode.objects.create(project=project, number=1, title="停电前", synopsis="线索浮现。")
    shot1 = Shot.objects.create(
        episode=episode, number=1, title="证物", narrative="林默展开烧焦的信纸。",
        camera="俯拍信纸后推近面部", dialogue=[{"speaker_id": "S1", "text": "这封信不是昨晚烧的。"}],
        scene=scene, duration_ms=3500, sound_effects="纸张摩擦", music="低音弦乐",
    )
    shot1.characters.add(character)
    shot2 = Shot.objects.create(
        episode=episode, number=2, title="闪电", narrative="闪电照亮门后的人影。",
        camera="快速摇镜到门口", dialogue=[{"speaker_id": "S1", "text": "别动。"}],
        scene=scene, duration_ms=2100, sound_effects="近雷炸响",
    )
    shot2.characters.add(character)
    return user, project, character, scene, episode, shot1, shot2
