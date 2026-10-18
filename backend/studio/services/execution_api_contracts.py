"""Project existing restricted-REST contracts into the execution interface.

This module describes declared inputs, not a runnable/published instance. It
does not reuse Smoke examples as defaults, reinterpret chat operations as media
operations, or let callers select another model through a scalar parameter.
The complete definition builder still checks strategy wiring and output adapters.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.models import ProviderProfile
from studio.services.async_media_contract import async_operation_contract
from studio.services.async_media_schema import api_parameter_rule
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_media_parameters import model_media_parameter_layers
from studio.services.execution_parameters import compile_execution_parameter_contract, restrict_execution_parameters
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache


# These are the existing standard request protocol's channel names. They do not
# translate business operations or select a model-specific strategy.
ASYNC_PROMPT_CHANNELS = {"positive_prompt": "prompt", "negative_prompt": "negative_prompt"}


def validate_async_execution_wiring(version):
    """All declared request fields must reach the selected driver's mapping.

    A provider may map additional fields for another operation. Only fields
    declared by this exact operation participate; no missing mapping is inferred.
    """
    from studio.services.provider_registry import deployment_execution_config_contract

    declared = async_operation_contract(version.deployment.profile_version, version.operation)["request_schema"]["properties"]
    configuration = deployment_execution_config_contract(version.deployment).value
    missing = sorted(set(declared) - configuration["request_field_map"].keys())
    if missing:
        raise ValidationError({"deployment.request_field_map": "模型声明的参数尚未接入服务请求：" + ", ".join(missing)})


@security_boundary_resolution_cache()
def async_execution_interface(version):
    """Read one exact async-media operation; never infer it from output kind."""
    profile = version.deployment.profile_version
    if profile.provider.driver_type != ProviderProfile.DriverType.ASYNC_REST or version.workflow_version_id is not None:
        raise ValidationError({"operation": "此接口只适用于不含 Workflow 的受限 REST 媒体能力"})
    contract = async_operation_contract(profile, version.operation)
    schema = contract["request_schema"]
    required = set(schema["required"])
    rules = {key: api_parameter_rule(key, row, required=key in required) for key, row in schema["properties"].items()}
    source = f"model.{profile.pk}.async_media.operations.{version.operation}.request_schema"
    channels = {}
    for channel, key in ASYNC_PROMPT_CHANNELS.items():
        if key not in rules:
            continue
        if rules[key]["type"] != "string":
            raise ValidationError({f"prompt_channels.{channel}": "提示词通道必须声明为字符串"})
        channels[channel] = {"semantic_key": key, "rule": rules.pop(key), "source": source}
    # The driver supplies the selected profile's identifier. A request schema
    # can constrain it, but cannot turn it into another user-selectable model.
    model_rule = rules.pop("model", None)
    if model_rule is not None:
        if model_rule["type"] != "string":
            raise ValidationError({"parameters.model": "模型标识的 API 类型必须是字符串"})
        compile_execution_parameter_contract({"model": [
            {"source": source, "rule": model_rule},
            {"source": f"model.{profile.pk}.model_identifier", "rule": {"type": "string", "required": True, "const": profile.model_identifier}},
        ]})
    media = model_media_parameter_layers(profile, {key: key for key in ("width", "height", "fps", "duration_seconds") if key in rules})
    parameter_layers = {key: [{"source": source, "rule": rule}] for key, rule in rules.items()}
    for key, layer in media["layers"].items():
        parameter_layers[key].insert(0, layer)
    parameters = compile_execution_parameter_contract(parameter_layers)
    return json_contract({
        "operation": version.operation, "model_identifier": profile.model_identifier,
        "request_schema": deepcopy(schema),
        "input_targets": {}, "prompt_channels": channels, "parameters": parameters,
        "output_kind": contract["output_kind"], "media_constraints": media["media_constraints"],
        "media_semantic_keys": media["semantic_keys"],
    }, max_bytes=execution_configuration_max_bytes()).value


@security_boundary_resolution_cache()
def validate_async_execution_parameters(version):
    """Intersect explicit instance restrictions with all declared API scalars."""
    interface = async_execution_interface(version)
    return restrict_execution_parameters(
        interface["parameters"], version.parameters.all(), source=f"instance.{version.pk}.parameters",
    )


def validate_async_prompt_configuration(version):
    from studio.services.execution_prompts import validate_execution_prompt_channels

    interface = async_execution_interface(version)
    return validate_execution_prompt_channels(
        version, interface["prompt_channels"],
        source=f"model.{version.deployment.profile_version_id}.async_media.operations.{version.operation}.request_schema",
        target_label="API",
    )
