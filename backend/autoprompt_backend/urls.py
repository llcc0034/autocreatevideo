from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve
from pathlib import Path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

urlpatterns = [
    path("api/v1/", include("studio.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

if settings.DEBUG or settings.SERVE_ASSETS_WITH_DJANGO:
    urlpatterns += [
        re_path(
            r"^assets/library/(?P<path>.+)$",
            serve,
            {"document_root": Path(settings.ASSET_LIBRARY_ROOT) / "library"},
        ),
    ]
