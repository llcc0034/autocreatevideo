from rest_framework import serializers


COLOR_CHOICES = (
    "black", "charcoal", "gray", "slate", "ivory", "white", "brown", "sepia",
    "navy", "blue", "cyan", "teal", "green", "amber", "gold", "copper", "orange",
    "red", "crimson", "magenta", "violet",
)


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Must be a JSON object.")
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({key: "Unsupported field." for key in sorted(unknown)})
        return super().to_internal_value(data)


class VisualRuleColorDesignSerializer(StrictSerializer):
    base_colors = serializers.ListField(
        child=serializers.ChoiceField(choices=COLOR_CHOICES),
        min_length=1, max_length=3,
    )
    accent_color = serializers.ChoiceField(choices=(*COLOR_CHOICES, "none"))
    palette_relation = serializers.ChoiceField(
        choices=("monochromatic", "analogous", "complementary", "neutral", "cinematic"),
    )
    lighting = serializers.ChoiceField(
        choices=(
            "soft_diffuse", "hard_directional", "low_key", "high_key", "practical",
            "volumetric", "rim_accent", "balanced_ambient",
        ),
    )
    contrast = serializers.ChoiceField(choices=("low", "restrained", "medium", "high"))
    saturation = serializers.ChoiceField(
        choices=("desaturated", "muted", "natural", "selective", "saturated"),
    )
    exposure = serializers.ChoiceField(choices=("dark", "balanced", "bright"))
    progression = serializers.ChoiceField(
        choices=(
            "stable", "cool_to_warm", "warm_to_cool", "dark_to_bright",
            "bright_to_dark", "muted_to_saturated", "saturated_to_muted",
        ),
    )
    preserve_natural_skin_exposure = serializers.BooleanField()

    def to_internal_value(self, data):
        if isinstance(data, dict):
            if not isinstance(data.get("base_colors"), list):
                raise serializers.ValidationError({"base_colors": "Must be a JSON array."})
            if type(data.get("preserve_natural_skin_exposure")) is not bool:
                raise serializers.ValidationError({
                    "preserve_natural_skin_exposure": "Must be a JSON boolean.",
                })
        return super().to_internal_value(data)

    def validate_base_colors(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("base_colors must not contain duplicates.")
        return value


class VisualRuleDraftFieldsSerializer(serializers.Serializer):
    negative_prompt = serializers.CharField(max_length=4_000, trim_whitespace=False)
    color_script = serializers.CharField(max_length=3_000, trim_whitespace=False)
    continuity_summary = serializers.CharField(max_length=4_000, trim_whitespace=False)


class VisualRuleDraftGenerateSerializer(serializers.Serializer):
    attempt = serializers.IntegerField(min_value=1, max_value=100, default=1)
    direction_note = serializers.CharField(
        max_length=2_000, required=False, allow_blank=True, default="",
        trim_whitespace=False,
    )
    variation_note = serializers.CharField(
        max_length=2_000, required=False, allow_blank=True, default="",
        trim_whitespace=False,
    )
    expected_context_fingerprint = serializers.RegexField(
        r"^[0-9a-f]{64}$", required=False, allow_blank=False,
    )


class VisualRuleDraftRuntimeSerializer(serializers.Serializer):
    provider = serializers.CharField()
    model = serializers.CharField()
    loaded_on_demand = serializers.BooleanField()
    unloaded_after_request = serializers.BooleanField()
    review_required = serializers.BooleanField()
    persisted = serializers.BooleanField()
    elapsed_ms = serializers.IntegerField(allow_null=True, required=False)
    repair_attempted = serializers.BooleanField()
    repair_succeeded = serializers.BooleanField()


class VisualRuleDraftSourceSummarySerializer(serializers.Serializer):
    project_fields = serializers.ListField(child=serializers.CharField())
    production_setting_fields = serializers.ListField(child=serializers.CharField())
    style_included = serializers.BooleanField()
    character_count = serializers.IntegerField(min_value=0)
    scene_count = serializers.IntegerField(min_value=0)
    prop_count = serializers.IntegerField(min_value=0)
    episode_summary_count = serializers.IntegerField(min_value=0)
    episode_scripts_included = serializers.BooleanField()
    shots_included = serializers.BooleanField()


class VisualRuleDraftGenerationSerializer(serializers.Serializer):
    attempt = serializers.IntegerField(min_value=1)
    direction_note_provided = serializers.BooleanField()
    variation_note_provided = serializers.BooleanField()
    generation_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")


class VisualRuleDraftFieldModesSerializer(serializers.Serializer):
    negative_prompt = serializers.CharField(
        help_text="Always deterministic_guard; Qwen cannot rewrite this field.",
    )
    color_script = serializers.CharField(
        help_text="Always structured_design; rendered only from the submitted color_design enums.",
    )
    continuity_summary = serializers.CharField(
        help_text="Always deterministic_guard; Qwen cannot rewrite this field.",
    )


class VisualRuleDraftResponseSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    context_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")
    context_current = serializers.BooleanField()
    candidate_language = serializers.ChoiceField(choices=("en",))
    review_required = serializers.BooleanField()
    persisted = serializers.BooleanField()
    color_design = VisualRuleColorDesignSerializer()
    draft = VisualRuleDraftFieldsSerializer()
    field_modes = VisualRuleDraftFieldModesSerializer()
    warnings = serializers.ListField(child=serializers.CharField())
    source_summary = VisualRuleDraftSourceSummarySerializer()
    generation = VisualRuleDraftGenerationSerializer()
    runtime = VisualRuleDraftRuntimeSerializer()


class VisualRuleDraftApplySerializer(StrictSerializer):
    context_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")
    generation_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")
    attempt = serializers.IntegerField(min_value=1, max_value=100)
    direction_note = serializers.CharField(
        max_length=2_000, required=False, allow_blank=True, default="",
        trim_whitespace=False,
    )
    variation_note = serializers.CharField(
        max_length=2_000, required=False, allow_blank=True, default="",
        trim_whitespace=False,
    )
    color_design = VisualRuleColorDesignSerializer()


class VisualRuleDraftApplyResponseSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    applied_context_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")
    current_context_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")
    updated_at = serializers.DateTimeField()
    applied = VisualRuleDraftFieldsSerializer()
