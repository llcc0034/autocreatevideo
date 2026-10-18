"""Persist the safe API response atomically with its contract, not the raw graph.

Readback never touches a host or recomputes a projection. A missing receipt is
unknown, not evidence that the original request did not commit.
"""
import uuid

from django.db import IntegrityError, transaction
from rest_framework.exceptions import APIException, NotFound

from studio.models import ComfyWorkflowContractDraft, ComfyWorkflowPreviewReceipt
from studio.services.json_contracts import assign_json_contract, json_contract, verify_json_contract
from studio.services.comfyui_workflow_imports import _workflow_graph_max_bytes


class PreviewReceiptConflict(APIException):
    status_code = 409
    default_code = "workflow_preview_receipt_conflict"
    default_detail = "原请求标识已用于其他合同请求；请只读核对，不能覆盖回执。"


def request_identity(deployment, data):
    profile = data.get("model_profile_version")
    return {
        "deployment_id": str(deployment.pk),
        "source_path": data["source_path"],
        "expected_sha256": data["expected_sha256"],
        "expected_source_scope_schema_version": data["expected_source_scope_schema_version"],
        "expected_source_scope_sha256": data["expected_source_scope_sha256"],
        "primary_output_node_ids": [str(value) for value in data["primary_output_node_ids"]],
        "model_profile_version": str(profile.pk) if profile else None,
    }


def read_receipt(receipt):
    return verify_json_contract(
        receipt.response, schema_version=receipt.response_schema_version,
        size_bytes=receipt.response_size_bytes, sha256=receipt.response_sha256,
        max_bytes=_workflow_graph_max_bytes(),
    ).value


def find_preview_receipt(*, owner, deployment, request_key):
    receipt = ComfyWorkflowPreviewReceipt.objects.filter(
        owner=owner, deployment=deployment, request_key=request_key,
    ).first()
    if receipt is None:
        raise NotFound("尚未查到原请求回执，不代表未保存；请稍后只读核对。")
    return read_receipt(receipt)


def commit_preview_receipt(*, owner, deployment, data, build):
    """The builder returns the existing serializer's safe public response."""
    data = dict(data)
    # Output discovery has no write and therefore no receipt.
    request_key = data.pop("request_key", "") or str(uuid.uuid4())
    if not data["primary_output_node_ids"]:
        return build(data, lambda save: save())
    identity = request_identity(deployment, data)
    fingerprint = json_contract(identity).sha256

    def existing_response():
        receipt = ComfyWorkflowPreviewReceipt.objects.filter(owner=owner, request_key=request_key).first()
        if receipt is None:
            return None
        if receipt.request_sha256 != fingerprint:
            raise PreviewReceiptConflict()
        return read_receipt(receipt)

    existing = existing_response()
    if existing is not None:
        return existing
    def persist(save):
        with transaction.atomic():
            existing = existing_response()
            if existing is not None:
                return existing
            response = dict(save())
            draft = ComfyWorkflowContractDraft.objects.get(pk=response["contract_draft_id"])
            response["request_receipt"] = {
                "request_key": request_key, "candidate_source_id": str(draft.candidate_source_id),
                "request": identity,
            }
            receipt = ComfyWorkflowPreviewReceipt(owner=owner, deployment=deployment,
                request_key=request_key, request_sha256=fingerprint, contract_draft=draft)
            assign_json_contract(receipt, "response", response, max_bytes=_workflow_graph_max_bytes())
            receipt.save(force_insert=True)
            return response
    try:
        return build(data, persist)
    except IntegrityError:
        # The losing transaction (including any new contract) has rolled back.
        existing = existing_response()
        if existing is None:
            raise
        return existing
    except Exception:
        # Another request may have committed while this one's host read or
        # projection failed. Prefer the exact proof, never rerun the writer.
        existing = existing_response()
        if existing is not None:
            return existing
        raise
