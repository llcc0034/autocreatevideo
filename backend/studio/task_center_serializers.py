"""Read-only task-center wire contract and closed query parameters."""
from rest_framework import serializers
from studio.execution_preparation_serializers import PreparationOutcomeSerializer


class ClosedQuerySerializer(serializers.Serializer):
    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({"query": "不支持的参数：" + ", ".join(sorted(unknown))})
        if hasattr(data, "getlist") and any(len(data.getlist(key)) != 1 for key in data):
            raise serializers.ValidationError({"query": "查询参数不能重复"})
        # QueryDict is not an HTML checkbox form: an absent bool must stay absent.
        return super().to_internal_value({key: data[key] for key in data})


class TaskCenterQuerySerializer(ClosedQuerySerializer):
    category = serializers.ChoiceField(choices=("production", "asset", "smoke"), required=False)
    status = serializers.CharField(max_length=24, required=False)
    project_id = serializers.UUIDField(required=False)
    production_run_id = serializers.UUIDField(required=False, help_text="按实际归属生产轮次筛选；不包含仅借用该轮内容的独立Smoke。")
    stage_run_id = serializers.UUIDField(required=False, help_text="按精确StageRun筛选；与轮次、项目等条件取交集，不按阶段名称匹配。")
    unit_key = serializers.CharField(max_length=191, required=False, help_text="按生产准备的原制作单元精确筛选；只返回有实际阶段归属的准备，不从任务JSON或镜头名称推断。")
    execution_instance_version_id = serializers.UUIDField(required=False)
    source_id = serializers.UUIDField(required=False)
    operation = serializers.CharField(max_length=100, required=False)
    created_after = serializers.DateTimeField(required=False)
    created_before = serializers.DateTimeField(required=False)
    anomaly = serializers.BooleanField(required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=25)
    cursor = serializers.CharField(max_length=4096, required=False)

    def validate(self, attrs):
        if attrs.get("created_after") and attrs.get("created_before") and attrs["created_after"] > attrs["created_before"]:
            raise serializers.ValidationError("created_after 不能晚于 created_before")
        return attrs


CONTENT_FIELDS = ("input.submitted", "input.resolved", "input.parameters", "input.prompts", "input.prompt_trace", "input.field_batch_trace", "input.media_trace",
                  "output.result", "output.text", "output.media")


class TaskCenterDetailQuerySerializer(ClosedQuerySerializer):
    attempt_id = serializers.UUIDField(required=False)
    call_page = serializers.IntegerField(min_value=1, default=1)
    call_page_size = serializers.IntegerField(min_value=1, max_value=100, default=25)
    output_page = serializers.IntegerField(min_value=1, default=1)
    output_page_size = serializers.IntegerField(min_value=1, max_value=100, default=25)
    content_field = serializers.ChoiceField(choices=CONTENT_FIELDS, required=False)
    content_offset = serializers.IntegerField(min_value=0, default=0)
    content_limit = serializers.IntegerField(min_value=4, max_value=65536, default=16384)


class TaskCenterOptionsQuerySerializer(ClosedQuerySerializer):
    domain = serializers.ChoiceField(choices=("all", "projects", "execution_versions"), default="all")
    search = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=25)


class TaskCenterRowSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=("preparation", "generation_task", "assembly"))
    id = serializers.UUIDField()
    category = serializers.CharField()
    title = serializers.CharField()
    status = serializers.CharField()
    operation = serializers.CharField(allow_blank=True)
    project_id = serializers.UUIDField(allow_null=True)
    project_title = serializers.CharField(allow_blank=True)
    production_run_id = serializers.UUIDField(allow_null=True)
    execution_instance_version_id = serializers.UUIDField(allow_null=True)
    execution_instance_name = serializers.CharField(allow_blank=True)
    execution_version = serializers.IntegerField(allow_null=True)
    preparation_id = serializers.UUIDField(allow_null=True)
    source_id = serializers.UUIDField(allow_null=True)
    source_kind = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)
    estimated_cost = serializers.DecimalField(max_digits=14, decimal_places=6, allow_null=True)
    actual_cost = serializers.DecimalField(max_digits=14, decimal_places=6, allow_null=True)
    cost_currency = serializers.CharField(allow_blank=True)
    anomaly = serializers.BooleanField()


class TaskCenterListSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = TaskCenterRowSerializer(many=True)
    counts = serializers.DictField(child=serializers.IntegerField())
    read_at = serializers.DateTimeField()


class TaskSubmissionEvidenceSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=("recorded", "unavailable", "inconsistent"))
    started_at = serializers.DateTimeField(allow_null=True, help_text="原调用submission_started_at，跨越本地单次提交边界，不证明已外呼。")
    registered_at = serializers.DateTimeField(allow_null=True, help_text="原调用submitted_at，可能由worker、恢复或人工核对登记，不等于网络发送时刻。")
    identifier_state = serializers.ChoiceField(choices=("recorded", "digest_only", "not_recorded", "unavailable", "inconsistent"),
        help_text="仅呈现原请求标识的存储／摘要核对状态，不返回标识或摘要；标识可能在提交前分配。")
    message = serializers.CharField()
    notes = serializers.ListField(child=serializers.CharField())


class TaskCenterActionsSerializer(serializers.Serializer):
    cancel_smoke = serializers.BooleanField()
    cancel_asset = serializers.BooleanField()


class TaskCenterDetailSerializer(TaskCenterRowSerializer):
    outcome = PreparationOutcomeSerializer(allow_null=True, help_text="准备与精确选中调用的只读说明及产物计数；Smoke和无准备记录为null。计数不是文件可用性证明。")
    submission = TaskSubmissionEvidenceSerializer(help_text="当前精确选中调用的本地提交字段；无额外查询或外呼，不重建发送正文，不推断送达，不改变任务／费用／审核事实。")
    actions = TaskCenterActionsSerializer()
    purpose = serializers.CharField(allow_null=True)
    preparation_status = serializers.CharField(allow_null=True)
    request_frozen_at = serializers.DateTimeField(allow_null=True)
    request_sha256 = serializers.CharField(allow_null=True)
    input = serializers.JSONField(help_text=(
        "media_trace.items.reference_binding复用原准备逐使用对应，含position、original_name、reference_name、name_disambiguated、input_slot和shot_reference；策略未记录的名称/序号为null，未记录分镜版本为null。不重新消歧、不读取当前版本，矛盾走input.error。"
        "原冻结输入、参数和消息；prompt_trace只读原策略装配审计及显式输入接线，state为recorded/unavailable。field_batch_trace解释字段生成/翻译的原批次及user JSON，state为matched/mismatch/unavailable。media_trace只读Comfy冻结媒体用途、实际图入口/连接和当前根任务暂存回执，state为recorded；scalar_wiring通过精确工作流版本及合同摘要核对声明标量端口，state为matched/mismatch/partial/unavailable，rows为matched/mismatch/omitted/unavailable；版本缺失不替换为最新版本。port_routes按逐次素材使用核对原图到冻结声明消费端口的一条最短路径；state为connected/disconnected/unavailable，edge_indexes引用同一media_trace.edges，从0计数；直接加载端口可无中间边，不代表运行或送达。"
        "processing_summary分列冻结声明declared与原处理元数据observed；state为not_applicable/declared/recorded/partial/unverifiable，行含key/label/unit/source/state/value，行state为recorded/missing/unavailable/invalid。byte_identity只比较原来源与提交摘要，shared_with说明内嵌原音共用视频记录；不重新探测或回填缺值。"
        "media_trace.nodes/edges列完整冻结图，包含无参考素材请求、上游及未连通节点；节点存在不代表已执行。origin为original/added/replaced/unavailable，按精确原图ID与类型比较。inputs含input_name/change(unchanged/changed/added/unavailable)/kind(literal/connection/media/redacted)/value，media及redacted的value为null；removed_inputs列原同类型节点已移除端口。缺基线不称未变，类型替换不推断字段继承；仅为编译前后配置差异，不证明节点算法、送达或实际运行结果。"
        "api_mapping按原配置摘要核对受限REST顶层字段，state为verified/unavailable，rows含key/field及included/omitted/unmapped状态；仅声明映射，不是已发送正文或送达证明。native_input_trace核对原生影视规则/剧本/独立规范化冻结计划与user JSON，state为matched/mismatch/partial/unavailable，rows含label/source/destination及matched/mismatch/unavailable状态，unmapped_count标明未解释字段数；规范化复用原条目的保护词转换，核对完整批次、返回骨架及策略数据，来源路径注明转换；不校验system平台正文。system_message_trace对新冻结原生文本及字段生成/翻译请求记录的system_message_layout核对完整连续分段、Unicode字符偏移和SHA256，state为matched/mismatch/unavailable，rows含label/source/start/end/state；start为从0计数且end不包含。无记录不反推历史，仅说明组装来源，不证明送达或执行成功。回执缺失不等于失败，损坏走input.error。null表示不适用、无冻结记录或按需加载。不重新编译、查询当前策略或补写历史。"))
    output = serializers.JSONField(help_text="原结果及校验证据；原生生产、素材及辅助文本的result.provider_response为原调用用量回执，含state(recorded/unavailable/unverifiable)、task_id、observed_at、usage。usage仅白名单非负整数prompt_tokens/completion_tokens/total_tokens，缺项未知，不作为实际费用或审核事实。历史未记录不补造。")
    validation = serializers.JSONField()
    calls = serializers.JSONField()
    call_count = serializers.IntegerField()
    calls_next = serializers.CharField(allow_null=True)
    calls_previous = serializers.CharField(allow_null=True)
    output_count = serializers.IntegerField()
    output_next = serializers.CharField(allow_null=True)
    output_previous = serializers.CharField(allow_null=True)
    links = serializers.JSONField()
    deferred_fields = serializers.JSONField()
    content = serializers.JSONField(required=False)


class TaskCenterOptionsSerializer(serializers.Serializer):
    categories = serializers.JSONField()
    statuses = serializers.JSONField()
    operations = serializers.JSONField()
    projects = serializers.JSONField()
    execution_versions = serializers.JSONField()
    pagination = serializers.JSONField()
