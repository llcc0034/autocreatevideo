"""Static production-deployment checks for hosts without Docker installed."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class DeploymentValidationError(RuntimeError):
    pass


class Checks:
    def __init__(self):
        self.count = 0
        self.errors: list[str] = []

    def require(self, condition: bool, message: str) -> None:
        self.count += 1
        if not condition:
            self.errors.append(message)

    def finish(self) -> None:
        if self.errors:
            rendered = "\n - ".join(self.errors)
            raise DeploymentValidationError(f"Production deployment validation failed:\n - {rendered}")
        print(f"Production deployment static validation passed ({self.count} checks).")


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def main() -> None:
    checks = Checks()
    compose = yaml.safe_load(_read("docker-compose.yml"))
    services = compose.get("services") or {}
    expected_services = {
        "db", "backend", "worker", "text-generation-worker",
        "async-media-worker", "assembly-worker", "platform-maintenance",
        "readiness", "frontend",
    }
    checks.require(expected_services <= set(services), "compose is missing a required production service")

    shared_environment = compose.get("x-backend-environment") or {}
    checks.require(
        shared_environment.get("AUTOPROMPT_DEPLOYMENT_PROFILE") == "production",
        "compose must opt into the fail-fast production settings profile",
    )
    checks.require(shared_environment.get("DJANGO_DEBUG") == "false", "compose must force DJANGO_DEBUG=false")
    for setting in (
        "SECURE_SSL_REDIRECT", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE",
    ):
        checks.require(shared_environment.get(setting) == "true", f"compose must force {setting}=true")
    for setting in (
        "DJANGO_SECRET_KEY", "AUTOPROMPT_JWT_SIGNING_KEY", "DJANGO_ALLOWED_HOSTS", "CORS_ALLOWED_ORIGINS",
        "CSRF_TRUSTED_ORIGINS", "DATABASE_URL",
        "AUTOPROMPT_CONFIG_MASTER_KEYS", "AUTOPROMPT_CONFIG_ACTIVE_KEY_ID",
        "AI_PROVIDER_ALLOWED_HOSTS",
    ):
        checks.require(bool(shared_environment.get(setting)), f"compose must pass {setting} to every backend process")
    operational_passthrough_settings = (
        "AUTOPROMPT_CONFIG_MASTER_KEYS", "AUTOPROMPT_CONFIG_ACTIVE_KEY_ID",
        "AI_PROVIDER_ALLOWED_HOSTS", "DRF_NUM_PROXIES",
        "COMFYUI_OUTPUT_DIR", "COMFYUI_EXTRA_ALLOWED_NODE_TYPES",
        "TASK_MAX_CONCURRENT",
        "FFMPEG_BINARY", "FFPROBE_BINARY",
    )
    documented_passthrough_settings = (
        "AUTOPROMPT_CONFIG_MASTER_KEYS", "AUTOPROMPT_CONFIG_ACTIVE_KEY_ID",
        "AI_PROVIDER_ALLOWED_HOSTS", "DRF_NUM_PROXIES",
        "COMFYUI_EXTRA_ALLOWED_NODE_TYPES", "TASK_MAX_CONCURRENT",
    )
    for setting in operational_passthrough_settings:
        checks.require(
            setting in shared_environment,
            f"compose must propagate production setting {setting}",
        )

    for service_name in (
        "backend", "worker", "text-generation-worker", "async-media-worker",
        "assembly-worker", "platform-maintenance",
    ):
        service = services.get(service_name) or {}
        environment = service.get("environment") or {}
        missing = sorted(set(shared_environment) - set(environment))
        checks.require(not missing, f"{service_name} is missing shared environment keys: {missing}")
        checks.require(service.get("read_only") is True, f"{service_name} root filesystem must be read-only")
        checks.require("ALL" in (service.get("cap_drop") or []), f"{service_name} must drop all Linux capabilities")
        checks.require(
            "no-new-privileges:true" in (service.get("security_opt") or []),
            f"{service_name} must set no-new-privileges",
        )
        checks.require(bool(service.get("tmpfs")), f"{service_name} must have an explicit writable tmpfs")

    for service_name in ("readiness", "frontend"):
        service = services.get(service_name) or {}
        checks.require(service.get("read_only") is True, f"{service_name} root filesystem must be read-only")
        checks.require("ALL" in (service.get("cap_drop") or []), f"{service_name} must drop all Linux capabilities")
        checks.require(
            "no-new-privileges:true" in (service.get("security_opt") or []),
            f"{service_name} must set no-new-privileges",
        )

    frontend_ports = services.get("frontend", {}).get("ports") or []
    checks.require(
        frontend_ports and all(str(port).startswith("127.0.0.1:") for port in frontend_ports),
        "frontend ports must be loopback-published for an external TLS terminator",
    )
    checks.require(not (services.get("db", {}).get("ports") or []), "PostgreSQL must not publish a host port")
    checks.require(
        "healthz" in str(services.get("frontend", {}).get("healthcheck", {}).get("test", "")),
        "frontend must have a static-service healthcheck",
    )
    backend_healthcheck = str(services.get("backend", {}).get("healthcheck", {}).get("test", ""))
    checks.require(
        "health/live" in backend_healthcheck and "X-Forwarded-Proto" in backend_healthcheck,
        "backend liveness must assert the trusted HTTPS proxy signal to avoid its production redirect",
    )
    readiness_healthcheck = str(services.get("readiness", {}).get("healthcheck", {}).get("test", ""))
    checks.require(
        "health/ready" in readiness_healthcheck and "X-Forwarded-Proto" in readiness_healthcheck,
        "readiness must gate frontend startup on the aggregate backend check without an HTTPS redirect",
    )
    volumes = compose.get("volumes") or {}
    checks.require("backend-static" in volumes, "collectstatic output must use a shared named volume")

    backend_dockerfile = _read("docker/backend.Dockerfile")
    frontend_dockerfile = _read("docker/frontend.Dockerfile")
    dockerignore = _read(".dockerignore")
    checks.require(
        re.search(r"(?m)^\.runtime(?:/\*\*)?$", dockerignore) is not None,
        "Docker build context must exclude runtime logs and process state",
    )
    checks.require(
        re.search(r"(?m)^backups(?:/\*\*)?$", dockerignore) is not None,
        "Docker build context must exclude database/media backup archives",
    )
    checks.require("USER 10001:10001" in backend_dockerfile, "backend runtime image must use the fixed non-root UID")
    checks.require("USER nginx" in frontend_dockerfile, "frontend runtime image must use the nginx non-root user")
    checks.require("EXPOSE 8080" in frontend_dockerfile, "non-root Nginx must listen on an unprivileged port")
    checks.require("HEALTHCHECK" in frontend_dockerfile, "frontend runtime image must define a healthcheck")
    for dockerfile_name, dockerfile in (
        ("backend", backend_dockerfile), ("frontend", frontend_dockerfile),
    ):
        from_lines = re.findall(r"(?m)^FROM\s+(\S+)", dockerfile)
        checks.require(
            bool(from_lines)
            and all(
                ":" in item.rsplit("/", 1)[-1]
                and ":latest" not in item.casefold()
                for item in from_lines
            ),
            f"{dockerfile_name} Dockerfile must not use an implicit/latest base image",
        )

    entrypoint = _read("docker/backend-entrypoint.sh")
    checks.require(
        "check --deploy --fail-level ERROR" in entrypoint,
        "backend bootstrap must run Django deployment checks before migration",
    )
    checks.require(
        "bootstrap_superadmin" not in entrypoint,
        "long-running backend bootstrap must not print a temporary superadministrator password into container logs",
    )

    nginx = _read("docker/nginx.conf")
    nginx_main = _read("docker/nginx-main.conf")
    security_headers = _read("docker/security-headers.conf")
    for header in (
        "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy",
        "Permissions-Policy", "Content-Security-Policy",
    ):
        checks.require(header in security_headers, f"Nginx must emit the {header} security header")
    checks.require("listen 8080;" in nginx, "Nginx must listen on an unprivileged port")
    checks.require("location = /healthz" in nginx, "Nginx must expose an internal health endpoint")
    checks.require("location ^~ /assets/library/" in nginx, "reference library must have a narrow URL prefix")
    library_block = re.search(
        r"location\s+\^~\s+/assets/library/\s*\{(?P<body>.*?)\n\s*\}",
        nginx,
        flags=re.DOTALL,
    )
    checks.require(
        bool(library_block) and "internal;" in library_block.group("body"),
        "raw reference-library URLs must be internal so application authorization cannot be bypassed",
    )
    checks.require(
        "location /assets/" in nginx and "root /usr/share/nginx/html" in nginx,
        "Vite hashed assets must be served from the built frontend",
    )
    checks.require(
        "alias /srv/autoprompt-assets/;" not in nginx,
        "a broad reference-library alias would shadow Vite assets",
    )
    checks.require("location /static/" in nginx, "Nginx must serve collected Django static files")
    checks.require(
        "$autoprompt_forwarded_proto" in nginx and "map $http_x_forwarded_proto" in nginx_main,
        "Nginx must preserve only the trusted HTTPS forwarding signal",
    )
    checks.require("7230s" not in nginx, "obsolete multi-hour synchronous API proxy timeouts must be removed")
    read_timeouts = [int(value) for value in re.findall(r"proxy_read_timeout\s+(\d+)s", nginx)]
    checks.require(read_timeouts and max(read_timeouts) <= 130, "API proxy read timeout must remain bounded")
    checks.require("pid /tmp/" in nginx_main, "non-root Nginx PID must live in writable tmpfs")

    root_env = _read(".env.example")
    local_env = _read("backend/.env.example")
    frontend_env = _read("frontend/.env.example")
    local_restart = _read("scripts/restart.ps1")
    checks.require(
        "AUTOPROMPT_DEPLOYMENT_PROFILE=production" in root_env,
        "Docker environment example must select production",
    )
    checks.require(
        "AUTOPROMPT_DEPLOYMENT_PROFILE=development" in local_env,
        "local backend environment example must explicitly remain development",
    )
    checks.require(
        "runserver', '127.0.0.1:8000'" in local_restart,
        "local Django restart entrypoint must remain loopback-only",
    )
    checks.require(
        "'--host', '127.0.0.1', '--port', '5173'" in local_restart,
        "local Vite restart entrypoint must remain loopback-only",
    )
    for setting in (
        "SECURE_SSL_REDIRECT=true", "SESSION_COOKIE_SECURE=true",
        "CSRF_COOKIE_SECURE=true", "CSRF_TRUSTED_ORIGINS=https://",
        "CORS_ALLOWED_ORIGINS=https://",
    ):
        checks.require(setting in root_env, f"Docker environment example is missing {setting}")
    for setting in documented_passthrough_settings:
        checks.require(
            re.search(rf"(?m)^{re.escape(setting)}=", root_env) is not None,
            f"Docker environment example must document production setting {setting}",
        )
    checks.require(
        "COMFYUI_OUTPUT_HOST_DIR=" in root_env,
        "Docker environment example must document the host ComfyUI output mount",
    )
    frontend_env_assignments = {
        match.group(1)
        for match in re.finditer(r"(?m)^\s*([A-Z][A-Z0-9_]*)\s*=", frontend_env)
    }
    frontend_env_content_lines = [
        line for line in frontend_env.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    checks.require(
        all(re.match(r"^\s*[A-Z][A-Z0-9_]*\s*=", line) for line in frontend_env_content_lines),
        "frontend environment example may contain only comments and KEY=value assignments",
    )
    checks.require(
        frontend_env_assignments == {"VITE_API_BASE_URL"},
        "frontend environment example may expose only VITE_API_BASE_URL",
    )
    checks.require(
        re.search(r"(?mi)^\s*VITE_(?:USE_)?MOCK", frontend_env) is None,
        "frontend environment example must not expose mock controls",
    )
    retired_local_process_env = (
        "QWEN_TRANSLATION_RUNTIME_PYTHON", "QWEN_TRANSLATION_DEVICE",
        "QWEN_CREATIVE_RUNTIME_PYTHON", "QWEN_CREATIVE_DEVICE",
        "QWEN_GPU_LAYERS",
    )
    for setting in retired_local_process_env:
        checks.require(
            re.search(rf"(?m)^\s*{re.escape(setting)}\s*=", local_env) is None,
            f"retired local-process setting {setting} must not remain in the backend environment example",
        )
    forbidden_business_env = (
        "ALLOW_PUBLIC_REGISTRATION", "ASSET_UPLOAD_MAX_BYTES",
        "COMFYUI_BASE_URL", "COMFYUI_TIMEOUT_SECONDS", "COMFYUI_CLIENT_ID",
        "MINIMAX_H3_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
        "H3_PROMPT_OPTIMIZER_ENABLED", "TASK_MAX_PENDING",
        "TASK_RECOMMENDED_MAX_PENDING", "ASSEMBLY_TIMEOUT_SECONDS",
    )
    for setting in forbidden_business_env:
        checks.require(
            setting not in shared_environment,
            f"business setting {setting} must come from the database, not compose",
        )
        checks.require(
            re.search(rf"(?m)^{re.escape(setting)}=", root_env) is None,
            f"business setting {setting} must not be documented as a deployment environment value",
        )

    checks.finish()


if __name__ == "__main__":
    main()
