"""Validate trusted refresh delivery metadata before indexing its native PSD layers."""

import re
from pathlib import Path


SHA256 = re.compile(r"[a-f0-9]{64}\Z")
REQUIRED_SOURCE = {"template", "layout", "copy", "bottle"}


def draft_spec(manifest, code, job_id, revision, text_layouts):
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("code") != code
            or manifest.get("job_id") != job_id or manifest.get("revision") != revision
            or manifest.get("approval_status") not in {"confirmed", "layout_only", "internal_layout_confirmed"}):
        raise ValueError("翻新交接清单与已确认版本不一致。")

    width, height = manifest.get("width"), manifest.get("height")
    screens = manifest.get("screens")
    if (type(width) is not int or type(height) is not int or min(width, height) <= 0
            or not isinstance(screens, list) or not screens):
        raise ValueError("翻新交接清单缺少画布或屏幕尺寸。")
    edges = [0]
    ids = set()
    for screen in screens:
        if (not isinstance(screen, dict) or not isinstance(screen.get("id"), str)
                or screen["id"] in ids or not isinstance(screen.get("name"), str)
                or screen.get("y") != edges[-1] or type(screen.get("height")) is not int
                or screen["height"] <= 0):
            raise ValueError("翻新交接清单的屏幕区间不连续。")
        ids.add(screen["id"])
        edges.append(edges[-1] + screen["height"])
    if edges[-1] != height:
        raise ValueError("翻新交接清单与整页高度不一致。")

    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        raise ValueError("翻新交接清单缺少确认文件。")
    for kind in ("psd", "png"):
        item = assets.get(kind)
        if not isinstance(item, dict):
            raise ValueError("翻新交接清单缺少确认文件。")
        filename, digest = item.get("file"), item.get("sha256")
        if (not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", filename)
                or Path(filename).suffix.lower() != f".{kind}"
                or not isinstance(digest, str) or not SHA256.fullmatch(digest)):
            raise ValueError("翻新交接清单的成稿引用无效。")

    if not isinstance(text_layouts, list):
        raise ValueError("翻新交接清单的文字区无效。")
    for layout in text_layouts:
        if not isinstance(layout, dict):
            raise ValueError("翻新交接清单的文字区无效。")
        path, area = layout.get("path"), layout.get("area")
        if (not isinstance(path, list) or len(path) < 2 or any(not isinstance(part, str) for part in path)
                or not isinstance(area, list) or len(area) != 4 or any(type(value) is not int for value in area)
                or not 0 <= area[0] < area[2] <= width or not 0 <= area[1] < area[3] <= height):
            raise ValueError("翻新交接清单的文字区无效。")
    source = manifest.get("source")
    if not isinstance(source, dict) or not REQUIRED_SOURCE <= source.keys():
        raise ValueError("翻新交接清单缺少素材来源。")
    return {"expected_width": width, "expected_height": height, "expected_groups": len(screens),
            "first_screen_height": edges[1], "screen_edges": edges, "text_layouts": text_layouts}
