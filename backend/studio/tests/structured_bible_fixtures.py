"""Explicit isolated M2 import structures; never native/model evidence."""
from copy import deepcopy
import json

from studio.services.business_outputs import output_reference, write_output
from studio.services.script_revision_structures import create_structured_script_source
from studio.services.script_sources import confirm_script_revision
from studio.tests.output_structure_fixtures import response_structure
from studio.tests.script_semantics_fixtures import each, literal, obj, path


def entities_fixture():
    return [
        {"key": "person_a", "kind": "character", "name": "海遥", "description": "车站工作人员",
            "identity_anchor": "黑色短发", "primary": {"reference_name": "海遥·主图", "anchor": "制服"},
            "variants": [{"key": "uniform", "name": "制服", "reference_name": "海遥·制服", "anchor": "白天穿制服"},
                {"key": "raincoat", "name": "雨衣", "reference_name": "海遥·雨衣", "anchor": "雨夜披着雨衣"}]},
        {"key": "station", "kind": "scene", "name": "车站", "description": "车站大厅", "identity_anchor": "木质长椅",
            "primary": {"reference_name": "車站ロビー", "anchor": "车站大厅有木质长椅"}, "variants": []},
    ]


def confirmed_structured_bible_source(testcase, *, entities=None, layout="a", episodes=None):
    entities = deepcopy(entities if entities is not None else entities_fixture())
    raw = {"素材设定": entities} if layout == "a" else {"world": {"cast": entities}}
    tokens = ("素材设定",) if layout == "a" else ("world", "cast")
    entity_mapping = path(*tokens, scope="root")
    if layout == "renamed":
        # External names are fixture configuration, not runtime defaults.
        raw = {"资源": [{"编号": entity["key"], "类别": entity["kind"], "显示名": entity["name"],
            "背景": entity["description"], "共性": entity["identity_anchor"],
            "造型": {"基准": {"素材名": entity["primary"]["reference_name"], "描述": entity["primary"]["anchor"]},
                "变化": [{"编号": variant["key"], "显示名": variant["name"],
                    "素材名": variant["reference_name"], "差异": variant["anchor"]}
                    for variant in entity["variants"]]}} for entity in entities]}
        entity_mapping = each(path("资源", scope="root"), obj(key=path("编号"), kind=path("类别"),
            name=path("显示名"), description=path("背景"), identity_anchor=path("共性"),
            primary=obj(reference_name=path("造型", "基准", "素材名"), anchor=path("造型", "基准", "描述")),
            variants=each(path("造型", "变化"), obj(key=path("编号"), name=path("显示名"),
                reference_name=path("素材名"), anchor=path("差异")))))
    elif layout not in {"a", "b"}:
        raise ValueError("unknown isolated Bible layout")
    if episodes:
        raw["分集"] = deepcopy(episodes)
    sample = deepcopy(raw)
    for episode in sample.get("分集", []):
        for shot in episode["shots"]:
            for use in shot.get("references", []):
                if use["variant_key"] is None:
                    use["variant_key"] = ""
    structure = response_structure(example=sample, nullable_fields=("variant_key",))
    output = write_output(actor=testcase.actor, data={"name": "Isolated Bible semantic input",
        "parser_key": "script_semantics.v1", "data_structure_version": str(structure.pk),
        "semantic_mapping": {"schema_version": 1,
            "node": obj(entities=entity_mapping,
                episodes=path("分集", scope="root") if episodes else literal([]))}})
    output = write_output(actor=testcase.actor, output_id=output.pk, expected_revision=output.revision, action="publish")
    # These custom fields are intentionally not mapped to business columns.
    raw["额外创作"] = {"zero": 0, "enabled": False, "note": None}
    text = " \r\n" + json.dumps(raw, ensure_ascii=False, indent=2) + "\r\n "
    _, revision, _ = create_structured_script_source(project=testcase.project, user=testcase.actor, content=text,
        output=output_reference(output), source_type="paste", max_bytes=1_000_000)
    return confirm_script_revision(revision, user=testcase.actor)
