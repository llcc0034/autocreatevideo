from django.contrib.auth import get_user_model

from studio.models import Asset, CharacterBible, Episode, SceneBible, SeriesProject, Shot, Style


def build_story(username="alice"):
    user = get_user_model().objects.create_user(username=username, password="StrongPass123!")
    style = Style.objects.create(
        slug=f"noir-{username}", name="黑色侦探", category=Style.Category.DETECTIVE,
        visual_prompt="rainy neo-noir, amber practical lights",
        negative_prompt="face drift, costume drift",
    )
    project = SeriesProject.objects.create(
        owner=user, title="雨夜证词", style=style, continuity_seed=42,
        logline="侦探在停电前找出说谎的人。",
    )
    portrait = Asset.objects.create(
        owner=user, style=style, kind=Asset.Kind.CHARACTER, name="林默标准像",
        local_path="C:/fixtures/lin.png", checksum="fixture-lin", mime_type="image/png",
    )
    scene_reference = Asset.objects.create(
        owner=user, style=style, kind=Asset.Kind.SCENE, name="档案室标准景",
        local_path="C:/fixtures/archive.png", checksum="fixture-archive", mime_type="image/png",
    )
    character = CharacterBible.objects.create(
        project=project, slug="lin", name="林默", stable_speaker_id="S1",
        visual_identity="三十岁短发女侦探，左眼下小痣", wardrobe="炭灰风衣",
        voice_profile="冷静低声", face_identity="高颧骨，深棕眼睛",
        anchor_asset=portrait,
    )
    scene = SceneBible.objects.create(
        project=project, slug="archive", name="旧档案室",
        visual_identity="狭窄高架档案室，尘埃漂浮", lighting="绿色台灯和窗外闪电",
        ambient_sound="雨打玻璃，老钟滴答",
        anchor_asset=scene_reference,
    )
    episode = Episode.objects.create(project=project, number=1, title="停电前", synopsis="线索浮现。")
    shot1 = Shot.objects.create(
        episode=episode, number=1, title="证物", narrative="林默展开烧焦的信纸。",
        camera="俯拍信纸后推近面部", dialogue=[{"speaker_id": "S1", "text": "这封信不是昨晚烧的。"}],
        scene=scene, duration_ms=3500, sound_effects="纸张摩擦", music="低音弦乐",
    )
    shot1.characters.add(character)
    shot2 = Shot.objects.create(
        episode=episode, number=2, title="闪电", narrative="闪电照亮门后的人影。",
        camera="快速摇镜到门口", dialogue=[{"speaker_id": "S1", "text": "别动。"}],
        scene=scene, duration_ms=2100, sound_effects="近雷炸响",
    )
    shot2.characters.add(character)
    return user, project, character, scene, episode, shot1, shot2
