import re
import unicodedata

from rest_framework import serializers

from studio.models import BibleVariant, BibleVariantRevision, BibleVariantReview
from studio.serializers import StrictSerializer
from studio.services.bible_reference_contract import bible_reference_name_key


BIBLE_SETTING_SOURCE_ORIGINS = ("manual", "missing", "source_value", "configured_literal", "configured_default")


class BibleAppearanceSource(serializers.Serializer):
    binding_id = serializers.UUIDField()
    script_revision_id = serializers.UUIDField()
    entity_key = serializers.CharField()
    variant_key = serializers.CharField(allow_null=True)
    declaration_sha256 = serializers.CharField()


class VariantNameField(serializers.CharField):
    def __init__(self, **kwargs):
        super().__init__(max_length=160, **kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if any(unicodedata.category(c) in {"Cc", "Cf"} for c in value) or any(c in value for c in "<>[]{}"):
            self.fail("invalid")
        if len(bible_reference_name_key(value)) > 191:
            self.fail("invalid")
        return value


class VariantAnchorField(serializers.CharField):
    """UTF-8 bounded detail, not a list payload or unbounded JSON."""
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if len(value.encode("utf-8")) > 16 * 1024:
            raise serializers.ValidationError("单份锚点不能超过 16 KiB UTF-8；请拆分明确的变体")
        return value


class BibleVariantSettingInput(StrictSerializer):
    name = VariantNameField()
    reference_name = VariantNameField()
    identity_anchor = VariantAnchorField(allow_blank=True)
    appearance_anchor = VariantAnchorField(allow_blank=True)
    basis_asset_id = serializers.UUIDField(allow_null=True)
    basis_asset_sha256 = serializers.CharField(allow_blank=True, max_length=64)

    def validate(self, attrs):
        # Creative blanks are saved as authored; execution contracts decide
        # what a later generation request requires, not an earlier revision.
        digest = attrs["basis_asset_sha256"]
        if attrs["basis_asset_id"] is not None and not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise serializers.ValidationError({"basis_asset_sha256": "身份参考图必须携带所见 SHA256"})
        if attrs["basis_asset_id"] is None and digest:
            raise serializers.ValidationError({"basis_asset_sha256": "没有参考图时摘要必须为空"})
        return attrs


class BibleVariantCreateInput(BibleVariantSettingInput):
    project_id = serializers.UUIDField()
    entity_kind = serializers.ChoiceField(choices=("character", "scene", "prop"))
    entity_id = serializers.UUIDField()
    appearance_role = serializers.ChoiceField(choices=BibleVariant._meta.get_field("appearance_role").choices)
    primary_setting_revision_id = serializers.UUIDField(allow_null=True)
    primary_setting_sha256 = serializers.CharField(allow_blank=True, max_length=64)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs["appearance_role"] == "primary":
            if attrs["primary_setting_revision_id"] is not None or attrs["primary_setting_sha256"]:
                raise serializers.ValidationError({"primary_setting_revision_id": "主图设定不能依赖其他设定"})
        elif attrs["primary_setting_revision_id"] is None or not re.fullmatch(r"[a-f0-9]{64}", attrs["primary_setting_sha256"]):
            raise serializers.ValidationError({"primary_setting_revision_id": "请选择确切的主图设定修订及所见摘要"})
        return attrs


class BibleVariantReviseInput(BibleVariantSettingInput):
    expected_revision = serializers.IntegerField(min_value=1)


class BibleVariantReviewInput(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    expected_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    decision = serializers.ChoiceField(choices=("approve", "reject"))
    comment = serializers.CharField(max_length=1000, allow_blank=True)


class BibleVariantPageQuery(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=50, required=False)


class BibleVariantSourceQuery(StrictSerializer):
    revision = serializers.IntegerField(min_value=1, max_value=2147483647)


class BibleSettingSourceField(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    declaration_value = serializers.CharField(allow_null=True, allow_blank=True)
    setting_value = serializers.CharField(allow_blank=True)
    origin = serializers.ChoiceField(choices=BIBLE_SETTING_SOURCE_ORIGINS)
    source_path = serializers.CharField(allow_null=True)
    changed_from_declaration = serializers.BooleanField(allow_null=True)


class BibleSettingSourceOutput(serializers.Serializer):
    appearance_id = serializers.UUIDField()
    setting_revision_id = serializers.UUIDField()
    setting_sha256 = serializers.CharField()
    source = BibleAppearanceSource(allow_null=True)
    document_sha256 = serializers.CharField(allow_null=True)
    fields = BibleSettingSourceField(many=True)


class BibleVariantQuery(BibleVariantPageQuery):
    project_id = serializers.UUIDField()
    entity_kind = serializers.ChoiceField(choices=("character", "scene", "prop"))
    entity_id = serializers.UUIDField()


class BibleVariantSummary(serializers.ModelSerializer):
    current_decision = serializers.CharField(read_only=True, allow_null=True)
    source = BibleAppearanceSource(read_only=True, allow_null=True)

    class Meta:
        model = BibleVariant
        fields = ("id", "project_id", "character_id", "scene_id", "prop_id", "name", "reference_name",
            "revision", "current_decision", "created_by_id", "created_at", "updated_at", "appearance_role", "source")
        read_only_fields = fields


class BibleVariantReviewOutput(serializers.ModelSerializer):
    class Meta:
        model = BibleVariantReview
        fields = ("id", "decision", "comment", "decided_by_id", "created_at")
        read_only_fields = fields


class BibleVariantRevisionSummary(serializers.ModelSerializer):
    review = BibleVariantReviewOutput(read_only=True, allow_null=True)
    primary_setting_revision_id = serializers.UUIDField(read_only=True, allow_null=True)

    class Meta:
        model = BibleVariantRevision
        fields = ("id", "revision", "name", "reference_name", "basis_asset_id", "basis_asset_sha256",
            "content_sha256", "created_by_id", "created_at", "review", "payload_schema_version",
            "source_declaration_sha256", "primary_setting_revision_id")
        read_only_fields = fields


class BibleVariantSettingOutput(BibleVariantRevisionSummary):
    class Meta(BibleVariantRevisionSummary.Meta):
        fields = (*BibleVariantRevisionSummary.Meta.fields, "identity_anchor", "appearance_anchor")
        read_only_fields = fields


class BiblePrimarySettingOutput(BibleVariantSettingOutput):
    variant_id = serializers.UUIDField(read_only=True)

    class Meta(BibleVariantSettingOutput.Meta):
        fields = (*BibleVariantSettingOutput.Meta.fields, "variant_id")
        read_only_fields = fields


class BibleVariantDetail(BibleVariantSummary):
    setting = BibleVariantSettingOutput(read_only=True)
    primary_setting = BiblePrimarySettingOutput(read_only=True, allow_null=True)

    class Meta(BibleVariantSummary.Meta):
        fields = (*BibleVariantSummary.Meta.fields, "setting", "primary_setting")
        read_only_fields = fields
