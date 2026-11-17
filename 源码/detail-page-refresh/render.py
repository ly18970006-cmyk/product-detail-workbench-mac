"""Compose PNG drafts and export editable PSDs with the same CPU layout engine."""

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Timer

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parent
DEFAULT_FONT = "Noto Sans SC"
INK = "#382218"
ACCENT = "#794327"
MUTED = "#654d3f"
PAPER = "#faf6ed"
WHITE = "#fffcf6"
DRAFT_LABEL = "内部技术样稿  /  未审核 · 不可发布"
ROLE_NAMES = {"kicker": "引导文字", "title": "标题", "subtitle": "副标题",
              "body": "正文", "footnote": "脚注"}


class RenderError(RuntimeError):
    pass


@dataclass
class Layer:
    name: str
    area: tuple
    text: str | None = None
    size: int = 0
    leading: int = 0
    color: str = INK
    image: Image.Image | None = None


def _renderer_binary():
    local = ROOT.parent / "photocraft-trial" / "app" / "photocraft-cli.exe"
    configured = os.environ.get("REFRESH_PHOTOCRAFT")
    binary = configured or (str(local) if local.is_file() else shutil.which("photocraft-cli"))
    if not binary:
        raise RenderError("未找到 PhotoCraft CLI。请配置 REFRESH_PHOTOCRAFT。")
    return binary


class PhotoCraft:
    def __init__(self, work):
        self.work = work
        self.sequence = 0

    def __enter__(self):
        self.log = (self.work / "photocraft.log").open("w", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                [_renderer_binary(), "serve", "--no-gpu", "--automation-read-root", str(self.work),
                 "--automation-write-root", str(self.work)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, RenderError) as error:
            self.log.close()
            raise RenderError("PhotoCraft CLI 无法启动，请检查 REFRESH_PHOTOCRAFT。") from error
        self.timer = Timer(240, self._stop)
        self.timer.daemon = True
        self.timer.start()
        return self

    def _stop(self):
        if self.process.poll() is None:
            self.process.kill()

    def __exit__(self, *_exception):
        self.timer.cancel()
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
            if not line:
                raise RenderError(f"PhotoCraft CLI 在 {method} 时超时或退出。")
            response = json.loads(line)
        except (OSError, ValueError) as error:
            raise RenderError(f"PhotoCraft CLI 在 {method} 时通信失败。") from error
        if response.get("id") != self.sequence or not response.get("ok"):
            raise RenderError(f"PhotoCraft {method} 失败：{response.get('error', '响应不匹配')}")
        return response["result"]

    def command(self, command, **params):
        return self.call("engine.execute", command=command, params=params)


def _font_path():
    custom = os.environ.get("REFRESH_FONT")
    if custom:
        return custom
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "NotoSansSC-VF.ttf"
    if windows.is_file():
        return str(windows)
    return "NotoSansSC-Regular.otf"


@lru_cache(maxsize=24)
def _measure_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError as error:
        raise RenderError("找不到排版字体。请配置 REFRESH_FONT 为已安装的字体文件。") from error


def _wrapped(text, width, size):
    font = _measure_font(_font_path(), size)
    lines = []
    # PhotoCraft may use a different fallback for Latin glyphs and full-width punctuation.
    limit = width - max(12, size // 2)
    for paragraph in text.strip().splitlines():
        line = ""
        for char in paragraph:
            if line and font.getlength(line + char) > limit:
                space = line.rfind(" ")
                if char.isascii() and char.isalnum() and space >= max(0, len(line) - 12):
                    lines.append(line[:space].rstrip())
                    line = line[space + 1:]
                else:
                    lines.append(line.rstrip())
                    line = ""
            if line or char != " ":
                line += char
        lines.append(line.rstrip())
    return "\n".join(lines)


def _type(name, text, x, y, width, size, color=INK):
    if not text.strip():
        raise RenderError(f"「{name}」没有可排文字。")
    wrapped = _wrapped(text, width, size)
    leading = round(size * 1.48)
    height = leading * (wrapped.count("\n") + 1) + 3
    return Layer(name, (x, y, x + width, y + height), wrapped, size, leading, color)


def _solid(name, x, y, width, height, color):
    return Layer(name, (x, y, x + width, y + height), image=Image.new("RGB", (width, height), color))


def _image(name, path, box, *, background=False, bottle=False):
    x, y, right, bottom = box
    size = (right - x, bottom - y)
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGBA")
    except (OSError, ValueError) as error:
        raise RenderError(f"无法读取「{name}」素材。") from error
    if background:
        result = ImageOps.fit(image, size, Image.Resampling.LANCZOS)
        base = Image.new("RGBA", size, PAPER)
        base.alpha_composite(result)
        return Layer(name, box, image=base.convert("RGB"))
    alpha = image.getchannel("A")
    if not alpha.getbbox() or (bottle and alpha.getextrema()[0] == 255):
        raise RenderError(f"「{name}」需要可见图案，瓶图需要透明背景。")
    image = image.crop(alpha.getbbox())
    contained = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
    result = Image.new("RGBA", size)
    result.alpha_composite(contained, ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2))
    return Layer(name, box, image=result)


def _overlap(first, second):
    return max(first[0], second[0]) < min(first[2], second[2]) and max(first[1], second[1]) < min(first[3], second[3])


def _inside(box, container):
    left, top, right, bottom = box
    return container[0] <= left < right <= container[2] and container[1] <= top < bottom <= container[3]


def _visual(screen, width):
    height = screen["height"]
    offsets = screen.get("offsets", {})
    background = (_image("背景｜场景", screen["background"], (0, 0, width, height), background=True)
                  if screen.get("background") else _solid("背景｜场景", 0, 0, width, height, PAPER))
    header_height = 112
    if screen.get("logo"):
        logo = _image("品牌｜官方标志", screen["logo"], (42, 36, 398, 169))
        left, top, right, bottom = logo.image.getchannel("A").getbbox()
        logo_width = right - left
        minimum_width = 300 if screen.get("logo_variant") == "tagline_lockup" else 150
        clearance = (left + 42, top + 36, width - 340 - right - 42)
        if logo_width <= minimum_width or min(clearance) < logo_width / 10:
            raise RenderError(f"官方标志需保留 10% 净空，当前组合宽度须大于 {minimum_width} px。")
        header_height = max(112, 37 + bottom + round(logo_width / 10 + .5))
        brand = logo
    else:
        brand = _type("品牌｜标志占位", "品牌标志待核准", 44, 38, 264, 23, MUTED)
    layers = [background, _solid("背景｜品牌白带", 0, 0, width, header_height, WHITE),
              _solid("品牌区分隔线", 0, header_height - 1, width, 1, "#d7c5ad"), brand]
    layers.append(_type("状态｜内部样稿", DRAFT_LABEL, width - 340, 43, 296, 18, ACCENT))

    product_box = None
    if screen.get("bottle"):
        dx, dy = offsets.get("product_x", 0), offsets.get("product_y", 0)
        product_box = (418 + dx, round(height * .27) + dy, width - 38 + dx, round(height * .90) + dy)
        if not _inside(product_box, (408, header_height + 15, width - 10, height - 24)):
            raise RenderError(f"「{screen['name']}」的瓶图超出右侧产品区。")
        layers.append(_image("产品｜独立瓶图", screen["bottle"], product_box, bottle=True))

    main_styles = {"kicker": (26, ACCENT, 27), "title": (67 if product_box else 62, INK, 21),
                   "subtitle": (34, ACCENT, 19), "body": (26, MUTED, 17)}
    main = [item for item in screen["items"] if item["role"] != "footnote"]
    footnotes = [item for item in screen["items"] if item["role"] == "footnote"]
    x = 44 + offsets.get("text_x", 0)
    area_right = 399 if product_box else width - 45
    available = min(385 if product_box else 580, area_right - x)
    if x < 28 or available < 260:
        raise RenderError(f"「{screen['name']}」的文字位移超出排版区。")
    y = max(header_height + 50, round(height * .23)) + offsets.get("text_y", 0)
    if y < header_height + 23:
        raise RenderError(f"「{screen['name']}」的文案遮挡品牌区。")
    accent_x = min(44, x)
    layers.append(_solid("装饰｜茶色短线", accent_x, y - 16, 48, 3, "#a6693a"))
    for item in main:
        role = item["role"]
        if role not in main_styles:
            raise RenderError(f"视觉屏不支持 {role} 文字。")
        size, color, gap = main_styles[role]
        layer = _type(f"{ROLE_NAMES[role]}｜{item['key']}", item["text"], x, y, available, size, color)
        if product_box and _overlap(layer.area, product_box):
            raise RenderError(f"「{item['key']}」与产品保护区重叠。")
        layers.append(layer)
        y = layer.area[3] + gap
    last_text_bottom = y
    footnote_bottom = height - 36
    for item in reversed(footnotes):
        probe = _type(f"脚注｜{item['key']}", item["text"], x, 0, available, 20, MUTED)
        footnote_y = footnote_bottom - probe.area[3]
        if footnote_y < last_text_bottom:
            raise RenderError(f"「{screen['name']}」文案与脚注重叠，请缩短或调整屏高。")
        layers.append(_type(probe.name, item["text"], x, footnote_y, available, 20, MUTED))
        footnote_bottom = footnote_y - 10
    if last_text_bottom > height - 34:
        raise RenderError(f"「{screen['name']}」内容超过屏高。")
    return layers


def _shop_graphic(name, area, paint, *, scale=2):
    width, height = area[2] - area[0], area[3] - area[1]
    image = Image.new("RGBA", (width * scale, height * scale))
    paint(ImageDraw.Draw(image), image.width, image.height, scale)
    return Layer(name, tuple(area), image=image.resize((width, height), Image.Resampling.LANCZOS))


def _shop_light(area, color, opacity):
    width, height = area[2] - area[0], area[3] - area[1]
    mask = Image.new("L", (width, height))
    inset = max(2, min(width, height) // 7)
    ImageDraw.Draw(mask).ellipse((inset, inset, width - inset, height - inset), fill=opacity)
    mask = mask.filter(ImageFilter.GaussianBlur(inset / 1.6))
    light = Image.new("RGBA", (width, height), color)
    light.putalpha(mask)
    return Layer("装饰｜光晕", tuple(area), image=light)


def _shop_ground(area, green=False):
    def paint(draw, width, height, scale):
        ink = (55, 73, 30) if green else (105, 48, 16)
        for step in range(38):
            pad = step * width / 195
            y = step * height / 121
            draw.ellipse((pad, y, width - pad, height - y), fill=(*ink, 9 + step))
        for step in range(4):
            pad = step * width / 35 + 4 * scale
            y = step * height / 10
            draw.arc((pad, y, width - pad, height - y), 15, 165,
                     fill=(*ink, 26 - step * 4), width=scale)

    return _shop_graphic("装饰｜产品承托", area, paint)


def _shop_botanical(name, area, green, flip=False):
    def paint(draw, width, height, scale):
        branch = (77, 108, 49, 92) if green else (129, 66, 26, 111)
        fills = ([(101, 128, 54, 72), (119, 144, 66, 58), (142, 160, 84, 54)] if green else
                 [(119, 65, 27, 78), (160, 91, 39, 75), (182, 117, 51, 70)])

        def point(x, y):
            return ((1 - x if flip else x) * width, y * height)

        spine = [(0.06, .91), (.21, .76), (.33, .62), (.43, .46), (.49, .27), (.55, .10)]
        draw.line([point(*pos) for pos in spine], fill=branch, width=3 * scale, joint="curve")
        leaves = [(.21, .76, .08, .38), (.27, .71, .67, .66), (.35, .57, .19, .19),
                  (.43, .46, .84, .32), (.47, .34, .37, .08), (.52, .19, .69, .04)]
        for index, (x, y, tx, ty) in enumerate(leaves):
            dx, dy = tx - x, ty - y
            vx, vy = -dy * .42, dx * .42
            points = []
            for sign in (1, -1):
                steps = range(17) if sign == 1 else range(16, -1, -1)
                for step in steps:
                    t = step / 16
                    curve = t * (1 - t) * 3.1 * sign
                    points.append(point(x + dx * t + vx * curve, y + dy * t + vy * curve))
            draw.polygon(points, fill=fills[index % len(fills)])
            draw.line([point(x, y), point(tx, ty)], fill=branch, width=max(1, scale))
            for step in (.32, .50, .68):
                xx, yy = x + dx * step, y + dy * step
                draw.line([point(xx, yy), point(xx + dx * .10 + vx * .55, yy + dy * .10 + vy * .55)],
                          fill=branch, width=max(1, scale // 2))

    return _shop_graphic(name, area, paint)


def _shop_contours(area):
    def paint(draw, width, height, scale):
        for index in range(3):
            x = width * (.06 + .04 * index)
            y = height * (.07 + .025 * index)
            draw.arc((x, y, width - x, height - y), 196, 338,
                     fill=(255, 252, 216, 131 - index * 24), width=(2 if index == 0 else 1) * scale)
        for index in range(3):
            x = width * (.02 + .07 * index)
            y = height * (.11 + .036 * index)
            draw.arc((x, y, width - x, height - y), 9, 151,
                     fill=(172, 99, 41, 54 - index * 10), width=scale)

    return _shop_graphic("装饰｜茶汤弧线", area, paint)


def _shop_green(area):
    width, height = area[2] - area[0], area[3] - area[1]
    tint = Image.new("RGBA", (36, 64))
    pixels = []
    for y in range(tint.height):
        for x in range(tint.width):
            horizontal = x / (tint.width - 1)
            vertical = y / (tint.height - 1)
            strength = (horizontal * .32 + vertical * .67) ** 1.3
            pixels.append((round(247 - 66 * strength), round(250 - 44 * strength),
                           round(223 - 74 * strength), round(189 + strength * 43)))
    tint.putdata(pixels)
    return Layer("装饰｜浅绿调色", tuple(area), image=tint.resize((width, height), Image.Resampling.BICUBIC))


def _shop_card(index, area):
    def paint(draw, width, height, scale):
        pad = 2 * scale
        draw.rounded_rectangle((pad, 5 * scale, width - pad, height - pad), 24 * scale,
                               fill=(72, 100, 38, 24))
        draw.rounded_rectangle((pad, pad, width - pad, height - 6 * scale), 23 * scale,
                               fill=(253, 253, 239, 236), outline=(144, 166, 102, 169), width=scale)

    return _shop_graphic(f"卖点组｜底板 {index}", area, paint)


def _shop_icon(icon, area):
    def paint(draw, width, height, scale):
        ink = (78, 108, 49, 255)
        draw.ellipse((2 * scale, 2 * scale, width - 2 * scale, height - 2 * scale),
                     fill=(255, 255, 245, 255), outline=(142, 161, 104, 255), width=2 * scale)

        def line(*points):
            draw.line([(x * width / 70, y * height / 70) for x, y in points],
                      fill=ink, width=2 * scale, joint="curve")

        def ellipse(bounds, fill=None):
            draw.ellipse(tuple(v * (width if i % 2 == 0 else height) / 70 for i, v in enumerate(bounds)),
                         outline=ink, fill=fill, width=2 * scale)

        if icon == "formula":
            line((21, 28), (33, 19), (47, 25), (48, 42), (36, 51), (22, 44), (21, 28),
                 (35, 34), (47, 25))
            line((35, 34), (36, 51))
            line((16, 52), (53, 16))
        elif icon == "taste":
            line((21, 35), (23, 46), (27, 51), (43, 51), (47, 46), (49, 35), (21, 35))
            line((19, 55), (51, 55))
            line((29, 30), (26, 26), (28, 21), (31, 17))
            line((39, 30), (36, 26), (38, 21), (41, 17))
        elif icon == "leaves":
            line((23, 49), (32, 39), (43, 28), (51, 22))
            line((28, 43), (27, 33), (32, 25), (42, 22), (51, 22), (47, 34), (41, 42), (33, 45), (28, 43))
            line((23, 48), (18, 42), (19, 34), (26, 39), (28, 43))
        elif icon == "polyphenol":
            line((23, 30), (35, 23), (47, 31), (47, 44), (35, 51), (23, 44), (23, 30))
            for x, y in ((23, 30), (35, 23), (47, 31), (47, 44), (35, 51), (23, 44)):
                ellipse((x - 3, y - 3, x + 3, y + 3), (255, 255, 245, 255))
        else:
            raise RenderError(f"模板卖点图标未登记：{icon}。")

    return _shop_graphic(f"图标｜{icon}", area, paint)


def _shop_brand(screen, spec):
    left, top, right, bottom = spec["brand_plate"]
    plate = _solid("背景｜品牌底板", left, top, right - left, bottom - top, WHITE)
    area = tuple(spec["brand"])
    if not screen.get("logo"):
        return [plate, _type("品牌｜标志占位", "品牌标志待核准", area[0], area[1], area[2] - area[0], 23, MUTED)]
    inset = 32 if screen.get("logo_variant") == "tagline_lockup" else 17
    logo_area = (left + inset, top + inset, right - inset, bottom - inset)
    logo = _image("品牌｜官方标志", screen["logo"], logo_area)
    x, y, xr, yb = logo.image.getchannel("A").getbbox()
    logo_width = xr - x
    minimum = 300 if screen.get("logo_variant") == "tagline_lockup" else 150
    clearance = min(x + inset, y + inset, logo.image.width - xr + inset, logo.image.height - yb + inset)
    if logo_width <= minimum or clearance < logo_width / 10:
        raise RenderError(f"官方标志需要 10% 净空及大于 {minimum} px 的屏显宽度。")
    return [plate, logo]


def _shop_text(item, slot, text_offset, width, height):
    size = slot["size"]
    left, top, right, bottom = slot["area"]
    if item["role"] != "footnote":
        left, top, right, bottom = (left + text_offset[0], top + text_offset[1],
                                    right + text_offset[0], bottom + text_offset[1])
    area = left, top, right, bottom
    if not _inside(area, (0, 0, width, height)):
        raise RenderError(f"「{item['key']}」超出店铺模板文字区。")
    text = _wrapped(item["text"], right - left, size)
    lines = text.splitlines()
    if not lines or len(lines) > slot["lines"]:
        raise RenderError(f"「{item['key']}」最多可排 {slot['lines']} 行，请恢复安全位置或缩短草图文字。")
    if slot["align"] == "center":
        font = _measure_font(_font_path(), size)
        measured = max(font.getlength(line) for line in lines)
        left = max(left, round((left + right - measured) / 2) - 4)
    return Layer(f"{ROLE_NAMES[item['role']]}｜{item['key']}", (left, top, right, bottom), text,
                 size, slot.get("leading", round(size * 1.48)), slot["color"])


def _shop_visual(screen, width):
    template_id = screen["template_id"]
    spec = screen.get("template", {})
    height = screen["height"]
    if spec.get("screen_id") != screen["id"] or spec.get("height") != height or width != 790:
        raise RenderError("店铺模板与所选模块、宽度或屏高不匹配。")
    if not screen.get("bottle") or not screen.get("background"):
        raise RenderError("店铺模板需要独立背景和透明瓶图。")
    slots = spec["slots"]
    items = screen["items"]
    if [slot["role"] for slot in slots] != [item["role"] for item in items]:
        raise RenderError("店铺模板的文字角色与数量不匹配，请按模块文案选择版式。")

    offsets = screen.get("offsets", {})
    dx, dy = offsets.get("product_x", 0), offsets.get("product_y", 0)
    left, top, right, bottom = spec["product"]
    product_box = (left + dx, top + dy, right + dx, bottom + dy)
    if not _inside(product_box, spec["product_safe"]):
        raise RenderError(f"「{screen['name']}」瓶图超出店铺模板的产品保护区。")
    bottle = _image("产品｜独立瓶图", screen["bottle"], product_box, bottle=True)
    ax, ay, ar, ab = bottle.image.getchannel("A").getbbox()
    visible_bottle = (product_box[0] + ax, product_box[1] + ay, product_box[0] + ar, product_box[1] + ab)
    text_offset = offsets.get("text_x", 0), offsets.get("text_y", 0)
    text = [_shop_text(item, slot, text_offset, width, height) for item, slot in zip(items, slots)]
    if any(_overlap(layer.area, visible_bottle) for layer in text):
        raise RenderError(f"「{screen['name']}」文字与瓶图重叠。")

    art = spec["art"]
    background = _image("背景｜场景", screen["background"], (0, 0, width, height), background=True)
    left, top, right, bottom = spec["status"]
    status = Layer("状态｜内部样稿", (left, top, right, bottom), DRAFT_LABEL, 18, 27, ACCENT)
    if any(_overlap(layer.area, status.area) for layer in text):
        raise RenderError(f"「{screen['name']}」文案压到内部状态标记。")
    layers = [background]
    if template_id == "shop_hero":
        brand = _shop_brand(screen, spec)
        if any(_overlap(layer.area, brand[0].area) for layer in text):
            raise RenderError("首屏文案压到品牌保护区。")
        layers.extend([_shop_light(art["glow"], "#fff4c2", 186), _shop_contours(art["contours"]),
                       _shop_ground(art["ground"]), _shop_botanical("装饰｜左侧茶叶", art["left_leaves"], False),
                       _shop_botanical("装饰｜右侧茶叶", art["right_leaves"], False, True), *brand])
        left, top, right, bottom = art["subtitle_rule"]
        layers.append(_solid("装饰｜副标题短线", left, top, right - left, bottom - top, "#ac7539"))
    elif template_id == "shop_benefits":
        layers.extend([_shop_green((0, 0, width, height)), _shop_light(art["glow"], "#ffffdf", 160),
                       _shop_botanical("装饰｜右上茶叶", art["leaves"], True, True), _shop_ground(art["ground"], True)])
        left, top, right, bottom = art["title_rule"]
        layers.append(_solid("装饰｜标题短线", left, top, right - left, bottom - top, "#76924c"))
        body = [(slot["icon"], layer) for slot, layer in zip(slots, text) if slot["role"] == "body"]
        if len(body) != len(art["cards"]) or len(body) != len(art["icons"]):
            raise RenderError("卖点模板需要一项文字对应一个图标和底板。")
        for index, ((icon, label), card_area, icon_area) in enumerate(zip(body, art["cards"], art["icons"]), 1):
            if not _inside(label.area, card_area) or _overlap(label.area, icon_area):
                raise RenderError(f"「{label.name}」文字离开卖点组或压到图标。")
            layers.extend([_shop_card(index, card_area), _shop_icon(icon, icon_area)])
    else:
        raise RenderError(f"店铺模板未登记：{template_id}。")
    return [*layers, bottle, status, *text]


def _information(screen, width):
    height = screen["height"]
    layers = [_solid("背景｜信息底色", 0, 0, width, height, PAPER),
              _type("状态｜内部样稿", DRAFT_LABEL, width - 340, 35, 296, 18, ACCENT),
              _solid("装饰｜茶色短线", 48, 87, 48, 3, "#a6693a")]
    y = 111
    footnotes = []
    fields = []
    in_nutrition = False
    for item in screen["items"]:
        key, role, text = item["key"], item["role"], item["text"]
        if role == "footnote":
            footnotes.append(item)
            continue
        if role != "field":
            size = 57 if role == "title" else 25
            layer = _type(f"{ROLE_NAMES[role]}｜{key}", text, 48, y, width - 96, size,
                          INK if role == "title" else MUTED)
            layers.append(layer)
            y = layer.area[3] + 10
            continue
        label = item["label"]
        is_nutrition_header = key == "nutrition_basis"
        if is_nutrition_header:
            in_nutrition = True
        columns = text.split(" / ") if in_nutrition else []
        if not is_nutrition_header and len(columns) != 2:
            in_nutrition = False
        if is_nutrition_header:
            if len(columns) != 3:
                raise RenderError("营养成分表表头需要项目、计量基础和 NRV 三列。")
            label, value, nrv = columns
        elif in_nutrition:
            value, nrv = columns
        else:
            value, nrv = text, None

        value_x = 227
        value_right = 526 if nrv is not None else width - 62
        label_layer = _type(f"字段名｜{key}", label, 61, y + 15, value_x - 74, 23, ACCENT)
        value_layer = _type(f"字段值｜{key}", value, value_x, y + 15, value_right - value_x, 23)
        row = [label_layer, value_layer]
        if nrv is not None:
            row.append(_type(f"营养NRV｜{key}", nrv, 548, y + 15, width - 610, 23))
        bottom = max(layer.area[3] for layer in row) + 16
        fields.append((key, y, bottom, is_nutrition_header, nrv is not None))
        layers.extend(row)
        y = bottom

    if fields:
        table_top, table_bottom = fields[0][1], fields[-1][2]
        table = Image.new("RGB", (width - 96, table_bottom - table_top), WHITE)
        grid = ImageDraw.Draw(table)
        for index, (_, top, bottom, is_header, third_column) in enumerate(fields):
            relative_top, relative_bottom = top - table_top, bottom - table_top
            fill = "#ead8be" if is_header else ("#f7eee1" if index % 2 else WHITE)
            grid.rectangle((0, relative_top, table.width - 1, relative_bottom - 1), fill=fill)
            grid.line((0, relative_bottom - 1, table.width - 1, relative_bottom - 1), fill="#ded0bd")
            grid.line((165, relative_top, 165, relative_bottom - 1), fill="#ded0bd")
            if third_column:
                grid.line((484, relative_top, 484, relative_bottom - 1), fill="#ded0bd")
        layers.insert(1, Layer("表格｜底色与分隔线", (48, table_top, width - 48, table_bottom), image=table))

    y += 26
    for item in footnotes:
        layer = _type(f"脚注｜{item['key']}", item["text"], 48, y, width - 96, 20, MUTED)
        layers.append(layer)
        y = layer.area[3] + 9
    if y > height - 30:
        raise RenderError(f"「{screen['name']}」表格或脚注超过屏高。")
    return layers


def _layout_screens(layout):
    width = layout["width"]
    screens = []
    top = 0
    for ordinal, screen in enumerate(layout["screens"], 1):
        height = screen["height"]
        group = f"{ordinal:02d}｜{screen['name']}"
        if screen.get("template_id"):
            layers = _shop_visual(screen, width)
        else:
            layers = (_visual(screen, width) if screen["kind"] == "visual" else _information(screen, width))
        if len({layer.name for layer in layers}) != len(layers):
            raise RenderError(f"「{group}」中存在重复文字或素材图层名称。")
        for layer in layers:
            if not _inside(layer.area, (0, 0, width, height)):
                raise RenderError(f"「{group} / {layer.name}」超出单屏范围。")
        screens.append((screen["id"], group, top, height, layers))
        top += height
    return screens, top


def _absolute(area, top):
    return area[0], area[1] + top, area[2], area[3] + top


def _add_layer(photo, layer, top, filename):
    x, y, right, bottom = _absolute(layer.area, top)
    if layer.image is not None:
        layer.image.save(photo.work / filename, compress_level=2)
        placed = photo.call("doc.place", path=filename, scale=100, fit=False, smartObject=True,
                            center=[(x + right) / 2, (y + bottom) / 2])["layer"]
        photo.command("layer.renameLayer", layer=placed, name=layer.name)
        return placed
    padding_top = 10 if layer.size >= 50 else 4
    created = photo.command("type.create", name=layer.name, text=layer.text, x=x + 4, y=y + padding_top,
                            box=[x + 4, y + padding_top, right - x - 4, bottom - y - padding_top],
                            font=os.environ.get("REFRESH_FONT_FAMILY", DEFAULT_FONT),
                            size=layer.size, leading=layer.leading, color=layer.color)["layer"]
    metrics = photo.command("type.info", layer=created)
    lines = metrics.get("lines", [])
    if (not lines or len(lines) != layer.text.count("\n") + 1 or lines[-1]["end"] != len(layer.text)
            or any(line["x1"] > right - x - 3.5
                   or line["baseline"] + line["descent"] > bottom - y - padding_top + .5
                   for line in lines)):
        raise RenderError(f"「{layer.name}」的字体或文案超出排版区。")
    return created


def _verify(document, screens, width, height):
    if (document["width"], document["height"]) != (width, height):
        raise RenderError("PSD 画布尺寸与本次排版不一致。")
    groups = [layer for layer in document["layers"] if layer["kind"] == "Group"]
    if len(groups) != len(document["layers"]) or [group["name"] for group in groups] != [screen[1] for screen in screens]:
        raise RenderError("PSD 屏分组、数量或顺序与本次排版不一致。")
    output = []
    for (screen_id, group, top, screen_height, layers), native in zip(screens, groups):
        by_name = {layer["name"]: layer for layer in native["children"]}
        if len(native["children"]) != len(layers):
            raise RenderError(f"「{group}」图层数量与本次排版不一致。")
        checked = []
        for expected in layers:
            layer = by_name.get(expected.name, {})
            kind = "Smart Object" if expected.image is not None else "Type"
            if layer.get("kind") != kind or not layer.get("visible") or not layer.get("bounds"):
                raise RenderError(f"「{group} / {expected.name}」未保留可见独立图层。")
            x, y, layer_width, layer_height = layer["bounds"]
            bounds = [x, y, x + layer_width, y + layer_height]
            area = list(_absolute(expected.area, top))
            if not _inside(bounds, area):
                raise RenderError(f"「{group} / {expected.name}」像素超出锁定区域：{bounds} / {area}。")
            info = {"name": expected.name, "kind": kind, "bounds": bounds, "area": area}
            if kind == "Type":
                if layer.get("text", {}).get("text") != expected.text:
                    raise RenderError(f"「{group} / {expected.name}」的文字未正确保留。")
                info.update(text=expected.text, size=expected.size, font=layer["text"].get("font"))
            else:
                info["asset_size"] = list(expected.image.size)
            if expected.name == "产品｜独立瓶图":
                if layer.get("smartContents", {}).get("instances") != 1:
                    raise RenderError(f"「{group}」的瓶图没有独立的智能对象内容。")
                info["independent_smart_object"] = True
            checked.append(info)
        output.append({"id": screen_id, "group": group, "top": top, "height": screen_height, "layers": checked})
    return {"width": width, "height": height, "screens": output,
            "text_layers": sum(layer["kind"] == "Type" for screen in output for layer in screen["layers"]),
            "smart_objects": sum(layer["kind"] == "Smart Object" for screen in output for layer in screen["layers"])}


def _render(layout, png_path, psd_path=None):
    screens, height = _layout_screens(layout)
    width = layout["width"]
    with tempfile.TemporaryDirectory(prefix="detail-refresh-render-", dir=Path(png_path).parent) as temporary:
        work = Path(temporary)
        with PhotoCraft(work) as photo:
            photo.call("doc.new", name=f"{layout['code']} 详情页翻新", width=width, height=height)
            starter = photo.call("doc.inspect")["layers"][0]
            for ordinal, (_, group, top, _, layers) in reversed(list(enumerate(screens, 1))):
                selected = [_add_layer(photo, layer, top, f"screen-{ordinal:02d}-layer-{index:02d}.png")
                            for index, layer in enumerate(layers, 1)]
                for index, layer_id in enumerate(selected):
                    photo.command("layer.select", layer=layer_id, mode="add" if index else "replace")
                photo.command("layer.groupLayers", name=group)
            photo.command("layer.delete", layer=starter["id"])
            warnings = []
            if psd_path is not None:
                warnings.extend(photo.call("doc.save", path="detail.psd").get("warnings", []))
                photo.call("doc.close", index=0)
                warnings.extend(photo.call("doc.open", path="detail.psd", approveHighMemory=True).get("warnings", []))
            report = _verify(photo.call("doc.inspect"), screens, width, height)
            png_warnings = photo.call("doc.save", path="detail.png").get("warnings", [])
        if psd_path is not None:
            shutil.copyfile(work / "detail.psd", psd_path)
        shutil.copyfile(work / "detail.png", png_path)
    return {**report, "warnings": list(dict.fromkeys(warnings)), "png_warnings": png_warnings,
            "reopened_psd": psd_path is not None}


def preview(layout, path):
    """Render the draft straight to PNG. No PSD is written in the preview path."""
    return _render(layout, path)


def export(layout, psd_path, png_path):
    """Write and reopen the layered PSD, then produce the PNG from that PSD."""
    return _render(layout, png_path, psd_path)


def compare(preview_path, export_path, screens):
    """Compare finalized and approved PNG pixels by screen."""
    with Image.open(preview_path) as draft, Image.open(export_path) as final:
        if draft.size != final.size:
            raise RenderError("导出图与确认稿画布尺寸不一致。")
        delta = ImageChops.difference(draft.convert("RGB"), final.convert("RGB"))
    channel_difference = delta.getchannel("R")
    for channel in ("G", "B"):
        channel_difference = ImageChops.lighter(channel_difference, delta.getchannel(channel))
    reports = []
    for screen in screens:
        top = screen["top"]
        part = channel_difference.crop((0, top, delta.width, top + screen["height"]))
        histogram = part.histogram()
        total = part.width * part.height
        meaningful = sum(histogram[4:])
        reports.append({"id": screen["id"], "changed_pixels": total - histogram[0],
                        "meaningful_pixels": meaningful, "meaningful_ratio": meaningful / total,
                        "max_channel_delta": part.getextrema()[1]})
    return reports
