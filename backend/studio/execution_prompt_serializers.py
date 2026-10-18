"""Read-only prompt display, explicitly separating candidates from frozen facts."""
from rest_framework import serializers


class ExecutionPromptQuerySerializer(serializers.Serializer):
    expected_preparation_revision = serializers.IntegerField(min_value=1, max_value=2147483647)

    def to_internal_value(self, data):
        import re
        if set(data) != {"expected_preparation_revision"} or not re.fullmatch(r"[1-9][0-9]{0,9}", data.get("expected_preparation_revision", "")):
            raise serializers.ValidationError({"query": "请只提供明确的正整数准备修订号"})
        return super().to_internal_value(data)


class PromptChannelSerializer(serializers.Serializer):
    key = serializers.CharField()
    text = serializers.CharField(allow_blank=True, trim_whitespace=False)
    character_count = serializers.IntegerField(min_value=0)


class PromptMessageSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=("system", "user", "assistant"))
    content = serializers.CharField(allow_blank=True, trim_whitespace=False)
    character_count = serializers.IntegerField(min_value=0)


class PromptLengthFragmentSerializer(serializers.Serializer):
    field_key = serializers.CharField()
    label = serializers.CharField()
    field_definition_version_id = serializers.UUIDField()
    source_selector = serializers.CharField()
    max_chars = serializers.IntegerField(min_value=1)
    text_chars = serializers.IntegerField(min_value=0)
    separator_chars = serializers.IntegerField(min_value=0)


class PromptLengthLimitSourceSerializer(serializers.Serializer):
    source = serializers.CharField()
    max_chars = serializers.IntegerField(min_value=1, allow_null=True)


class PromptProfileBasisSerializer(serializers.Serializer):
    version_id = serializers.UUIDField()
    version = serializers.IntegerField(min_value=1)
    contract_sha256 = serializers.CharField()


class PromptLengthDiagnosticSerializer(serializers.Serializer):
    channel = serializers.CharField()
    character_count = serializers.IntegerField(min_value=0)
    max_chars = serializers.IntegerField(min_value=1, allow_null=True)
    over_by = serializers.IntegerField(min_value=0)
    fragments = PromptLengthFragmentSerializer(many=True)
    limit_sources = PromptLengthLimitSourceSerializer(many=True)


class PromptFieldBasisSerializer(serializers.Serializer):
    unit_key = serializers.CharField()
    field_key = serializers.CharField()
    field_value_id = serializers.UUIDField()
    revision = serializers.IntegerField(min_value=1)
    origin = serializers.ChoiceField(choices=("adopted", "candidate"))
    status = serializers.ChoiceField(choices=("pending", "confirmed"))


class PromptReferenceSerializer(serializers.Serializer):
    label = serializers.CharField(allow_blank=True)
    name = serializers.CharField(allow_blank=True)
    category = serializers.CharField()
    guidance = serializers.CharField(allow_blank=True)


class PromptDialogueSerializer(serializers.Serializer):
    unit_key = serializers.CharField()
    subject = serializers.CharField(allow_blank=True)
    text = serializers.CharField(allow_blank=True, trim_whitespace=False)
    language = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False,
        help_text="本句实际发声语言；缺失或空值表示原记录未声明，不从项目或导演输出语言补值。")
    start_ms = serializers.IntegerField(min_value=0)
    end_ms = serializers.IntegerField(min_value=0)
    voiceover = serializers.BooleanField()
    delivery = serializers.CharField(allow_blank=True)


class PromptSourcesSerializer(serializers.Serializer):
    references = PromptReferenceSerializer(many=True)
    dialogue = PromptDialogueSerializer(many=True)


class ExecutionPromptDisplaySerializer(serializers.Serializer):
    preparation_id = serializers.UUIDField()
    preparation_revision = serializers.IntegerField(min_value=1)
    mode = serializers.ChoiceField(choices=("preview", "frozen", "unavailable", "diagnostic"))
    prompt_mode = serializers.ChoiceField(choices=("profile", "manual", "none"))
    content_kind = serializers.ChoiceField(choices=("channels", "messages", "not_applicable", "unavailable"),
        help_text="按冻结提示词类型投影：profile/manual保留完整通道；none聊天请求展示保存的消息及其中的结构化协议；none非聊天不适用；未能预览为unavailable。不重组、不表示已发送。")
    request_sha256 = serializers.CharField(allow_null=True)
    frozen_at = serializers.DateTimeField(allow_null=True)
    channels = PromptChannelSerializer(many=True)
    messages = PromptMessageSerializer(many=True, help_text="仅content_kind=messages时非空；按保存顺序返回payload.messages原文，字符数为Unicode码点数，不是影视对白。")
    field_basis = PromptFieldBasisSerializer(many=True)
    sources = PromptSourcesSerializer()
    issues = serializers.ListField(child=serializers.CharField())
    length_diagnostics = PromptLengthDiagnosticSerializer(many=True)
    profile_basis = PromptProfileBasisSerializer(allow_null=True)
