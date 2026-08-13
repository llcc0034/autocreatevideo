"""Build the project-local reference asset library from 3x3 source boards.

The generated master boards retain their original PNG metadata.  Individual
cards are derivative crops used by the application; their manifest entries
point back to the master checksum for provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


STYLE_LIBRARY = [
    {
        "slug": "wuxia",
        "name": "武侠",
        "english_name": "Cinematic Wuxia",
        "category": "wuxia",
        "description": "冷峻写实的新派江湖，薄雾、雨夜与克制的兵刃美学。",
        "visual_prompt": "original live-action cinematic Chinese wuxia, realistic faces and layered hanfu, cool mist, restrained warm practical light, premium streaming drama",
        "items": {
            "character": [
                ("凌霜", "Disciplined female swordswoman", "年轻女剑客，冷静克制，月灰色层叠劲装，高束黑发，佩窄刃长剑。"),
                ("顾千山", "Weathered blade master", "中年刀客，风霜面容，深靛色旧袍，沉稳站姿与旧刀鞘。"),
                ("谢知微", "Elegant strategist", "青年谋士，青黛色文士袍，温和而警觉，发冠与衣纹固定。"),
            ],
            "scene": [
                ("雾竹石径", "Misty bamboo path", "黎明薄雾中的竹林石径，湿润青石与低位灯笼，无人物。"),
                ("雨夜客栈", "Rain-soaked inn courtyard", "深夜古代客栈院落，檐下暖灯映照雨水，无人物。"),
                ("云海崖祠", "Cliff shrine above clouds", "云海之上的高山崖壁小祠与孤松，冷日光，无人物。"),
            ],
            "prop": [
                ("玄纹剑", "Engraved jian sword", "带鞘窄刃长剑，暗银雕纹与深色缨绳，磨损位置固定。"),
                ("密信竹筒", "Sealed bamboo message tube", "铜箍竹制密信筒，黑绳封口，无文字。"),
                ("门派铜令", "Bronze sect token", "圆形旧铜令牌，抽象结纹，不含真实宗派与文字。"),
            ],
        },
    },
    {
        "slug": "xianxia",
        "name": "玄幻仙侠",
        "english_name": "Eastern Xianxia Fantasy",
        "category": "xuanhuan",
        "description": "仙山、灵境与克制特效构成的东方幻想世界。",
        "visual_prompt": "original premium Chinese xianxia fantasy, pearlescent moonlight, teal and gold accents, realistic fabrics, elegant restrained VFX",
        "items": {
            "character": [
                ("云织", "Celestial sword cultivator", "年轻女剑修，月白银纹长袍，小型玉质发饰，清冷神态。"),
                ("烬渊", "Demon-realm guardian", "青年魔域守卫，黑红层叠甲，角形冠饰，威严但非邪怪。"),
                ("木隐婆婆", "Elder alchemist", "老年女炼丹师，苔绿色长袍，银发盘髻，温和锐利。"),
            ],
            "scene": [
                ("云上仙门", "Floating immortal sect", "群峰云海中的悬空仙门建筑群，月昼交界，无人物。"),
                ("灵花秘林", "Luminous spirit forest", "蓝色灵花与清溪环绕的古树森林，夜间微光，无人物。"),
                ("天宫露台", "Celestial palace terrace", "日落时分的恢弘天宫露台与远山，无人物。"),
            ],
            "prop": [
                ("碧霄灵剑", "Glowing jade sword", "半透明青玉飞剑，低强度灵气流动，轮廓与纹理固定。"),
                ("凝露丹瓶", "Spirit pill vessel", "青玉玻璃丹瓶，内部柔光，无文字与标签。"),
                ("裂星罗盘", "Cracked celestial compass", "开裂青铜星盘，机械环与指针位置固定，无文字。"),
            ],
        },
    },
    {
        "slug": "anime",
        "name": "国创动漫",
        "english_name": "Donghua Mystery Adventure",
        "category": "anime",
        "description": "手绘背景与赛璐璐人物结合的青春悬疑冒险。",
        "visual_prompt": "original high-end 2D donghua mystery adventure, expressive clean linework, hand-painted backgrounds, subtle cel shading, cinematic indigo and amber palette",
        "items": {
            "character": [
                ("林柚", "Resourceful teenage girl", "短黑发少女，芥末黄外套，敏锐目光，固定小型星图吊坠。"),
                ("周砚", "Quiet teenage researcher", "圆框眼镜少年，藏蓝校服开衫，安静专注。"),
                ("唐澄", "Young mechanic", "赤棕马尾青年女机械师，青绿色工装，爽朗自信。"),
            ],
            "scene": [
                ("雨坡旧巷", "Rainy hillside old-town alley", "雨洗山城旧巷与红伞，深蓝夜色，无人物。"),
                ("天文社教室", "Astronomy club room", "放学后的天文社活动室，手作器材与暖灯，无人物。"),
                ("海岸废弃天文台", "Abandoned coastal observatory", "星空下的海岸旧天文台，远处海面，无人物。"),
            ],
            "prop": [
                ("星图吊坠", "Brass star-map locket", "黄铜星图吊坠，抽象星点，无文字。"),
                ("旧磁带录音机", "Weathered cassette recorder", "便携磁带录音机，按钮颜色与磨损固定，无品牌。"),
                ("自制望远镜", "Hand-built telescope", "小型手作望远镜，白铜混合材质，固定三脚支架。"),
            ],
        },
    },
    {
        "slug": "thriller",
        "name": "心理惊悚",
        "english_name": "Psychological Supernatural Thriller",
        "category": "thriller",
        "description": "医院、旧楼与雾湖组成的写实心理惊悚，不依赖血腥画面。",
        "visual_prompt": "original live-action psychological suspense, grounded realism, cold green-gray grade, restrained burgundy accents, practical fluorescent falloff, no gore",
        "items": {
            "character": [
                ("顾岚", "Exhausted emergency doctor", "三十岁急诊女医生，凌乱盘发，深炭色外套，疲惫但警觉。"),
                ("陈伯", "Apartment caretaker", "五十岁旧楼管理员，橄榄绿工装，沉默凝重。"),
                ("苏黛", "Mysterious vintage woman", "齐刘海短发年轻女子，暗酒红复古连衣裙，苍白冷静。"),
            ],
            "scene": [
                ("医院地下走廊", "Hospital basement corridor", "午夜医院地下长廊，荧光灯轻微闪烁，无人物。"),
                ("停电旧楼梯", "Apartment stairwell blackout", "停电中的狭窄旧公寓楼梯，少量窗光，无人物。"),
                ("雾湖公交站", "Fog-bound lakeside bus stop", "黎明前的湖边公交站，浓雾与湿路，无人物。"),
            ],
            "prop": [
                ("停摆腕表", "Stopped analog wristwatch", "旧式腕表，指针停摆，表盘无品牌。"),
                ("空牌房门钥匙", "Tarnished room key", "生锈钥匙与无字金属牌，磨损固定。"),
                ("断线红电话", "Disconnected red rotary phone", "暗红转盘电话，断开的卷线清晰可见，无标识。"),
            ],
        },
    },
    {
        "slug": "detective",
        "name": "民国侦探",
        "english_name": "Period Detective Noir",
        "category": "detective",
        "description": "虚构年代都市中的装饰艺术、雨街和舞台谜案。",
        "visual_prompt": "original 1930s Shanghai-inspired period detective noir, fictional city, art-deco production design, smoky amber and cyan, hard window light",
        "items": {
            "character": [
                ("沈墨兰", "Female private investigator", "女私家侦探，炭黑旗袍式外套与钟形帽，冷静锐利。"),
                ("许砚舟", "Forensic clerk", "男法证文员，金属圆眼镜、棕色马甲与白手套，细致谨慎。"),
                ("严鹤庭", "Grand theater owner", "年长剧院老板，象牙色长衫与黑外套，威严克制。"),
            ],
            "scene": [
                ("无字雨街", "Rainy period street", "无招牌文字的年代都市雨街、轨道与车灯，无人物。"),
                ("装饰艺术酒店", "Art-deco hotel lobby", "黑白棋盘地面的装饰艺术酒店大堂，无人物。"),
                ("旧剧院后台", "Old theater backstage", "旧剧院化妆走廊、灯泡镜与深红幕布，无人物。"),
            ],
            "prop": [
                ("折叠相机", "Folding camera", "黑皮折叠相机，机械结构与磨损固定，无品牌。"),
                ("几何银烟盒", "Geometric silver case", "银色金属盒，抽象几何刻纹，无字母与商标。"),
                ("封存证物袋", "Sealed evidence envelope", "牛皮纸证物袋，黑绳与封蜡，无文字。"),
            ],
        },
    },
    {
        "slug": "urban-drama",
        "name": "都市剧情",
        "english_name": "Contemporary Human Drama",
        "category": "drama",
        "description": "现代城市中的职业、家庭和关系故事，强调自然表演。",
        "visual_prompt": "original contemporary Chinese urban relationship drama, naturalistic live action, warm neutrals, soft window light, blue-hour accents, believable interiors",
        "items": {
            "character": [
                ("许晴川", "Woman architect", "三十岁女建筑师，黑色中长发与驼色大衣，理性自持。"),
                ("周行", "Neighborhood cafe owner", "中年咖啡店主与单亲父亲，深色针织衫、牛仔围裙，温和可靠。"),
                ("苏曼青", "Retired literature teacher", "退休女教师，短卷发、藏蓝开衫，优雅坚定。"),
            ],
            "scene": [
                ("屋顶设计室", "Sunlit design studio", "俯瞰城市屋顶的小型设计工作室，日光充足，无人物。"),
                ("打烊后咖啡馆", "Cafe after closing", "打烊后的社区咖啡馆，暖色吊灯与木质桌椅，无人物。"),
                ("蓝调家庭餐厅", "Family dining room at blue hour", "高层住宅家庭餐厅，蓝调窗景与暖灯，无人物。"),
            ],
            "prop": [
                ("金缮咖啡杯", "Kintsugi ceramic cup", "手作陶杯与金色修补缝，造型和裂纹固定。"),
                ("旧屋钥匙", "Old house key", "旧房钥匙和素木牌，无文字。"),
                ("社区院落模型", "Courtyard architecture model", "纸质社区院落建筑模型，植物与布局固定。"),
            ],
        },
    },
    {
        "slug": "cyberpunk",
        "name": "未来科幻",
        "english_name": "Near-future Cyberpunk",
        "category": "sci_fi",
        "description": "可触摸、可生产的近未来城市技术与记忆悬疑。",
        "visual_prompt": "original near-future Chinese cyberpunk thriller, plausible tactile technology, graphite materials, wet neon cyan-magenta-amber, controlled volumetric haze",
        "items": {
            "character": [
                ("季零", "Memory-recovery courier", "记忆修复女快递员，不对称黑短发、石墨色机能夹克。"),
                ("罗城", "Municipal systems engineer", "中年市政系统工程师，低调眼部植入体与深蓝制服。"),
                ("岚", "Underground radio host", "地下电台主持人，侧剃发型与半透明雨衣，身份表达中性。"),
            ],
            "scene": [
                ("雨夜层城市场", "Layered megacity night market", "暴雨中的多层未来夜市，无可读招牌，无人物。"),
                ("自动地铁控制室", "Automated metro control room", "无人值守的环形地铁控制室与屏幕墙，无人物。"),
                ("霓雾屋顶温室", "Rooftop greenhouse", "巨城霓雾下的屋顶温室与作物舱，无人物。"),
            ],
            "prop": [
                ("加密记忆晶片", "Encrypted memory shard", "透明记忆晶片，内部洋红光路，无文字。"),
                ("掌上信号扫描器", "Signal scanner", "耐用掌上信号扫描器，圆屏与按键布局固定，无品牌。"),
                ("折叠侦察无人机", "Folded modular drone", "折叠旋翼模块无人机，石墨材质与紫色状态灯。"),
            ],
        },
    },
    {
        "slug": "palace-intrigue",
        "name": "古装权谋",
        "english_name": "Palace Political Intrigue",
        "category": "historical",
        "description": "架空王朝的档案、朝堂和边关权谋，不绑定真实历史人物。",
        "visual_prompt": "original fictional ancient Chinese court intrigue, grounded prestige live action, ink black plum bronze winter blue, restrained ceremonial design",
        "items": {
            "character": [
                ("宁素", "Imperial archivist", "青年女宫廷档案官，深梅色圆领袍，沉着端正。"),
                ("裴朔", "Frontier general", "中年边将，深色低调札甲与披风，原则坚定。"),
                ("章太后", "Senior empress dowager", "年长太后，黑金礼服与克制冠饰，威严冷静。"),
            ],
            "scene": [
                ("晨光朝堂", "Imperial audience hall", "日出前的恢弘架空王朝大殿，无人物。"),
                ("烛影宫档", "Palace archive", "竹卷柜环绕的宫廷档案室，烛火照明，无人物。"),
                ("雪关帅亭", "Frontier command pavilion", "雪地边关指挥亭与远山，无人物。"),
            ],
            "prop": [
                ("无字玉牒", "Blank ivory memorial tablet", "象牙色无字礼制玉牒，边缘云纹。"),
                ("玄漆官印", "Black lacquer official seal", "黑漆官印与架空抽象纹章，无文字。"),
                ("绢帛地形图", "Silk terrain map", "折叠绢帛地形图，仅含山水线条，无文字。"),
            ],
        },
    },
    {
        "slug": "post-apocalypse",
        "name": "末日废土",
        "english_name": "Hopeful Post-apocalypse",
        "category": "other",
        "description": "灾后生态复苏与公路生存，强调修复而非血腥。",
        "visual_prompt": "original hopeful post-apocalyptic road drama, believable repaired gear, ecological reclamation, dust gold oxidized green storm blue, no zombie gore",
        "items": {
            "character": [
                ("阮青", "Former civil engineer", "前土木工程师，沙色修补夹克，坚韧、务实。"),
                ("陆野", "Teenage mapmaker", "少年地图师，铁锈色连帽衫与圆眼镜，谨慎好奇。"),
                ("叶岚", "Field medic", "银色短发女医护，苔绿实用背心与无标识布袋。"),
            ],
            "scene": [
                ("藤蔓高架", "Reclaimed elevated highway", "被藤蔓重新占据的废弃高架路，无人物。"),
                ("风沙光伏场", "Stormy solar farm", "废城旁的风沙光伏场与积水，无人物。"),
                ("旧站安全屋", "Railway station safehouse", "废弃铁路站内搭建的临时安全屋，无人物。"),
            ],
            "prop": [
                ("手摇应急电台", "Hand-crank radio", "无品牌手摇应急电台，金属磨损固定。"),
                ("便携净水罐", "Water purifier canister", "凹陷金属净水罐与手动泵，结构固定。"),
                ("荒原罗盘", "Mechanical compass", "机械罗盘置于无字旧地图上，指针与磨损固定。"),
            ],
        },
    },
    {
        "slug": "healing-fantasy",
        "name": "治愈奇幻",
        "english_name": "Healing Magical Realism",
        "category": "other",
        "description": "灯笼、纸船与钟表构成的温柔魔法现实小镇。",
        "visual_prompt": "original painterly 3D healing fantasy, tactile handcrafted textures, cream sky blue moss warm amber, soft cinematic depth, family-friendly",
        "items": {
            "character": [
                ("小满", "Night-market soup maker", "年轻汤铺店主，天蓝上衣与米白围裙，温柔明快。"),
                ("阿听", "Boy who hears forgotten objects", "能听见旧物声音的少年，苔绿开衫，害羞好奇。"),
                ("钟爷爷", "Clock repairer", "白发圆眼镜钟表匠，酒红马甲，乐观专注。"),
            ],
            "scene": [
                ("老树灯笼汤铺", "Lantern soup shop", "巨树下的微型灯笼汤铺，夜市暖光，无人物。"),
                ("发光纸船河镇", "Glowing paper-boat river town", "黄昏河镇与水面发光纸船，无人物。"),
                ("晨光钟表阁楼", "Clock workshop attic", "晨光穿过圆窗的阁楼钟表工坊，无人物。"),
            ],
            "prop": [
                ("星光瓷碗", "Starlight porcelain bowl", "青花瓷碗中泛起柔和星光，纹样固定。"),
                ("蓝纸船", "Blue paper boat", "无文字的蓝色折纸船，折痕固定。"),
                ("新芽怀表", "Leaf-sprouting pocket watch", "黄铜怀表长出一片嫩芽，表盘与划痕固定。"),
            ],
        },
    },
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_source(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("source must be SLUG=PATH")
    slug, value = raw.split("=", 1)
    return slug.strip(), Path(value.strip()).expanduser().resolve()


def _runs(values: list[int]) -> list[tuple[int, int]]:
    groups: list[list[int]] = []
    for value in values:
        if not groups or value > groups[-1][-1] + 1:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [(group[0], group[-1]) for group in groups]


def _bright_line_runs(image: Image.Image, axis: str) -> list[tuple[int, int]]:
    """Find pale, near-uniform gutters even when the three rows are unequal."""
    gray = image.convert("L")
    width, height = gray.size
    length = width if axis == "x" else height
    cross_length = height if axis == "x" else width
    matches: list[int] = []
    for index in range(length):
        strip = gray.crop((index, 0, index + 1, height)) if axis == "x" else gray.crop((0, index, width, index + 1))
        histogram = strip.histogram()
        if sum(histogram[170:]) / cross_length >= 0.85:
            matches.append(index)
    return _runs(matches)


def _panel_bounds(image: Image.Image, axis: str, inset: int) -> list[tuple[int, int]]:
    size = image.width if axis == "x" else image.height
    runs = _bright_line_runs(image, axis)
    interior = [(start, end) for start, end in runs if size * 0.15 < (start + end) / 2 < size * 0.85]
    if len(interior) != 2:
        raise ValueError(f"expected two interior {axis}-gutters, found {interior}")
    interior.sort()
    outer_start = next(((start, end) for start, end in runs if start <= 5), None)
    outer_end = next(((start, end) for start, end in reversed(runs) if end >= size - 6), None)
    content_start = outer_start[1] + 1 if outer_start else 0
    content_end = outer_end[0] if outer_end else size
    bounds = [
        (content_start, interior[0][0]),
        (interior[0][1] + 1, interior[1][0]),
        (interior[1][1] + 1, content_end),
    ]
    return [(start + inset, end - inset) for start, end in bounds]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="append", type=parse_source, required=True)
    parser.add_argument("--output", type=Path, default=Path("assets/library"))
    parser.add_argument("--crop-inset", type=int, default=3)
    args = parser.parse_args()

    sources = dict(args.source)
    expected = {style["slug"] for style in STYLE_LIBRARY}
    missing = sorted(expected - sources.keys())
    extra = sorted(sources.keys() - expected)
    if missing or extra:
        parser.error(f"source slugs mismatch; missing={missing}, extra={extra}")
    for slug, path in sources.items():
        if not path.is_file():
            parser.error(f"source does not exist for {slug}: {path}")

    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    flat_assets: list[dict] = []
    style_entries: list[dict] = []

    for style in STYLE_LIBRARY:
        slug = style["slug"]
        style_dir = root / slug
        style_dir.mkdir(parents=True, exist_ok=True)
        master_path = style_dir / "master.png"
        shutil.copy2(sources[slug], master_path)

        with Image.open(master_path) as image:
            image.load()
            if image.width != image.height:
                raise ValueError(f"{slug} master must be square: {image.size}")
            x_bounds = _panel_bounds(image, "x", args.crop_inset)
            y_bounds = _panel_bounds(image, "y", args.crop_inset)
            master_info = {
                "local_path": master_path.relative_to(root.parent.parent).as_posix(),
                "preview_url": f"/assets/library/{slug}/master.png",
                "sha256": sha256(master_path),
                "width": image.width,
                "height": image.height,
                "mime_type": "image/png",
            }

            style_assets: list[dict] = []
            for row, kind in enumerate(("character", "scene", "prop")):
                for column, (name, english_name, description) in enumerate(style["items"][kind]):
                    sequence = column + 1
                    left, right = x_bounds[column]
                    upper, lower = y_bounds[row]
                    card = image.crop((left, upper, right, lower)).convert("RGB")
                    card_path = style_dir / f"{kind}-{sequence:02d}.jpg"
                    card.save(card_path, "JPEG", quality=94, optimize=True, progressive=True)
                    asset = {
                        "id": f"{slug}-{kind}-{sequence:02d}",
                        "style_slug": slug,
                        "kind": kind,
                        "sequence": sequence,
                        "name": name,
                        "english_name": english_name,
                        "description": description,
                        "prompt_hint": f"{style['visual_prompt']}; {description}; preserve identity, materials, colors and distinctive details across every shot",
                        "local_path": card_path.relative_to(root.parent.parent).as_posix(),
                        "preview_url": f"/assets/library/{slug}/{kind}-{sequence:02d}.jpg",
                        "mime_type": "image/jpeg",
                        "checksum": sha256(card_path),
                        "metadata": {
                            "source_master": master_info["local_path"],
                            "source_master_sha256": master_info["sha256"],
                            "crop_box": [left, upper, right, lower],
                            "generated_with": "OpenAI built-in image generation",
                        },
                    }
                    style_assets.append(asset)
                    flat_assets.append(asset)

        style_entries.append(
            {
                "slug": slug,
                "name": style["name"],
                "english_name": style["english_name"],
                "category": style["category"],
                "description": style["description"],
                "visual_prompt": style["visual_prompt"],
                "negative_prompt": "identity drift, face drift, age drift, costume drift, prop mutation, inconsistent environment, duplicate people, extra limbs, readable text, watermark, logo, flicker",
                "master": master_info,
                "asset_count": len(style_assets),
                "assets": style_assets,
            }
        )

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "library_name": "AutoPrompt Reference Library",
        "source_type": "AI-generated original contact sheets",
        "notice": "Assets are AI-generated reference crops linked to their source contact sheets by checksum and crop metadata.",
        "style_count": len(style_entries),
        "asset_count": len(flat_assets),
        "styles": style_entries,
        "assets": flat_assets,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Built {len(style_entries)} styles and {len(flat_assets)} assets at {root}")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
