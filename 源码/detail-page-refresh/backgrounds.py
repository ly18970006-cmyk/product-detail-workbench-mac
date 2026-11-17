"""Build clearly labeled local test scenery without an external model call."""

from PIL import Image, ImageDraw, ImageFilter


PALETTES = {
    "hero": ((253, 248, 234), (226, 174, 94), (151, 78, 42)),
    "selling": ((254, 241, 226), (214, 154, 114), (155, 77, 55)),
    "leaves": ((248, 246, 224), (192, 187, 114), (101, 109, 55)),
    "process": ((251, 243, 230), (198, 168, 132), (119, 82, 65)),
    "tea": ((254, 246, 229), (238, 185, 100), (181, 108, 60)),
    "occasions": ((252, 244, 231), (204, 176, 135), (135, 103, 74)),
}


def make(screen_id, width, height, variant, destination):
    ivory, amber, brown = PALETTES.get(screen_id, PALETTES["hero"])
    miniature = Image.new("RGB", (79, max(32, round(height / 10))))
    small_width, small_height = miniature.size
    pixels = []
    for y in range(small_height):
        v = y / (small_height - 1)
        for x in range(small_width):
            u = x / (small_width - 1)
            light = ((u - (0.7 if variant == "warm" else 0.86)) ** 2 + (v - 0.30) ** 2) * 1.5
            color = tuple(a + (b - a) * min(1, light + v * 0.22) for a, b in zip(amber, brown))
            strength = u ** (2.9 if variant == "warm" else 4.2) * (0.57 + v * 0.27)
            pixels.append(tuple(round(a + (b - a) * strength) for a, b in zip(ivory, color)))
    miniature.putdata(pixels)
    image = miniature.resize((width, height), Image.Resampling.BICUBIC).convert("RGBA")
    light = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(light)
    for index in range(6):
        spread = index * width * 0.042
        draw.arc((width * 0.50 - spread, height * 0.76 - spread * 0.28,
                  width * 1.19 + spread, height * 1.08 + spread * 0.3),
                 start=194, end=335, fill=(255, 233, 183, max(12, 64 - index * 8)),
                 width=max(2, width // 220))
    image = Image.alpha_composite(image, light.filter(ImageFilter.GaussianBlur(width / 155)))
    image.convert("RGB").save(destination, compress_level=3)
    return {
        "provider": "local_placeholder", "version": "light-study-v1", "variant": variant,
        "task_id": None, "count": 1, "width": width, "height": height,
        "billing_status": "not_billed", "estimated_cost": None, "actual_cost": None,
    }
