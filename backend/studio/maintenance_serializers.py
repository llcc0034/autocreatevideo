from rest_framework import serializers


class AssetBatchDeleteInput(serializers.Serializer):
    asset_ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=200)
    confirmation = serializers.ChoiceField(choices=("DELETE_SELECTED_ASSETS",))

    def validate_asset_ids(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("素材 ID 不能重复。")
        return value


class AssetBatchDeleteResult(serializers.Serializer):
    deleted_ids = serializers.ListField(child=serializers.UUIDField())
    deleted_count = serializers.IntegerField()


class HistoryCleanupScope(serializers.Serializer):
    include_maintenance = serializers.BooleanField(default=False)


class HistoryCleanupInput(HistoryCleanupScope):
    pause_token = serializers.UUIDField()
    plan_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$")
    confirmation = serializers.ChoiceField(choices=("CLEAR_ALL_HISTORY_KEEP_ASSETS",))


class HistoryCleanupBlocker(serializers.Serializer):
    model = serializers.CharField()
    id = serializers.CharField()
    reason = serializers.CharField()
    label = serializers.CharField()
    detail_url = serializers.CharField(allow_null=True)
    link_label = serializers.CharField()
    status = serializers.CharField(allow_null=True)
    started_at = serializers.DateTimeField(allow_null=True)
    heartbeat_at = serializers.DateTimeField(allow_null=True)
    progress_marker = serializers.CharField(allow_null=True, allow_blank=True)


class HistoryCleanupPreview(serializers.Serializer):
    include_maintenance = serializers.BooleanField()
    plan_sha256 = serializers.CharField()
    delete_counts = serializers.DictField(child=serializers.IntegerField())
    preserve_counts = serializers.DictField(child=serializers.IntegerField())
    blockers = HistoryCleanupBlocker(many=True)
    can_execute = serializers.BooleanField()
    confirmation = serializers.CharField()
    files_deleted = serializers.BooleanField()


class HistoryCleanupResult(serializers.Serializer):
    deleted_counts = serializers.DictField(child=serializers.IntegerField())
    retained_assets = serializers.IntegerField()
    files_deleted = serializers.BooleanField()


class HistoryCleanupPauseInput(HistoryCleanupScope):
    token = serializers.UUIDField()


class HistoryCleanupPauseResult(HistoryCleanupPauseInput):
    expires_at = serializers.DateTimeField()


class HistoryCleanupPauseStatus(serializers.Serializer):
    active = serializers.BooleanField()
    expires_at = serializers.DateTimeField(allow_null=True)
    server_time = serializers.DateTimeField()
    include_maintenance = serializers.BooleanField()
