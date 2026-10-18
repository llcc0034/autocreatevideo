"""GET-only task aggregation; ownership is enforced before projection."""
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.task_center import list_tasks, options
from studio.task_center_details import task_detail
from studio.task_center_serializers import (TaskCenterDetailQuerySerializer, TaskCenterDetailSerializer,
    TaskCenterListSerializer, TaskCenterOptionsQuerySerializer, TaskCenterOptionsSerializer, TaskCenterQuerySerializer)


class TaskCenterBaseView(APIView):
    permission_classes = (IsAuthenticated,)
    http_method_names = ("get", "head", "options")

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def check_permissions(self, request):
        super().check_permissions(request)
        if not request.user.is_active:
            raise PermissionDenied("当前登录身份不可用")

    @staticmethod
    def query(request, serializer_class):
        serializer = serializer_class(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


class TaskCenterListView(TaskCenterBaseView):
    @extend_schema(operation_id="task_center_list", parameters=[TaskCenterQuerySerializer], responses=TaskCenterListSerializer)
    def get(self, request):
        data = list_tasks(request.user, self.query(request, TaskCenterQuerySerializer), request)
        return Response(TaskCenterListSerializer(data).data)


class TaskCenterDetailView(TaskCenterBaseView):
    @extend_schema(operation_id="task_center_detail", parameters=[TaskCenterDetailQuerySerializer], responses=TaskCenterDetailSerializer)
    def get(self, request, kind, identifier):
        data = task_detail(request.user, kind, identifier, self.query(request, TaskCenterDetailQuerySerializer), request)
        return Response(TaskCenterDetailSerializer(data).data)


class TaskCenterOptionsView(TaskCenterBaseView):
    @extend_schema(parameters=[TaskCenterOptionsQuerySerializer], responses=TaskCenterOptionsSerializer)
    def get(self, request):
        data = options(request.user, self.query(request, TaskCenterOptionsQuerySerializer), request)
        return Response(TaskCenterOptionsSerializer(data).data)
