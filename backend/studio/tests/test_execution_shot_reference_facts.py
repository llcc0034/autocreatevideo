"""M5 selection -> exact per-call version -> frozen reference/auxiliary facts.

Disposable PNG/SQLite only. No provider calls, business adoption or live data.
"""
from copy import deepcopy
import json
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import AssetReferenceSeries, ShotReferenceUse
from studio.services.asset_references import attach_asset
from studio.services.bible_variants import revise_variant, setting_payload
from studio.services.execution_text_contracts import build_execution_text_payload
from studio.services.json_contracts import JsonContract, json_contract
from studio.services.production_reference_mapping import expand_reviewed_reference_sources, reference_use_records
from studio.services.shot_reference_sources import resolve_shot_reference_sources
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.tests.test_execution_input_resolution import ExecutionInputResolutionTests
from studio.tests.test_production_reference_mapping import mapping
from studio.tests.test_reference_use_names import ReferenceUseNameTests
from studio.tests import test_profile_media_source_policy as preparation_fixtures


class ExecutionShotReferenceFactTests(TestCase):
    setUpTestData = classmethod(ReferenceUseNameTests.setUpTestData.__func__)
    setUp = ReferenceUseNameTests.setUp
    definition = ReferenceUseNameTests.definition
    resolve = ReferenceUseNameTests.resolve
    item = ReferenceUseNameTests.item
    project = ReferenceUseNameTests.project
    profile = ReferenceUseNameTests.profile
    _profile = ReferenceUseNameTests._profile
    _field = ReferenceUseNameTests._field
    _publish = ReferenceUseNameTests._publish
    _rule = ReferenceUseNameTests._rule
    context_for = ReferenceUseNameTests.context_for
    prepare = ReferenceUseNameTests.prepare

    def selections(self):
        project, shot, subject, first, _ = ExecutionInputResolutionTests.reviewed_shot_use(self)
        series = AssetReferenceSeries.objects.create(owner=self.owner, name="Exact revisions", role="primary",
            bible_variant=first.setting_revision.variant)
        one, _ = attach_asset(actor=self.owner, series_id=series.pk, asset_id=self.asset.pk,
            source="existing", setting_revision_id=first.setting_revision_id)
        setting = first.setting_revision
        revise_variant(actor=self.owner, variant=setting.variant, expected_revision=1,
            value={**setting_payload(setting), "appearance_anchor": "NEW_APPEARANCE_NOT_IMPLICIT_INPUT"})
        revised = setting.variant.revisions.get(revision=2)
        two, _ = attach_asset(actor=self.owner, series_id=series.pk, asset_id=self.asset.pk,
            source="existing", setting_revision_id=revised.pk)
        first.asset = None
        first.reference_version = one
        first.save(update_fields=("asset", "reference_version"))
        second = ShotReferenceUse.objects.create(shot=shot, use_key="new-coat", position=1,
            media_kind="image", purpose="Another creative use", setting_revision=revised,
            source_mode="existing", reference_version=two, asset_sha256=self.asset.checksum)
        self.spec["semantic_contract"]["allow_repeated_asset"] = True
        return project, shot, first, second, one, two

    def mapped(self, project, shot):
        uses = resolve_shot_reference_sources(actor=self.owner, project_id=project.pk,
            shot_ids=[str(shot.pk)]).value["shots"][0]["uses"]
        sources, excluded = expand_reviewed_reference_sources(specs=[self.spec],
            sources={"references": mapping("images")}, records=reference_use_records(uses), shot_id=str(shot.pk))
        self.assertEqual(excluded, [])
        return uses, sources

    def projection(self, resolved, shot):
        return self.project(resolved, prompt_type="video", scope_units={("shot", str(shot.pk)): ["first", "second"]})

    def test_same_asset_keeps_two_exact_versions_settings_and_ports_without_extra_queries_per_use(self):
        project, shot, first, second, one, two = self.selections()
        uses, sources = self.mapped(project, shot)
        with CaptureQueriesContext(connection) as queries:
            resolved = self.resolve(sources=sources, project_id=project.pk)
        rows = resolved["logical_values"]["references"]
        self.assertEqual([row["shot_reference"] for row in rows], uses)
        self.assertEqual([row["asset_id"] for row in rows], [str(self.asset.pk)] * 2)
        self.assertEqual([row["shot_reference"]["source"]["version"] for row in rows], [1, 2])
        self.assertEqual([row["shot_reference"]["setting"]["setting_revision_id"] for row in rows],
            [str(first.setting_revision_id), str(second.setting_revision_id)])
        self.assertEqual(len([q for q in queries if 'FROM "studio_shotreferenceuse"' in q["sql"]]), 1)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        projected = self.projection(resolved, shot)["references"]
        self.assertEqual([row["shot_reference"] for row in projected], uses)
        self.assertEqual([row["input_label"] for row in projected], ["<Picture 1>", "<Picture 2>"])
        self.assertEqual([row["purpose"] for row in projected], ["", ""])
        self.assertNotIn("NEW_APPEARANCE_NOT_IMPLICIT_INPUT", str(projected))
        sources["references"]["items"].reverse()
        for index, item in enumerate(sources["references"]["items"]):
            item["order"] = index
        reordered = self.projection(self.resolve(sources=sources, project_id=project.pk), shot)["references"]
        self.assertEqual([row["shot_reference"] for row in reordered], uses[::-1])
        self.assertEqual([row["input_label"] for row in reordered], ["<Picture 1>", "<Picture 2>"])

    def test_unversioned_asset_does_not_acquire_a_series_or_setting_by_asset_lookup(self):
        project, shot, _, use, _ = ExecutionInputResolutionTests.reviewed_shot_use(self)
        use.setting_revision = None
        use.save(update_fields=("setting_revision",))
        series = AssetReferenceSeries.objects.create(owner=self.owner, name="Unselected series", role="primary")
        attach_asset(actor=self.owner, series_id=series.pk, asset_id=self.asset.pk, source="existing")
        uses, sources = self.mapped(project, shot)
        row = self.projection(self.resolve(sources=sources, project_id=project.pk), shot)["references"][0]
        self.assertEqual(row["shot_reference"], uses[0])
        self.assertEqual(row["shot_reference"]["source"], {"kind": "asset", "asset_id": str(self.asset.pk)})
        self.assertIsNone(row["shot_reference"]["setting"])
        self.assertIsNone(row["entity_id"])
        self.assertNotIn("shot_reference", self.resolve()["logical_values"]["references"][0])

    def test_unassociated_uses_cannot_bypass_the_exact_choice_check_or_inject_provenance(self):
        project, shot, subject, use, item = ExecutionInputResolutionTests.reviewed_shot_use(self)
        with self.assertRaisesMessage(ValidationError, "本镜确切用途"):
            self.resolve([{**item, "subject_ref": None}], project_id=project.pk)
        use.setting_revision = None
        use.save(update_fields=("setting_revision",))
        item["subject_ref"] = None
        self.resolve([item], project_id=project.pk)
        for change in ({"item_key": "reviewed:" + "f" * 64}, {"asset_id": str(subject.anchor_asset_id)},
                {"subject_ref": {"kind": "character", "id": str(subject.pk)}}):
            with self.subTest(change=change), self.assertRaisesMessage(ValidationError, "本镜确切用途"):
                self.resolve([{**item, **change}], project_id=project.pk)
        with self.assertRaisesMessage(ValidationError, "伪造来源"):
            self.resolve([{**item, "shot_reference": {"source": {"version": 123}}}], project_id=project.pk)

    def test_frozen_projection_and_auxiliary_wire_keep_exact_versions_after_choice_change(self):
        project, shot, first, second, one, two = self.selections()
        uses, sources = self.mapped(project, shot)
        resolved = self.resolve(sources=sources, project_id=project.pk)
        original = self.projection(resolved, shot)
        # Same Asset, changed exact version: new preparations differ; old ones do not.
        first.reference_version = two
        first.setting_revision = second.setting_revision
        first.save(update_fields=("reference_version", "setting_revision"))
        # Resolve system safety limits once for this read; the frozen business
        # evidence itself must remain query-free, including after choice edits.
        self.enterContext(security_boundary_resolution_cache())
        self.projection(resolved, shot)
        with self.assertNumQueries(0):
            self.assertEqual(self.projection(resolved, shot), original)
        fresh = self.resolve(sources=sources, project_id=project.pk)["logical_values"]["references"][0]
        self.assertEqual(fresh["shot_reference"]["source"]["reference_version_id"], str(two.pk))
        self.assertNotEqual(fresh["source_sha256"], resolved["logical_values"]["references"][0]["source_sha256"])
        source = "Keep the authored creative text and exact media choices."
        context = self.context_for(original, source)
        for mode, operation in (("model_generated", "prompt.fields.generate"), ("translate_only", "prompt.fields.translate")):
            with self.subTest(operation=operation):
                prepared = self.prepare(context, self.profile("video", mode), source)
                batch = JsonContract(**prepared["field_requests"][0])
                wire = build_execution_text_payload({"operation": operation, "output": {"adapter": "text.prompt_fields/v1"},
                    "parameters": {}, "prompt": {"mode": "none", "channels": {}}, "chat_request_options": {}},
                    parameters={}, channels={}, field_request=batch)
                payload = json.loads(wire["messages"][1]["content"])
                for row in payload["items"]:
                    self.assertEqual(row["source_text"], source)
                    self.assertEqual([ref["shot_reference"] for ref in row["reference_facts"]["logical_inputs"]], uses)
                    self.assertNotIn("NEW_APPEARANCE_NOT_IMPLICIT_INPUT", str(row))

    def test_rehashed_malformed_or_mismatched_frozen_provenance_fails_without_database_fallback(self):
        project, shot, *_ = self.selections()
        _, sources = self.mapped(project, shot)
        resolved = self.resolve(sources=sources, project_id=project.pk)
        self.enterContext(security_boundary_resolution_cache())
        self.projection(resolved, shot)
        changes = [lambda use: use.update(source=None), lambda use: use.update(setting={}),
            lambda use: use.update(use_key="other"), lambda use: use["asset"].update(sha256="a" * 64),
            lambda use: use["setting"].update(entity_id=str(uuid4())),
            lambda use: use["source"].update(version=True), lambda use: use["source"].update(series_id="missing"),
            lambda use: use["setting"].update(content_sha256="not-a-sha"), lambda use: use.update(source_mode="unknown")]
        for change in [None, *changes]:
            with self.subTest(change=change), self.assertNumQueries(0), self.assertRaisesMessage(ValidationError, "冻结分镜素材来源"):
                bad = deepcopy(resolved)
                row = bad["logical_values"]["references"][0]
                if change is None:
                    row.pop("shot_reference")
                else:
                    change(row["shot_reference"])
                row["source_sha256"] = json_contract({key: value for key, value in row.items() if key != "source_sha256"}).sha256
                self.projection(bad, shot)
        bad = deepcopy(resolved)
        bad["logical_values"]["references"][0]["shot_reference"]["source"]["version"] = 999
        with self.assertRaisesMessage(ValidationError, "素材来源事实摘要"):
            self.projection(bad, shot)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class ShotReferencePreparationPersistenceTests(TestCase):
    """Original published root/preparation writer; no actual model dispatch."""
    _create_version = preparation_fixtures.ProfileMediaSourcePolicyTests._create_version
    selection = preparation_fixtures.ProfileMediaSourcePolicyTests.selection
    prepare = preparation_fixtures.ProfileMediaSourcePolicyTests.prepare

    def configure(self, profile):
        from studio.services.execution_definitions import build_execution_definition
        from studio.services.execution_instances import update_execution_draft
        from studio.models import ModelExecutionInstanceVersion
        from studio.tests.test_video_reference_projection import VideoAuxiliaryMediaPreparationTests
        bindings = VideoAuxiliaryMediaPreparationTests.configure_generation(self, profile)
        version = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        spec = deepcopy(self.definition.value["configuration"]["inputs"][0])
        spec["semantic_contract"]["allowed_scope_kinds"].append("shot")
        update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.user,
            changes={"inputs": [spec]})
        self.definition = build_execution_definition(version.pk)
        return bindings

    def setUp(self):
        from studio.models import SeriesProject, Episode, Shot
        from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
        self.enterContext(patch("studio.services.provider_registry.resolve_credential",
            side_effect=AssertionError("No credentials in isolated preparation tests")))
        preparation_fixtures.ProfileMediaSourcePolicyTests.setUp(self, configure_profile=self.configure)
        self.job.allow_external_calls = True  # Isolated root permission; remote clients remain guarded.
        self.job.save(update_fields=("allow_external_calls",))
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=("is_active",))
        self.asset.mime_type = "image/png"
        self.asset.save(update_fields=("mime_type",))
        self.project_row = SeriesProject.objects.create(owner=self.user, title="Isolated M6 frozen use", **PROJECT_CONFIG)
        episode = Episode.objects.create(project=self.project_row, number=1, title="One")
        self.shot = Shot.objects.create(episode=episode, number=1, title="One", media_type="video", duration_ms=5000, include_audio=True)
        series = AssetReferenceSeries.objects.create(owner=self.user, name="Frozen exact version", role="primary")
        version, _ = attach_asset(actor=self.user, series_id=series.pk, asset_id=self.asset.pk, source="existing")
        self.use = ShotReferenceUse.objects.create(shot=self.shot, use_key="exact", position=0,
            media_kind="image", purpose="Creative note, not model purpose", source_mode="existing",
            reference_version=version, asset_sha256=self.asset.checksum)
        uses = resolve_shot_reference_sources(actor=self.user, project_id=self.project_row.pk,
            shot_ids=[str(self.shot.pk)]).value["shots"][0]["uses"]
        mapped, _ = expand_reviewed_reference_sources(specs=[self.spec], sources={"references": mapping("images")},
            records=reference_use_records(uses), shot_id=str(self.shot.pk))
        self.sources["references"]["selection"]["items"] = mapped["references"]["items"]
        self.expected = uses[0]

    def resolve(self, definition, owner):
        from studio.services.execution_input_resolution import resolve_execution_input_sources
        from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
        from studio.services.execution_profile_preparation import prepare_execution_profile
        from studio.services.execution_preparations import ResolvedExecutionPreparation
        inputs = resolve_execution_input_sources(definition, self.sources, owner=owner.owner,
            subject_facts=[], max_media_bytes=65536, project_id=self.project_row.pk)
        parameters = initialize_execution_definition_parameters(definition, self.parameter_values, policies=[],
            resolve_source=lambda *_: self.fail("No implicit parameter source"))
        profile = prepare_execution_profile(definition, inputs, profile=self.prompt_profile, unit_key="composition",
            source_language="English", target_language="English", prepared_parameters=parameters,
            scope_units={("shot", str(self.shot.pk)): ["composition"]})
        return ResolvedExecutionPreparation(json_contract({**profile.value, "inputs": inputs.value, "parameters": parameters}),
            json_contract({"schema_version": 1, "mode": "manual"}))

    def test_original_saved_parent_and_frozen_child_keep_version_after_choice_change(self):
        from studio.services.execution_asset_auxiliaries import create_asset_field_child
        from studio.services.execution_preparations import read_frozen_execution_request
        from studio.services.execution_preparation_requests import compile_prepared_execution_transport
        from studio.tests.test_execution_asset_requests import freeze
        parent = self.prepare()
        parent.refresh_from_db()
        facts = deepcopy(parent.contract.resolved_inputs)
        self.assertEqual(facts["inputs"]["logical_values"]["references"][0]["shot_reference"], self.expected)
        batch = facts["field_requests"][0]
        self.assertEqual(batch["value"]["items"][0]["reference_facts"]["logical_inputs"][0]["shot_reference"], self.expected)
        child, created = create_asset_field_child(preparation_id=parent.pk, expected_preparation_revision=parent.revision,
            field_batch_key=batch["sha256"], attempt_number=1, actor=self.user)
        self.assertTrue(created)
        prep = child.execution_preparations.get()
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.user).transport.value
        freeze(child, self.user)  # Isolated admission fixture, never dispatch.
        prep.refresh_from_db()
        frozen = read_frozen_execution_request(prep)[2].value
        self.assertEqual(frozen, preview)
        wire = json.loads(frozen["payload"]["messages"][1]["content"])
        self.assertEqual(wire, batch["value"])
        self.assertEqual(wire["items"][0]["reference_facts"]["logical_inputs"][0]["shot_reference"], self.expected)
        self.use.reference_version = None
        self.use.asset = self.asset
        self.use.save(update_fields=("reference_version", "asset"))
        with patch("studio.services.shot_reference_sources.resolve_shot_reference_sources",
                side_effect=AssertionError("Replays must not read today's selected version")):
            self.assertEqual(self.prepare().pk, parent.pk)
            self.assertEqual(read_frozen_execution_request(prep)[2].value, frozen)
        parent.refresh_from_db()
        self.assertEqual(parent.contract.resolved_inputs, facts)
        self.assertFalse(parent.field_selections.exists())
        child.refresh_from_db()
        self.assertIsNone(child.submission_started_at)
        self.assertEqual(child.result_text, "")
