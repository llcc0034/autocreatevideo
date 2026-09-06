from uuid import uuid4

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


REPAIR_AUDIT_KEY = "_migration_0130_asset_info_repair"


class AssetSourceInfoMigrationTests(_MigrationTestCase):
    migrate_from = "0129_model_registry_lifecycle_integrity"
    migrate_to = "0130_asset_source_info"

    def set_up_before_migration(self, apps):
        User = apps.get_model("auth", "User")
        Asset = apps.get_model("studio", "Asset")
        CharacterBible = apps.get_model("studio", "CharacterBible")
        GeneratedArtifact = apps.get_model("studio", "GeneratedArtifact")
        ProductionRecipeVersion = apps.get_model("studio", "ProductionRecipeVersion")
        ProductionRun = apps.get_model("studio", "ProductionRun")
        PropBible = apps.get_model("studio", "PropBible")
        SceneBible = apps.get_model("studio", "SceneBible")
        SeriesProject = apps.get_model("studio", "SeriesProject")

        owner = User.objects.create(username="asset-source-migration")
        project = SeriesProject.objects.create(
            owner=owner,
            title="素材来源迁移",
            language="Chinese",
            aspect_ratio="9:16",
            width=768,
            height=1344,
            fps=24,
            default_shot_duration_ms=5000,
            continuity_seed=20260831,
        )
        recipe = ProductionRecipeVersion.objects.create(
            recipe_key="asset-source-migration",
            version=1,
            name="素材来源迁移夹具",
            definition={},
            definition_schema_version=1,
            definition_size_bytes=2,
            definition_sha256="1" * 64,
        )
        self.run_id = "b4dc0fd9-5ee8-456c-9d2f-c91a599f4407"
        run = ProductionRun.objects.create(
            id=self.run_id,
            owner=owner,
            project=project,
            recipe_version=recipe,
            idempotency_key="asset-source-migration",
            request_sha256="2" * 64,
            config_snapshot={},
            config_snapshot_schema_version=1,
            config_snapshot_size_bytes=2,
            config_snapshot_sha256="3" * 64,
            input_snapshot={},
            input_snapshot_schema_version=1,
            input_snapshot_size_bytes=2,
            input_snapshot_sha256="4" * 64,
        )
        entities = {
            "character": CharacterBible.objects.create(
                project=project,
                slug="lin-che",
                name="林澈",
                description="资质平庸但倔强的铸剑少年。",
                visual_identity="年轻男性，黑色束发，粗布灰衣，左掌有暗红剑纹。",
                stable_speaker_id="S1",
            ),
            "scene": SceneBible.objects.create(
                project=project,
                slug="sword-cave",
                name="藏剑洞",
                description="潮湿幽暗、遍布残剑的地下洞窟。",
                visual_identity="深灰岩壁，冷蓝天光，锈蚀残剑固定散落。",
            ),
            "prop": PropBible.objects.create(
                project=project,
                slug="broken-sword",
                name="断刃",
                description="林澈随身携带的残缺古剑。",
                visual_identity="黑铁直刃从中段断裂，剑柄缠旧灰布。",
            ),
        }

        def candidate_asset(
            *,
            entity_type,
            entity,
            variant_index,
            name=None,
            description=None,
            prompt_hint="",
        ):
            artifact_id = uuid4()
            sha256 = f"{variant_index % 10}" * 64
            manifest_sha256 = f"{(variant_index + 1) % 10}" * 64
            legacy_source = f"ProductionRun {self.run_id} 审核绑定的 Bible 参考图"
            metadata = {
                "generation_purpose": "bible_reference_image",
                "production_run_id": self.run_id,
                "generated_artifact_id": str(artifact_id),
                "entity_type": entity_type,
                "entity_id": str(entity.id),
                "source_sha256": sha256,
                "source_manifest_sha256": manifest_sha256,
            }
            asset = Asset.objects.create(
                owner=owner,
                kind=entity_type,
                name=name or f"{entity.name} · 参考图 {variant_index}",
                description=(
                    legacy_source if description is None else description
                ),
                prompt_hint=prompt_hint,
                checksum=sha256,
                metadata=metadata,
            )
            GeneratedArtifact.objects.create(
                id=artifact_id,
                manifest_key=f"asset-source-migration-{artifact_id}",
                production_run=run,
                asset=asset,
                kind="image",
                role="bible_reference_candidate",
                variant_group=(
                    f"bible:{entity_type}:{entity.id}:candidate:{variant_index}"
                ),
                variant_index=variant_index,
                status="available",
                sha256=sha256,
                manifest_metadata={
                    "schema_version": 1,
                    "generation_purpose": "bible_reference_image",
                    "production_run_id": self.run_id,
                    "target": {
                        "schema_version": 1,
                        "entity_type": entity_type,
                        "entity_id": str(entity.id),
                        "entity_name": entity.name,
                    },
                },
                manifest_schema_version=1,
                manifest_metadata_size_bytes=2,
                manifest_metadata_sha256=manifest_sha256,
            )
            return asset

        self.expected = {}
        for offset, (entity_type, entity) in enumerate(entities.items(), start=1):
            asset = candidate_asset(
                entity_type=entity_type,
                entity=entity,
                variant_index=offset * 10000 + 1,
            )
            self.expected[entity_type] = {
                "id": asset.id,
                "name": entity.name,
                "description": entity.description,
                "prompt_hint": entity.visual_identity,
                "variant_index": offset * 10000 + 1,
                "updated_at": asset.updated_at,
            }

        edited = candidate_asset(
            entity_type="character",
            entity=entities["character"],
            variant_index=40001,
            name="林澈 · 参考图 40001（人工精选）",
            description="人工补写且必须保留的说明",
        )
        self.edited_id = edited.id

        invalid = Asset.objects.create(
            owner=owner,
            kind="character",
            name="非法血缘不得修复",
            description="ProductionRun not-a-uuid 审核绑定的 Bible 参考图",
            prompt_hint="",
            checksum="5" * 64,
            metadata={
                "generation_purpose": "bible_reference_image",
                "production_run_id": "not-a-uuid",
                "generated_artifact_id": "also-not-a-uuid",
                "entity_type": "character",
                "entity_id": "invalid-entity",
            },
        )
        self.invalid_id = invalid.id

        character_asset_id = self.expected["character"]["id"]
        character_asset = Asset.objects.get(pk=character_asset_id)
        forged = Asset.objects.create(
            owner=owner,
            kind="character",
            name="林澈 · 参考图 10001",
            description=f"ProductionRun {self.run_id} 审核绑定的 Bible 参考图",
            prompt_hint="",
            checksum=character_asset.checksum,
            metadata=dict(character_asset.metadata),
        )
        self.forged_id = forged.id

        manifest_asset = Asset.objects.create(
            owner=None,
            kind="scene",
            name="系统清单素材",
            local_path="C:/managed/library/manifest.png",
            metadata={"manifest": "assets/library/manifest.json"},
        )
        self.manifest_id = manifest_asset.id
        client_manifest = Asset.objects.create(
            owner=owner,
            kind="scene",
            name="客户端伪造清单来源",
            local_path="C:/managed/library/client.png",
            metadata={"manifest": "assets/library/manifest.json"},
        )
        self.client_manifest_id = client_manifest.id
        smoke = Asset.objects.create(
            owner=None,
            kind="reference_video",
            name="系统烟测素材",
            local_path="C:/managed/smoke/reference.mp4",
            metadata={"fixture_scope": "workflow_smoke"},
        )
        self.smoke_id = smoke.id
        retired = Asset.objects.create(
            owner=owner,
            kind="character",
            name="退役候选",
            local_path="C:/managed/retired.png",
            metadata={"generation_purpose": "bible_image_candidate"},
        )
        self.retired_id = retired.id

    def test_backfill_uses_verified_lineage_and_preserves_authored_fields(self):
        Asset = self.apps.get_model("studio", "Asset")

        for expected in self.expected.values():
            asset = Asset.objects.get(pk=expected["id"])
            self.assertEqual(asset.name, expected["name"])
            self.assertEqual(asset.description, expected["description"])
            self.assertEqual(asset.prompt_hint, expected["prompt_hint"])
            self.assertEqual(
                asset.source_info,
                (
                    f"ProductionRun {self.run_id} 审核绑定的 Bible 参考图"
                    f" · 候选 {expected['variant_index']}"
                ),
            )
            marker = asset.metadata[REPAIR_AUDIT_KEY]
            self.assertEqual(marker["schema_version"], 1)
            self.assertGreater(asset.updated_at, expected["updated_at"])

        edited = Asset.objects.get(pk=self.edited_id)
        self.assertEqual(edited.name, "林澈 · 参考图 40001（人工精选）")
        self.assertEqual(edited.description, "人工补写且必须保留的说明")
        self.assertEqual(
            edited.prompt_hint,
            "年轻男性，黑色束发，粗布灰衣，左掌有暗红剑纹。",
        )
        self.assertEqual(
            set(edited.metadata[REPAIR_AUDIT_KEY]["fields"]),
            {"prompt_hint"},
        )

        for asset_id in (self.invalid_id, self.forged_id):
            asset = Asset.objects.get(pk=asset_id)
            self.assertEqual(asset.source_info, "")
            self.assertEqual(asset.prompt_hint, "")
            self.assertNotIn(REPAIR_AUDIT_KEY, asset.metadata)

        self.assertEqual(
            Asset.objects.get(pk=self.manifest_id).source_info,
            "系统素材清单导入",
        )
        self.assertEqual(
            Asset.objects.get(pk=self.smoke_id).source_info,
            "系统工作流烟测素材",
        )
        self.assertEqual(Asset.objects.get(pk=self.client_manifest_id).source_info, "")
        self.assertEqual(Asset.objects.get(pk=self.retired_id).source_info, "")

    def test_reverse_restores_only_migration_authored_business_fields(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        old_apps = executor.loader.project_state([
            ("studio", self.migrate_from),
        ]).apps
        Asset = old_apps.get_model("studio", "Asset")

        for expected in self.expected.values():
            asset = Asset.objects.get(pk=expected["id"])
            self.assertEqual(
                asset.name,
                f"{expected['name']} · 参考图 {expected['variant_index']}",
            )
            self.assertEqual(
                asset.description,
                f"ProductionRun {self.run_id} 审核绑定的 Bible 参考图",
            )
            self.assertEqual(asset.prompt_hint, "")
            self.assertEqual(asset.updated_at, expected["updated_at"])
            self.assertNotIn(REPAIR_AUDIT_KEY, asset.metadata)

        edited = Asset.objects.get(pk=self.edited_id)
        self.assertEqual(edited.name, "林澈 · 参考图 40001（人工精选）")
        self.assertEqual(edited.description, "人工补写且必须保留的说明")
        self.assertEqual(edited.prompt_hint, "")
        self.assertNotIn(REPAIR_AUDIT_KEY, edited.metadata)
