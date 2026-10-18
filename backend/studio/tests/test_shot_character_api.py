from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import Asset, CharacterBible, Episode, SeriesProject, Shot, ShotCharacterRelation, ShotReferenceUse


class ShotCharacterApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="shot-relations")
        self.project = SeriesProject.objects.create(owner=self.user, title="关系隔离测试", language="Chinese",
            aspect_ratio="9:16", width=768, height=1344, fps=24, default_shot_duration_ms=6000, continuity_seed=1)
        self.episode = Episode.objects.create(project=self.project, number=1, title="第一集")
        self.visible = CharacterBible.objects.create(project=self.project, slug="visible", name="同名", stable_speaker_id="S1")
        self.speaker = CharacterBible.objects.create(project=self.project, slug="speaker", name="同名", stable_speaker_id="S2")
        self.client = APIClient(); self.client.force_authenticate(self.user)
        for name, value in (("_resolved_video_shot_duration_policy", {"minimum_duration_ms":1000,"maximum_duration_ms":10000,"default_duration_ms":6000}),
                ("_resolved_project_creation_policy", {"default_shot_media_type":"video","default_include_audio":True})):
            context = patch(f"studio.serializers.{name}", return_value=value)
            context.start(); self.addCleanup(context.stop)
        self.dialogue = [{"speaker_id":"S2", "text":"  原文\n保持。  ", "delivery":" softly ",
            "voiceover":True, "start_ms":123, "end_ms":987}]
        self.relations = [{"role":"visible", "position":0, "character_id":str(self.visible.pk)},
            {"role":"speaker", "position":0, "character_id":str(self.speaker.pk)}]

    def create_shot(self, **changes):
        payload = {"episode":str(self.episode.pk), "number":1, "media_type":"video",
            "episode_start_ms":1200, "episode_end_ms":7200, "dialogue":deepcopy(self.dialogue),
            "character_relations":deepcopy(self.relations), **changes}
        return self.client.post("/api/v1/shots/", payload, format="json")

    def test_exact_roles_round_trip_without_old_pool_or_name_inference(self):
        response = self.create_shot()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("characters", response.data)
        shot = Shot.objects.get()
        self.assertNotIn("characters", {field.name for field in shot._meta.get_fields()})
        self.assertEqual(shot.dialogue, self.dialogue)
        self.assertEqual((shot.episode_start_ms, shot.episode_end_ms, shot.duration_ms), (1200,7200,6000))
        for response in (self.client.get(f"/api/v1/shots/{shot.pk}/"),
                self.client.patch(f"/api/v1/shots/{shot.pk}/", {"title":"只改标题"}, format="json")):
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["dialogue"], self.dialogue)
            self.assertCountEqual(response.data["character_relations"], self.relations)
        self.assertEqual(ShotCharacterRelation.objects.filter(role="visible").get().character, self.visible)
        ids = set(shot.character_relations.values_list("pk",flat=True))
        response = self.client.patch(f"/api/v1/shots/{shot.pk}/", {"character_relations":self.relations},format="json")
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(set(shot.character_relations.values_list("pk",flat=True)),ids)

    def test_relation_and_dialogue_replacement_is_atomic(self):
        response = self.create_shot(); self.assertEqual(response.status_code, 201, response.data)
        shot = Shot.objects.get(); url = f"/api/v1/shots/{shot.pk}/"
        bad = self.client.patch(url, {"title":"不能保存", "dialogue":[]}, format="json")
        self.assertEqual(bad.status_code,400,bad.data)
        shot.refresh_from_db(); self.assertEqual(shot.title, ""); self.assertEqual(shot.dialogue,self.dialogue)
        changed = self.client.patch(url, {"dialogue":[], "character_relations":[self.relations[0]]}, format="json")
        self.assertEqual(changed.status_code,200,changed.data)
        self.assertEqual(ShotCharacterRelation.objects.count(),1)
        with patch("studio.serializers.ShotCharacterRelation.objects.bulk_create", side_effect=RuntimeError("write failed")):
            with self.assertRaisesMessage(RuntimeError,"write failed"):
                self.client.patch(url, {"title":"回滚", "character_relations":[]}, format="json")
        shot.refresh_from_db(); self.assertEqual(shot.title, ""); self.assertEqual(ShotCharacterRelation.objects.count(),1)

    def test_retired_material_fields_reject_create_and_update_without_partial_writes(self):
        for field, value in (("reference_bindings", {}), ("reference_assets", [])):
            with self.subTest(field=field):
                response = self.create_shot(**{field: value})
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(field, response.data["error"]["details"])
                self.assertEqual(Shot.objects.count(), 0)
        self.assertEqual(self.create_shot().status_code, 201)
        shot = Shot.objects.get(); before = shot.updated_at
        for field, value in (("reference_bindings", {}), ("reference_assets", [])):
            response = self.client.patch(f"/api/v1/shots/{shot.pk}/", {"title":"不能部分保存",field:value}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        shot.refresh_from_db()
        self.assertEqual((shot.title, shot.updated_at), ("", before))
        self.assertEqual(ShotReferenceUse.objects.count(), 0)

    def test_content_save_never_reads_or_rewrites_material_selection(self):
        self.assertEqual(self.create_shot().status_code, 201)
        shot = Shot.objects.get()
        # Stored selection fixture: this test verifies persistence isolation, not media resolution.
        asset = Asset.objects.create(owner=self.user, kind="reference_image", name="确切已选素材",
            mime_type="image/png", checksum="a" * 64)
        use = ShotReferenceUse.objects.create(shot=shot, use_key="same-source", position=0, media_kind="image",
            purpose="  创作描述 first_frame  ", source_mode="existing", asset=asset, asset_sha256=asset.checksum)
        before = ShotReferenceUse.objects.values().get(pk=use.pk)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.patch(f"/api/v1/shots/{shot.pk}/", {"title":"内容保存","media_type":"image"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(any('studio_shot_reference_assets' in row['sql'] or 'studio_shotreferenceuse' in row['sql'] for row in queries))
        self.assertEqual(ShotReferenceUse.objects.values().get(pk=use.pk), before)
        shot.refresh_from_db()
        self.assertEqual(shot.dialogue, self.dialogue)
        self.assertEqual((shot.episode_start_ms,shot.episode_end_ms), (1200,7200))
        for response in (response, self.client.get(f"/api/v1/shots/{shot.pk}/"), self.client.get("/api/v1/shots/")):
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('"reference_bindings"', str(response.render().content))
            self.assertNotIn('"reference_assets"', str(response.content))

    def test_retired_material_fields_do_not_bypass_owner_permission(self):
        self.assertEqual(self.create_shot().status_code, 201)
        url = f"/api/v1/shots/{Shot.objects.get().pk}/"
        self.client.force_authenticate(get_user_model().objects.create_user(username="not-shot-owner"))
        self.assertEqual(self.client.patch(url, {"reference_bindings":{}}, format="json").status_code,404)
        self.client.force_authenticate(None)
        self.assertIn(self.client.patch(url, {"reference_assets":[]}, format="json").status_code,(401,403))

    def test_rejects_invalid_and_cross_project_relations_without_writes(self):
        other_project = SeriesProject.objects.create(owner=self.user, title="其他项目", language="Chinese", aspect_ratio="9:16",
            width=768,height=1344,fps=24,default_shot_duration_ms=6000,continuity_seed=1)
        outsider = CharacterBible.objects.create(project=other_project,name="同名",stable_speaker_id="S2")
        for relations in ([], [self.relations[0]], self.relations + [self.relations[1]],
                [{**self.relations[1],"position":True}], [{**self.relations[1],"position":"0"}],
                [{**self.relations[1],"role":["speaker"]}], [{**self.relations[1],"position":1}],
                [{**self.relations[1],"character_id":str(outsider.pk)}],
                [{**self.relations[1],"character_id":str(self.visible.pk)}],
                [{**self.relations[1],"character_id":"unknown"}], [{**self.relations[1],"extra":1}]):
            with self.subTest(relations=relations):
                response = self.create_shot(character_relations=relations)
                self.assertEqual(response.status_code,400,response.data)
                self.assertFalse(Shot.objects.exists())
        response = self.create_shot(characters=[str(self.speaker.pk)])
        self.assertEqual(response.status_code,400,response.data)

    def test_scalar_edit_rechecks_current_dialogue_inside_the_lock(self):
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory, force_authenticate
        from studio.serializers import ShotSerializer
        response = self.create_shot(); self.assertEqual(response.status_code,201,response.data)
        shot = Shot.objects.get()
        request = APIRequestFactory().patch("/shots/", {"title":"标题"}, format="json")
        force_authenticate(request,user=self.user)
        serializer = ShotSerializer(shot,data={"title":"标题"},partial=True,context={"request":Request(request)})
        self.assertTrue(serializer.is_valid(),serializer.errors)
        current = [{"speaker_id":"S1","text":"  新的明确对白  "}]
        Shot.objects.filter(pk=shot.pk).update(dialogue=current)
        ShotCharacterRelation.objects.filter(shot=shot,role="speaker").update(character=self.visible)
        saved = serializer.save()
        self.assertEqual(saved.dialogue,current)
        self.assertEqual(saved.character_relations.get(role="speaker").character,self.visible)

    def test_relation_reordering_uses_line_positions_and_never_visibility(self):
        second = {"speaker_id":"S1", "text":"  第二句  "}
        relations = [self.relations[0],self.relations[1],{"role":"speaker","position":1,"character_id":str(self.visible.pk)}]
        response = self.create_shot(dialogue=self.dialogue+[second],character_relations=relations)
        self.assertEqual(response.status_code,201,response.data)
        url = f"/api/v1/shots/{response.data['id']}/"
        response = self.client.patch(url,{"dialogue":[second]+self.dialogue,"character_relations":[
            {**relations[2],"position":0},{**relations[1],"position":1}]},format="json")
        self.assertEqual(response.status_code,200,response.data)
        self.assertFalse(ShotCharacterRelation.objects.filter(role="visible").exists())

    def test_owner_scope_and_read_queries_do_not_load_old_character_pool(self):
        response = self.create_shot(); self.assertEqual(response.status_code,201,response.data)
        shots = Shot.objects.bulk_create([Shot(episode=self.episode, number=number, duration_ms=6000,
            media_type="image", include_audio=False) for number in range(2, 201)])
        ShotCharacterRelation.objects.bulk_create([ShotCharacterRelation(shot=shot, role="visible", position=0,
            character=self.visible) for shot in shots])
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/api/v1/shots/",{"episode":str(self.episode.pk),"page_size":200})
        self.assertEqual(response.status_code,200,response.data)
        self.assertFalse(any('studio_shot_characters' in row['sql'] for row in captured))
        self.assertEqual(sum('FROM "studio_shotcharacterrelation"' in row['sql'] for row in captured),1)
        plan = ShotCharacterRelation.objects.filter(shot_id__in=[row.pk for row in shots]).only(
            "shot_id", "character_id", "role", "position").explain()
        self.assertIn("INDEX",plan)
        self.client.force_authenticate(get_user_model().objects.create_user(username="other-shot-user"))
        self.assertEqual(self.client.get(f"/api/v1/shots/{shots[0].pk}/").status_code,404)
        self.assertEqual(self.create_shot(number=201).status_code,400)

    def test_retired_creative_columns_reject_create_and_patch_atomically(self):
        fields = ("narrative", "detailed_description", "camera", "action", "sound_effects", "music", "continuity_notes")
        for field in fields:
            response = self.create_shot(**{field: ""})
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn(field, response.data["error"]["details"])
            self.assertFalse(Shot.objects.exists())
        self.assertEqual(self.create_shot().status_code, 201)
        shot = Shot.objects.get()
        before = shot.updated_at
        for field in fields:
            response = self.client.patch(f"/api/v1/shots/{shot.pk}/",
                {"title": "不得部分保存", field: "旧内容"}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        for key in ("framing", "negative_prompt"):
            response = self.client.patch(f"/api/v1/shots/{shot.pk}/",
                {"title": "不得部分保存", "metadata": {key: ""}}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        shot.refresh_from_db()
        self.assertEqual((shot.title, shot.updated_at, shot.dialogue), ("", before, self.dialogue))

    def test_reads_exclude_retired_creative_fields_without_rewriting_storage(self):
        self.assertEqual(self.create_shot().status_code, 201)
        shot = Shot.objects.get()
        # Retired metadata remains historical; reading it must not rewrite storage.
        Shot.objects.filter(pk=shot.pk).update(
            metadata={"framing": "旧构图", "negative_prompt": "旧约束", "test_note": "保留"})
        for response in (self.client.get(f"/api/v1/shots/{shot.pk}/"),
                self.client.get("/api/v1/shots/")):
            self.assertEqual(response.status_code, 200, response.data)
            rows = response.data.get("results", [response.data])
            for row in rows:
                for field in ("narrative", "detailed_description", "camera", "action", "sound_effects", "music", "continuity_notes"):
                    self.assertNotIn(field, row)
                self.assertEqual(row["metadata"], {"test_note": "保留"})
        shot.refresh_from_db()
        self.assertNotIn("narrative", {field.name for field in Shot._meta.get_fields()})
        self.assertEqual(shot.metadata["framing"], "旧构图")
