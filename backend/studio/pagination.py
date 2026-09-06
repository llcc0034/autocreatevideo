from rest_framework.pagination import PageNumberPagination

from studio.services.configuration import ConfigurationResolutionError, resolve_configuration
from studio.services.security_boundaries import boundary_int


def _api_default_page_size(*, maximum: int) -> int:
    value = resolve_configuration(
        required_keys=("api.default_page_size",),
    ).values["api.default_page_size"]
    if type(value) is not int or not 1 <= value <= maximum:
        raise ConfigurationResolutionError(
            "api.default_page_size 必须是不超过 security.api.max_page_size 的正整数",
            code="api_default_page_size_invalid",
            missing_keys=("api.default_page_size",),
        )
    return value


class SecurityBoundedPageNumberPagination(PageNumberPagination):
    page_size_query_param = "page_size"

    def get_page_size(self, request):
        self.max_page_size = boundary_int("security.api.max_page_size")
        self.page_size = _api_default_page_size(maximum=self.max_page_size)
        return super().get_page_size(request)


class StandardPageNumberPagination(SecurityBoundedPageNumberPagination):
    pass


class WorkflowSmokeCandidatePagination(SecurityBoundedPageNumberPagination):
    pass


class AcceptanceSmokePagination(SecurityBoundedPageNumberPagination):
    """Bound the cross-registry acceptance projection independently."""

    acceptance_max_page_size = 200

    def get_page_size(self, request):
        security_maximum = boundary_int("security.api.max_page_size")
        self.max_page_size = min(security_maximum, self.acceptance_max_page_size)
        self.page_size = min(
            _api_default_page_size(maximum=security_maximum),
            self.max_page_size,
        )
        return PageNumberPagination.get_page_size(self, request)
