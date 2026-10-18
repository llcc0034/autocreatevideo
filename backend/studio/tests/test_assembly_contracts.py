"""M8 output contracts and the original queue, with no encoder/model calls."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import AssemblyJob, GenerationAttempt, GenerationTask, ProductionManualCommand, SeriesProject
from studio.services.assembly import AssemblyError, create_assembly, resolve_assembly_configuration, _ordered_frozen_sources_for_episode
from studio.services.assembly_contracts import (
    project_assembly_output, validate_assembly_configuration, validate_assembly_output, validate_assembly_sources,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.production_manual_assembly import _read_contract
from studio.services.security_boundaries import boundary_int
from studio.tests.output_structure_fixtures import frozen_response_structure
from studio.tests import test_assembly as assembly_fixtures
from studio.tests import test_production_manual_assembly as manual_fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_structure_mapping_preview import field, obj


class AssemblyContractTests(TestCase):
    def setUp(self):
        owner = get_user_model().objects.create_user(username="assembly-contract-owner")
        project = SeriesProject.objects.create(owner=owner, title="Contract fixture", **PROJECT_CONFIG)
        self.configuration = resolve_assembly_configuration(project)
        self.limit = boundary_int("security.snapshot.stage_max_bytes")

    def project(self, value, node, *, structure=None, text=None, maximum=None):
        return project_assembly_output(source_structure=structure or frozen_response_structure(value),
            source_text=json.dumps(value, ensure_ascii=False) if text is None else text,
            mapping={"schema_version": 1, "node": node}, configuration=self.configuration,
            max_bytes=self.limit if maximum is None else maximum)

    def test_two_layouts_only_change_mapping_and_keep_original_profile(self):
        layouts = [({"render": {"width": 640, "height": 360, "fps": 30}, "note": "原样保留"},
            obj(**{key: field("render", key) for key in ("width", "height", "fps")})),
            ([{"output": {"横": 640, "纵": 360, "率": 30}, "custom": False}],
                obj(width=field(0, "output", "横"), height=field(0, "output", "纵"), fps=field(0, "output", "率")))]
        before = deepcopy(self.configuration)
        configs = []
        for value, node in layouts:
            structure = frozen_response_structure(value)
            result = self.project(value, node, structure=structure)
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["raw_value"], value)
            self.assertEqual(result["source_structure_version_id"], structure["id"])
            self.assertEqual(len(result["provenance"]), 3)
            self.assertTrue(all(row["source_structure_version_id"] == structure["id"] for row in result["provenance"]))
            configs.append(result["configuration"])
        self.assertEqual(configs[0], configs[1])
        self.assertEqual(configs[0], {**before, "width": 640, "height": 360, "fps": 30})
        self.assertEqual(self.configuration, before)
        configs[0]["media_profile"]["overlay"]["box_opacity"] = 0
        self.assertEqual(self.configuration, before)

    def test_missing_mapping_and_invalid_values_never_use_base_defaults(self):
        node = obj(**{key: field(key) for key in ("width", "height", "fps")})
        structure = frozen_response_structure({"width": 640, "height": 360, "fps": 30})
        for key, bad in (("width", 0), ("height", True), ("fps", "30"), ("fps", 30.0),
                ("fps", None), ("width", boundary_int("security.media.max_dimension") + 1)):
            raw = {"width": 640, "height": 360, "fps": 30, key: bad}
            with self.subTest(key=key, value=bad):
                result = self.project(raw, node, structure=structure)
                self.assertFalse(result["valid"], result)
                self.assertIsNone(result["configuration"])
                self.assertEqual(result["raw_value"], raw)
                self.assertTrue(result["errors"])
        result = self.project({"width": 640, "height": 360, "fps": 30}, obj(width=field("width")))
        self.assertFalse(result["valid"])
        self.assertIn("configuration.output.height", result["errors"])
        self.assertIsNone(result["configuration"])

    def test_no_default_lookup_script_or_unregistered_parameter(self):
        raw = {"w": 640, "h": 360, "rate": 30}
        for node in (obj(width=field("missing"), height=field("h"), fps=field("rate")),
                obj(width=field("w"), height=field("h"), fps=field("rate"), fontfile=field("w")),
                {"kind": "script", "code": "return defaults"}):
            with self.subTest(node=node):
                result = self.project(raw, node)
                self.assertFalse(result["valid"])
                self.assertIsNone(result["configuration"])
                self.assertTrue(result["errors"])

    def test_explicit_source_default_has_provenance_and_unknown_data_is_not_lost(self):
        raw = {"width": 640, "height": 360, "fps": 24}
        structure = frozen_response_structure(raw)
        fps = next(row for row in structure["nodes"] if row["property_name"] == "fps")
        fps.update(required=False, has_default=True, default_value=25)
        supplied = {"width": 640, "height": 360, "extra": {"false": False, "zero": 0}}
        result = self.project(supplied, obj(**{key: field(key) for key in raw}), structure=structure)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["configuration"]["fps"], 25)
        self.assertEqual(result["raw_value"], supplied)
        self.assertTrue(result["defaults"])
        self.assertTrue(result["warnings"])

    def test_bad_json_duplicate_keys_and_budget_do_not_return_usable_config(self):
        raw = {"width": 640, "height": 360, "fps": 24}
        node = obj(**{key: field(key) for key in raw})
        for text in ('{"width":640,"width":1280,"height":360,"fps":24}', 'not json',
                '{"width": NaN}', '{"width": 640' ):
            with self.subTest(text=text):
                result = self.project(raw, node, text=text)
                self.assertFalse(result["valid"])
                self.assertIsNone(result["configuration"])
        with self.assertRaises(ValidationError):
            self.project(raw, node, maximum=10)

    def test_closed_frozen_configuration_rejects_wrong_types_missing_hash_and_drift(self):
        cases = [None, {}, {**self.configuration, "extra": 1}]
        for key, bad in (("schema_version", True), ("width", "640"), ("fps", 0),
                ("max_attempts", False), ("max_attempts", 0), ("label_enabled", "false"),
                ("fontfile", None), ("label_text", "bad\x00text"), ("media_profile_key", "other"),
                ("media_profile_sha256", ""), ("media_profile_sha256", "0" * 64)):
            cases.append({**self.configuration, key: bad})
        changed = deepcopy(self.configuration)
        changed["media_profile"]["video_crf"] += 1
        cases.append(changed)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate_assembly_configuration(value, max_bytes=self.limit)

    def test_frozen_read_never_resolves_current_business_configuration(self):
        stage = SimpleNamespace()
        assign_json_contract(stage, "input_snapshot", {"schema_version": 1, "kind": "manual_assembly",
            "configuration": self.configuration})
        with patch("studio.services.assembly.resolve_assembly_configuration", side_effect=AssertionError("No current defaults")), \
                patch("studio.services.media_processing_profiles.resolve_configuration",
                    side_effect=AssertionError("No current media profile")):
            self.assertEqual(_read_contract(stage).value["configuration"], self.configuration)
        corrupt = deepcopy(stage.input_snapshot)
        corrupt["configuration"]["media_profile_sha256"] = "1" * 64
        # Even a freshly checksummed outer snapshot cannot mask the wrong profile.
        assign_json_contract(stage, "input_snapshot", corrupt)
        with self.assertRaises(ValidationError):
            _read_contract(stage)

    def test_output_bounds_use_existing_limits_without_coercion(self):
        expected = {"width": 1, "height": boundary_int("security.media.max_dimension"),
            "fps": boundary_int("security.media.max_fps")}
        self.assertEqual(validate_assembly_output(expected), expected)
        for key in expected:
            with self.assertRaises(ValidationError):
                validate_assembly_output({**expected, key: expected[key] + 1 if key != "width" else False})


class AssemblyContractQueueTests(TestCase):
    setUp = assembly_fixtures.AssemblyTests.setUp
    _selected_artifact = assembly_fixtures.AssemblyTests._selected_artifact
    _read_fixture_candidate = assembly_fixtures.AssemblyTests._read_fixture_candidate

    def freeze_fixture_sources(self):
        self._selected_artifact(self.shot1, "first.mp4")
        self._selected_artifact(self.shot2, "second.mp4")
        return assembly_fixtures.frozen_sources_for_test(self.episode, self.sources)

    def enqueue(self, sources):
        return create_assembly(owner=self.user, project=self.project, episode=self.episode, dry_run=False,
            frozen_sources=sources, frozen_configuration=resolve_assembly_configuration(self.project))

    def test_queue_uses_frozen_timing_and_signature_not_mutable_shot_instances(self):
        self.shot1.duration_ms = 125
        self.shot1.episode_start_ms, self.shot1.episode_end_ms = 0, 125
        self.shot1.save()
        self.shot2.duration_ms = 875
        self.shot2.episode_start_ms, self.shot2.episode_end_ms = 125, 1000
        self.shot2.save()
        sources = self.freeze_fixture_sources()
        original = deepcopy(sources)

        def alter_loaded_instances(*args):
            selections = _ordered_frozen_sources_for_episode(*args)
            # Isolated read-side mutation, NOT a database update. The validated
            # immutable timing, not the model instances, must drive FFmpeg/SHA.
            for selection in selections:
                selection.shot.duration_ms = 9000
                selection.shot.episode_start_ms = selection.shot.episode_end_ms = None
            return selections

        with patch("studio.services.assembly._ordered_frozen_sources_for_episode", side_effect=alter_loaded_instances):
            first = self.enqueue(sources)
        second = self.enqueue(sources)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(sources, original)
        self.assertEqual(list(first.source_items.order_by("ordinal").values_list("duration_ms", flat=True)), [125, 875])
        filters = first.ffmpeg_command[first.ffmpeg_command.index("-filter_complex") + 1]
        self.assertIn("trim=duration=0.125", filters)
        self.assertIn("trim=duration=0.875", filters)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GenerationTask.objects.exists())

    def test_incomplete_or_ambiguous_sources_fail_before_probe_files_and_job(self):
        sources = self.freeze_fixture_sources()
        cases = [{}, {**sources, "schema_version": True}, {**sources, "extra": 1}, {**sources, "episodes": []}]
        for key, value in (("duration_ms", True), ("duration_ms", "5000"), ("duration_ms", 0),
                ("number", False), ("shot_id", "bad"), ("media_kind", "audio"), ("episode_start_ms", 0)):
            changed = deepcopy(sources)
            changed["episodes"][0]["shots"][0][key] = value
            cases.append(changed)
        for key, value in (("decision_revision", True), ("decision_id", ""), ("sha256", ""), ("manifest_sha256", "x" * 64)):
            changed = deepcopy(sources)
            changed["episodes"][0]["shots"][0]["source"][key] = value
            cases.append(changed)
        for mode in ("reverse", "duplicate", "missing", "extra"):
            changed = deepcopy(sources)
            rows = changed["episodes"][0]["shots"]
            if mode == "reverse":
                rows.reverse()
            elif mode == "duplicate":
                rows[1]["shot_id"] = rows[0]["shot_id"]
            elif mode == "missing":
                del rows[0]["episode_end_ms"]
            else:
                rows[0]["source"]["generation_task_id"] = str(uuid4())
            cases.append(changed)
        files = set(self.media_root.rglob("*"))
        with patch("studio.services.assembly._resolve_output", side_effect=AssertionError("No media probe")):
            for changed in cases:
                with self.subTest(changed=changed), self.assertRaises(AssemblyError):
                    self.enqueue(changed)
        self.assertFalse(AssemblyJob.objects.exists())
        self.assertEqual(set(self.media_root.rglob("*")), files)

    def test_valid_contract_keeps_raw_values_and_applies_existing_size_limit(self):
        sources = self.freeze_fixture_sources()
        value = validate_assembly_sources(sources, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.assertEqual(value, sources)
        self.assertIsNot(value["episodes"][0]["shots"], sources["episodes"][0]["shots"])
        with self.assertRaises(ValidationError):
            validate_assembly_sources(sources, max_bytes=10)

    def test_current_scope_and_timing_drift_do_not_override_the_frozen_sources(self):
        sources = self.freeze_fixture_sources()
        for key, value in (("duration_ms", 4000), ("number", 3)):
            old = getattr(self.shot1, key)
            setattr(self.shot1, key, value)
            self.shot1.save(update_fields=(key,))
            with self.subTest(key=key), patch("studio.services.assembly._resolve_output", side_effect=AssertionError("No probe")):
                with self.assertRaises(AssemblyError):
                    self.enqueue(sources)
            setattr(self.shot1, key, old)
            self.shot1.save(update_fields=(key,))
        # A valid newly authored interval must still conflict with the original
        # duration-only snapshot. Invalid half-pairs are rejected by the DB.
        self.shot1.episode_start_ms, self.shot1.episode_end_ms = 0, self.shot1.duration_ms
        self.shot1.save(update_fields=("episode_start_ms", "episode_end_ms"))
        with patch("studio.services.assembly._resolve_output", side_effect=AssertionError("No probe")):
            with self.assertRaises(AssemblyError):
                self.enqueue(sources)
        self.shot1.episode_start_ms = self.shot1.episode_end_ms = None
        self.shot1.save(update_fields=("episode_start_ms", "episode_end_ms"))
        changed = deepcopy(sources)
        changed["episodes"][0]["episode_id"] = str(uuid4())
        with self.assertRaises(AssemblyError):
            self.enqueue(changed)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_frozen_selection_identity_cannot_be_replaced_by_same_artifact_new_decision(self):
        from studio.models import GeneratedArtifact
        from studio.services.generated_artifacts import select_artifact

        sources = self.freeze_fixture_sources()
        first = GeneratedArtifact.objects.get(pk=self.sources[str(self.shot1.pk)]["generated_artifact_id"])
        self._selected_artifact(self.shot1, "replacement.mp4")
        select_artifact(first, user=self.user)
        with patch("studio.services.assembly._resolve_output", side_effect=AssertionError("No probe")):
            with self.assertRaisesMessage(AssemblyError, "采用修订已变化"):
                self.enqueue(sources)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_two_mapped_layouts_reuse_same_original_queue_without_generation(self):
        self._selected_artifact(self.shot1, "first.mp4")
        self._selected_artifact(self.shot2, "second.mp4")
        base = resolve_assembly_configuration(self.project)
        attempts, tasks = GenerationAttempt.objects.count(), GenerationTask.objects.count()
        jobs = []
        for raw, node in (({"width": 640, "height": 360, "fps": 30},
                obj(**{key: field(key) for key in ("width", "height", "fps")})),
                ({"render": {"w": 640, "h": 360, "f": 30}},
                    obj(width=field("render", "w"), height=field("render", "h"), fps=field("render", "f")))):
            result = project_assembly_output(source_structure=frozen_response_structure(raw), source_text=json.dumps(raw),
                mapping={"schema_version": 1, "node": node}, configuration=base,
                max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
            self.assertTrue(result["valid"], result)
            with patch("studio.services.assembly.resolve_assembly_configuration", side_effect=AssertionError("Frozen")), \
                    patch("studio.services.assembly.subprocess.run", side_effect=AssertionError("No encoding")):
                jobs.append(create_assembly(owner=self.user, project=self.project, episode=self.episode, dry_run=False,
                    frozen_sources=assembly_fixtures.frozen_sources_for_test(self.episode, self.sources),
                    frozen_configuration=result["configuration"]))
        self.assertEqual(jobs[0].pk, jobs[1].pk)
        self.assertEqual(AssemblyJob.objects.count(), 1)
        self.assertEqual(jobs[0].status, "queued")
        filters = jobs[0].ffmpeg_command[jobs[0].ffmpeg_command.index("-filter_complex") + 1]
        self.assertIn("scale=640:360", filters)
        self.assertIn("fps=30", filters)
        self.assertEqual(list(jobs[0].source_items.order_by("ordinal").values_list("shot_id", flat=True)),
            [self.shot1.pk, self.shot2.pk])
        self.assertEqual(GenerationAttempt.objects.count(), attempts)
        self.assertEqual(GenerationTask.objects.count(), tasks)
        self.assertFalse(Path(jobs[0].output_path).exists())

    def test_invalid_frozen_config_fails_before_probe_files_or_job_creation(self):
        base = resolve_assembly_configuration(self.project)
        before = set(self.media_root.rglob("*"))
        for changed in ({**base, "width": 0}, {**base, "fps": True}, {**base, "media_profile_sha256": "0" * 64}):
            with self.subTest(changed=changed), patch("studio.services.assembly._resolve_output", side_effect=AssertionError("No probe")):
                with self.assertRaises(AssemblyError):
                    create_assembly(owner=self.user, project=self.project, episode=self.episode, dry_run=False,
                        frozen_sources={}, frozen_configuration=changed)
        self.assertFalse(AssemblyJob.objects.exists())
        self.assertEqual(set(self.media_root.rglob("*")), before)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class AssemblyContractAPITests(TestCase):
    setUp = manual_fixtures.ManualAssemblyTests.setUp
    mark_submitted = manual_fixtures.ManualAssemblyTests.mark_submitted
    archive = manual_fixtures.ManualAssemblyTests.archive
    resolve = manual_fixtures.ManualAssemblyTests.resolve
    approve = manual_fixtures.ManualAssemblyTests.approve
    detail = manual_fixtures.ManualAssemblyTests.detail
    request = manual_fixtures.ManualAssemblyTests.request
    post = manual_fixtures.ManualAssemblyTests.post

    def test_invalid_current_configuration_is_400_before_enqueue(self):
        self.approve()
        body = self.request()
        attempts = GenerationAttempt.objects.count()
        before = set(Path(settings.MEDIA_ROOT).rglob("*"))
        self.project.width = 0
        self.project.save(update_fields=("width",))
        for response in (self.client.get(self.read_url), self.post(body)):
            self.assertEqual(response.status_code, 400, response.data)
            self.assertNotIn("target_id", response.data)
        self.assertFalse(AssemblyJob.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.filter(action="assemble").exists())
        self.assertEqual(GenerationAttempt.objects.count(), attempts)
        self.assertEqual(set(Path(settings.MEDIA_ROOT).rglob("*")), before)

    def test_invalid_frozen_configuration_is_400_without_defaults_or_new_job(self):
        self.approve()
        first = self.post(self.request())
        self.assertEqual(first.status_code, 202, first.data)
        detail = self.detail()
        request = self.request(detail)
        stage = self.run.stages.get(stage_key="assembly")
        original = deepcopy(stage.input_snapshot)
        attempts = GenerationAttempt.objects.count()
        before = set(Path(settings.MEDIA_ROOT).rglob("*"))
        base = original["configuration"]
        incomplete = deepcopy(base)
        incomplete.pop("fps")
        for config in ({**base, "width": 0}, {**base, "media_profile_sha256": "0" * 64}, incomplete):
            with self.subTest(config=config):
                # Recompute the outer digest: configuration validation must not
                # rely on snapshot checksum failure to catch a bad inner value.
                assign_json_contract(stage, "input_snapshot", {**original, "configuration": config})
                stage.save(update_fields=("input_snapshot", "input_snapshot_schema_version",
                    "input_snapshot_size_bytes", "input_snapshot_sha256"))
                request["idempotency_key"] = str(uuid4())
                with patch("studio.services.production_manual_assembly.resolve_assembly_configuration",
                        side_effect=AssertionError("No current defaults")):
                    for response in (self.client.get(self.read_url), self.post(request)):
                        self.assertEqual(response.status_code, 400, response.data)
                        self.assertIn("configuration", str(response.data))
                stage.refresh_from_db()
                self.assertEqual(stage.input_snapshot["configuration"], config)
        self.assertEqual(AssemblyJob.objects.count(), 1)
        self.assertEqual(AssemblyJob.objects.get().status, "queued")
        self.assertEqual(ProductionManualCommand.objects.filter(action="assemble").count(), 1)
        self.assertEqual(GenerationAttempt.objects.count(), attempts)
        self.assertEqual(set(Path(settings.MEDIA_ROOT).rglob("*")), before)
