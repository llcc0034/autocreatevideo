from django.db.models.deletion import ProtectedError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    if isinstance(exc, ProtectedError):
        return Response(
            {
                "error": {
                    "status_code": status.HTTP_409_CONFLICT,
                    "code": "protected_resource",
                    "details": {
                        "detail": (
                            "该记录已被任务、发行清单或审计证据引用，不能直接删除。"
                            "请先创建替代版本，或保留该记录作为不可变证据。"
                        ),
                    },
                }
            },
            status=status.HTTP_409_CONFLICT,
        )
    response = exception_handler(exc, context)
    if response is None:
        return None
    response.data = {
        "error": {
            "status_code": response.status_code,
            "code": getattr(exc, "default_code", "error"),
            "details": response.data,
        }
    }
    return response
