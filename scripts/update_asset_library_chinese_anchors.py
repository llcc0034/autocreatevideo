from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


STYLE_SLUG = "glamour-wuxia"
STYLE_NAME = "魅影风华"
STYLE_ENGLISH_NAME = "Elegant Glamour Wuxia"
STYLE_CATEGORY = "wuxia"
STYLE_DESCRIPTION = "成年角色的东方奇幻与夜色电影感参考风格，强调成熟优雅、非裸露的服化道和场景氛围。"
STYLE_VISUAL_PROMPT = (
    "original cinematic elegant wuxia fantasy noir, mature adult characters, "
    "refined wardrobe, dramatic night lighting, premium live-action concept art"
)
STYLE_NEGATIVE_PROMPT = (
    "identity drift, face drift, age drift, costume drift, prop mutation, "
    "inconsistent environment, duplicate people, extra limbs, readable text, "
    "watermark, logo, nudity, explicit sexual content, minors"
)

NEW_ITEMS: dict[str, list[tuple[str, str, str]]] = {
    "character": [
        ("柳绮罗", "Adult dancer spy", "成年女舞姬，酒红长裙与金线披帛，长卷黑发，神态自信从容。"),
        ("秦曼姝", "Refined intelligence broker", "成年女情报商，黑色丝绒旗袍式长外套，珍珠耳坠，短卷发，冷静克制。"),
        ("苏夜澜", "Silver-haired bounty hunter", "成年女赏金猎人，暗紫皮革束腰长衣与高筒靴，银色短发，气质危险冷峻。"),
    ],
    "scene": [
        ("红帘夜宴厅", "Red curtain banquet room", "深夜红帘包厢，金色屏风、烛光、漆木桌面和薄雾香气，画面无人。"),
        ("霓虹雨巷酒吧", "Neon rainy alley bar", "雨夜霓虹巷口的隐秘酒吧外景，湿润石板路、红蓝灯牌和玻璃门，画面无人。"),
        ("月色露台温泉", "Moonlit rooftop hot spring", "高楼露台温泉与月色纱幔，暖石地面、蒸汽和远处城市灯火，画面无人。"),
    ],
    "prop": [
        ("金丝羽扇", "Black gold feather fan", "黑金羽扇，细金丝骨架、暗红流苏和轻微磨损边缘。"),
        ("红宝石发簪", "Ruby tassel hairpin", "红宝石流苏发簪，金属花枝结构，宝石切面和细小刻纹固定。"),
        ("银链腰坠", "Silver crescent waist pendant", "银链腰坠，月牙形坠饰、紫色水晶和可识别的细密链节。"),
    ],
}

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def chinese_anchor(kind: str, description: str) -> str:
    text = str(description or "").strip().rstrip("。；; ")
    if kind == "character":
        value = (
            f"人物视觉锚点：{text}。固定面部身份、年龄感、发型、服装层次、主色、材质、随身标记和关键道具；"
            "只允许根据镜头改变表情、姿态和动作，不改变五官、发型、服装结构、材质颜色或身份特征。"
        )
    elif kind == "scene":
        value = (
            f"场景视觉锚点：{text}。固定空间布局、建筑结构、主要物件位置、材质、主色、光源方向和天气氛围；"
            "画面保持无人状态；只允许根据镜头改变机位、景别和局部光影，不新增人物或改变场景结构。"
        )
    elif kind == "prop":
        value = (
            f"道具视觉锚点：{text}。固定外形轮廓、尺寸比例、材质、主色、纹样、磨损痕迹和可识别标记；"
            "只允许根据镜头改变摆放角度、持握位置或光影，不改变道具种类、结构、数量或标记。"
        )
    else:
        raise ValueError(f"unsupported kind: {kind!r}")
    return value


def crop_box_for(kind: str, sequence: int) -> list[int]:
    x_bounds = [(10, 413), (425, 828), (840, 1243)]
    y_bounds = [(11, 415), (427, 830), (842, 1246)]
    row = {"character": 0, "scene": 1, "prop": 2}[kind]
    left, right = x_bounds[sequence - 1]
    top, bottom = y_bounds[row]
    return [left, top, right, bottom]


def project_relative(project_root: Path, path: Path) -> str:
    return path.resolve().relative_to(project_root).as_posix()


def build_new_assets(project_root: Path) -> tuple[dict, list[dict]]:
    style_dir = project_root / "assets" / "library" / STYLE_SLUG
    master_path = style_dir / "master.png"
    if not master_path.is_file():
        raise FileNotFoundError(master_path)
    master_info = {
        "local_path": project_relative(project_root, master_path),
        "preview_url": f"/assets/library/{STYLE_SLUG}/master.png",
        "sha256": sha256(master_path),
        "width": 1254,
        "height": 1254,
        "mime_type": "image/png",
    }
    assets: list[dict] = []
    for kind in ("character", "scene", "prop"):
        for sequence, (name, english_name, description) in enumerate(NEW_ITEMS[kind], start=1):
            card_path = style_dir / f"{kind}-{sequence:02d}.jpg"
            if not card_path.is_file():
                raise FileNotFoundError(card_path)
            asset = {
                "id": f"{STYLE_SLUG}-{kind}-{sequence:02d}",
                "style_slug": STYLE_SLUG,
                "kind": kind,
                "sequence": sequence,
                "name": name,
                "english_name": english_name,
                "description": description,
                "prompt_hint": chinese_anchor(kind, description),
                "local_path": project_relative(project_root, card_path),
                "preview_url": f"/assets/library/{STYLE_SLUG}/{kind}-{sequence:02d}.jpg",
                "mime_type": "image/jpeg",
                "checksum": sha256(card_path),
                "metadata": {
                    "source_master": master_info["local_path"],
                    "source_master_sha256": master_info["sha256"],
                    "crop_box": crop_box_for(kind, sequence),
                    "generated_with": "OpenAI built-in image generation",
                },
            }
            assets.append(asset)
    style = {
        "slug": STYLE_SLUG,
        "name": STYLE_NAME,
        "english_name": STYLE_ENGLISH_NAME,
        "category": STYLE_CATEGORY,
        "description": STYLE_DESCRIPTION,
        "visual_prompt": STYLE_VISUAL_PROMPT,
        "negative_prompt": STYLE_NEGATIVE_PROMPT,
        "master": master_info,
        "asset_count": len(assets),
        "assets": assets,
    }
    return style, assets


def update_manifest(project_root: Path) -> dict:
    manifest_path = project_root / "assets" / "library" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    new_style, new_assets = build_new_assets(project_root)
    payload["styles"] = [style for style in payload.get("styles", []) if style.get("slug") != STYLE_SLUG]
    payload["assets"] = [asset for asset in payload.get("assets", []) if asset.get("style_slug") != STYLE_SLUG]
    for asset in payload.get("assets", []):
        if asset.get("kind") in {"character", "scene", "prop"}:
            asset["prompt_hint"] = chinese_anchor(asset["kind"], asset.get("description", ""))
    for style in payload.get("styles", []):
        for asset in style.get("assets", []):
            if asset.get("kind") in {"character", "scene", "prop"}:
                asset["prompt_hint"] = chinese_anchor(asset["kind"], asset.get("description", ""))
    payload["styles"].append(new_style)
    payload["assets"].extend(new_assets)
    payload["style_count"] = len(payload["styles"])
    payload["asset_count"] = len(payload["assets"])
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def update_database(project_root: Path, payload: dict) -> None:
    db_path = project_root / "backend" / "db.sqlite3"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        rel_to_hint = {asset["local_path"]: asset["prompt_hint"] for asset in payload["assets"]}
        for row in cur.execute("select id, local_path from studio_asset").fetchall():
            raw = row["local_path"] or ""
            try:
                relative = Path(raw).resolve().relative_to(project_root).as_posix()
            except Exception:
                relative = raw.replace("\\", "/")
            if relative in rel_to_hint:
                cur.execute(
                    "update studio_asset set prompt_hint=?, updated_at=? where id=?",
                    (rel_to_hint[relative], now, row["id"]),
                )

        style = next(item for item in payload["styles"] if item["slug"] == STYLE_SLUG)
        style_row = cur.execute(
            "select id from studio_style where owner_id is null and slug=?",
            (STYLE_SLUG,),
        ).fetchone()
        if style_row:
            style_id = style_row["id"]
            cur.execute(
                """update studio_style
                   set name=?, category=?, description=?, visual_prompt=?, negative_prompt=?,
                       thumbnail_path=?, defaults=?, is_system=1, is_active=1, updated_at=?
                   where id=?""",
                (
                    STYLE_NAME,
                    STYLE_CATEGORY,
                    STYLE_DESCRIPTION,
                    STYLE_VISUAL_PROMPT,
                    STYLE_NEGATIVE_PROMPT,
                    style["master"]["local_path"],
                    json.dumps({"aspect_ratio": "9:16", "fps": 24, "model": "minimax-h3"}, ensure_ascii=False),
                    now,
                    style_id,
                ),
            )
        else:
            style_id = uuid.uuid4().hex
            cur.execute(
                """insert into studio_style
                   (id, created_at, updated_at, slug, name, category, description, visual_prompt,
                    negative_prompt, thumbnail_path, defaults, is_system, is_active, owner_id)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, null)""",
                (
                    style_id,
                    now,
                    now,
                    STYLE_SLUG,
                    STYLE_NAME,
                    STYLE_CATEGORY,
                    STYLE_DESCRIPTION,
                    STYLE_VISUAL_PROMPT,
                    STYLE_NEGATIVE_PROMPT,
                    style["master"]["local_path"],
                    json.dumps({"aspect_ratio": "9:16", "fps": 24, "model": "minimax-h3"}, ensure_ascii=False),
                ),
            )

        # The earlier inline PowerShell run may have created corrupted rows for this brand-new style.
        # Recreate this style's assets from the UTF-8 manifest snapshot to keep the catalog deterministic.
        cur.execute("delete from studio_asset where owner_id is null and style_id=?", (style_id,))
        manifest_path = project_root / "assets" / "library" / "manifest.json"
        base_index = 1 + len([asset for asset in payload["assets"] if asset.get("style_slug") != STYLE_SLUG])
        for offset, asset in enumerate(style["assets"]):
            metadata = dict(asset["metadata"])
            metadata["manifest"] = str(manifest_path)
            metadata["manifest_index"] = base_index + offset
            cur.execute(
                """insert into studio_asset
                   (id, created_at, updated_at, kind, name, description, prompt_hint, local_path,
                    reference_url, mime_type, checksum, metadata,
                    is_active, owner_id, style_id, file)
                   values (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, 1, null, ?, '')""",
                (
                    uuid.uuid4().hex,
                    now,
                    now,
                    asset["kind"],
                    asset["name"],
                    asset["description"],
                    asset["prompt_hint"],
                    str((project_root / asset["local_path"]).resolve()),
                    asset["mime_type"],
                    asset["checksum"],
                    json.dumps(metadata, ensure_ascii=False),
                    style_id,
                ),
            )
        con.commit()
    finally:
        con.close()


def assert_clean(project_root: Path, payload: dict) -> None:
    question_marks = [
        asset["id"]
        for asset in payload["assets"]
        if asset.get("style_slug") == STYLE_SLUG
        and ("?" in asset.get("name", "") or "?" in asset.get("description", "") or "?" in asset.get("prompt_hint", ""))
    ]
    if question_marks:
        raise ValueError(f"corrupted new asset text remains: {question_marks}")
    con = sqlite3.connect(project_root / "backend" / "db.sqlite3")
    try:
        total = con.execute("select count(*) from studio_asset").fetchone()[0]
        kind_counts = dict(con.execute("select kind, count(*) from studio_asset group by kind").fetchall())
    finally:
        con.close()
    print(f"Updated manifest: {payload['style_count']} styles, {payload['asset_count']} assets")
    print(f"Updated database: {total} assets, kind_counts={kind_counts}")
    print("Visual anchors refreshed; corruption and file checks passed.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    payload = update_manifest(project_root)
    update_database(project_root, payload)
    assert_clean(project_root, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
