from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.maintenance_serializers import (HistoryCleanupInput, HistoryCleanupPreview, HistoryCleanupResult,
    HistoryCleanupPauseInput, HistoryCleanupPauseResult, HistoryCleanupPauseStatus, HistoryCleanupScope)
from studio.permissions import IsSuperUser
from studio.services.history_cleanup import build_history_cleanup_plan, clear_history
from studio.services.history_cleanup_pause import pause_background_checks, resume_background_checks, background_pause_status


class HistoryCleanupView(APIView):
    permission_classes = (IsSuperUser,)

    @extend_schema(parameters=[HistoryCleanupScope], responses=HistoryCleanupPreview,
        description="全安装业务历史清理预览。include_maintenance默认false，保留主机探测、能力/文件观测和系统级推荐；true时一并预览这些维护历史。保留用户、配置、素材和库内主图/变体关系。仅超级管理员。")
    def get(self, request):
        scope = HistoryCleanupScope(data=request.query_params)
        scope.is_valid(raise_exception=True)
        report, _ = build_history_cleanup_plan(**scope.validated_data)
        return Response(HistoryCleanupPreview(report).data)

    @extend_schema(request=HistoryCleanupInput, responses=HistoryCleanupResult,
        description="锁内重新核对范围、暂停令牌、预览摘要和所选在途执行后，原子清理业务、审核、Smoke及审计历史。include_maintenance默认false，不等待或删除主机探测；true时同时清理维护历史且须等待探测终态。保留素材文件，不触发模型或重启。")
    def post(self, request):
        data = HistoryCleanupInput(data=request.data)
        data.is_valid(raise_exception=True)
        result = clear_history(actor=request.user, expected_sha256=data.validated_data["plan_sha256"],
            pause_token=data.validated_data["pause_token"], include_maintenance=data.validated_data["include_maintenance"])
        return Response(HistoryCleanupResult(result).data)


class HistoryCleanupPauseView(APIView):
    permission_classes = (IsSuperUser,)

    @extend_schema(responses=HistoryCleanupPauseStatus,
        description="只读当前后台检查暂停及到期时间，不创建或续期、不返回持有者token，也不代表删除进度。仅超级管理员。")
    def get(self, request):
        return Response(HistoryCleanupPauseStatus(background_pause_status()).data)

    @extend_schema(request=HistoryCleanupPauseInput, responses=HistoryCleanupPauseResult,
        description="暂停新的执行准备自动检查5分钟；仅include_maintenance=true时同时暂停新增定时主机探测。原在途继续结束，不取消或删除。令牌绑定范围，相同token及范围重放不续期。")
    def post(self, request):
        data = HistoryCleanupPauseInput(data=request.data)
        data.is_valid(raise_exception=True)
        result = pause_background_checks(actor=request.user, **data.validated_data)
        return Response(HistoryCleanupPauseResult(result).data)

    @extend_schema(request=HistoryCleanupPauseInput, responses={204: None},
        description="恢复本窗口暂停的后台检查；按操作者和token核对，不能释放其他窗口。")
    def delete(self, request):
        data = HistoryCleanupPauseInput(data=request.data)
        data.is_valid(raise_exception=True)
        resume_background_checks(actor=request.user, token=data.validated_data["token"])
        return Response(status=204)
