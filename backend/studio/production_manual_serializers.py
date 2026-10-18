"""Closed transport for manual commands; business validation is server-owned."""
from rest_framework import serializers
from studio.execution_instance_serializers import ExecutionAuthoringSerializer
from studio.models import BibleVariant, ProductionManualCommand
from studio.services.production_execution_units import PRODUCTION_SCOPE_KINDS
from studio.services.production_manual_change_impact import MANUAL_CONTENT_CHANGE_STAGES


class ManualActionScopeSerializer(ExecutionAuthoringSerializer):
    kind = serializers.ChoiceField(choices=PRODUCTION_SCOPE_KINDS)
    id = serializers.UUIDField()

    def validate_id(self, value):
        return str(value)


class ProductionManualActionInputSerializer(ExecutionAuthoringSerializer):
    action = serializers.ChoiceField(choices=ProductionManualCommand.Action.choices)
    stage_key = serializers.CharField(max_length=100)
    unit_key = serializers.CharField(max_length=191)
    scope = ManualActionScopeSerializer()
    expected_run_plan_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    expected_selection_revision = serializers.IntegerField(min_value=0, allow_null=True)
    idempotency_key = serializers.CharField(max_length=191)
    payload = serializers.JSONField(help_text="由已登记动作决定。配方仅支持storyboard_review/select：expected_selection_revision=null，payload恰为expected_source_snapshot_sha256/expected_uses_sha256/choices，基线及冻结权限从recipe-shot-references读取；只允许冻结来源内首次选择或allow_override允许的替换，不审批或恢复。无配方人工运行的storyboard_review/select 在shot范围提交expected_storyboard_adoption_sha256、expected_uses_sha256、choices；修订取manual-shot-references的storyboard_adoption_revision。choices每项为use_id/source_mode/reference_version_id/asset_id/asset_sha256，版本和Asset恰选一项；仅选择已声明用途，不审核、不生成、不推进阶段。201返回shot_reference_choices及原shot UUID，原请求重放200；旧基线409。generate 提交 execution_instance_version_id、expected_definition_sha256、inputs、parameters、manual_channels、subject_facts、source_language、target_language；选本镜已审核用途时另必传expected_storyboard_review（decision_id/snapshot_sha256），媒体仍为asset引用且scope_ref为当前shot、item_key为reviewed:稳定用途摘要；服务端沿原逐镜审核和确切用途读者复核并冻结版本/设定，不接受调用方来源快照，也不把创作描述推断成模型用途。未选此来源不能附带审核引用；只创建准备，不调用模型。retry 只提交 preparation_id、expected_preparation_revision、expected_attempt_id；复用原冻结准备重试明确失败的主调用，不改输入、不重跑辅助字段。202表示动作已评估并返回原准备，不代表已排队或成功；准入原因从原准备读取；同键重放200，处理阻断后用新显式命令核对最新准备修订。script_input/adopt 提交 content/comment；script_review/approve 提交 text_candidate（artifact_id/expected_sha256/expected_manifest_sha256）、expected_current_script 和 comment；可另传 script_content 完整JSON，按原候选冻结合同校验并另存人工修订后采用，原返回不变；省略则采用原候选；采用剧本只更新剧本修订与项目母稿，保留项目原标题和梗概；visual_rules_review/approve 提交同一候选、原输出合同所选全部字段的 candidate_overrides 及 comment；无需已采用剧本，按候选的冻结输入及项目规则基线核对；bible_materialize/adopt 提交 expected_current_script/comment，仅解析候选；bible_review/approve 提交 expected_current_script/expected_source_snapshot_sha256/expected_project_bible_sha256/reviewed_candidate_ids/reviewed_setting_keys/reviewed_project_bible/comment，需核对完整实体声明、主图/变体设定及项目创作设定后同事务写入；storyboard/adopt 提交 expected_content_sha256/source/comment；source.kind=existing_storyboard 时另含 reviewed_shot_ids，source.kind=confirmed_script 时 source 另含 expected_materialization_sha256，明确采用已确认剧本创建原分镜，不覆盖母稿或自动审核媒体；storyboard_review/approve 提交 expected_contract_sha256/reviewed_shot_ids/comment，引用 manual-storyboard-review 返回的完整核对合同，不写入或替换素材、不执行模型；media_generation/adopt 只提交 source：kind=artifact 时含 artifact_id/expected_sha256/expected_manifest_sha256，kind=asset 时含 asset_id/expected_sha256/media_kind；artifact_selection/approve 提交 selection/expected_selection_sha256/comment，引用 manual-media-review 返回的完整选片清单。assembly/assemble 在 episode 范围提交 expected_contract_sha256、expected_assembly_job_id（首次 null；重试为准确失败job ID），引用 manual-assembly 核对结果；202仅表示提交原装配队列。delivery/deliver 在 project 范围提交 expected_contract_sha256/comment，expected_selection_revision 为清单中的选片审核修订；201表示已核验并封存原ReleaseBundle，重放200。此动作不执行转码、不新建任务、不发布到外部平台。不接受执行覆盖或客户端伪造的批准事实。")


class ProductionManualActionResultSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    action = serializers.CharField()
    created_at = serializers.DateTimeField()
    result_kind = serializers.CharField()
    target_id = serializers.UUIDField()


class ManualShotReferenceQuerySerializer(ExecutionAuthoringSerializer):
    shot_id = serializers.UUIDField()


class ManualShotReferenceEditabilitySerializer(serializers.Serializer):
    allowed = serializers.BooleanField()
    code = serializers.CharField()
    reason = serializers.CharField()


class ManualShotReferenceDetailSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    stage_key = serializers.CharField()
    run_plan_sha256 = serializers.CharField()
    shot_id = serializers.UUIDField()
    storyboard_adoption_revision = serializers.IntegerField()
    storyboard_adoption_sha256 = serializers.CharField(allow_null=True)
    uses_sha256 = serializers.CharField()
    state = serializers.JSONField(help_text="schema_version=1，project_id、shot_id及按position排序的uses；保留明确用途、设定修订、来源模式、确切素材版本或Asset与SHA。只读基线，不证明媒体可用。")
    editability = ManualShotReferenceEditabilitySerializer()


class RecipeShotReferenceDetailSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    stage_key = serializers.CharField()
    run_plan_sha256 = serializers.CharField()
    shot_id = serializers.UUIDField()
    source_snapshot_sha256 = serializers.CharField()
    uses_sha256 = serializers.CharField()
    state = serializers.JSONField(help_text="与人工单镜读取共用schema1用途基线，不包含人工采用修订，也不证明文件可用。")
    selection_rule = serializers.JSONField(help_text="原Stage冻结的{rule,upstream}；rule含source/source_stage_key/selection/allow_override，upstream为原Stage及输出SHA或null。")
    upstream_candidates = serializers.ListField(child=serializers.JSONField(), help_text="原冻结上游目录，仅元数据。每项含source原结果引用、source_mode、reference_version_id、version、setting_revision_id、asset_id/asset_sha256、name/mime_type；非上游来源为空。提交时重新验证原结果、文件及确切设定。")
    editability = ManualShotReferenceEditabilitySerializer()


class ManualRegisteredActionSerializer(serializers.Serializer):
    stage_key = serializers.CharField()
    action = serializers.CharField()
    operation = serializers.CharField(allow_null=True)
    output_kinds = serializers.ListField(child=serializers.CharField())
    scope_kinds = serializers.ListField(child=serializers.CharField())
    source_stage_keys = serializers.ListField(child=serializers.CharField(), required=False)


class ManualScriptAdoptionSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=0)
    decision_id = serializers.UUIDField(allow_null=True)
    content = serializers.JSONField(allow_null=True)


class ManualScriptCandidateIdentitySerializer(serializers.Serializer):
    artifact_id = serializers.UUIDField()
    variant_index = serializers.IntegerField(min_value=1)
    stage_id = serializers.UUIDField()
    stage_key = serializers.CharField()
    attempt_id = serializers.UUIDField()
    preparation_id = serializers.UUIDField()


class ManualScriptStructureCheckSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    current_script = serializers.JSONField(allow_null=True, help_text="本次检查的原已确认修订引用；不含正文。")
    current_script_decision_id = serializers.UUIDField(allow_null=True)
    status = serializers.ChoiceField(choices=("no_script", "structured", "unstructured"))
    reason_code = serializers.CharField(allow_blank=True)
    reason = serializers.CharField()
    episode_count = serializers.IntegerField(min_value=0, allow_null=True)
    shot_count = serializers.IntegerField(min_value=0, allow_null=True)


class ManualScriptIdentitySerializer(serializers.Serializer):
    source_name = serializers.CharField(allow_blank=True, help_text="原稿名称，可能未命名；不从当前项目标题或正文猜测。")
    source_type_label = serializers.CharField()
    source_format_label = serializers.CharField()
    decision_stage_key = serializers.ChoiceField(choices=("script_input", "script_review"))
    candidate = ManualScriptCandidateIdentitySerializer(allow_null=True, help_text="当前决定直接审核的候选；改选已确认修订时为null，不推断旧候选采用。")


class ManualVisualRulesApprovalSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=0)
    decision_id = serializers.UUIDField(allow_null=True)
    status = serializers.ChoiceField(choices=("not_reviewed", "current", "changed"))
    reasons = serializers.ListField(child=serializers.CharField())


class ManualActivityCountSerializer(serializers.Serializer):
    status = serializers.CharField()
    label = serializers.CharField()
    count = serializers.IntegerField(min_value=1)


class ManualCallCountSerializer(ManualActivityCountSerializer):
    role = serializers.CharField()
    role_label = serializers.CharField()


class ManualExecutionActivitySerializer(serializers.Serializer):
    stage_id = serializers.UUIDField()
    stage_key = serializers.CharField()
    stage_status = serializers.CharField()
    lifecycle_label = serializers.CharField()
    preparations = ManualActivityCountSerializer(many=True)
    calls = ManualCallCountSerializer(many=True)


class ProductionManualActionOptionsSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    run_plan_sha256 = serializers.CharField()
    content_change_stage_keys = serializers.ListField(child=serializers.CharField())
    execution_activity = ManualExecutionActivitySerializer(many=True, help_text="阶段是否开放与真实准备/主辅调用状态分别呈现；原事实聚合，不触发执行。")
    script_adoption = ManualScriptAdoptionSerializer()
    script_review = ManualScriptAdoptionSerializer()
    visual_rules_review = ManualVisualRulesApprovalSerializer()
    current_script = serializers.JSONField(allow_null=True, help_text="本轮当前明确采用的已确认剧本引用；不同于某步骤过去的采用记录。")
    current_script_decision_id = serializers.UUIDField(allow_null=True, help_text="本轮当前剧本指向的原采用决定；不按步骤或时间推断。")
    current_script_identity = ManualScriptIdentitySerializer(allow_null=True, help_text="只读身份摘要，不进入expected_current_script或任何冻结采用请求。")
    actions = ManualRegisteredActionSerializer(many=True)


class ManualChangeImpactQuerySerializer(serializers.Serializer):
    stage_key = serializers.ChoiceField(choices=MANUAL_CONTENT_CHANGE_STAGES)
    offset = serializers.IntegerField(min_value=0, default=0)
    limit = serializers.IntegerField(min_value=1, max_value=50, default=20)

    def to_internal_value(self, data):
        # Query parameters are strings, unlike strict JSON authoring inputs.
        if set(data) - set(self.fields):
            raise serializers.ValidationError({"query": "改选影响查询含未登记参数"})
        return super().to_internal_value(data)


class ReviewedVisualSourceQuerySerializer(ExecutionAuthoringSerializer):
    stage_key = serializers.CharField(max_length=100)


class ReviewedVisualSourceSerializer(serializers.Serializer):
    selector_key = serializers.CharField()
    label = serializers.CharField()
    content = serializers.CharField(allow_blank=True)
    content_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    review = serializers.JSONField(help_text="原审核 stage_run_id、decision_id、revision、snapshot_sha256。")
    output = serializers.JSONField(help_text="原任务、准备、候选、结果摘要和单项输出字段；只读，不创建任务或采用。")


class ReviewedVisualSourcesSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    project_id = serializers.UUIDField()
    stage_key = serializers.CharField()
    run_plan_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    review_contract = serializers.JSONField(help_text="原生产请求冻结的业务输出版本身份、摘要及审核字段定义；不读取当前目录或自动采用。")
    sources = ReviewedVisualSourceSerializer(many=True)


class ManualChangeSourceSerializer(serializers.Serializer):
    stage_run_id = serializers.UUIDField()
    stage_key = serializers.CharField()
    decision_id = serializers.UUIDField(allow_null=True)
    revision = serializers.IntegerField(min_value=0)
    script_revision_id = serializers.UUIDField(allow_null=True)


class ManualChangeStageSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    stage_key = serializers.CharField()
    status = serializers.CharField()
    preparation_count = serializers.IntegerField(min_value=0)


class ManualInputReferenceSerializer(serializers.Serializer):
    input_path = serializers.CharField()
    kind = serializers.ChoiceField(choices=("script_input", "reviewed_content", "related_review", "selected_output", "adoption_baseline"))


class ManualAttemptCountSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=("primary", "auxiliary"))
    status = serializers.CharField()
    count = serializers.IntegerField(min_value=1)


class ManualChangePreparationSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    stage_run_id = serializers.UUIDField()
    stage_key = serializers.CharField()
    unit_key = serializers.CharField()
    status = serializers.CharField()
    revision = serializers.IntegerField(min_value=1)
    reference_state = serializers.ChoiceField(choices=("recorded_reference", "manual_review_required", "unverifiable"))
    references = ManualInputReferenceSerializer(many=True)
    attempts = ManualAttemptCountSerializer(many=True)


class ManualContentReplacementSerializer(serializers.Serializer):
    allowed = serializers.BooleanField()
    code = serializers.ChoiceField(choices=("no_dependents", "stopped_unused", "review_only", "review_and_stopped", "media_review_required", "text_review_required", "run_read_only", "assembly_locked",
        "dependent_results", "active_preparations", "inconsistent_records", "unresolved_calls"))
    reason = serializers.CharField()


class ManualChangeImpactSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    run_plan_sha256 = serializers.CharField()
    checked_at = serializers.DateTimeField()
    source = ManualChangeSourceSerializer()
    assembly_locked = serializers.BooleanField()
    run_read_only = serializers.BooleanField()
    replacement = ManualContentReplacementSerializer()
    stages = ManualChangeStageSerializer(many=True)
    count = serializers.IntegerField(min_value=0)
    offset = serializers.IntegerField(min_value=0)
    limit = serializers.IntegerField(min_value=1, max_value=50)
    next_offset = serializers.IntegerField(min_value=0, allow_null=True)
    results = ManualChangePreparationSerializer(many=True)


class ManualTargetSerializer(serializers.Serializer):
    scope = ManualActionScopeSerializer()
    unit_key = serializers.CharField()
    label = serializers.CharField()


class ManualTargetPageSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    run_plan_sha256 = serializers.CharField()
    count = serializers.IntegerField(min_value=0)
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = ManualTargetSerializer(many=True)


class ManualMediaCandidateSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    stage_key = serializers.CharField()
    unit_key = serializers.CharField()
    scope = ManualActionScopeSerializer()
    run_plan_sha256 = serializers.CharField()
    source_kind = serializers.CharField(help_text="execution：真实执行调用；asset：明确采用素材库媒体，没有模型调用。")
    source_asset_id = serializers.UUIDField(allow_null=True)
    preparation_id = serializers.UUIDField(allow_null=True)
    request_sha256 = serializers.CharField(allow_null=True)
    selection_revision = serializers.IntegerField(min_value=0)
    selected_artifact_id = serializers.UUIDField(allow_null=True)
    selection_decision_id = serializers.UUIDField(allow_null=True)


class ManualMediaSelectionMemberSerializer(serializers.Serializer):
    artifact_id = serializers.UUIDField()
    kind = serializers.CharField()
    artifact_sha256 = serializers.CharField()
    manifest_sha256 = serializers.CharField()
    decision_id = serializers.UUIDField()
    decision_revision = serializers.IntegerField(min_value=1)
    variant_group = serializers.CharField()
    scope = ManualActionScopeSerializer()
    unit_key = serializers.CharField()


class ManualReviewTimelineShotSerializer(serializers.Serializer):
    episode_start_ms = serializers.IntegerField(allow_null=True)
    episode_end_ms = serializers.IntegerField(allow_null=True)
    shot_id = serializers.UUIDField()
    number = serializers.IntegerField()
    title = serializers.CharField(allow_blank=True)
    duration_ms = serializers.IntegerField(allow_null=True)
    media_kind = serializers.CharField(allow_blank=True)


class ManualReviewTimelineEpisodeSerializer(serializers.Serializer):
    episode_id = serializers.UUIDField()
    number = serializers.IntegerField()
    title = serializers.CharField(allow_blank=True)
    shots = ManualReviewTimelineShotSerializer(many=True)


class ManualMediaContentBasisSerializer(serializers.Serializer):
    script_sha256 = serializers.CharField()
    storyboard_sha256 = serializers.CharField()
    bible_sha256 = serializers.CharField()
    visual_rules_sha256 = serializers.CharField()
    upstream_reviews_sha256 = serializers.CharField()


class ManualMediaSelectionSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    run_plan_sha256 = serializers.CharField()
    source_stage_id = serializers.UUIDField()
    items = ManualMediaSelectionMemberSerializer(many=True)
    content_basis = ManualMediaContentBasisSerializer(help_text="schema v4：本次人工复用审核的当前内容与原上游审核修订摘要；不是媒体原生成输入。")
    delivery_timeline = ManualReviewTimelineEpisodeSerializer(many=True, help_text="本次审核同时核对的完整交付分镜；顺序、时长、标题、类型或范围改变需重新审核，不重写媒体生成输入。")


class ManualMediaApprovalSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=0)
    decision_id = serializers.UUIDField(allow_null=True)
    status = serializers.CharField(help_text="not_reviewed：尚未审核；current：选片、交付分镜及上游内容/审核依据均未变化；changed：需重新核对复用或重生成。不是模型烟测或交付验收结论。")
    selection_sha256 = serializers.CharField(allow_null=True)
    changes = serializers.ListField(child=serializers.CharField(), help_text="相较原选片审核已变化的内容类别；服务端生成，不代表每个旧输入均已失效。")


class ManualMediaReviewSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    run_plan_sha256 = serializers.CharField()
    selection = ManualMediaSelectionSerializer()
    selection_sha256 = serializers.CharField()
    approval = ManualMediaApprovalSerializer()


class ManualBibleDecisionSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=0)
    decision_id = serializers.UUIDField(allow_null=True)
    snapshot_sha256 = serializers.CharField(allow_null=True)
    status = serializers.CharField(help_text="current 为依据仍匹配；changed 为内容/来源变化，不是模型烟测。")


class ManualBibleSourceSerializer(ManualBibleDecisionSerializer):
    content = serializers.JSONField(allow_null=True)
    candidate_sha256 = serializers.CharField(allow_null=True)


class ManualBibleImageContentBasisSerializer(serializers.Serializer):
    script_sha256 = serializers.CharField()
    bible_sha256 = serializers.CharField()
    visual_rules_sha256 = serializers.CharField()
    upstream_reviews_sha256 = serializers.CharField()


class ManualBibleImageDecisionSerializer(ManualBibleDecisionSerializer):
    changes = serializers.ListField(child=serializers.CharField(), help_text="与上次审核相比发生变化的类别；不是模型烟测结果。")


class BibleImagePrimarySettingReferenceSerializer(serializers.Serializer):
    appearance_id = serializers.UUIDField()
    setting_revision_id = serializers.UUIDField()
    revision = serializers.IntegerField(min_value=1)
    content_sha256 = serializers.CharField()


class BibleImageTargetScopeSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=("character", "scene", "prop"))
    id = serializers.UUIDField()


class BibleImageTargetSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(min_value=2, max_value=2)
    project_id = serializers.UUIDField()
    scope = BibleImageTargetScopeSerializer()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    primary_setting = BibleImagePrimarySettingReferenceSerializer(allow_null=True,
        help_text="确切文字设定引用，不复制锚点或代表图片就绪；未建立主图设定时为null，损坏引用报错。")
    anchor_asset_id = serializers.UUIDField(allow_null=True)
    reference_asset_ids = serializers.ListField(child=serializers.UUIDField())


class ManualBibleImageReviewSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    run_plan_sha256 = serializers.CharField()
    candidates = serializers.JSONField(help_text="与原参考图审核共用的真实执行准备候选目录；不会把生成步骤伪装成完成。")
    candidates_sha256 = serializers.CharField()
    targets = BibleImageTargetSerializer(many=True, help_text="本轮明确实体的内容、主图设定修订与当前图片关系。")
    targets_sha256 = serializers.CharField()
    content_basis = ManualBibleImageContentBasisSerializer()
    content_basis_sha256 = serializers.CharField()
    maximum_selected_images = serializers.IntegerField(min_value=1)
    approval = ManualBibleImageDecisionSerializer()


class ManualBibleCandidateSerializer(serializers.Serializer):
    id = serializers.CharField()
    entity_type = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    identity_anchor = serializers.CharField(allow_blank=True, allow_null=True,
        help_text="原剧本声明的共同身份；null表示未声明，空字符串表示明确空文本，不是已有主图设定正文。")
    action = serializers.CharField(help_text="create 创建新条目；reuse 保留页面展示的已有内容，不用候选覆盖。")
    conflict_code = serializers.CharField(allow_null=True)
    existing = serializers.JSONField(allow_null=True)


class BibleSettingReviewSerializer(serializers.Serializer):
    review_key = serializers.CharField()
    kind = serializers.ChoiceField(choices=("declaration", "stored"))
    entity_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    entity_name = serializers.CharField()
    entity_key = serializers.CharField(allow_null=True)
    appearance_role = serializers.ChoiceField(choices=BibleVariant._meta.get_field("appearance_role").choices)
    variant_key = serializers.CharField(allow_null=True)
    name = serializers.CharField()
    reference_name = serializers.CharField()
    identity_anchor = serializers.CharField(allow_blank=True)
    appearance_anchor = serializers.CharField(allow_blank=True)
    source_facts = serializers.JSONField(allow_null=True, help_text="原声明出处、原文/设计来源与是否已入库；不冒充当前人工修订。")
    setting = serializers.JSONField(allow_null=True, help_text="当前确切修订的完整设定与审核引用；待物化声明为null。")
    primary_setting = serializers.JSONField(allow_null=True, help_text="当前设定依赖的确切主图设定全文与修订；历史只读，不表示已采用图片。")


class BibleContentConfirmationSerializer(ExecutionAuthoringSerializer):
    expected_source_snapshot_sha256 = serializers.RegexField(regex=r"^[a-f0-9]{64}$", allow_null=True,
        help_text="声明来源阶段的确切快照；纯手工配方明确为null。")
    expected_project_bible_sha256 = serializers.RegexField(regex=r"^[a-f0-9]{64}$")
    reviewed_candidate_ids = serializers.ListField(child=serializers.CharField(max_length=191), allow_empty=True)
    reviewed_setting_keys = serializers.ListField(child=serializers.CharField(max_length=191), allow_empty=True)
    reviewed_project_bible = serializers.BooleanField()


class RecipeBibleReviewSerializer(serializers.Serializer):
    plan_source = serializers.ChoiceField(choices=("recipe",))
    run_id = serializers.UUIDField()
    run_plan_sha256 = serializers.CharField()
    source_snapshot_sha256 = serializers.CharField(allow_null=True)
    project_bible = serializers.JSONField()
    project_bible_sha256 = serializers.CharField()
    candidates = ManualBibleCandidateSerializer(many=True)
    settings = BibleSettingReviewSerializer(many=True)


class ManualBibleReviewSerializer(serializers.Serializer):
    plan_source = serializers.ChoiceField(choices=("manual",))
    run_id = serializers.UUIDField()
    run_plan_sha256 = serializers.CharField()
    current_script = serializers.JSONField(allow_null=True)
    source = ManualBibleSourceSerializer()
    project_bible = serializers.JSONField(help_text="项目现有完整创作设定：characters/scenes/props，含人物声音与说话人标识等原字段；不含另行审核的主图和参考图绑定。必须与候选一并明确核对。")
    project_bible_sha256 = serializers.CharField()
    candidates = ManualBibleCandidateSerializer(many=True)
    settings = BibleSettingReviewSerializer(many=True)
    approval = ManualBibleDecisionSerializer()


class ManualStoryboardReviewSerializer(serializers.Serializer):
    display = serializers.JSONField(allow_null=True, help_text="未就绪时null；否则与配方审核共用name_basis=current_bible_by_id及有序shots展示投影。scene、characters(role/position/entity)、props的原关系来自canonical；实体id/name/status为同项目确切ID的当前名称或unavailable，不进入审核合同。")
    run_id = serializers.UUIDField()
    run_plan_sha256 = serializers.CharField()
    ready = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)
    contract = serializers.JSONField(allow_null=True, help_text="已采用分镜的完整创作/参考清单、原素材身份与上游审核依据。只核对当前关联事实，不分配执行实例槽位、不证明Smoke通过。")
    contract_sha256 = serializers.CharField(allow_null=True)
    references = serializers.ListField(child=serializers.JSONField(), help_text="由同份合同覆盖的ShotReferenceUse精确解析，保留用途身份/顺序/原purpose及确切设定主体，不读旧reference_bindings。空用途镜头保留items空数组；待选用途明确未就绪，不推断首尾帧或音画配对。")
    source_options = serializers.ListField(child=serializers.JSONField(), help_text="与配方reference_source_options共用的value/label/kind/scope_kind/stage_key/paired目录；页面按服务端名称显示，不维护第二套来源名称。")
    approval = ManualBibleDecisionSerializer()


class ManualStoryboardAdoptionSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=0)
    decision_id = serializers.UUIDField(allow_null=True)
    snapshot_sha256 = serializers.CharField(allow_null=True)
    content_sha256 = serializers.CharField(allow_null=True)
    status = serializers.CharField(help_text="not_adopted/current/changed：是否与本轮明确采用的内容一致，不代表媒体已生成。")
    source = serializers.JSONField(allow_null=True, help_text="existing_storyboard 或 confirmed_script；后者仅保存 kind/contract/sha256，contract 为与配方共用的 schema3 结构化来源及冻结参数；不含旧解析方式或警告。原采用快照为 schema5，不转换旧回执。")


class ManualStoryboardMaterializationSerializer(serializers.Serializer):
    ready = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)
    contract = serializers.JSONField(allow_null=True, help_text="schema3：source_basis（确切结构化剧本、原采用步骤及 Bible 决定、声明关系摘要）、run_config_sha256、parameters；与配方共用。没有集数猜测、自动切段或别名扫描参数。缺少有效结构或 Bible 依据时未就绪且为空，读取不创建分镜。")
    sha256 = serializers.CharField(allow_null=True)


class ManualStoryboardDetailSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    run_plan_sha256 = serializers.CharField()
    content = serializers.JSONField(help_text="schema_version=3、run_plan_sha256、script（本轮已采用引用，可空）、script_decision_id（原采用决定；无剧本时为null）、episodes（本轮范围的原剧集/镜头字段及角色场景道具ID）。镜头另含script_source（确切结构修订/Stage/原集镜坐标及摘要；无绑定为null）、character_relations（显式visible/speaker及位置）、reference_uses（有序用途/媒体类型/确切设定修订）。不复制结构化原字段值，不从旧character_ids或参考素材推断新角色关系；创作视图不含用途的素材选择字段、reference_bindings/reference_asset_ids、执行路由或生命周期状态。读取不物化，参考素材另行审核；重新采用同一正文也改变审核依据。")
    content_sha256 = serializers.CharField()
    adoption = ManualStoryboardAdoptionSerializer()
    materialization = ManualStoryboardMaterializationSerializer()


class ManualAssemblyJobSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.CharField()
    attempts = serializers.IntegerField(min_value=0)
    max_attempts = serializers.IntegerField(min_value=1)
    error_message = serializers.CharField(allow_blank=True)
    updated_at = serializers.DateTimeField()


class ManualAssemblyShotSerializer(serializers.Serializer):
    episode_start_ms = serializers.IntegerField(allow_null=True)
    episode_end_ms = serializers.IntegerField(allow_null=True)
    shot_id = serializers.UUIDField()
    number = serializers.IntegerField()
    title = serializers.CharField(allow_blank=True)
    duration_ms = serializers.IntegerField()
    media_kind = serializers.CharField()
    source = ManualMediaSelectionMemberSerializer(allow_null=True)


class ManualAssemblyEpisodeSerializer(serializers.Serializer):
    episode_id = serializers.UUIDField()
    number = serializers.IntegerField()
    title = serializers.CharField(allow_blank=True)
    shots = ManualAssemblyShotSerializer(many=True)
    assembly_link_id = serializers.UUIDField(allow_null=True)
    job = ManualAssemblyJobSerializer(allow_null=True)


class ManualAssemblyPreviewSerializer(ExecutionAuthoringSerializer):
    expected_run_plan_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    authors = serializers.DictField(child=serializers.JSONField(), help_text="可选映射集合（可空对象）：assembly_output/assembly_timing，各含output_version_id、expected_contract_sha256、source_text和M1 mapping；仅预览，不写快照、不探测、不入队。")


class ManualAssemblyDetailSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    run_plan_sha256 = serializers.CharField()
    contract_sha256 = serializers.CharField(help_text="本轮完整选片、镜头时长和输出配置摘要；提交时必须回传。")
    frozen = serializers.BooleanField()
    blockers = serializers.ListField(child=serializers.CharField())
    approval = ManualMediaApprovalSerializer()
    episodes = ManualAssemblyEpisodeSerializer(many=True)
    authors = serializers.JSONField(help_text="本次预览或已冻结的作者原文、确切输出版本及M1映射；未配置为{}。首次assemble须原样传入authors，后续复用冻结值。")
    author_contracts = serializers.JSONField(help_text="按作者键返回完整output_reference和projection（原值、结构、默认来源、映射来源及结果）；不取当前版本修复冻结内容。")
    author_schemas = serializers.JSONField(help_text="共用assembly_output/assembly_timing目标结构；不另设人工字段字典。")
    output = serializers.JSONField(help_text="width/height/fps、原 media_profile 与摘要、max_attempts、label_enabled/label_text；不暴露服务端字体路径。装配提交后读取已冻结配置。")


class ManualDeliveryEpisodeSerializer(serializers.Serializer):
    episode_id = serializers.UUIDField()
    number = serializers.IntegerField()
    title = serializers.CharField(allow_blank=True)
    assembly_job_id = serializers.UUIDField(allow_null=True)
    artifact_id = serializers.UUIDField(allow_null=True)
    sha256 = serializers.CharField(allow_null=True)
    manifest_sha256 = serializers.CharField(allow_null=True)
    size_bytes = serializers.IntegerField(allow_null=True)
    mime_type = serializers.CharField(allow_null=True)


class ManualDeliveryContractSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    kind = serializers.CharField()
    run_plan_sha256 = serializers.CharField()
    assembly_contract_sha256 = serializers.CharField()
    selection_revision = serializers.IntegerField(min_value=0)
    episodes = ManualDeliveryEpisodeSerializer(many=True)


class ManualDeliveryBundleSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.CharField()
    manifest_sha256 = serializers.CharField(allow_blank=True)
    sealed_at = serializers.DateTimeField(allow_null=True)


class ManualDeliveryDetailSerializer(serializers.Serializer):
    run_id = serializers.UUIDField()
    run_plan_sha256 = serializers.CharField()
    contract = ManualDeliveryContractSerializer()
    contract_sha256 = serializers.CharField(help_text="确认成片时必须回传；读取只核对数据库清单，提交封存时再实时核验文件和签名。")
    blockers = serializers.ListField(child=serializers.CharField())
    bundle = ManualDeliveryBundleSerializer(allow_null=True, help_text="已记录发布包摘要，不等于本次实时文件核验通过。")
