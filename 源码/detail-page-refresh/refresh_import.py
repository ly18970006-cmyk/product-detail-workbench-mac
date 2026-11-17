"""Discover versioned source assets and parse historical operations workbooks."""

import hashlib
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image


CODE = re.compile(r"[0-9]{10,16}\Z")
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
MODULE_BOUNDS = re.compile(r"(\d{1,6})\s*[–—－~～至-]\s*(\d{1,6})\s*(?:px|像素)", re.I)
INFO_MODULE = re.compile(r"(?:产品|商品|营养|生产|规格|包装)\s*(?:信息|详情|参数|标签|说明)|营养成分")
FOOTNOTE = re.compile(r"^(?:[*＊※]|[·•]?\s*(?:图(?:片|案)|数据来[自源]|以实物为准))")
CELL = re.compile(r"([A-Z]+)([1-9][0-9]*)\Z")
MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
OFFICE_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
APPROVAL_FIELDS = ("packaging", "channel", "brand_rule", "approved_copy", "official_logo")


def _sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _candidate(path, root, sku, kind):
    resolved = path.resolve()
    if not resolved.is_relative_to(sku) or not resolved.is_file():
        raise ValueError("商品素材路径不在登记目录内")
    relative = path.relative_to(root).as_posix()
    sha = _sha256(resolved)
    identifier = hashlib.sha256(f"{kind}\0{relative}\0{sha}".encode()).hexdigest()
    return {"id": identifier, "path": relative, "name": path.name, "sha256": sha,
            "bytes": resolved.stat().st_size, "status": "pending"}


def checked_asset(asset_root, candidate):
    """Resolve a previously discovered record and reject traversal and changed content."""
    root = Path(asset_root).resolve()
    relative = Path(candidate["path"])
    if relative.is_absolute() or len(relative.parts) < 2 or not CODE.fullmatch(relative.parts[0]):
        raise ValueError("素材不是商品目录内的相对路径")
    sku = (root / relative.parts[0]).resolve()
    path = (root / relative).resolve()
    if not sku.is_relative_to(root) or not path.is_relative_to(sku) or not path.is_file():
        raise ValueError("商品素材缺失或超出目录")
    if _sha256(path) != candidate["sha256"]:
        raise ValueError("商品素材版本已变化")
    return path


def _image_info(path, *, alpha=False):
    with Image.open(path) as image:
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("仅支持单帧图像")
        info = {"width": image.width, "height": image.height, "format": image.format}
        if alpha:
            channel = image.convert("RGBA").getchannel("A") if "A" in image.getbands() or "transparency" in image.info else None
            bounds = channel.getbbox() if channel else None
            info.update(transparent=bool(bounds and channel.getextrema()[0] < 255),
                        alpha_bounds=list(bounds) if bounds else None)
        return info


def _paths(directory, sku, extensions):
    if not directory.is_dir() or not directory.resolve().is_relative_to(sku):
        return []
    return sorted((path for path in directory.iterdir()
                   if path.suffix.lower() in extensions and not path.name.startswith(("~$", "."))
                   and path.is_file() and path.resolve().is_relative_to(sku)), key=lambda path: path.name.casefold())


def _xml(archive, name):
    if archive.getinfo(name).file_size > 16 * 1024 * 1024:
        raise ValueError("运营表单个 XML 超过读取限制")
    return ET.fromstring(archive.read(name))


def _string(element):
    if element is None:
        return ""
    return "".join(node.text or "" for node in element.iter(f"{MAIN}t"))


def _workbook(path):
    """Read cell strings and sheet labels without executing formulas or external references."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = [_string(element) for element in _xml(archive, "xl/sharedStrings.xml").findall(f"{MAIN}si")]
        rels = {rel.attrib["Id"]: rel.attrib["Target"]
                for rel in _xml(archive, "xl/_rels/workbook.xml.rels").findall(f"{PACKAGE_REL}Relationship")
                if rel.get("TargetMode") != "External" and rel.get("Type", "").endswith("/worksheet")}
        for sheet in _xml(archive, "xl/workbook.xml").findall(f"{MAIN}sheets/{MAIN}sheet"):
            target = rels.get(sheet.get(f"{OFFICE_REL}id"))
            if not target or sheet.get("state", "visible") != "visible":
                continue
            member = posixpath.normpath(target.lstrip("/") if target.startswith("/") else f"xl/{target}")
            if not member.startswith("xl/worksheets/") or member not in names:
                continue
            rows = []
            for row in _xml(archive, member).findall(f"{MAIN}sheetData/{MAIN}row"):
                values = {}
                for cell in row.findall(f"{MAIN}c"):
                    address = cell.get("r", "")
                    column = CELL.fullmatch(address)
                    if not column or cell.find(f"{MAIN}f") is not None:
                        continue
                    text = cell.findtext(f"{MAIN}v", "")
                    if cell.get("t") == "s":
                        text = shared[int(text)] if text else ""
                    elif cell.get("t") == "inlineStr":
                        text = _string(cell.find(f"{MAIN}is"))
                    elif cell.get("t") == "e":
                        continue
                    text = "".join(c for c in text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
                                   if ord(c) >= 32 or c == "\n").strip()
                    if text:
                        values[column[1]] = (address, text)
                if values:
                    rows.append(values)
            yield sheet.get("name", "工作表"), rows


def _historical_item(cell, role, workbook, sheet, *, label=None, suffix=""):
    if cell is None:
        return None
    address, text = cell
    result = {"key": f"{address.lower()}{suffix}", "role": role, "text": text, "status": "pending",
              "source": f"{workbook} · {sheet}!{address} · 历史参考"}
    if label:
        result["label"] = label
    return result


def _sections(path):
    for sheet, rows in _workbook(path):
        heading = next((index for index, row in enumerate(rows)
                        if row.get("A", (None, ""))[1] == "顺序" and "模块" in row.get("B", (None, ""))[1]), None)
        if heading is None:
            continue
        header = {cell[0]: cell[1] for row in rows[:heading] for cell in row.values()}
        reference = re.search(r"来源《([^》]+)》", header.get("A3", ""))
        title = header.get("A2", "").split("｜", 1)[0].strip()
        sections = []
        section = None
        for row in rows[heading + 1:]:
            if "A" in row and "B" in row:
                name = row["B"][1]
                kind = "info" if INFO_MODULE.search(name) else "visual"
                section = {"id": f"s{len(sections) + 1:02d}", "name": name, "kind": kind, "items": []}
                bounds = MODULE_BOUNDS.search(row.get("E", (None, ""))[1])
                section["historical_range"] = [int(bounds[1]), int(bounds[2])] if bounds else None
                sections.append(section)
                if kind == "visual":
                    item = _historical_item(row.get("C"), "title", path.name, sheet)
                    if item:
                        section["items"].append(item)
                elif "D" not in row and "C" in row:
                    section["items"].append(_historical_item(row["C"], "title", path.name, sheet))
            if section is None or "D" not in row:
                continue
            if section["kind"] == "info":
                section["items"].append(_historical_item(row["D"], "field", path.name, sheet,
                                                         label=row.get("C", (None, "说明"))[1]))
            else:
                cell, text = row["D"]
                for index, line in enumerate(text.splitlines(), 1):
                    if line.strip():
                        role = "footnote" if FOOTNOTE.match(line.strip()) else "body"
                        section["items"].append(_historical_item((cell, line.strip()), role, path.name, sheet,
                                                                 suffix=f".{index:02d}"))
        return {"name": title, "source_filename": reference[1] if reference else None,
                "sheet": sheet, "sections": sections}
    raise ValueError("运营表缺少可识别的模块表头")


def _ranges(sections, height, historical_matches):
    """Use coherent workbook boundaries as anchors, estimating only unmarked spans."""
    count = len(sections)
    anchors = {0: 0, count: height}
    if historical_matches:
        for index, section in enumerate(sections):
            if section["historical_range"] is None:
                continue
            start, stop = section["historical_range"]
            if not 0 <= start < stop <= height:
                continue
            if any(edge in anchors and anchors[edge] != coord for edge, coord in ((index, start), (index + 1, stop))):
                continue
            anchors.update({index: start, index + 1: stop})
    ordered = sorted(anchors.items())
    if any(stop - start < right - left for (left, start), (right, stop) in zip(ordered, ordered[1:])):
        ordered = [(0, 0), (count, height)]
    boundaries = dict(ordered)
    for (left, start), (right, stop) in zip(ordered, ordered[1:]):
        for index in range(left + 1, right):
            boundaries[index] = start + round((stop - start) * (index - left) / (right - left))
    for index, section in enumerate(sections):
        span = [boundaries[index], boundaries[index + 1]]
        yield {**section, "source_range": span,
               "range_status": "historical_reference" if historical_matches and section["historical_range"] == span else "estimated_pending"}


def build_review(found, operations=None, old_page=None):
    """Build a reviewable plan for selected source candidates. No claim is approved here."""
    selected_page = old_page or (found["old_pages"][0] if found["old_pages"] else None)
    parsed = found["_operations_plans"].get(operations["id"]) if operations else None
    warnings = ["历史文案只供核对，含活动字样、识别错字与未经复核数值。"]
    if selected_page is None:
        return {"source": "no_old_page", "screens": [], "warnings": ["缺实际旧详情页。"]}
    if not parsed or not parsed["sections"]:
        return {"source": "old_page_unsegmented", "screens": [{"id": "s01", "name": "旧页待拆分", "kind": "visual",
                "source_range": [0, selected_page["height"]], "range_status": "estimated_pending", "items": []}],
                "warnings": ["未登记可解析的运营模块表。需要审阅旧图并确认分屏与文案。"]}
    same_page = parsed["source_filename"] == selected_page["name"]
    screens = list(_ranges(parsed["sections"], selected_page["height"], same_page))
    if not same_page:
        warnings.append("运营表未标注匹配的旧图名称。分屏坐标仅供初审。")
    if any(screen["range_status"] == "estimated_pending" for screen in screens):
        warnings.append("部分分屏边界按运营顺序估算，需看旧图复核。")
    return {"source": "operations_workbook_reference", "screens": screens, "warnings": warnings}


def discover(asset_root, code):
    """Enumerate real old pages and assets in a server-owned, code-keyed catalog."""
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise ValueError("无效商品编码")
    root = Path(asset_root).resolve()
    directory = root / code
    sku = directory.resolve()
    if not sku.is_dir() or sku != directory:
        raise FileNotFoundError(code)
    errors = []
    pages, bottles, logos, operations, parsed = [], [], [], [], {}
    for kind, paths, target in (
        ("old_page", _paths(sku, sku, IMAGE_TYPES) + _paths(sku / "详情页", sku, IMAGE_TYPES), pages),
        ("bottle", _paths(sku / "产品照片", sku, IMAGE_TYPES), bottles),
        ("logo", _paths(sku / "品牌标志", sku, IMAGE_TYPES), logos),
    ):
        for path in paths:
            try:
                info = _image_info(path, alpha=kind != "old_page")
                if kind == "old_page" and info["height"] < info["width"] * 1.6:
                    continue
                target.append({**_candidate(path, root, sku, kind), **info})
            except (OSError, ValueError, Image.DecompressionBombError):
                errors.append({"kind": kind, "name": path.name, "reason": "unreadable_asset"})
    for path in _paths(sku, sku, {".xlsx"}):
        candidate = _candidate(path, root, sku, "operations")
        try:
            info = _sections(path)
        except (OSError, zipfile.BadZipFile, ValueError, KeyError, IndexError, ET.ParseError):
            errors.append({"kind": "operations", "name": path.name, "reason": "unreadable_operations"})
            operations.append({**candidate, "readable": False})
        else:
            parsed[candidate["id"]] = info
            operations.append({**candidate, "readable": True, "sheet": info["sheet"], "screen_count": len(info["sections"]),
                               "source_filename": info["source_filename"]})
    referenced = {info["source_filename"] for info in parsed.values()}
    pages.sort(key=lambda page: (page["name"] not in referenced, "详情" not in page["name"], page["name"]))
    bottles.sort(key=lambda bottle: (not bottle["transparent"], bottle["name"]))
    missing = [key for key, present in (("old_page", pages), ("bottle", any(b["transparent"] for b in bottles)),
                                       ("operations", parsed), ("logo", logos)) if not present]
    ambiguity = [key for key, values in (("old_page", pages), ("bottle", [b for b in bottles if b["transparent"]]),
                                          ("operations", operations), ("logo", logos)) if len(values) > 1]
    default_ops = next((entry for entry in operations if entry["readable"]), None)
    name = parsed.get(default_ops["id"], {}).get("name") if default_ops else None
    found = {"code": code, "name": name or f"商品 {code}", "old_pages": pages, "bottles": bottles, "operations": operations,
             "logos": logos, "missing": missing, "ambiguous": ambiguity, "asset_errors": errors,
             "approvals": {field: "pending" for field in APPROVAL_FIELDS}, "_operations_plans": parsed}
    found["screens"] = build_review(found, default_ops)["screens"]
    return found


def scan(asset_root):
    """Scan the configured catalog without selecting a packaging or approval version."""
    root = Path(asset_root).resolve()
    return [discover(root, path.name) for path in sorted(root.iterdir())
            if path.is_dir() and CODE.fullmatch(path.name) and path.resolve() == path]
