from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from studio.models import CharacterBible, Episode, SceneBible, SeriesProject, Shot, Style
from studio.services.configuration import project_creation_policy
from studio.services.consistency import sync_consistency_locks


STYLES = [
    ("wuxia", "武侠", "wuxia", "cinematic neo-wuxia, expressive ink-wash atmosphere, realistic faces, flowing fabric, restrained jade and vermilion palette, dynamic wire-action composition"),
    ("xianxia", "玄幻仙侠", "xuanhuan", "epic Chinese xianxia fantasy, celestial mountains, luminous spiritual energy, ornate ancient robes, grand volumetric clouds, premium cinematic VFX"),
    ("anime", "国创动漫", "anime", "premium Chinese animation, precise linework, expressive eyes, layered parallax backgrounds, controlled highlights, coherent character sheets, cinematic framing"),
    ("thriller", "心理惊悚", "thriller", "psychological thriller, oppressive practical lighting, weathered textures, restrained visual clues, escalating dread, no graphic gore"),
    ("detective", "民国侦探", "detective", "Republic-era Chinese detective noir, rain-streaked glass, hard chiaroscuro, tobacco amber and midnight blue, methodical visual clues, elegant slow camera"),
    ("urban-drama", "都市现实剧情", "drama", "grounded contemporary urban drama, natural window light, authentic lived-in spaces, nuanced micro-expressions, handheld intimacy, realistic color science"),
    ("cyberpunk", "未来科幻", "sci_fi", "future cyberpunk China, plausible near-future infrastructure, functional industrial design, neon reflected on rain, precise technology details, large-format cinematography"),
    ("palace-intrigue", "古装权谋", "historical", "historically grounded Chinese palace intrigue, rigorous court etiquette, restrained luxury, carved timber halls, candlelit political tension, sober cinematic realism"),
    ("post-apocalypse", "末日废土", "sci_fi", "post-apocalyptic wasteland, weathered survival gear, reclaimed industrial ruins, windblown dust, harsh natural light, grounded cinematic realism"),
    ("healing-fantasy", "治愈奇幻", "xuanhuan", "healing gentle fantasy, warm pastoral light, tactile handcrafted environments, benevolent magical creatures, soft natural palette, emotionally comforting cinema"),
    ("glamour-wuxia", "魅影风华", "wuxia", "original cinematic elegant wuxia fantasy noir, mature adult characters, refined wardrobe, dramatic night lighting, premium live-action concept art"),
]


class Command(BaseCommand):
    help = (
        "Seed display styles and, only when explicitly requested, a local demo project. "
        "System workflows are initialized exclusively by versioned data migrations."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--with-demo-project",
            action="store_true",
            help="Also create a local demo user with a one-time random password and a sample series",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        active_slugs = {item[0] for item in STYLES}
        Style.objects.filter(owner=None, is_system=True).exclude(
            slug__in=active_slugs,
        ).update(is_active=False)
        for slug, name, category, visual_prompt in STYLES:
            Style.objects.update_or_create(
                owner=None,
                slug=slug,
                defaults={
                    "name": name,
                    "category": category,
                    "visual_prompt": visual_prompt,
                    "negative_prompt": (
                        "identity drift, face drift, costume drift, duplicate people, "
                        "duplicate objects, duplicate weapons, extra weapons, extra limbs, "
                        "extra fingers, malformed hands, text, watermark, logo, low detail, flicker"
                    ),
                    "defaults": {"aspect_ratio": "9:16", "fps": 24},
                    "description": f"{name}商业短剧预设，可绑定本地人物、场景与道具参考图。",
                    "is_system": True,
                    "is_active": True,
                },
            )
        if options["with_demo_project"]:
            self._seed_demo_project()
        self.stdout.write(self.style.SUCCESS(
            "Seeded 11 display styles. System workflows were not changed.",
        ))

    def _seed_demo_project(self):
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username="demo",
            defaults={"email": "demo@example.test"},
        )
        if created:
            one_time_password = secrets.token_urlsafe(32)
            user.set_password(one_time_password)
            user.save(update_fields=("password",))
            self.stdout.write(self.style.WARNING(
                "Created local demo user 'demo'. One-time password: "
                f"{one_time_password}\nThis password will not be displayed again. "
                "Store it through an approved secret channel.",
            ))
        style = Style.objects.get(owner=None, slug="wuxia")
        creation_policy = project_creation_policy()
        project, project_created = SeriesProject.objects.get_or_create(
            owner=user,
            title="无名剑影",
            defaults={
                "logline": "失忆剑客在雨夜客栈追查一枚残缺玉佩的主人。",
                "style": style,
                "status": SeriesProject.Status.ACTIVE,
                "language": creation_policy["default_language"],
                "aspect_ratio": creation_policy["default_aspect_ratio"],
                "width": creation_policy["width"],
                "height": creation_policy["height"],
                "fps": creation_policy["fps"],
                "default_shot_duration_ms": creation_policy["default_shot_duration_ms"],
                "continuity_seed": 8675309,
                "continuity_summary": "玉佩始终由沈砚贴身携带；雨势逐集增强。",
            },
        )
        if not project_created:
            project_policy_values = {
                "language": creation_policy["default_language"],
                "aspect_ratio": creation_policy["default_aspect_ratio"],
                "width": creation_policy["width"],
                "height": creation_policy["height"],
                "fps": creation_policy["fps"],
                "default_shot_duration_ms": creation_policy[
                    "default_shot_duration_ms"
                ],
            }
            changed_fields = [
                field
                for field, value in project_policy_values.items()
                if getattr(project, field) != value
            ]
            if changed_fields:
                for field in changed_fields:
                    setattr(project, field, project_policy_values[field])
                project.save(update_fields=(*changed_fields, "updated_at"))
                self.stdout.write(
                    "Updated seeded demo project from published creation policy: "
                    + ", ".join(changed_fields)
                )
        character, _ = CharacterBible.objects.get_or_create(
            project=project,
            slug="shen-yan",
            defaults={
                "name": "沈砚",
                "stable_speaker_id": "S1",
                "role": "主角",
                "visual_identity": "清瘦青年剑客，冷峻轮廓，右眉尾浅疤，长发高束",
                "face_identity": "狭长深色眼睛，直鼻，薄唇",
                "wardrobe": "深靛窄袖劲装，旧黑披风",
                "voice_profile": "低沉克制，语速偏慢",
                "is_locked": True,
            },
        )
        scene, _ = SceneBible.objects.get_or_create(
            project=project,
            slug="rain-inn",
            defaults={
                "name": "雨夜客栈",
                "location": "山道旁两层木客栈",
                "time_of_day": "深夜",
                "visual_identity": "潮湿乌木梁柱，破旧纸窗，门外连绵冷雨",
                "lighting": "暖黄油灯对比门外青蓝雨光",
                "ambient_sound": "屋檐雨滴，远雷，木板轻响",
            },
        )
        episode, _ = Episode.objects.get_or_create(
            project=project,
            number=1,
            defaults={"title": "残玉", "synopsis": "沈砚在客栈听见陌生人叫出自己的旧名。"},
        )
        shot, shot_created = Shot.objects.get_or_create(
            episode=episode,
            number=1,
            defaults={
                "title": "推门",
                "narrative": "沈砚推开客栈木门，雨水沿披风滴落，他停在门槛扫视众人。",
                "camera": "低机位中景缓慢推近，最后停在右眉浅疤特写",
                "duration_ms": project.default_shot_duration_ms,
                "media_type": creation_policy["default_shot_media_type"],
                "include_audio": (
                    creation_policy["default_include_audio"]
                    if creation_policy["default_shot_media_type"] == Shot.MediaType.VIDEO
                    else False
                ),
                "dialogue": [{"speaker_id": "S1", "text": "有人在等我？"}],
                "scene": scene,
                "sound_effects": "木门吱呀，雨声突然清晰",
                "music": "低沉古琴单音",
            },
        )
        if not shot_created:
            shot_policy_values = {
                "duration_ms": project.default_shot_duration_ms,
                "media_type": creation_policy["default_shot_media_type"],
                "include_audio": (
                    creation_policy["default_include_audio"]
                    if creation_policy["default_shot_media_type"]
                    == Shot.MediaType.VIDEO
                    else False
                ),
            }
            if creation_policy["default_shot_media_type"] == Shot.MediaType.IMAGE:
                shot_policy_values.update({
                    "conditioning_mode": "",
                    "reference_bindings": {},
                })
            changed_fields = [
                field
                for field, value in shot_policy_values.items()
                if getattr(shot, field) != value
            ]
            if changed_fields:
                for field in changed_fields:
                    setattr(shot, field, shot_policy_values[field])
                shot.save(update_fields=(*changed_fields, "updated_at"))
                self.stdout.write(
                    "Updated seeded demo shot from published creation policy: "
                    + ", ".join(changed_fields)
                )
        shot.characters.add(character)
        sync_consistency_locks(project)
