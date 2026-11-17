"""Preserve registered copy, validate geometry, and render editable refresh screens."""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

import render


RULE_DIR = Path(__file__).resolve().parent / "templates"
RULES = json.loads((RULE_DIR / "refresh-rules-v1.json").read_bytes())
WIDTH = RULES["width"]
MIN_HEIGHT = RULES["screen"]["minimum_height"]
MAX_HEIGHT = RULES["screen"]["maximum_height"]
MAX_SCREENS = RULES["screens_limit"]
ROLES = {**render.ROLE_NAMES, "field": "字段"}
ID = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")


def problem(message):
    raise render.RenderError(message)


def source_geometry(registration):
    old = registration["selection"]["old_page"]
    width, height = old["width"], old["height"]
    if type(width) is not int or type(height) is not int or width < 100 or height < 100:
        problem("登记的旧页缺少有效宽高。")
    return width, height


def review_screens(registration):
    plan = registration.get("review_plan", {})
    screens = plan if isinstance(plan, list) else plan.get("screens", [])
    if not screens or len(screens) > MAX_SCREENS:
        problem(f"待审分屏数量应在 1 至 {MAX_SCREENS} 之间。")
    _, old_height = source_geometry(registration)
    known = set()
    all_keys = set()
    result = []
    approved = registration.get("approved_copy", {})
    prior_end = 0
    for index, source in enumerate(screens, 1):
        screen_id = source.get("id", f"screen-{index:02d}")
        if not isinstance(screen_id, str) or not ID.fullmatch(screen_id) or screen_id in known:
            problem("待审分屏编号无效或重复。")
        known.add(screen_id)
        old_range = source.get("source_range", source.get("old_range"))
        if (not isinstance(old_range, (list, tuple)) or len(old_range) != 2
                or any(type(bound) is not int for bound in old_range)
                or not prior_end <= old_range[0] < old_range[1] <= old_height):
            problem(f"「{screen_id}」的旧图分屏范围重叠、越界或缺失。")
        prior_end = old_range[1]
        source_items = source.get("items", [])
        items = []
        for item in source_items:
            key = item.get("key")
            if not isinstance(key, str) or not ID.fullmatch(key) or key in all_keys:
                problem("商品文案字段编号无效或重复。")
            role = item.get("role", "body")
            if role not in ROLES:
                problem(f"「{key}」含未知文字角色。")
            all_keys.add(key)
            edit = approved.get(key)
            text = edit["text"] if edit else item.get("text", "")
            if not isinstance(text, str) or len(text) > 1200:
                problem(f"「{key}」文案太长，请按原字段拆为可审核段落。")
            if text.strip():
                items.append({"key": key, "role": role, "text": text,
                              "status": "approved" if edit else item.get("status", "pending"),
                              "source": edit["approval_ref"] if edit else item.get("source", "旧页资料"),
                              **({"label": item["label"]} if item.get("label") else {})})
                if not edit and items[-1]["status"] == "approved":
                    items[-1]["status"] = "pending"
        name = source.get("name", f"第 {index} 屏")
        if not items:
            items = [{"key": f"{screen_id}:pending", "role": "body", "text": "本屏文案待登记核准",
                      "status": "internal", "source": "无可读运营文案"}]
        result.append({"id": screen_id, "name": name, "kind": "info" if source.get("kind") == "info" else "visual",
                       "old_range": list(old_range), "items": items, "offsets": {}})
    if not set(approved) <= all_keys:
        problem("核准文案包含运营资料未登记的字段，请核对字段键。")
    return result


def _fit_lines(text, size, width):
    return render._wrapped(text, width, size)


def derive_layout(registration):
    """Fit source-aligned workbook fields to explicitly local archetypes."""
    old_width, _ = source_geometry(registration)
    screens = review_screens(registration)
    for index, screen in enumerate(screens):
        info = screen["kind"] == "info"
        hero = index == 0 and not info
        typ = RULES["modules"]["hero" if hero else screen["kind"]]
        measured = round((screen["old_range"][1] - screen["old_range"][0]) * WIDTH / old_width)
        min_height = max(MIN_HEIGHT, round(typ["screen_height"] * .7))
        height = max(min_height, min(measured, 1400))
        has_bottle = bool(registration["selection"].get("bottle")) and not info
        # Use a short CJK headline as a native vertical column in the first local archetype.
        title = next((item for item in screen["items"] if item["role"] == "title"), None)
        short_vertical = bool(hero and has_bottle and title and re.fullmatch(r"[\u3400-\u9fff\n ]{2,16}", title["text"]))
        palette = "neutral" if info else ("warm" if hero or index % 2 == 0 else "green")
        brand = [24, 59, 289, 163] if hero else None
        start = 184 if hero else 81
        text_x, text_right = (40, 291 if short_vertical else 355) if has_bottle else (38, 752)
        slots = {}
        cursor = start
        for item in screen["items"]:
            role = item["role"]
            size = 20 if role == "footnote" else typ.get(f"{role}_size", typ["body_size"])
            if role == "title" and has_bottle:
                size = min(size, 48)
            content = display_text(item)
            orientation = "vertical" if short_vertical and item is title else "horizontal"
            leading = round(size * (1.20 if orientation == "vertical" else 1.38))
            if orientation == "vertical":
                rows = content.splitlines()
                advance = max(len(line) for line in rows) * size
                block_height = max(248, advance + 30)
                right = text_x + min(186, (len(rows) + 1) * leading + 20)
            else:
                right = text_right
                count = _fit_lines(content, size, right - text_x).count("\n") + 1
                block_height = leading * count + 16
            slots[item["key"]] = {"box": [text_x, cursor, right, cursor + block_height], "size": size,
                                  "leading": leading, "orientation": orientation, "align": "left"}
            cursor += block_height + (8 if info else 10)
        height = max(height, cursor + 42)
        if height > MAX_HEIGHT:
            problem(f"「{screen['name']}」文案超出单屏容量，请拆屏后登记。")
        product = None
        if has_bottle:
            product = ([318, max(180, height // 4), 522, height - 74] if short_vertical
                       else [415, max(150, round(height * .29)), 749, height - 67])
        screen.update(height=height, palette=palette, lighting="upper_left", scene_profile="tea_still_life",
                      bottle_box=product, brand_box=brand, slots=slots,
                      preserve=["registered_copy", "actual_product", "module_order", "unapproved_brand_placeholder"],
                      geometry_source="local_template", composition="center_product" if short_vertical else "left_copy")
        verify_screen(screen)
    return layout_root(registration, screens, "local_template")


def layout_root(registration, screens, source):
    if sum(screen["height"] for screen in screens) > RULES["height_limit"]:
        problem("总画布高度超过排版上限，请分批评审后导出。")
    return {"rules_version": RULES["version"], "code": registration["code"], "width": WIDTH,
            "name": registration["name"], "geometry_source": source, "screens": screens}


def reference_layout(registration, profile):
    """Use the reviewed ranges and roles of a specific, verified old page."""
    screens = review_screens(registration)
    for screen, spec in zip(screens, profile["screens"], strict=True):
        screen.update({key: value for key, value in spec.items() if key not in {"id", "name", "kind", "source_range", "items"}})
        screen["geometry_source"] = "verified_old_page"
        if screen.get("review_source") not in ("confirmed_png", "reused_png", "old_crop", "scene_edit", "asset_patch"):
            verify_screen(screen)
        elif screen["height"] != screen["old_range"][1] - screen["old_range"][0]:
            problem("保真的旧页区段和登记屏高不一致。")
    return {**layout_root(registration, screens, "verified_old_page"),
            "reference_version": profile["version"], "rules_version": profile["version"],
            **({"whole_review": True} if profile.get("whole_review") else {})}


def display_text(item):
    return f"{item['label']}  {item['text']}" if item.get("label") else item["text"]


def box(value, bounds, label):
    if (not isinstance(value, list) or len(value) != 4 or any(type(entry) is not int for entry in value)
            or not render._inside(value, bounds)):
        problem(f"「{label}」坐标超出安全区或不是有效矩形。")


def verify_screen(screen):
    height = screen["height"]
    if type(height) is not int or not MIN_HEIGHT <= height <= MAX_HEIGHT:
        problem("屏高超出支持范围。")
    if screen["palette"] not in RULES["palettes"] or screen["lighting"] not in RULES["lighting"]:
        problem("场景色调或照明方向未登记。")
    top = 20 if "reference_art" in screen else RULES["screen"]["draft_strip_height"]
    safe = (20, top, WIDTH - 20, height - 20)
    boxes = []
    for key in ("bottle_box", "brand_box"):
        value = screen[key]
        if value is not None:
            box(value, safe, key)
            boxes.append(value)
    for item in screen["items"]:
        slot = screen["slots"][item["key"]]
        value = slot["box"]
        box(value, safe, item["key"])
        size, leading = slot["size"], slot["leading"]
        if (type(size) is not int or type(leading) is not int or not 18 <= size <= 90 or not size <= leading <= 150
                or slot["orientation"] not in ("horizontal", "vertical") or slot["align"] not in ("left", "center", "right")):
            problem(f"「{item['key']}」文字尺寸、方向或对齐无效。")
        boxes.append(value)
    if any(render._overlap(first, second) for i, first in enumerate(boxes) for second in boxes[i+1:]):
        problem(f"「{screen['name']}」的文案、品牌或产品保护区发生重叠。")


def _required_object(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        problem(f"{label}字段不符合已登记的模型契约。")


def validate_layout(model_result, registration):
    """Normalize a model response into the same representation as local layouts."""
    _required_object(model_result, {"schema_version", "width", "screens"}, "模型布局")
    if type(model_result["schema_version"]) is not int or model_result["schema_version"] != 1 or model_result["width"] != WIDTH:
        problem("模型版式版本或画布宽度不匹配。")
    source = review_screens(registration)
    modeled = model_result["screens"]
    if not isinstance(modeled, list) or len(modeled) != len(source):
        problem("模型返回的分屏数与已登记待审模块不一致。")
    required = {"id", "old_range", "height", "palette", "lighting", "scene_profile", "bottle_box", "brand_box",
                "slots", "preserve", "composition"}
    for model, original in zip(modeled, source):
        _required_object(model, required, "分屏布局")
        if model["id"] != original["id"] or model["old_range"] != original["old_range"]:
            problem("模型调整了登记模块顺序或旧图对应范围。")
        slots = model["slots"]
        _required_object(slots, {item["key"] for item in original["items"]}, "文字槽位")
        for slot in slots.values():
            _required_object(slot, {"box", "size", "leading", "orientation", "align"}, "文字槽位")
        if model["scene_profile"] not in {"tea_still_life", "fruit_still_life", "tabletop", "studio", "informational"}:
            problem("模型场景超出已登记的场景类型。")
        if model["composition"] not in {"center_product", "left_copy", "right_copy", "top_copy", "info_grid"}:
            problem("模型构图超出已登记类型。")
        preserve = model["preserve"]
        if (not isinstance(preserve, list) or any(value not in {"registered_copy", "actual_product", "module_order",
                                                               "unapproved_brand_placeholder", "source_light_direction"}
                                                  for value in preserve)):
            problem("模型给出了未登记的保留动作。")
        if not {"registered_copy", "actual_product", "module_order"} <= set(preserve):
            problem("模型遗漏必需的保留约束。")
        if model["bottle_box"] is not None and not registration["selection"].get("bottle"):
            problem("模型尝试使用未登记的瓶图。")
        if original["kind"] == "info" and model["bottle_box"] is not None:
            problem("信息模块不应占用独立产品图位置。")
        original.update(model, geometry_source="company_model")
        verify_screen(original)
    return layout_root(registration, source, "company_model")


def reconcile_layout(model_result, registration):
    """Fit company proposals to actual local glyph metrics without changing copy."""
    _required_object(model_result, {"schema_version", "width", "screens"}, "模型布局")
    if model_result["schema_version"] != 1 or model_result["width"] != WIDTH:
        problem("模型版式版本或画布宽度不匹配。")
    source = review_screens(registration)
    proposed = model_result["screens"]
    if not isinstance(proposed, list) or len(source) != len(proposed):
        problem("模型分屏与登记模块数量不一致。")
    fallback = derive_layout(registration)["screens"]
    screens = []
    for original, model, backup in zip(source, proposed, fallback):
        if not isinstance(model, dict) or model.get("id") != original["id"] or model.get("old_range") != original["old_range"]:
            problem("模型分屏顺序或对应旧图区间不匹配。")
        candidate = json.loads(json.dumps(model))
        candidate["preserve"] = list(dict.fromkeys([*candidate.get("preserve", []), "registered_copy", "actual_product", "module_order"]))
        fixed = []
        try:
            if set(candidate["slots"]) != {item["key"] for item in original["items"]}:
                problem("模型未返回完整文字槽位。")
            height = candidate["height"]
            safe = (20, RULES["screen"]["draft_strip_height"], WIDTH - 20, height - 20)
            occupied = [candidate[key] for key in ("bottle_box", "brand_box") if candidate[key] is not None]
            for item in original["items"]:
                key = item["key"]
                slot = candidate["slots"][key]
                first = dict(slot)
                left, top, right, bottom = slot["box"]
                text = display_text(item)
                if slot["orientation"] == "vertical" and text.isascii():
                    slot["orientation"] = "horizontal"
                for size in range(min(90, slot["size"]), 17, -1):
                    leading = max(size + 2, min(slot["leading"], round(size * 1.30)))
                    if slot["orientation"] == "vertical":
                        columns = text.splitlines()
                        fitted_width = max(right - left, len(columns) * leading + 14)
                        fitted_height = max(bottom - top, max(map(len, columns)) * size + 22)
                    else:
                        fitted_width = right - left
                        count = _fit_lines(text, size, fitted_width).count("\n") + 1
                        fitted_height = max(bottom - top, count * leading + 12)
                    for y in (top, top + 12, top + 24, top + 42, top - 12, top - 24, top + 64, top + 88):
                        area = [left, y, left + fitted_width, y + fitted_height]
                        if render._inside(area, safe) and not any(render._overlap(area, other) for other in occupied):
                            slot.update(box=area, size=size, leading=leading)
                            occupied.append(area)
                            break
                    else:
                        continue
                    break
                else:
                    problem(f"「{key}」的模型槽位需要重新排版。")
                if slot != first:
                    fixed.append(key)
            validated = validate_layout({"schema_version": 1, "width": WIDTH, "screens": [candidate]},
                                        {**registration, "review_plan": {"screens": [next(row for row in registration["review_plan"]["screens"] if row["id"] == original["id"])]},
                                         "approved_copy": {key: value for key, value in registration.get("approved_copy", {}).items()
                                                           if key in candidate["slots"]}})["screens"][0]
        except (KeyError, TypeError, ValueError, render.RenderError) as error:
            screens.append({**backup, "geometry_source": "local_template_after_model", "fit_adjustment": "local_reflow",
                            "scene_profile": model.get("scene_profile", backup["scene_profile"])
                            if model.get("scene_profile") in {"tea_still_life", "fruit_still_life", "tabletop", "studio", "informational"}
                            else backup["scene_profile"],
                            "fit_note": str(error) if isinstance(error, render.RenderError) else "模型槽位字段无效"})
        else:
            screens.append({**validated, "fit_adjustment": fixed})
    return layout_root(registration, screens, "company_model_with_local_fit")


def model_prompt(registration):
    """Only field roles, glyph lengths and layout inputs accompany the allowed old page."""
    instructions = (RULE_DIR / "refresh-model-prompt-v1.md").read_text(encoding="utf-8")
    schema = (RULE_DIR / "refresh-layout-schema-v1.json").read_text(encoding="utf-8")
    width, height = source_geometry(registration)
    context = {"source_size": [width, height], "has_bottle": bool(registration["selection"].get("bottle")),
               "screens": [{"id": screen["id"], "kind": screen["kind"], "old_range": screen["old_range"],
                            "fields": [{"key": item["key"], "role": item["role"],
                                        "line_lengths": [len(row) for row in display_text(item).splitlines()]}
                                       for item in screen["items"]]} for screen in review_screens(registration)]}
    return f"{instructions}\n\nJSON schema:\n{schema}\n\nInputs:\n{json.dumps(context, ensure_ascii=False)}"


def scene_prompt(screen):
    subject = {"tea_still_life": "neutral tea leaf still life and tactile dry leaves along the near edge",
               "fruit_still_life": "restrained fruit-toned still-life surfaces with unobtrusive ingredients at the edge",
               "tabletop": "quiet premium editorial tabletop and restrained out-of-focus props",
               "studio": "quiet seamless studio surfaces and subtle material texture",
               "informational": "neutral matte studio surface"}[screen["scene_profile"]]
    light = {"upper_left": "soft natural light from the upper left", "upper_right": "soft natural light from the upper right",
             "diffuse": "diffuse natural light from the front"}[screen["lighting"]]
    layout = {"canvas": [WIDTH, screen["height"]], "unoccupied_product_box": screen["bottle_box"],
              "quiet_type_areas": [item["box"] for item in screen["slots"].values()], "quiet_brand_area": screen["brand_box"]}
    return (f"Single photographic background plate for a premium retail composition. {subject}. Use {light}. "
            f"Tone: {screen['palette']}. Respect these pixel-relative zones: {json.dumps(layout)}. "
            "Create a coherent wall and landing plane. Keep the reserved zones low-texture, uncrowded and empty. "
            "No bottle, packaging, product silhouette, label, brand, logo, glyphs, writing, numbers or watermark.")


def safe_file(folder, filename):
    if not isinstance(filename, str) or Path(filename).name != filename:
        problem("确认素材必须使用工作目录内的冻结名称。")
    path = (folder / filename).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        problem("冻结素材缺失或不在当前任务目录。")
    return path


def translated(rect, dx, dy):
    return [rect[0] + dx, rect[1] + dy, rect[2] + dx, rect[3] + dy]


def art_overlay(name, plate, color):
    x, y, r, b = plate
    image = Image.new("RGBA", (r - x, b - y), color)
    return render.Layer(name, tuple(plate), image=image)


def contrast_underlay(screen, height):
    """Give text a stable local contrast on generated backgrounds and placeholders."""
    image = Image.new("L", (WIDTH, height), 0)
    draw = ImageDraw.Draw(image)
    for slot in screen["slots"].values():
        x, y, r, b = slot["box"]
        draw.rounded_rectangle((x - 8, y - 5, r + 8, b + 5), radius=11, fill=155)
    image = image.filter(ImageFilter.GaussianBlur(7))
    extent = image.getbbox()
    if extent is None:
        return None
    base = Image.new("RGBA", image.size, RULES["palettes"][screen["palette"]]["paper"])
    base.putalpha(image)
    return render.Layer("光影｜文字衬底", extent, image=base.crop(extent))


def product_shadows(product, lighting, screen_size):
    """Create a short projected shadow and contact shadow from the actual alpha silhouette."""
    width, height = screen_size
    mask = product.image.getchannel("A")
    al, at, ar, ab = mask.getbbox()
    x = product.area[0] + al
    y = product.area[1] + at
    silhouette = mask.crop((al, at, ar, ab))
    right, ground = x + silhouette.width, y + silhouette.height
    dx = {"upper_left": .22, "upper_right": -.22, "diffuse": .04}[lighting]
    depth = .09
    cast = silhouette.transform((width, height), Image.Transform.AFFINE,
        (1, -dx / depth, -x + ground * dx / depth, 0, -1 / depth, silhouette.height + ground / depth),
        Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(11)).point(lambda value: round(value * .16))
    contact = Image.new("L", (width, height))
    pad = max(7, silhouette.width // 16)
    ImageDraw.Draw(contact).ellipse((x + pad, ground - 9, right - pad, ground + 10), fill=118)
    contact = contact.filter(ImageFilter.GaussianBlur(5))
    combined = Image.frombytes("L", cast.size,
        bytes(min(150, a + b) for a, b in zip(cast.tobytes(), contact.tobytes())))
    bbox = combined.getbbox()
    rgba = Image.new("RGBA", combined.size, (45, 27, 17, 0))
    rgba.putalpha(combined)
    return render.Layer("光影｜投影与接触阴影", bbox, image=rgba.crop(bbox))


def product_bounce(product, lighting):
    """Add a restrained environment reflection behind the unchanged bottle pixels."""
    width, height = product.image.size
    mask = product.image.getchannel("A")
    offset = -3 if lighting == "upper_left" else 3 if lighting == "upper_right" else 0
    light = Image.new("L", mask.size)
    light.paste(mask, (offset, 0))
    light = light.filter(ImageFilter.GaussianBlur(4)).point(lambda value: round(value * .18))
    tint = Image.new("RGBA", (width, height), (251, 222, 166, 0))
    tint.putalpha(light)
    return render.Layer("光影｜局部轮廓反光", product.area, image=tint)


def brand_layers(screen, folder):
    brand = screen.get("brand_box")
    if brand is None:
        return []
    x, y, r, b = brand
    base = [render._solid("品牌｜留白底板", x, y, r - x, b - y, "#fffdf7")]
    if not screen.get("logo"):
        label = RULES["brand_pending_display"]
        inset = 9
        font_size = min(20, max(18, (r - x - 2 * inset - 12) // len(label)))
        marker = render._type("品牌｜标志占位", label, x + inset, y + 8, r - x - 2 * inset, font_size)
        if not render._inside(marker.area, brand):
            problem("品牌占位区域过小，请扩大品牌留白。")
        base.append(marker)
        return base
    inset = max(16, round((r - x) * RULES["brand"]["clear_space_ratio"]))
    if min(r - x, b - y) <= 2 * inset:
        problem("品牌标志区不足以留出净空。")
    placed = render._image("品牌｜正式标志", safe_file(folder, screen["logo"]), (x + inset, y + inset, r - inset, b - inset))
    al, _, ar, _ = placed.image.getchannel("A").getbbox()
    if ar - al <= RULES["brand"]["minimum_logo_width"]:
        problem("正式标志屏显宽度低于登记规则，请调整品牌槽位。")
    base.append(placed)
    return base


def refresh_type(item, slot, ink):
    x, y, r, b = slot["box"]
    text = display_text(item)
    size, leading = slot["size"], slot["leading"]
    orientation = slot["orientation"]
    if orientation == "horizontal":
        text = _fit_lines(text, size, r - x)
        if (text.count("\n") + 1) * leading + 8 > b - y:
            problem(f"「{item['key']}」文案超过本次横排槽位，须调整排版。")
    else:
        columns = text.splitlines()
        if (len(columns) * leading + 12 > r - x
                or max((len(col) for col in columns), default=0) * size + 16 > b - y):
            problem(f"「{item['key']}」文案超过本次竖排槽位，须调整排版。")
    layer = render.Layer(f"{ROLES[item['role']]}｜{item['key']}", (x, y, r, b), text, size, leading, ink)
    return layer, orientation, slot["align"]


def validate_offsets(screen):
    offsets = screen.get("offsets", {})
    if not offsets:
        return screen
    x, y = offsets.get("text_x", 0), offsets.get("text_y", 0)
    dx, dy = offsets.get("product_x", 0), offsets.get("product_y", 0)
    changed = {**screen, "slots": {key: {**slot, "box": translated(slot["box"], x, y)}
                                   for key, slot in screen["slots"].items()}}
    if screen["bottle_box"] is not None:
        changed["bottle_box"] = translated(screen["bottle_box"], dx, dy)
    return changed


def screen_layers(screen, folder):
    screen = validate_offsets(screen)
    verify_screen(screen)
    if "reference_art" in screen:
        return reference_layers(screen, folder)
    height = screen["height"]
    area = (0, 0, WIDTH, height)
    palette = RULES["palettes"][screen["palette"]]
    if screen.get("background"):
        background = render._image("背景｜环境", safe_file(folder, screen["background"]), area, background=True)
    else:
        background = render._solid("背景｜信息底色", 0, 0, WIDTH, height, palette["paper"])
    images = [background]
    if screen.get("background"):
        underlay = contrast_underlay(screen, height)
        if underlay:
            images.append(underlay)
    if screen["bottle_box"] is not None and screen.get("bottle"):
        bottle = render._image("产品｜独立瓶图", safe_file(folder, screen["bottle"]), tuple(screen["bottle_box"]), bottle=True)
        images.extend([product_shadows(bottle, screen["lighting"], (WIDTH, height)), product_bounce(bottle, screen["lighting"]), bottle])
    if screen["kind"] == "info":
        for item in screen["items"]:
            if item["role"] in ("body", "field"):
                x, y, r, b = screen["slots"][item["key"]]["box"]
                images.append(art_overlay(f"表格底｜{item['key']}", (x - 3, y, r + 3, b), "#ffffff55"))
    brand = brand_layers(screen, folder)
    images.extend(layer for layer in brand if layer.image is not None)
    images.append(render._solid("标注｜内部评审底条", 0, 0, WIDTH, RULES["screen"]["draft_strip_height"], "#fffcf6"))
    flags = [*(layer for layer in brand if layer.image is None),
             render._type("状态｜内部评审", RULES["pending_display"], 24, 5, WIDTH - 48, 19)]
    text = [refresh_type(item, screen["slots"][item["key"]], palette["ink"]) for item in screen["items"]]
    return [*((layer, None, None) for layer in images), *((layer, "horizontal", "left") for layer in flags), *text]


def reference_layers(screen, folder):
    images = []
    for asset in screen["reference_art"]:
        source = safe_file(folder, asset["file"])
        x, y, r, b = area = tuple(asset["box"])
        with Image.open(source) as art:
            if art.size != (r - x, b - y):
                problem(f"「{asset['name']}」尺寸与登记槽位不一致。")
            images.append(render.Layer(asset["name"], area, image=art.convert("RGBA")))
    if screen["bottle_box"] and screen.get("bottle"):
        bottle = render._image("产品｜独立瓶图", safe_file(folder, screen["bottle"]), tuple(screen["bottle_box"]), bottle=True)
        images.extend([product_shadows(bottle, screen["lighting"], (WIDTH, screen["height"])), bottle])
    if screen["brand_box"]:
        x, y, r, b = screen["brand_box"]
        inset = max(12, round((r - x) * RULES["brand"]["clear_space_ratio"]))
        if not screen.get("logo"):
            problem("缺少已登记的品牌标志。")
        logo = render._image("品牌｜正式标志", safe_file(folder, screen["logo"]), (x + inset, y + inset, r - inset, b - inset))
        left, _, right, _ = logo.image.getchannel("A").getbbox()
        if right - left < RULES["brand"]["minimum_logo_width"]:
            problem("正式标志的可见宽度低于登记规则。")
        images.append(logo)
    ink = RULES["palettes"][screen["palette"]]["ink"]
    text = []
    for item in screen["items"]:
        slot = screen["slots"][item["key"]]
        text.append(refresh_type(item, slot, slot.get("color", ink)))
    return [*((layer, None, None) for layer in images), *text]


def build_layers(layout, folder):
    folder = Path(folder).resolve()
    total = 0
    result = []
    for index, screen in enumerate(layout["screens"], 1):
        group = f"{index:02d}｜{screen['name']}"
        height = screen["height"]
        layers = screen_layers(screen, folder)
        names = [layer.name for layer, _, _ in layers]
        if len(set(names)) != len(names) or any(not render._inside(layer.area, (0, 0, WIDTH, height)) for layer, _, _ in layers):
            problem("逐屏图层存在重名或位置超出画布。")
        result.append((screen["id"], group, total, height, layers))
        total += height
    return result, total


def place(photo, layer, orientation, align, top, image_name):
    if layer.image is not None:
        return render._add_layer(photo, layer, top, image_name)
    if orientation not in ("horizontal", "vertical"):
        problem(f"「{layer.name}」缺少有效的文字方向。")
    x, y, r, b = render._absolute(layer.area, top)
    padding = 10 if layer.size >= 50 else 4
    created = photo.command("type.create", name=layer.name, text=layer.text, x=x + 4, y=y + padding,
                            box=[x + 4, y + padding, r - x - 8, b - y - padding], align=align,
                            orientation=orientation, font=os.environ.get("REFRESH_FONT_FAMILY", render.DEFAULT_FONT),
                            size=layer.size, leading=layer.leading, color=layer.color)["layer"]
    metrics = photo.command("type.info", layer=created)
    lines = metrics.get("lines", [])
    if (not lines or len(lines) != layer.text.count("\n") + 1 or lines[-1]["end"] != len(layer.text)
            or metrics.get("orientation", "").lower() != orientation):
        problem(f"「{layer.name}」未能按登记方向完整排版。")
    if not render._inside(metrics["bounds"], (x, y, r, b)):
        problem(f"「{layer.name}」的字形超出排版区。")
    return created


def rasterize(layout, folder, png_path, psd_path=None):
    scenes, total = build_layers(layout, folder)
    if not scenes or total > RULES["height_limit"]:
        problem("总画布高度超出支持范围。")
    with tempfile.TemporaryDirectory(prefix="refresh-layout-", dir=Path(png_path).parent) as temp:
        work = Path(temp)
        with render.PhotoCraft(work) as photo:
            photo.call("doc.new", name=f"{layout['code']} 商品翻新", width=WIDTH, height=total)
            starter = photo.call("doc.inspect")["layers"][0]["id"]
            for ordinal, (_, group, top, _, layers) in reversed(list(enumerate(scenes, 1))):
                selected = [place(photo, layer, orientation, align, top, f"s{ordinal:02d}-{index:02d}.png")
                            for index, (layer, orientation, align) in enumerate(layers, 1)]
                for index, identifier in enumerate(selected):
                    photo.command("layer.select", layer=identifier, mode="add" if index else "replace")
                photo.command("layer.groupLayers", name=group)
            photo.command("layer.delete", layer=starter)
            warnings = []
            if psd_path is not None:
                warnings.extend(photo.call("doc.save", path="detail.psd").get("warnings", []))
                photo.call("doc.close", index=0)
                warnings.extend(photo.call("doc.open", path="detail.psd", approveHighMemory=True).get("warnings", []))
            native = photo.call("doc.inspect")
            expected = [(*scene[:4], [layer for layer, _, _ in scene[4]]) for scene in scenes]
            report = render._verify(native, expected, WIDTH, total)
            for got, source, group in zip(report["screens"], scenes, native["layers"]):
                by_name = {layer["name"]: layer["id"] for layer in group["children"]}
                orientations = {layer.name: orientation for layer, orientation, _ in source[4] if orientation}
                for layer in got["layers"]:
                    orientation = orientations.get(layer["name"])
                    if orientation:
                        layer["orientation"] = orientation
                    if orientation == "vertical":
                        metrics = photo.command("type.info", layer=by_name[layer["name"]])
                        if metrics.get("orientation", "").lower() != orientation:
                            problem(f"「{layer['name']}」重开后未保留竖排文字层。")
            photo.call("doc.save", path="detail.png")
        shutil.copyfile(work / "detail.png", png_path)
        if psd_path is not None:
            shutil.copyfile(work / "detail.psd", psd_path)
    return {**report, "warnings": warnings, "reopened_psd": psd_path is not None}


def preview(layout, folder, png_path):
    return rasterize(layout, folder, png_path)


def export(layout, folder, psd_path, png_path):
    return rasterize(layout, folder, png_path, psd_path)
