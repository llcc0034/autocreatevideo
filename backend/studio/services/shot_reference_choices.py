"""Internal single-shot selection transaction, not a production command/API.

Only existing ShotReferenceUse selections are changed. The original Run/Stage
adapter must check frozen source policy, override permission and execution
state, and own its command receipt/replay in the enclosing transaction. This
service does not approve material, infer defaults, or resume any execution.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import re
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import Shot, ShotReferenceUse
from studio.services.json_contracts import canonical_json_bytes
from studio.services.shot_reference_sources import (
    ResolvedShotReferenceSources, _fail, _reference_project, _resolve_rows, _use_rows, _uuid,
)


_SELECTION_FIELDS = ("source_mode", "reference_version_id", "asset_id", "asset_sha256")
_CHOICE_FIELDS = frozenset(("use_id", *_SELECTION_FIELDS))


@dataclass(frozen=True)
class ShotReferenceChoiceState:
    project_id: str
    shot_id: str
    _uses: tuple

    @property
    def value(self):
        return {"schema_version": 1, "project_id": self.project_id, "shot_id": self.shot_id,
            "uses": deepcopy(list(self._uses))}

    @property
    def sha256(self):
        return sha256(canonical_json_bytes(self.value)).hexdigest()


@dataclass(frozen=True)
class AppliedShotReferenceChoices:
    before_sha256: str
    state: ShotReferenceChoiceState
    changed_use_ids: tuple[str, ...]
    # Only submitted uses have been resolved, not necessarily the entire shot.
    selected: ResolvedShotReferenceSources


def _state(project, shot_id, rows):
    return ShotReferenceChoiceState(str(project.pk), shot_id, tuple({
        key: str(value) if isinstance(value, UUID) else value for key, value in row.items()
    } for row in rows))


def _shot(project, shot_id, *, lock=False):
    shot_id = _uuid(shot_id, "shot_id")
    shots = Shot.objects.filter(pk=shot_id, episode__project_id=project.pk).only("id")
    if lock:
        shots = shots.select_for_update(of=("self",))
    if shots.first() is None:
        _fail("shot_id", "镜头不存在或不属于本项目")
    return shot_id


def read_shot_reference_choices(*, actor, project_id, shot_id):
    """Read the selection baseline, including pending uses, without file I/O.

    The fingerprint covers exact declarations and choices, not timestamps.
    It is an optimistic edit baseline, never proof of usable execution inputs.
    """
    project = _reference_project(actor, project_id)
    shot_id = _shot(project, shot_id)
    return _state(project, shot_id, list(_use_rows([shot_id])))


def _sha(value, path):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _fail(path, "需要明确的64位小写SHA256")
    return value


def _choices(value):
    if not isinstance(value, list) or not value:
        _fail("choices", "需要非空的用途选择数组")
    choices = {}
    for index, choice in enumerate(value):
        path = f"choices[{index}]"
        if not isinstance(choice, dict) or set(choice) != _CHOICE_FIELDS:
            _fail(path, "仅提交use_id、source_mode、reference_version_id、asset_id和asset_sha256")
        use_id = UUID(_uuid(choice["use_id"], f"{path}.use_id"))
        if use_id in choices:
            _fail(f"{path}.use_id", "同一用途不能重复提交")
        mode = choice["source_mode"]
        if not isinstance(mode, str) or mode not in {"existing", "upload", "generated"}:
            _fail(f"{path}.source_mode", "必须明确指定已有、上传或原生成来源")
        ids = {key: None if choice[key] is None else UUID(_uuid(choice[key], f"{path}.{key}"))
            for key in ("reference_version_id", "asset_id")}
        if sum(pk is not None for pk in ids.values()) != 1:
            _fail(path, "必须且只能选择一个确切素材版本或直接Asset")
        choices[use_id] = {"source_mode": mode, **ids,
            "asset_sha256": _sha(choice["asset_sha256"], f"{path}.asset_sha256")}
    return choices


@transaction.atomic
def apply_shot_reference_choices(*, actor, project_id, shot_id, expected_uses_sha256, choices):
    """Validate all submitted choices before writing any; preserve other uses.

    Lock Project -> Shot -> ordered uses, matching the source writer. A stale
    baseline is a conflict even when the desired values happen to match: only
    the original command receipt may prove a retry already succeeded.
    """
    project = _reference_project(actor, project_id, lock=True)
    shot_id = _shot(project, shot_id, lock=True)
    expected = _sha(expected_uses_sha256, "expected_uses_sha256")
    choices = _choices(choices)
    rows = list(_use_rows([shot_id]).select_for_update())
    before = _state(project, shot_id, rows)
    if before.sha256 != expected:
        raise ValidationError({"expected_uses_sha256": ValidationError(
            "镜头用途或选材已改变，请重新读取后确认；不覆盖其他修改", code="shot_reference_choices_stale")})
    if set(choices) - {row["id"] for row in rows}:
        _fail("choices", "所选用途缺失或不属于本镜头，不能新增或改写其他镜头用途")
    prospective = [{**row, **choices.get(row["id"], {})} for row in rows]
    selected = _resolve_rows(project, [shot_id], [row for row in prospective if row["id"] in choices])
    changed_ids = []
    now = timezone.now()
    for original, proposed in zip(rows, prospective):
        if any(original[key] != proposed[key] for key in _SELECTION_FIELDS):
            written = ShotReferenceUse.objects.filter(pk=original["id"], shot_id=shot_id).update(
                **{key: proposed[key] for key in _SELECTION_FIELDS}, updated_at=now)
            if written != 1:
                _fail("choices", "用途写入目标已变化，本镜头全部选择回滚")
            changed_ids.append(str(original["id"]))
    return AppliedShotReferenceChoices(expected, _state(project, shot_id, prospective), tuple(changed_ids), selected)
