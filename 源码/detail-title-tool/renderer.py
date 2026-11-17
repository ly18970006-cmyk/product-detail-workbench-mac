"""Prepare and publish layered PSDs with the CPU-only PhotoCraft CLI."""

import json
import math
import shutil
import subprocess
import threading
from collections import Counter
from pathlib import Path

from PIL import Image, ImageChops, ImageOps

from psd_text_alignment import PSDTextAlignmentError, sync_right_alignment


class RenderError(RuntimeError):
    """An asset, layout, or renderer failed verification."""

    def __init__(self, message, field_id=None):
        super().__init__(message)
        self.field_id = field_id


class RenderTimeout(RenderError):
    """PhotoCraft exceeded its render deadline."""


class PhotoCraft:
    def __init__(self, binary, work):
        self.binary = str(binary)
        self.work = work
        self.sequence = 0
        self.timed_out = threading.Event()

    def __enter__(self):
        self.log = (self.work / "render.log").open("a", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                [self.binary, "serve", "--no-gpu", "--automation-read-root", str(self.work),
                 "--automation-write-root", str(self.work)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            self.log.close()
            raise RenderError("无法启动 PhotoCraft CLI。请检查服务器上的渲染器路径。") from error
        self.deadline = threading.Timer(180, self._expire)
        self.deadline.daemon = True
        self.deadline.start()
        return self

    def _stop(self):
        if self.process.poll() is None:
            self.process.kill()

    def _expire(self):
        self.timed_out.set()
        self._stop()

    def __exit__(self, *exception):
        self.deadline.cancel()
        self._stop()
        self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
        self.log.close()

    def call(self, method, **params):
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params}
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
        except OSError as error:
            if self.timed_out.is_set():
                raise RenderTimeout("排版超时，请重试。") from error
            raise RenderError("PhotoCraft CLI 未响应。") from error
        if self.timed_out.is_set():
            raise RenderTimeout("排版超时，请重试。")
        if not line:
            raise RenderError("PhotoCraft CLI 超时或提前退出。")
        try:
            response = json.loads(line)
        except ValueError as error:
            raise RenderError("PhotoCraft CLI 返回了无效响应。") from error
        if response.get("id") != self.sequence or not response.get("ok"):
            raise RenderError(f"PhotoCraft {method} 失败：{response.get('error', '响应不匹配')}")
        return response["result"]

    def command(self, command, **params):
        return self.call("engine.execute", command=command, params=params)

    def open(self, file):
        opened = self.call("doc.open", path=file, approveHighMemory=True)
        return self.call("doc.inspect"), opened.get("warnings", [])


def _matching_layers(document, path):
    layers = document["layers"]
    for group in path[:-1]:
        matches = [layer for layer in layers if layer["name"] == group and layer["kind"] == "Group"]
        if len(matches) != 1:
            raise RenderError(f"PSD 组无法唯一匹配：{group}")
        layers = matches[0]["children"]
    return [layer for layer in layers if layer["name"] == path[-1]]


def _layer(document, path, kind=None, layer_id=None, text=None):
    matches = [layer for layer in _matching_layers(document, path)
               if (kind is None or layer["kind"] == kind)
               and (layer_id is None or layer["id"] == layer_id)
               and (text is None or layer.get("text", {}).get("text") == text)]
    if len(matches) != 1:
        raise RenderError(f"PSD 图层无法唯一匹配：{' / '.join(path)}")
    return matches[0]


def _walk(layers):
    for layer in layers:
        yield layer
        yield from _walk(layer.get("children", []))


def _document_report(document, spec):
    width, height = document["width"], document["height"]
    first_screen = spec["first_screen_height"]
    if width != spec.get("expected_width", 790) or not 0 < first_screen <= height:
        raise RenderError("PSD 宽度或首屏边界与已登记母版不符。")
    groups = [layer["name"] for layer in document["layers"] if layer["kind"] == "Group"]
    if spec.get("expected_groups") is not None and len(groups) != spec["expected_groups"]:
        raise RenderError("PSD 的页面分组数量与已登记母版不符。")
    counts = Counter(layer["kind"] for layer in _walk(document["layers"]))
    return {"width": width, "height": height, "groups": len(groups), "group_names": groups,
            "text_layers": counts["Type"], "smart_objects": counts["Smart Object"],
            "first_screen_height": first_screen}


def _rect(bounds):
    x, y, width, height = bounds
    return [x, y, x + width, y + height]


def _inside(inner, outer):
    return (outer[0] <= inner[0] < inner[2] <= outer[2]
            and outer[1] <= inner[1] < inner[3] <= outer[3])


def _overlap(first, second):
    return (max(first[0], second[0]) < min(first[2], second[2])
            and max(first[1], second[1]) < min(first[3], second[3]))


def _protected_areas(spec, path, *, draft=False):
    for area in spec.get("protected_areas", []):
        if isinstance(area, dict):
            if path not in area.get("member_paths", []):
                bounds = area.get("draft_bounds", [area["bounds"]]) if draft else [area["bounds"]]
                yield from ((region, area["name"]) for region in bounds)
        else:
            # Drafts created before the green grape catalog declared its tagline
            # as a member still carry the old anonymous rectangle.
            if (area == [304, 207, 616, 298]
                    and spec.get("title_path") == ["01｜品牌首屏 · 上标题下双瓶", "此时清茶"]
                    and path == ["01｜品牌首屏 · 上标题下双瓶", "此刻年轻"]):
                continue
            yield area, "首屏的瓶身或品牌标志"


def _check_layout(layer, spec, product_bounds=None):
    if not layer["visible"] or not layer.get("bounds"):
        raise RenderError("标题文字层没有可见内容。")
    bounds = _rect(layer["bounds"])
    canvas = [0, 0, spec.get("expected_width", 790), spec["first_screen_height"]]
    area = spec.get("title_area", canvas)
    if not _inside(bounds, canvas) or not _inside(bounds, area):
        raise RenderError("标题超过可用排版区域。请缩短标题或调整换行。")
    protected = [bounds for bounds, _ in _protected_areas(spec, spec["title_path"])]
    if product_bounds is not None:
        protected.append(product_bounds)
    if any(_overlap(bounds, region) for region in protected):
        raise RenderError("标题与瓶身或已锁定元素重叠。请缩短标题或调整换行。")
    return bounds


def _check_text_metrics(result, title, spec):
    lines = result.get("lines", [])
    if result.get("text") != title or not lines or lines[-1]["end"] != len(title):
        raise RenderError("排版结果没有容纳完整标题。请缩短标题或调整换行。")
    shape = result.get("shape")
    box = shape.get("Box") if isinstance(shape, dict) else None
    if "title_box" in spec and box is None:
        raise RenderError("保存的文字层没有保留母版的段落框。")
    if box and any(line["baseline"] + line["descent"] > box["height"] + 0.5
                   or line["x0"] < -0.5 or line["x1"] > box["width"] + 0.5 for line in lines):
        raise RenderError("标题文字框会裁切内容。请缩短标题。")
    return len(lines)


def _save_bottle(source, destination, width, height, stretch=False):
    with Image.open(source) as image:
        pixels = image.convert("RGBA")
    alpha = pixels.getchannel("A")
    low, high = alpha.getextrema()
    if low == 255 or high == 0:
        raise RenderError("登记的瓶图需要有透明背景与可见产品像素。")
    cropped = pixels.crop(alpha.getbbox())
    size = (width, height)
    if stretch:
        fit = cropped.resize(size, Image.Resampling.LANCZOS)
    else:
        fit = ImageOps.contain(cropped, size, Image.Resampling.LANCZOS)
    output = Image.new("RGBA", size)
    output.alpha_composite(fit, ((width - fit.width) // 2, height - fit.height))
    output.save(destination)


def _replace_smart_object(photo, layer, file):
    if layer.get("smartContents", {}).get("instances") != 1:
        raise RenderError(f"「{layer['name']}」不是独立的智能对象。")
    result = photo.call("doc.replaceSmartObject", path=file, layer=layer["id"])
    if result.get("instances") != [layer["id"]]:
        raise RenderError(f"「{layer['name']}」替换了其他智能对象实例。")


def _apply_product(photo, work, document, spec):
    for path in spec.get("hide_paths", []):
        layer = _layer(document, path)
        if layer["visible"]:
            photo.command("layer.setProps", layer=layer["id"], visible=False)

    scene = spec.get("scene_replacement")
    if scene:
        target = _layer(document, scene["path"], "Smart Object")
        shutil.copyfile(scene["image"], work / "prepared-scene.png")
        _replace_smart_object(photo, target, "prepared-scene.png")

    if "product_path" in spec:
        target = _layer(document, spec["product_path"], "Smart Object")
        x, y, width, height = target["bounds"]
        name = target["name"]
    elif "product_box" in spec:
        x, y, right, bottom = spec["product_box"]
        width, height = right - x, bottom - y
        name = "本期产品｜已登记透明原瓶"
    else:
        return

    if not _inside([x, y, x + width, y + height],
                   [0, 0, document["width"], spec["first_screen_height"]]):
        raise RenderError("瓶图放置区域超出首屏。")
    _save_bottle(spec["product_image"], work / "registered-bottle.png", width, height,
                 stretch=spec.get("product_stretch", False))
    if "product_path" in spec:
        _replace_smart_object(photo, target, "registered-bottle.png")
    else:
        anchor_path = spec["product_anchor_path"]
        anchor = _layer(document, anchor_path)
        photo.command("layer.select", layer=anchor["id"], mode="replace")
        placed = photo.call("doc.place", path="registered-bottle.png", scale=100, fit=False,
                            smartObject=True, center=[x + width / 2, y + height / 2])
        photo.command("layer.setProps", layer=placed["layer"], name=name)


def _product_path(spec):
    if "product_path" in spec:
        return spec["product_path"]
    if "product_box" in spec:
        return [*spec["product_anchor_path"][:-1], "本期产品｜已登记透明原瓶"]
    return None


def _verify_product(document, spec):
    scene = spec.get("scene_replacement")
    if scene:
        layer = _layer(document, scene["path"], "Smart Object")
        if not layer["visible"] or layer.get("smartSource", {}).get("fileName") != "prepared-scene.png":
            raise RenderError("保存的 PSD 未保留已核准的独立背景。")
    for hidden in spec.get("hide_paths", []):
        if _layer(document, hidden)["visible"]:
            raise RenderError("保存的 PSD 仍显示原先的瓶贴修正层。")
    path = _product_path(spec)
    if path is None:
        return None
    layer = _layer(document, path, "Smart Object")
    if not layer["visible"] or layer.get("smartSource", {}).get("fileName") != "registered-bottle.png":
        raise RenderError("保存的 PSD 中没有对应登记瓶图的独立智能对象。")
    return _rect(layer["bounds"])


def _crop_preview(source, target, height):
    with Image.open(source) as image:
        image.crop((0, 0, image.width, height)).save(target)


def _difference(first_path, second_path, top=0):
    with Image.open(first_path) as first, Image.open(second_path) as second:
        if first.size != second.size:
            raise RenderError("对照 PNG 的画布尺寸不一致。")
        if top >= first.height:
            return {"max_channel_difference": 0, "affected_pixels": 0, "affected_ratio": 0.0}
        box = (0, top, first.width, first.height)
        difference = ImageChops.difference(first.crop(box).convert("RGBA"), second.crop(box).convert("RGBA"))
    channels = difference.split()
    combined = channels[0]
    for channel in channels[1:]:
        combined = ImageChops.lighter(combined, channel)
    affected = combined.width * combined.height - combined.histogram()[0]
    return {"max_channel_difference": combined.getextrema()[1], "affected_pixels": affected,
            "affected_ratio": affected / (combined.width * combined.height)}


def inspect(cli, work, psd):
    """Read native layer details without editing the supplied PSD."""
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(psd, work / "inspection-source.psd")
    with PhotoCraft(cli, work) as photo:
        document, warnings = photo.open("inspection-source.psd")
    return {**document, "warnings": warnings}


def prepare(cli, work, spec, title):
    """Create the immutable candidate and before/after first-screen previews."""
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(spec["source_psd"], work / "source.psd")
    with PhotoCraft(cli, work) as photo:
        original, warnings = photo.open("source.psd")
        report = _document_report(original, spec)
        layer = _layer(original, spec["title_path"], "Type", spec.get("title_layer_id"))
        previous = layer["text"]["text"]
        if title == previous:
            raise RenderError("新标题与母版原文相同，请输入要替换的标题。")
        photo.call("doc.save", path="before-full.png")
        _apply_product(photo, work, original, spec)
        params = {"layer": layer["id"], "text": title, "name": layer["name"]}
        if "title_box" in spec:
            params["box"] = spec["title_box"]
        result = photo.command("type.edit", **params)
        lines = _check_text_metrics(result, title, spec)
        current = photo.call("doc.inspect")
        current_title = _layer(current, spec["title_path"], "Type", text=title)
        _check_layout(current_title, spec, _verify_product(current, spec))
        saved = photo.call("doc.save", path="candidate.psd")
        warnings.extend(saved.get("warnings", []))
        photo.call("doc.close", index=0)
        reopened, reopened_warnings = photo.open("candidate.psd")
        warnings.extend(reopened_warnings)
        final_report = _document_report(reopened, spec)
        for key in ("group_names", "text_layers", "width", "height"):
            if report[key] != final_report[key]:
                raise RenderError("保存后的 PSD 分组、尺寸或可编辑文字层发生了非预期变化。")
        candidate_title = _layer(reopened, spec["title_path"], "Type", text=title)
        product_bounds = _verify_product(reopened, spec)
        bounds = _check_layout(candidate_title, spec, product_bounds)
        if _check_text_metrics(photo.command("type.info", layer=candidate_title["id"]), title, spec) != lines:
            raise RenderError("保存后的 PSD 标题换行与预览不一致。")
        photo.call("doc.save", path="preview.png")

    height = report["first_screen_height"]
    before = work / "before-full.png"
    after = work / "preview.png"
    remainder = _difference(before, after, top=height)
    if remainder["max_channel_difference"] > 2 or remainder["affected_ratio"] > 0.005:
        raise RenderError("渲染器在首屏以外产生了非预期的像素变化。")
    _crop_preview(before, work / "before.png", height)
    _crop_preview(after, work / "after.png", height)
    before.unlink()
    return {**final_report, "original_title": previous, "title": title, "title_bounds": bounds,
            "title_lines": lines, "product_bounds": product_bounds,
            "outside_first_screen": remainder, "warnings": list(dict.fromkeys(warnings))}


def _visible_tree(layers, path=(), position=()):
    for index, layer in enumerate(layers):
        if not layer["visible"] or layer["opacity"] <= 0 or layer["fill"] <= 0:
            continue
        branch = (*path, layer["name"])
        location = (*position, index)
        yield location, branch, layer
        yield from _visible_tree(layer.get("children", []), branch, location)


def _intersection(first, second):
    bounds = [max(first[0], second[0]), max(first[1], second[1]),
              min(first[2], second[2]), min(first[3], second[3])]
    return bounds if bounds[0] < bounds[2] and bounds[1] < bounds[3] else None


def _screen_regions(document, spec):
    width, height = document["width"], document["height"]
    groups = [(index, layer) for index, layer in enumerate(document["layers"]) if layer["kind"] == "Group"]
    edges = spec.get("screen_edges")
    if edges is not None:
        if (len(edges) != len(groups) + 1 or edges[:2] != [0, spec["first_screen_height"]]
                or edges[-1] != height or any(top >= bottom for top, bottom in zip(edges, edges[1:]))):
            raise RenderError("登记的屏区间与母版不符。")
        return {index: [0, top, width, bottom]
                for (index, _), top, bottom in zip(groups, edges, edges[1:])}
    regions = {}
    for ordinal, (index, group) in enumerate(groups):
        if ordinal == 0:
            regions[index] = [0, 0, width, spec["first_screen_height"]]
            continue
        spans = {(layer["bounds"][1], layer["bounds"][1] + layer["bounds"][3])
                 for _, _, layer in _visible_tree(group["children"]) if layer.get("bounds")
                 and layer["kind"] in {"Smart Object", "Pixel"} and layer["bounds"][0] == 0
                 and layer["bounds"][2] == width and layer["bounds"][1] >= spec["first_screen_height"]
                 and layer["bounds"][1] + layer["bounds"][3] <= height}
        if len(spans) == 1:
            top, bottom = spans.pop()
            regions[index] = [0, top, width, bottom]
    return regions


def _draft_fields(document, spec):
    page = [0, 0, document["width"], document["height"]]
    screens = _screen_regions(document, spec)
    fields = []
    for position, path, layer in _visible_tree(document["layers"]):
        if layer["kind"] != "Type" or not layer.get("bounds") or not layer["text"]["text"].strip():
            continue
        bounds = _intersection(_rect(layer["bounds"]), page)
        if bounds is None:
            continue
        fields.append({"id": str(layer["id"]), "position": list(position), "path": list(path),
                       "screen": path[0] if len(path) > 1 else "整页",
                       "bounds": bounds, "text": layer["text"]["text"],
                       "screen_bounds": screens.get(position[0], page)})
    return fields


def _check_structure(initial, final):
    for key in ("width", "height", "group_names", "text_layers", "smart_objects"):
        if initial[key] != final[key]:
            raise RenderError("保存后的 PSD 分组、尺寸或可编辑图层发生了非预期变化。")


def prepare_draft(cli, work, spec):
    """Compose the registered bottle and index the visible native text on the fixed baseline."""
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(spec["source_psd"], work / "source.psd")
    with PhotoCraft(cli, work) as photo:
        original, warnings = photo.open("source.psd")
        initial = _document_report(original, spec)
        title = _layer(original, spec["title_path"], "Type", spec.get("title_layer_id"))
        same_path = [layer["id"] for layer in _matching_layers(original, spec["title_path"])
                     if layer["kind"] == "Type"]
        _apply_product(photo, work, original, spec)
        composed = _document_report(photo.call("doc.inspect"), spec)
        warnings.extend(photo.call("doc.save", path="baseline.psd").get("warnings", []))
        photo.call("doc.close", index=0)
        baseline, reopened_warnings = photo.open("baseline.psd")
        warnings.extend(reopened_warnings)
        report = _document_report(baseline, spec)
        _check_structure(composed, report)
        if any(initial[key] != report[key] for key in ("width", "height", "group_names", "text_layers")):
            raise RenderError("准备改前稿时母版尺寸、分组或文字层发生了非预期变化。")
        product_bounds = _verify_product(baseline, spec)
        fields = _draft_fields(baseline, spec)
        baseline_titles = [layer["id"] for layer in _matching_layers(baseline, spec["title_path"])
                           if layer["kind"] == "Type"]
        if len(baseline_titles) != len(same_path):
            raise RenderError("准备改前稿后首屏标题的图层位置发生了变化。")
        title_id = str(baseline_titles[same_path.index(title["id"])])
        for field in fields:
            field["is_title"] = field["id"] == title_id
        if not fields:
            raise RenderError("该母版没有可点选的可见文字层。")
        photo.call("doc.save", path="baseline.png")
    return {**report, "fields": fields, "product_bounds": product_bounds,
            "warnings": list(dict.fromkeys(warnings))}


def prepare_confirmed_draft(cli, work, spec, psd, png):
    """Index the confirmed PSD without recomposing its scene or product artwork."""
    work = Path(work).resolve()
    shutil.copyfile(psd, work / "baseline.psd")
    with PhotoCraft(cli, work) as photo:
        document, warnings = photo.open("baseline.psd")
        report = _document_report(document, spec)
        if report["height"] != spec["expected_height"]:
            raise RenderError("确认稿画布与交接清单不一致。")
        fields = _draft_fields(document, spec)
        layouts = {tuple(layout["path"]) for layout in spec["text_layouts"]}
        if layouts:
            fields = [field for field in fields if tuple(field["path"]) in layouts]
        if not fields:
            raise RenderError("确认稿没有可点选的真实文字层。")
        photo.call("doc.save", path="baseline.png")
    if _difference(png, work / "baseline.png")["affected_pixels"]:
        raise RenderError("确认 PSD 的回读画面与确认 PNG 不一致。")
    return {**report, "fields": fields, "product_bounds": None, "warnings": warnings}


def _field_layer(entries, field):
    path, layer = entries.get(tuple(field["position"]), (None, None))
    if list(path or ()) != field["path"] or layer["kind"] != "Type":
        raise RenderError("文字层的位置或类型与改前稿不一致。请重新加载商品。", field["id"])
    return layer


def _field_layout(field, spec, text=None):
    if field.get("is_title", field["path"] == spec.get("title_path")):
        return {"area": spec.get("title_area", field["screen_bounds"]),
                "box": spec.get("title_box")}
    registered = next((layout for layout in spec.get("text_layouts", [])
                       if layout["path"] == field["path"]), None)
    if registered is not None:
        if registered.get("explicit_lines") and text and "\n" in text:
            return {**registered, **registered["explicit_lines"]}
        return registered
    left, top, right, bottom = field["bounds"]
    margin = max(4, min(18, (bottom - top) // 3))
    region = next((region for region in spec.get("text_regions", {}).get(field["path"][0], [])
                   if _inside(field["bounds"], region["original"])), None)
    limits = region["horizontal"] if region else (left - margin, right + margin)
    area = _intersection([limits[0], top - margin, limits[1], bottom + margin], field["screen_bounds"])
    return {"area": area, "area_name": region["name"] if region else None}


def _draft_metrics(result, text, layout):
    lines = result.get("lines", [])
    if result.get("text") != text or not lines or lines[-1]["end"] != len(text):
        raise RenderError("文字区容纳不了完整文案，请缩短内容。")
    if layout.get("max_lines") is not None and len(lines) > layout["max_lines"]:
        raise RenderError("文字须保持单行，请缩短文案。")
    shape = result.get("shape")
    box = shape.get("Box") if isinstance(shape, dict) else None
    if layout.get("box") and box is None:
        raise RenderError("保存后段落文字框发生变化，请重新预览。")
    if box and any(line["baseline"] + line["descent"] > box["height"] + 0.5
                   or line["x0"] < -0.5 or line["x1"] > box["width"] + 0.5 for line in lines):
        raise RenderError("文字框会裁切内容，请缩短文案。")
    return [(line["start"], line["end"]) for line in lines]


def _point_wrap(photo, layer, text, layout):
    # Use the registered frame to find break positions on a disposable copy.
    # Apply only the breaks to the original point layer, keeping its anchor.
    probe = photo.command("layer.duplicate", layer=layer["id"])["layer"]
    try:
        measured = photo.command("type.edit", layer=probe, text=text, box=layout["box"])
        lines = _draft_metrics(measured, text, layout)
    finally:
        photo.command("layer.delete", layer=probe)
    for index in reversed([start for (_, end), (start, _) in zip(lines, lines[1:]) if start == end]):
        text = text[:index] + "\u2028" + text[index:]
    return text


def _check_point_anchor(metrics, original_transform):
    current = metrics.get("transform", [])
    if metrics.get("shape") != "Point" or len(current) != len(original_transform) or any(
            not math.isclose(before, after, rel_tol=0, abs_tol=1e-5)
            for before, after in zip(original_transform, current)):
        raise RenderError("保存后的点文字类型或锚点与母版不一致，请重新预览。")


def _align_right(photo, layer, anchor):
    layer_id = layer["id"]
    photo.command("type.setStyle", layer=layer_id, align="right")
    result = photo.command("type.info", layer=layer_id)
    if not result.get("bounds"):
        raise RenderError("该文字层没有可见内容。")
    # Point text aligns its advance. Keep the visible glyph edge on the design anchor.
    dx = anchor - result["bounds"][2]
    if dx:
        transform = result["transform"].copy()
        transform[4] += dx
        result = photo.command("type.edit", layer=layer_id, transform=transform, name=layer["name"])
    return result


def _field_guards(field, spec, entries):
    guards = list(_protected_areas(spec, field["path"], draft=True))
    registered_layers = {tuple(area["layer_path"]) for area in spec.get("protected_areas", [])
                         if isinstance(area, dict) and "layer_path" in area}
    protected_layers = {tuple(path) for path in spec.get("draft_guard_paths", [])}
    for position, (path, layer) in entries.items():
        if position == tuple(field["position"]) or not layer.get("bounds") or path in registered_layers:
            continue
        name = path[-1]
        if layer["kind"] == "Type" or path in protected_layers:
            guards.append((_rect(layer["bounds"]), name))
        elif layer["kind"] in {"Smart Object", "Pixel"} and any(
                label in name for label in ("瓶图", "原瓶", "标志", "品牌标识")):
            bounds = _rect(layer["bounds"])
            if not _inside(field["screen_bounds"], bounds):
                guards.append((bounds, name))
    return guards


def _check_field_bounds(layer, field, layout, guards, canvas=None):
    if not layer.get("bounds"):
        raise RenderError("该文字层没有可见内容。")
    bounds = _rect(layer["bounds"])
    if canvas is not None and not _inside(bounds, canvas):
        raise RenderError("文字超出画布，会裁切内容，请缩短文案。")
    for region, name in guards:
        overlap = _intersection(bounds, region)
        if overlap is None:
            continue
        original_overlap = _intersection(field["bounds"], region)
        if original_overlap is None or not _inside(overlap, original_overlap):
            raise RenderError(f"文字会遮挡「{name}」，请缩短文案。")
    if layout["area"] is None or not _inside(bounds, layout["area"]):
        if name := layout.get("area_name"):
            raise RenderError(f"文字超出「{name}」，请缩短文案。")
        raise RenderError("文字超出可用排版区域，请缩短文案。")
    return bounds


def _draft_difference(before, after, changes):
    with Image.open(before) as first, Image.open(after) as second:
        if first.size != second.size:
            raise RenderError("对照 PNG 的画布尺寸不一致。")
        delta = ImageChops.difference(first.convert("RGBA"), second.convert("RGBA"))
    red, green, blue, alpha = delta.split()
    delta = ImageChops.lighter(ImageChops.lighter(red, green), ImageChops.lighter(blue, alpha))
    regions = []
    for change in changes:
        for bounds in (change["original_bounds"], change["bounds"]):
            left, top, right, bottom = bounds
            regions.append((max(0, left - 2), max(0, top - 2),
                            min(delta.width, right + 2), min(delta.height, bottom + 2)))
    for region in regions:
        delta.paste(0, region)
    affected = delta.width * delta.height - delta.histogram()[0]
    ratio = affected / (delta.width * delta.height)
    maximum = delta.getextrema()[1]
    if maximum > 2 or ratio > 0.005:
        raise RenderError("文字区域外出现非预期像素变化，当前预览无法确认。")
    return {"max_channel_difference": maximum, "affected_pixels": affected, "affected_ratio": ratio}


def render_draft(cli, work, spec, baseline_dir, edits, fields):
    """Replay changed native layers from the fixed baseline and verify the exact export pair."""
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(baseline_dir) / "baseline.psd", work / "baseline.psd")
    by_id = {field["id"]: field for field in fields}
    if unknown := edits.keys() - by_id.keys():
        raise RenderError("该文字层未登记在当前母版中，请重新选择。", min(unknown))
    edits = {key: value for key, value in edits.items() if value != by_id[key]["text"]}
    changes = []
    expected_lines = {}
    point_anchors = {}
    rendered_text = edits.copy()
    with PhotoCraft(cli, work) as photo:
        document, warnings = photo.open("baseline.psd")
        report = _document_report(document, spec)
        entries = {position: (path, layer) for position, path, layer in _visible_tree(document["layers"])}
        layouts = {key: _field_layout(by_id[key], spec, text) for key, text in edits.items()}
        canvas = [0, 0, report["width"], report["height"]] if "text_regions" in spec else None
        for key, text in edits.items():
            field = by_id[key]
            layout = layouts[key]
            try:
                layer = _field_layer(entries, field)
                if layer["text"]["text"] != field["text"]:
                    raise RenderError("文字层原文与改前稿不一致。请重新加载商品。")
                params = {"layer": layer["id"], "text": text, "name": layer["name"]}
                if layout.get("box"):
                    original = photo.command("type.info", layer=layer["id"])
                    if original["shape"] == "Point":
                        point_anchors[key] = original["transform"]
                    else:
                        params["box"] = layout["box"]
                result = photo.command("type.edit", **params)
                if key in point_anchors:
                    if result.get("bounds") and not _inside(result["bounds"], layout["area"]):
                        wrapped = _point_wrap(photo, layer, text, layout)
                        if wrapped != text:
                            rendered_text[key] = wrapped
                            result = photo.command("type.edit", **{**params, "text": wrapped})
                    layout = layouts[key] = {**layout, "box": None}
                if "right_anchor" in layout:
                    result = _align_right(photo, layer, layout["right_anchor"])
                expected_lines[key] = _draft_metrics(result, rendered_text[key], layout)
            except RenderTimeout:
                raise
            except RenderError as error:
                raise RenderError(f"「{field['path'][-1]}」{error}", key) from error
        warnings.extend(photo.call("doc.save", path="candidate.psd").get("warnings", []))
        photo.call("doc.close", index=0)
        right_aligned = [by_id[key]["path"] for key in edits if "right_anchor" in layouts[key]]
        if right_aligned:
            try:
                sync_right_alignment(work / "candidate.psd", right_aligned)
            except PSDTextAlignmentError as error:
                raise RenderError("无法保存 Photoshop 兼容的右对齐文字，请联系维护人员。") from error
        reopened, reopened_warnings = photo.open("candidate.psd")
        warnings.extend(reopened_warnings)
        final_report = _document_report(reopened, spec)
        _check_structure(report, final_report)
        product_bounds = _verify_product(reopened, spec)
        entries = {position: (path, layer) for position, path, layer in _visible_tree(reopened["layers"])}
        updated = []
        for field in fields:
            layer = _field_layer(entries, field)
            key = field["id"]
            if layer["text"]["text"] != rendered_text.get(key, field["text"]):
                raise RenderError("保存后的 PSD 文字与本次编辑不一致。", key)
            if key not in edits:
                updated.append(field)
                continue
            text, layout = edits[key], layouts[key]
            try:
                metrics = photo.command("type.info", layer=layer["id"])
                if key in point_anchors:
                    _check_point_anchor(metrics, point_anchors[key])
                if _draft_metrics(metrics, rendered_text[key], layout) != expected_lines[key]:
                    raise RenderError("保存后的换行与排版结果不一致。")
                bounds = _check_field_bounds(layer, field, layout, _field_guards(field, spec, entries), canvas)
                if "right_anchor" in layout and (
                        abs(bounds[2] - layout["right_anchor"]) > 1
                        or not all(run["style"]["align"] == "Right" for run in metrics["paragraphs"])):
                    raise RenderError("保存后的文字未保留右对齐，请重新预览。")
            except RenderTimeout:
                raise
            except RenderError as error:
                raise RenderError(f"「{field['path'][-1]}」{error}", key) from error
            updated.append({**field, "text": text, "bounds": bounds})
            changes.append({"id": key, "path": field["path"], "screen": field["screen"],
                            "before": field["text"], "after": text, "bounds": bounds,
                            "original_bounds": field["bounds"], "lines": len(expected_lines[key])})
            if rendered_text[key] != text:
                changes[-1]["rendered_text"] = rendered_text[key]
        photo.call("doc.save", path="preview.png")
    outside = _draft_difference(Path(baseline_dir) / "baseline.png", work / "preview.png", changes)
    return {**final_report, "fields": updated, "changes": changes, "product_bounds": product_bounds,
            "outside_text": outside, "warnings": list(dict.fromkeys(warnings))}


def publish(work, destination, checks):
    """Copy the exact pair already rendered and reopened during preparation."""
    shutil.copyfile(work / "candidate.psd", destination / "detail.psd")
    shutil.copyfile(work / "preview.png", destination / "detail.png")
    return {**checks, "preview_matches": True}
