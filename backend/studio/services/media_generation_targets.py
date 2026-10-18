"""Media targets: generated requests or fulfillment by one exact existing Asset.

Generated facts live in the original Preparation. Fulfillment writes only the
original AssetReferenceVersion relations, never a synthetic job or preparation.
Neither a setting reference nor an uploaded variant proves a model used a primary.
"""
from copy import deepcopy
from dataclasses import dataclass, replace

from django.core.exceptions import ValidationError
from django.db.models import Q

from studio.models import Asset, AssetReferenceVersion, BibleVariantRevision
from studio.services.asset_provenance import is_uploaded_asset
from studio.services.asset_visibility import exclude_workflow_smoke_fixture_assets
from studio.services.bible_setting_evidence import primary_setting_content_reference, read_setting_revisions
from studio.services.execution_catalog_readiness import output_kind
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_media_sources import resolve_execution_asset_media
from studio.services.execution_preparations import ResolvedExecutionPreparation
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.json_contracts import json_contract


MEDIA_KINDS = frozenset({"image", "video", "audio"})


@dataclass(frozen=True)
class ResolvedMediaFulfillmentTarget:
    """Transient validation result; the writer persists the original FKs only."""
    asset: Asset
    setting: BibleVariantRevision | None
    source_mode: str
    appearance_role: str
    media_type: str
    asset_sha256: str
    generation_origin: dict | None = None

    @property
    def value(self):
        # A small transient projection of typed identities, not a stored JSON
        # payload. Fulfillment does not need an execution configuration gate.
        value = {"schema_version": 1, "source_mode": self.source_mode,
            "appearance_role": self.appearance_role, "media_type": self.media_type,
            "execution_instance_version_id": None, "definition_sha256": None,
            "candidates": None, "inputs_sha256": None,
            "setting": primary_setting_content_reference(self.setting),
            "asset": {"asset_id": str(self.asset.pk), "sha256": self.asset_sha256},
            "generated_from": None}
        if self.generation_origin is not None:
            value["generated_from"] = self.generation_origin["generated_from_id"]
            value["generation_origin"] = self.generation_origin
        return value


def resolve_media_fulfillment_target(*, series, asset_id, source_mode, setting_revision_id=None,
        expected_media_type=None):
    """Resolve a zero-call target in its authorized owner's write transaction.

    The caller authorizes/locks the real series; byte and setting readers here
    remain the same ones used by execution. A related primary series is only an
    organizational relationship, not a generated-from fact or required input.
    """
    if source_mode not in (AssetReferenceVersion.Source.UPLOAD, AssetReferenceVersion.Source.EXISTING):
        raise ValidationError({"source": "本入口仅关联上传或已有素材，不能声明模型生成事实"})
    if expected_media_type is not None and expected_media_type not in MEDIA_KINDS:
        raise ValidationError({"asset_id": "目标媒体类型未登记"})
    series.clean()
    assets = exclude_workflow_smoke_fixture_assets(Asset.objects.filter(
        Q(owner_id=series.owner_id) | Q(owner__isnull=True)))
    asset = assets.filter(pk=asset_id).first()
    if asset is None:
        raise ValidationError({"asset_id": "引用不存在或不属于当前可访问范围"})
    if not asset.is_active:
        raise ValidationError({"asset_id": "素材已停用"})
    kind = asset.mime_type.partition("/")[0]
    if kind not in MEDIA_KINDS:
        raise ValidationError({"asset_id": "主图/变体系列需要图片、视频或音频素材"})
    if expected_media_type is not None and kind != expected_media_type:
        raise ValidationError({"asset_id": "所选素材类型不符合本次目标要求"})
    try:
        media = resolve_execution_asset_media(owner=series.owner, asset_id=asset.pk,
            expected_kind=kind, max_media_bytes=None)
    except ValidationError as exc:
        raise ValidationError({"asset_id": exc.messages}) from exc
    asset = media.asset  # Use the re-authorized original Asset, not a file copy.
    if source_mode == AssetReferenceVersion.Source.UPLOAD and not is_uploaded_asset(asset):
        raise ValidationError({"source": "所选Asset不是正式上传来源；请使用已有素材关联"})
    if media.media_facts["mime_type"] != asset.mime_type:
        raise ValidationError({"asset_id": "素材登记MIME与实际文件不一致，不能作为目标结果"})
    setting = None
    if setting_revision_id is not None:
        if not BibleVariantRevision.objects.filter(pk=setting_revision_id,
                variant_id=series.bible_variant_id).exists():
            raise ValidationError({"setting_revision_id": "引用不存在或不属于本系列明确关联的设定"})
        setting = read_setting_revisions([setting_revision_id],
            project_id=series.bible_variant.project_id)[str(setting_revision_id)]
    # No current-setting inference, candidate policy, execution instance or model
    # inputs are invented for fulfillment. The observed hash is a use-time check,
    # not a replacement historical file manifest or a copied setting snapshot.
    return ResolvedMediaFulfillmentTarget(asset, setting, source_mode, series.role, kind, media.media_facts["sha256"])


def read_media_fulfillment_version(version, *, expected_media_type=None):
    """Reuse an exact library version without manufacturing a new association.

    Selection policy is separate from original provenance. Generated versions
    must pass the original registration reader, including the frozen request's
    actual primary basis; a new current primary never replaces that basis.
    """
    version.clean()
    if version.source != AssetReferenceVersion.Source.GENERATED:
        if version.generated_artifact_id or version.generation_job_id or version.generated_from_id:
            raise ValidationError("非生成素材版本不能携带生成来源")
        return resolve_media_fulfillment_target(series=version.series, asset_id=version.asset_id,
            source_mode=version.source, setting_revision_id=version.setting_revision_id,
            expected_media_type=expected_media_type)
    if bool(version.generated_artifact_id) == bool(version.generation_job_id):
        raise ValidationError("生成版本缺少唯一原产物或独立任务依据")
    from studio.services.asset_references import read_generated_material
    original = read_generated_material(artifact=version.generated_artifact, job=version.generation_job)
    if original is None or original.pk != version.pk:
        raise ValidationError("生成素材缺少原登记版本，不能重新关联或猜测来源")
    resolved = resolve_media_fulfillment_target(series=version.series, asset_id=version.asset_id,
        source_mode=AssetReferenceVersion.Source.EXISTING, setting_revision_id=version.setting_revision_id,
        expected_media_type=expected_media_type)
    return replace(resolved, source_mode=version.source, generation_origin={
        "reference_version_id": str(version.pk),
        "generated_artifact_id": str(version.generated_artifact_id) if version.generated_artifact_id else None,
        "generation_job_id": str(version.generation_job_id) if version.generation_job_id else None,
        "generated_from_id": str(version.generated_from_id) if version.generated_from_id else None})


def _candidate_policy(count, evaluator):
    # Count describes the original candidate batch, not retry/append ordinals.
    # A null evaluator means no automatic evaluator was configured; it is not
    # evidence of a human decision, even for a single-result asset request.
    if type(count) is not int or count < 1:
        raise ValidationError("生成目标需要明确的正整数候选数")
    if evaluator not in (None, "first_succeeded"):
        raise ValidationError("生成目标的候选选择策略未登记，不推断质量排名或人工采用")
    return {"count": count, "evaluator": evaluator}


def _target_value(definition, resolved_inputs, *, candidate_count, candidate_evaluator, appearance=None, request=None):
    kind = output_kind(definition)
    if kind not in MEDIA_KINDS:
        raise ValidationError("共用媒体生成目标只能引用实际图片、视频或音频输出合同")
    inputs = resolved_inputs.get("inputs")
    if not isinstance(inputs, dict):
        raise ValidationError("生成目标缺少原准备解析输入，不从当前配置补造来源")
    # The original Bible target is a writeback baseline. It does NOT prove that
    # a selected primary Asset was mapped to an image input (M4-02/M4-04).
    bible = resolved_inputs.get("bible_target")
    if bible is not None and (not isinstance(bible, dict) or "primary_setting" not in bible):
        raise ValidationError("生成目标缺少完整的原Bible设定基线")
    setting = None if bible is None else deepcopy(bible["primary_setting"])
    if appearance is not None:
        from studio.services.media_generation_appearances import verify_media_appearance_target
        if kind != "image":
            raise ValidationError("主图/变体生成声明需要实际图片输出合同")
        verify_media_appearance_target(appearance, inputs, definition=definition, resolved_inputs=resolved_inputs, request=request)
        # Entity-based recipe units still write the original primary target.
        # Variant units must not overwrite that entity through the old writer.
        if bible is not None and (appearance["role"] != "primary" or appearance["setting"] != setting):
            raise ValidationError("原实体单元只接受其确切主图设定目标；变体须使用后续动态素材单元")
        setting = deepcopy(appearance["setting"])
    return {"schema_version": 1, "source_mode": "generate", "media_type": kind,
        "execution_instance_version_id": definition.value["version_id"],
        "definition_sha256": definition.sha256,
        "candidates": _candidate_policy(candidate_count, candidate_evaluator),
        "inputs_sha256": json_contract(inputs, max_bytes=execution_resolved_inputs_max_bytes()).sha256,
        "setting": setting, **({"appearance": deepcopy(appearance)} if appearance is not None else {})}


def resolve_media_generation_target(definition, request, *, candidate_count, candidate_evaluator, appearance_target=None,
        appearance_target_source=None, **context):
    """Called only in the original root's first-resolution transaction.

    Both business adapters use the same input/parameter/Profile resolver. The
    target links its resulting inputs by digest rather than copying a second
    source dictionary. Replays never call this function or draw another seed.
    """
    _candidate_policy(candidate_count, candidate_evaluator)
    if appearance_target_source not in (None, "bible_primary"):
        raise ValidationError("生成目标来源未声明，不从当前对象猜测角色")
    if appearance_target_source == "bible_primary" and (not context.get("production_run_id")
            or not isinstance(context.get("production_scope"), dict)
            or context["production_scope"].get("kind") not in {"character", "scene", "prop"}):
        raise ValidationError("配方主图目标必须来自本次明确的实体制作单元")
    if output_kind(definition) not in MEDIA_KINDS:
        raise ValidationError("共用媒体生成目标不接受文本或仅关系请求")
    if (appearance_target is not None or appearance_target_source is not None) and output_kind(definition) != "image":
        raise ValidationError("主图/变体生成声明需要实际图片输出合同")
    from studio.services.media_generation_appearances import normalize_media_appearance_request, resolve_media_appearance_target
    appearance_target = normalize_media_appearance_request(appearance_target)
    if appearance_target is not None and appearance_target["role"] == "variant":
        # Reuse the exact authoring catalog for both roots. Output kind and an
        # operation name cannot establish that this selected input consumes images.
        from studio.services.media_appearance_input_catalog import media_appearance_input_options
        key = appearance_target["primary_input"]["input_key"]
        if not any(row["input_key"] == key for row in media_appearance_input_options(definition.value)["primary_inputs"]):
            raise ValidationError({"appearance_target": f"输入 {key} 未映射到实例的主图片端口；上下文素材不能充当生成依据"})
    if context.get("production_run_id") and appearance_target is not None and appearance_target["role"] == "variant":
        context = {**context, "production_appearance_role": "variant"}
    resolved = resolve_execution_request_facts(definition, request, **context)
    facts = deepcopy(resolved.resolved_inputs.value)
    appearance = resolve_media_appearance_target(appearance_target, owner=context["owner"],
        max_media_bytes=context["max_media_bytes"], project_id=context.get("project_id"),
        production_scope=context.get("production_scope"), definition=definition, resolved_inputs=facts, request=request)
    if appearance_target_source == "bible_primary":
        # The original Bible generation stage explicitly owns the primary
        # entity image. Reuse its just-captured writeback baseline, not another
        # current-setting query or an inferred role for every image request.
        bible = facts.get("bible_target")
        if (not isinstance(bible, dict) or "primary_setting" not in bible
                or bible.get("scope") != context["production_scope"]
                or bible.get("project_id") != str(context.get("project_id"))):
            raise ValidationError("配方主图目标缺少本次原准备捕获的设定基线")
        captured = {"role": "primary", "setting": deepcopy(bible["primary_setting"]),
            "primary": None, "primary_input": None}
        if appearance is not None and appearance != captured:
            raise ValidationError("原实体单元只接受其确切主图设定目标；变体须使用后续动态素材单元")
        appearance = captured
    facts["media_generation_target"] = _target_value(definition, facts,
        candidate_count=candidate_count, candidate_evaluator=candidate_evaluator, appearance=appearance,
        request={**request, "appearance_target": appearance_target})
    return ResolvedExecutionPreparation(
        json_contract(facts, max_bytes=execution_resolved_inputs_max_bytes()), resolved.review_contract)


def verify_media_generation_target(definition, resolved_inputs, *, request=None):
    """Verify a saved generated target, without resolving any current source."""
    target = resolved_inputs.get("media_generation_target")
    if not isinstance(target, dict):
        raise ValidationError("原准备未记录共用生成目标，不能读取时回填")
    candidates = target.get("candidates")
    if not isinstance(candidates, dict) or set(candidates) != {"count", "evaluator"}:
        raise ValidationError("生成目标缺少完整候选策略")
    expected = _target_value(definition, resolved_inputs,
        candidate_count=candidates["count"], candidate_evaluator=candidates["evaluator"], appearance=target.get("appearance"), request=request)
    if target != expected or type(target.get("schema_version")) is not int:
        raise ValidationError("生成目标与原实例、解析输入或设定基线不一致")
    return json_contract(target, max_bytes=execution_configuration_max_bytes())


def read_media_generation_target(preparation, *, detail=None):
    """Read after original owner authorization; never infer a ProductionRun."""
    from studio.services.execution_preparations import read_execution_preparation_detail, preparation_execution_definition
    detail = read_execution_preparation_detail(preparation, detail=detail)
    definition = preparation_execution_definition(preparation, detail=detail)
    return verify_media_generation_target(definition, detail.resolved_inputs, request=detail.intent["request"]["requested"])
