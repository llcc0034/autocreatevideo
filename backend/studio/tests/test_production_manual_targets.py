"""Real target pagination, frozen range, ownership and narrow SQL projections."""
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import CharacterBible, Episode, PropBible, SceneBible, ProductionRun, Shot
from studio.services.production_execution_targets import production_target_queryset, production_target_row
from studio.services.production_execution_units import resolve_production_unit, verify_production_unit
from django.core.exceptions import ValidationError
from studio.tests import test_production_manual_creation as creation_fixtures


class ManualTargetTests(TestCase):
    create = creation_fixtures.ProductionManualCreationTests.create

    def setUp(self):
        creation_fixtures.ProductionManualCreationTests.setUp(self)
        self.first = Episode.objects.create(project=self.project, number=1, title="One")
        self.second = Episode.objects.create(project=self.project, number=2, title="Outside this run")
        self.shots = [Shot.objects.create(episode=self.first, number=n, title=f"Shot {n}", duration_ms=1000,
            media_type="image", include_audio=False) for n in range(1, 4)]
        Shot.objects.create(episode=self.second, number=1, title="Excluded", duration_ms=1000, media_type="image", include_audio=False)
        self.payload["manual_plan"]["scope"] = {"kind": "episodes", "episode_ids": [str(self.first.pk)]}
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        self.run = ProductionRun.objects.get(pk=response.data["id"])
        self.url = f"/api/v1/production-runs/{self.run.pk}/targets/"

    def test_paginated_shots_use_frozen_scope_and_unchanged_formal_plan(self):
        first = self.client.get(self.url, {"kind": "shot", "page_size": 2})
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["count"], 3)
        self.assertEqual(len(first.data["results"]), 2)
        self.assertEqual(first["Cache-Control"], "private, no-store")
        second = self.client.get(first.data["next"])
        self.assertEqual(second.status_code, 200)
        rows = [*first.data["results"], *second.data["results"]]
        self.assertEqual([row["scope"]["id"] for row in rows], [str(shot.pk) for shot in self.shots])
        self.assertEqual(first.data["run_plan_sha256"], second.data["run_plan_sha256"])
        self.assertEqual(first.data["run_id"], str(self.run.pk))
        for row in rows:
            self.assertEqual(row["unit_key"], f'shot:{row["scope"]["id"]}')
        episodes = self.client.get(self.url, {"kind": "episode"})
        self.assertEqual([row["scope"]["id"] for row in episodes.data["results"]], [str(self.first.pk)])
        project = self.client.get(self.url, {"kind": "project"})
        self.assertEqual(project.data["results"][0]["scope"], {"kind": "project", "id": str(self.project.pk)})

    def test_permissions_unknown_kind_and_duplicate_query_do_not_fallback(self):
        for query in ("", "?kind=unknown", "?kind=shot&kind=episode", "?kind=shot&owner=any"):
            self.assertEqual(self.client.get(self.url + query).status_code, 400)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.url, {"kind": "shot"}).status_code, 404)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.url, {"kind": "shot"}).status_code, (401, 403))

    def test_target_query_is_narrow_and_has_no_per_shot_lookups(self):
        _, queryset = production_target_queryset(self.run, "shot")
        with CaptureQueriesContext(connection) as queries:
            rows = [production_target_row(row, "shot") for row in queryset[:2]]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(queries), 1)
        sql = queries[0]["sql"]
        for large in ("detailed_description", "action_prompt", "config_snapshot", "content_payload"):
            self.assertNotIn(large, sql)
        self.assertIn("LIMIT 2", sql)
        explanation = queryset[:2].explain()
        self.assertIn("INDEX", explanation.upper())

    def test_entity_targets_use_real_identity_and_only_the_selected_episode_scope(self):
        for kind, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible)):
            with self.subTest(kind=kind):
                included = model.objects.create(project=self.project, slug=f"included-{kind}", name=f"Included {kind}",
                    **({"stable_speaker_id": "S1"} if kind == "character" else {}))
                model.objects.create(project=self.project, slug=f"outside-{kind}", name="Outside selected episode",
                    **({"stable_speaker_id": "S2"} if kind == "character" else {}))
                if kind == "scene":
                    self.shots[0].scene = included
                    self.shots[0].save(update_fields=("scene",))
                else:
                    getattr(self.shots[0], "characters" if kind == "character" else "props").add(included)
                response = self.client.get(self.url, {"kind": kind})
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["count"], 1)
                target, = response.data["results"]
                self.assertEqual(target["scope"], {"kind": kind, "id": str(included.pk)})
                self.assertEqual(target["label"], included.name)
                unit = resolve_production_unit(self.run, target["scope"])
                self.assertIsNone(unit["episode_id"])
                self.assertEqual(verify_production_unit(self.run, unit), unit)
                _, rows = production_target_queryset(self.run, kind)
                with CaptureQueriesContext(connection) as queries:
                    self.assertEqual([production_target_row(row, kind) for row in rows[:2]], [target])
                self.assertEqual(len(queries), 1)
                self.assertNotIn("visual_identity", queries[0]["sql"])
                self.assertIn("INDEX", rows[:2].explain().upper())
                if kind == "scene":
                    self.shots[0].scene = None
                    self.shots[0].save(update_fields=("scene",))
                else:
                    getattr(self.shots[0], "characters" if kind == "character" else "props").remove(included)
                with self.assertRaises(ValidationError):
                    verify_production_unit(self.run, unit)
