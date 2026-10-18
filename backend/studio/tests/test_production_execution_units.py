"""Explicit typed targets and ancestry, never parsing unit names or prompts."""
from copy import deepcopy
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import CharacterBible, Episode, PropBible, SceneBible, SeriesProject, Shot, ShotCharacterRelation
from studio.services.production_execution_units import (
    resolve_production_unit, verify_production_unit, production_unit_scope_members, production_entity_queryset,
)
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.tests import test_production_run_plans as fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class ProductionExecutionUnitTests(TestCase):
    root = fixtures.ProductionRunPlanTests.root

    def setUp(self):
        fixtures.ProductionRunPlanTests.setUp(self)
        self.episode = Episode.objects.create(project=self.project, number=1, title="Chosen episode")
        self.second = Episode.objects.create(project=self.project, number=2, title="Not in this round")
        self.shot = Shot.objects.create(episode=self.episode, number=1,
            duration_ms=1000, media_type="video", include_audio=False)
        self.run = self.root()
        self.request["scope"] = {"kind": "episodes", "episode_ids": [str(self.episode.pk)]}
        freeze_run_execution_plan(run=self.run, **self.request)

    def test_exact_shot_ancestors_without_inventing_sibling_units(self):
        unit = resolve_production_unit(self.run, {"kind": "shot", "id": str(self.shot.pk)})
        self.assertEqual(unit["episode_id"], str(self.episode.pk))
        self.assertEqual(verify_production_unit(self.run, unit), unit)
        opaque = "not-a-shot-name"
        self.assertEqual(production_unit_scope_members(unit, opaque), {
            ("project", str(self.project.pk)): [opaque], ("episode", str(self.episode.pk)): [opaque],
            ("shot", str(self.shot.pk)): [opaque]})
        project = resolve_production_unit(self.run, {"kind": "project", "id": str(self.project.pk)})
        self.assertIsNone(project["episode_id"])
        self.assertEqual(production_unit_scope_members(project, "one"), {("project", str(self.project.pk)): ["one"]})

    def test_missing_foreign_and_excluded_scope_never_expands_run(self):
        foreign = SeriesProject.objects.create(owner=self.other, title="Other owner", **PROJECT_CONFIG)
        episode = Episode.objects.create(project=foreign, number=1, title="Other episode")
        cases = [None, {}, {"kind": "shot", "id": str(uuid4())}, {"kind": "project", "id": str(foreign.pk)},
            {"kind": "episode", "id": str(episode.pk)}, {"kind": "episode", "id": str(self.second.pk)},
            {"kind": "shot", "id": "not-an-id"}, {"kind": "request", "id": str(self.project.pk)}]
        for scope in cases:
            with self.subTest(scope=scope), self.assertRaises(ValidationError):
                resolve_production_unit(self.run, scope)

    def test_changed_scope_plan_or_parent_is_not_repaired(self):
        unit = resolve_production_unit(self.run, {"kind": "shot", "id": str(self.shot.pk)})
        for key, value in (("episode_id", str(self.second.pk)), ("project_id", str(self.other.pk)),
                ("run_plan_sha256", "0" * 64), ("schema_version", True)):
            changed = deepcopy(unit)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValidationError):
                verify_production_unit(self.run, changed)
        Shot.objects.filter(pk=self.shot.pk).update(episode=self.second)
        with self.assertRaises(ValidationError):
            verify_production_unit(self.run, unit)
        with self.assertRaises(ValidationError):
            verify_production_unit(self.run, None)

    def test_episode_entity_scope_uses_explicit_roles_without_duplicate_or_foreign_targets(self):
        people = [CharacterBible.objects.create(project=self.project, slug=f"p{n}", name="Same name", stable_speaker_id=f"S{n}")
            for n in (1, 2, 3)]
        outside = Shot.objects.create(episode=self.second, number=1, duration_ms=1000, media_type="image", include_audio=False)
        foreign_project = SeriesProject.objects.create(owner=self.other, title="Foreign", **PROJECT_CONFIG)
        foreign = CharacterBible.objects.create(project=foreign_project, slug="foreign", name="Same name", stable_speaker_id="S9")
        ShotCharacterRelation.objects.bulk_create([
            ShotCharacterRelation(shot=shot, character=person, role=role, position=position)
            for shot, person, role, position in ((self.shot, people[0], "visible", 0),
                (self.shot, people[0], "speaker", 0), (self.shot, people[1], "speaker", 1),
                (outside, people[2], "visible", 0), (self.shot, foreign, "visible", 1))
        ])  # Deliberate damaged cross-project row must not expand the run's ownership.
        plan = get_run_execution_plan(self.run)
        query = production_entity_queryset(self.run, plan, "character").values_list("pk", flat=True)
        with CaptureQueriesContext(connection) as queries:
            self.assertCountEqual(list(query), [people[0].pk, people[1].pk])
        self.assertEqual(len(queries), 1)
        self.assertNotIn("studio_shot_characters", queries[0]["sql"])
        sql, params = query.query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + sql, params)
            details = " ".join(str(row[-1]) for row in cursor.fetchall()).upper()
        self.assertIn("INDEX", details)
        self.assertFalse(self.shot.character_relations.filter(character=people[1], role="visible").exists())
        for person in people[:2]:
            self.assertEqual(resolve_production_unit(self.run, {"kind": "character", "id": str(person.pk)})["scope"]["id"], str(person.pk))
        for person in (people[2], foreign):
            with self.assertRaises(ValidationError):
                resolve_production_unit(self.run, {"kind": "character", "id": str(person.pk)})
        for kind, model in (("scene", SceneBible), ("prop", PropBible)):
            entity = model.objects.create(project=self.project, slug=kind, name=kind)
            if kind == "scene":
                self.shot.scene = entity; self.shot.save(update_fields=("scene",))
            else:
                self.shot.props.add(entity)
            self.assertEqual(list(production_entity_queryset(self.run, plan, kind)), [entity])
