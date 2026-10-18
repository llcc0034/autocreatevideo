"""Bounded project readers shared by preview and frozen input resolution.

These are implemented reader protocols, not a user-extensible source registry.
Strategy versions select the semantic source; execution definitions freeze its
reader contract. A signed, explicitly confirmed preview supplies the value.
"""
from django.core.exceptions import ValidationError
from studio.models import SeriesProject, Style


PROJECT_SOURCE_READERS = {
    "project.logline": {"label": "项目梗概（已保存）", "location": "连续剧项目 → 项目设定 → 一句话梗概"},
    "style.visual_prompt": {"label": "项目视觉风格（已保存）", "location": "连续剧项目 → 视觉风格 → 风格视觉提示词"},
}


def project_source_reader(selector):
    if not isinstance(selector, str) or selector not in PROJECT_SOURCE_READERS:
        raise ValidationError("项目来源读取协议尚未支持此字段")
    from studio.services.execution_source_readers import implemented_source_reader
    return implemented_source_reader(selector)


def input_project_reader(spec, interface):
    from studio.services.execution_source_readers import input_source_reader
    reader = input_source_reader(spec, interface)
    return reader if reader and reader["protocol"] == "project_snapshot/v1" else None


def authorized_project(owner, project_id):
    if not project_id:
        raise ValidationError("请明确选择来源项目；不会猜测最近项目或使用示例值")
    project = SeriesProject.objects.select_related("style").only("id", "owner_id", "title", "logline", "updated_at", "style_id",
        "style__id", "style__owner_id", "style__name", "style__visual_prompt", "style__updated_at").filter(pk=project_id, owner=owner).first()
    if project is None:
        raise ValidationError("项目不存在或无权读取")
    return project


def read_project_source(owner, project_id, selector):
    from studio.services.execution_source_readers import implemented_source_reader
    if implemented_source_reader(selector)["protocol"] != "project_snapshot/v1":
        raise ValidationError("不是项目快照读取协议")
    project = authorized_project(owner, project_id)
    origin = {"project_id": str(project.pk), "project_name": project.title,
        "project_updated_at": project.updated_at.isoformat(), "location": PROJECT_SOURCE_READERS[selector]["location"]}
    if selector == "project.logline":
        content = project.logline
        origin.update(source_id=str(project.pk), source_updated_at=project.updated_at.isoformat())
    else:
        if project.style is None:
            raise ValidationError("当前项目未选择视觉风格，请先在项目中选择并保存")
        if project.style.owner_id not in (None, owner.pk):
            raise ValidationError("项目引用的视觉风格无权读取")
        content = project.style.visual_prompt
        origin.update(source_id=str(project.style_id), source_name=project.style.name,
            source_updated_at=project.style.updated_at.isoformat(),
            note="只读取视觉提示词，不包含风格简介、负面词、完整剧本或人物库。")
    if not content.strip():
        raise ValidationError("项目梗概或风格视觉提示词为空，请先在来源页面填写并保存")
    return content, origin


def verify_project_source_access(owner, receipt, project_id):
    """Recheck authorization, never substitute newer business content."""
    source_project_id = receipt.get("project_id")
    if project_id is not None and str(project_id) != source_project_id:
        raise ValidationError("来源项目与本次准备项目不一致，请重新预览")
    authorized_project(owner, source_project_id)
    if receipt["source_reader"]["selector"] == "style.visual_prompt":
        style = Style.objects.filter(pk=receipt["origin"]["source_id"]).only("owner_id").first()
        if style is None or style.owner_id not in (None, owner.pk):
            raise ValidationError("所确认的风格已不存在或无权读取，请重新预览")
