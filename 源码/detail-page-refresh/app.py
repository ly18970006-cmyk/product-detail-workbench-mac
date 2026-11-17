"""Independent, CPU-based detail-page draft and PSD service."""

import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from PIL import Image

import backgrounds
import render
from refresh_storage import data_directory


ROOT = Path(__file__).resolve().parent
CODE = re.compile(r"\d{10,16}\Z", re.ASCII)
JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
LAYOUT_OFFSETS = {"text_x", "text_y", "product_x", "product_y"}
STORE_LAYOUT_PATH = ROOT / "templates" / "store-oolong-v1.json"


class InputError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def asset(asset_root, relative):
    path = (asset_root / relative).resolve()
    if not path.is_relative_to(asset_root) or not path.is_file():
        raise InputError("asset_missing", "登记素材缺失，请核对素材目录。", 422)
    return path


def product(code):
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise InputError("invalid_code", "请输入有效的商品编码。")
    path = ROOT / "data" / f"{code}.json"
    if not path.is_file():
        raise InputError("product_not_found", "该编码尚无独立翻新计划。", 404)
    return json.loads(path.read_bytes())


def store_layouts(plan):
    catalog = json.loads(STORE_LAYOUT_PATH.read_bytes())
    if plan["width"] != catalog["width"]:
        return {}
    screens = {screen["id"]: screen for screen in plan["screens"]}
    return {
        identifier: (catalog["version"], layout)
        for identifier, layout in catalog["layouts"].items()
        if (screen := screens.get(layout["screen_id"]))
        and screen["kind"] == "visual" and screen["height"] == layout["height"]
    }


def selected_screens(body, plan):
    ids = body.get("screens", [screen["id"] for screen in plan["screens"]])
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids):
        raise InputError("invalid_screens", "至少选择一屏。")
    requested = set(ids)
    screens = [screen for screen in plan["screens"] if screen["id"] in requested]
    if len(screens) != len(ids):
        raise InputError("invalid_screens", "屏幕编号重复或未登记。")
    return screens


def screen_options(body, screens, templates):
    options = body.get("options", {})
    ids = {screen["id"] for screen in screens}
    if not isinstance(options, dict) or not options.keys() <= ids:
        raise InputError("invalid_options", "调整项必须属于本次所选的屏。")
    for screen in screens:
        opt = options.get(screen["id"], {})
        if not isinstance(opt, dict) or not opt.keys() <= {"variant", "offsets", "text", "template"}:
            raise InputError("invalid_options", "仅支持版式、背景候选、位置和文字调整。")
        if "template" in opt:
            template_id = opt["template"]
            if (not isinstance(template_id, str) or template_id not in templates
                    or templates[template_id][1]["screen_id"] != screen["id"]):
                raise InputError("invalid_template", "所选店铺版式不适用于此屏。")
        if opt.get("variant", "warm") not in ("warm", "soft") or (screen["kind"] != "visual" and "variant" in opt):
            raise InputError("invalid_background", "视觉屏可选择暖色或柔光背景。")
        offsets = opt.get("offsets", {})
        if (not isinstance(offsets, dict) or not offsets.keys() <= LAYOUT_OFFSETS
                or (screen["kind"] != "visual" and offsets)
                or (not screen.get("use_bottle", True) and offsets.keys() & {"product_x", "product_y"})
                or any(type(value) is not int or abs(value) > 36 for value in offsets.values())):
            raise InputError("invalid_offset", "位置微调须为 -36 到 36 像素的整数。")
        edits = opt.get("text", {})
        keys = {item["key"] for item in screen["items"]}
        if (not isinstance(edits, dict) or not edits.keys() <= keys or any(
                not isinstance(value, str) or not value.strip() or len(value) > 160
                or any(ord(c) < 32 and c != "\n" for c in value) for value in edits.values())):
            raise InputError("invalid_text", "文字必须对应已有字段，长度为 1–160 字。")
    return options


def snapshot(plan, screens, options, folder, asset_root, base, templates):
    sources = {key: asset(asset_root, plan["source"][key]) for key in ("old_page", "bottle", "operations")}
    files = {}
    bottle_needed = any(screen["kind"] == "visual" and screen.get("use_bottle", True) for screen in screens)
    if bottle_needed:
        bottle = folder / "bottle.png"
        with Image.open(sources["bottle"]) as image:
            image.convert("RGBA").save(bottle)
        files[bottle.name] = file_digest(bottle)
    if any(screen["id"] == "hero" for screen in screens) and plan["source"].get("logo"):
        approved_logo = any(item["field"] == "official_logo" and item["status"] == "approved"
                            for item in plan["approvals"])
        if approved_logo:
            logo = folder / "logo.png"
            sources["logo"] = asset(asset_root, plan["source"]["logo"])
            with Image.open(sources["logo"]) as image:
                image.convert("RGBA").save(logo)
            files[logo.name] = file_digest(logo)

    old_screens = {} if base is None else {s["id"]: s for s in base[1]["layout"]["screens"]}
    candidate = {"code": plan["code"], "name": plan["name"], "width": plan["width"], "screens": []}
    for template in screens:
        screen_id = template["id"]
        opt = options.get(screen_id, {})
        screen = dict(template, offsets=opt.get("offsets", {}), background=None, bottle=None, logo=None,
                      background_run=None)
        if "template" in opt:
            template_id = opt["template"]
            version, configuration = templates[template_id]
            screen.update(template_id=template_id, template_version=version, template=configuration)
        edits = opt.get("text", {})
        screen["items"] = [
            dict(item, text=edits[item["key"]], status="pending", source="本次内部草图编辑")
            if item["key"] in edits and edits[item["key"]] != item["text"] else item
            for item in template["items"]
        ]
        if screen["kind"] == "visual":
            variant = opt.get("variant", "warm")
            previous = old_screens.get(screen_id, {})
            previous_run = previous.get("background_run", {})
            filename = f"scene-{screen_id}.png"
            if (base and previous.get("height") == screen["height"] and base[1]["layout"]["width"] == plan["width"]
                    and previous_run.get("variant") == variant and previous.get("background")):
                old_path = base[0] / previous["background"]
                if not old_path.is_file() or file_digest(old_path) != base[1]["files"].get(old_path.name):
                    raise InputError("snapshot_changed", "原草图背景校验失败，请重新生成候选。", 409)
                shutil.copyfile(old_path, folder / filename)
                run = dict(previous_run, reused_from=base[0].name, billing_status="not_billed")
            else:
                run = backgrounds.make(screen_id, plan["width"], screen["height"], variant, folder / filename)
            files[filename] = file_digest(folder / filename)
            screen.update(background=filename, background_run=run)
            if screen.get("use_bottle", True):
                screen["bottle"] = "bottle.png"
            if screen_id == "hero" and "logo.png" in files:
                screen["logo"] = "logo.png"
        candidate["screens"].append(screen)
    return {
        "layout": candidate, "files": files, "approvals": plan["approvals"],
        "plan_sha256": digest(dump(plan)),
        "sources": {key: {"path": plan["source"][key], "sha256": file_digest(path)} for key, path in sources.items()},
        "use": "internal_draft",
    }


def renderer_layout(folder, manifest):
    layout = manifest["layout"]
    return dict(layout, screens=[
        dict(screen, **{key: str(folder / screen[key]) if screen[key] else None
                        for key in ("background", "bottle", "logo")})
        for screen in layout["screens"]
    ])


def create_app():
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.update(MAX_CONTENT_LENGTH=32 * 1024)
    jobs = data_directory(os.environ.get("REFRESH_DATA_ROOT"), ROOT / ".data", "独立旧版") / "jobs"
    assets = Path(os.environ.get("REFRESH_ASSET_ROOT", "E:/工作/规划落地对话专用素材")).resolve()
    export_locks = [threading.Lock() for _ in range(32)]
    renderer_slot = threading.BoundedSemaphore(1)

    @app.errorhandler(InputError)
    def input_error(error):
        return jsonify(code=error.code, message=error.message), error.status

    @app.errorhandler(413)
    def request_too_large(error):
        return jsonify(code="request_too_large", message="请求超过大小限制。"), 413

    def read_body():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise InputError("invalid_request", "需要 JSON 对象。")
        return body

    def read_job(identifier):
        if not isinstance(identifier, str) or not JOB_ID.fullmatch(identifier):
            raise InputError("job_not_found", "找不到该草图。", 404)
        folder = jobs / identifier
        if not (folder / "manifest.json").is_file():
            raise InputError("job_not_found", "找不到该草图。", 404)
        raw = (folder / "manifest.json").read_bytes()
        return folder, json.loads(raw), digest(raw)

    def render_work(action, *args):
        if not renderer_slot.acquire(blocking=False):
            raise InputError("renderer_busy", "排版服务正在处理另一任务，请稍后重试。", 429)
        try:
            return action(*args)
        except (ValueError, getattr(render, "RenderError", ValueError)) as error:
            raise InputError("layout_failed", str(error), 422) from error
        except Exception as error:
            app.logger.exception("Renderer failed")
            raise InputError("renderer_failed", "排版失败，请查看服务日志和渲染器配置。", 503) from error
        finally:
            renderer_slot.release()

    @app.after_request
    def prevent_asset_caching(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "background_provider": "local_placeholder", "use": "internal_draft"}

    @app.get("/api/products/<code>")
    def get_product(code):
        plan = product(code)
        templates = store_layouts(plan)
        return dict(plan, layout_templates=[
            {"id": identifier, "version": version, "label": layout["label"], "screen_id": layout["screen_id"]}
            for identifier, (version, layout) in templates.items()
        ])

    @app.get("/api/products/<code>/old-page")
    def get_old_page(code):
        return send_file(asset(assets, product(code)["source"]["old_page"]))

    @app.post("/api/previews")
    def make_preview():
        body = read_body()
        if body.get("background_mode", "local") == "company":
            raise InputError("gateway_unconfigured", "公司图像网关尚未接入。请使用本地背景完成内部排版验证。", 503)
        if body.get("background_mode", "local") != "local":
            raise InputError("invalid_background", "未知背景来源。")
        plan = product(body.get("code"))
        screens = selected_screens(body, plan)
        templates = store_layouts(plan)
        options = screen_options(body, screens, templates)
        base = read_job(body["base_job_id"]) if "base_job_id" in body else None
        if base and base[1]["layout"]["code"] != plan["code"]:
            raise InputError("different_product", "只能复用同一编码的背景。", 409)

        identifier = uuid.uuid4().hex
        stage = jobs / f".{identifier}.draft"
        stage.mkdir(parents=True)
        try:
            manifest = snapshot(plan, screens, options, stage, assets, base, templates)
            manifest["preview_checks"] = render_work(render.preview, renderer_layout(stage, manifest), stage / "preview.png")
            manifest["files"]["preview.png"] = file_digest(stage / "preview.png")
            raw = dump(manifest)
            (stage / "manifest.json").write_bytes(raw)
            stage.rename(jobs / identifier)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return jsonify(
            job_id=identifier, revision=digest(raw), use=manifest["use"],
            preview=f"/api/jobs/{identifier}/files/preview.png",
            manifest=f"/api/jobs/{identifier}/files/manifest.json",
            background_runs=[dict(s["background_run"], screen=s["id"])
                             for s in manifest["layout"]["screens"] if s["background_run"]],
        ), 201

    @app.post("/api/jobs/<identifier>/confirm")
    def confirm(identifier):
        body = read_body()
        folder, manifest, revision = read_job(identifier)
        submitted = body.get("revision")
        if (not isinstance(submitted, str) or not re.fullmatch(r"[0-9a-f]{64}", submitted)
                or not hmac.compare_digest(submitted, revision)):
            raise InputError("revision_mismatch", "请重新查看并确认当前草图版本。", 409)
        reviewer = body.get("reviewer")
        if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 60:
            raise InputError("reviewer_required", "请填写内部确认人。")
        destination = folder / "export"
        with export_locks[int(identifier[:2], 16) % len(export_locks)]:
            if not destination.exists():
                for filename, expected in manifest["files"].items():
                    path = folder / filename
                    if not path.is_file() or file_digest(path) != expected:
                        raise InputError("snapshot_changed", "草图或冻结素材已变化，请重新生成草图。", 409)
                stage = folder / f".export-{uuid.uuid4().hex}"
                stage.mkdir()
                try:
                    checks = render_work(render.export, renderer_layout(folder, manifest), stage / "detail.psd", stage / "detail.png")
                    comparison = render.compare(folder / "preview.png", stage / "detail.png", checks["screens"])
                    match = all(screen["changed_pixels"] == 0 for screen in comparison)
                    if not match:
                        raise InputError("preview_export_mismatch", "成稿与确认草图存在渲染差异，需排查后重新出稿。", 422)
                    checks["preview_export"] = comparison
                    (stage / "checks.json").write_bytes(dump(checks))
                    (stage / "confirmation.json").write_bytes(dump({
                        "job_id": identifier, "revision": revision, "reviewer": reviewer.strip(),
                        "confirmed_at": datetime.now(timezone.utc).isoformat(), "use": "internal_draft",
                        "approval": "layout_only", "preview_matches_export": match,
                        "files": {name: file_digest(stage / name) for name in ("detail.psd", "detail.png", "checks.json")},
                    }))
                    stage.rename(destination)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
        return {"job_id": identifier, "revision": revision, "use": "internal_draft",
                "files": {name: f"/api/jobs/{identifier}/files/{name}"
                          for name in ("detail.psd", "detail.png", "checks.json", "confirmation.json")}}

    @app.get("/api/jobs/<identifier>/files/<name>")
    def job_file(identifier, name):
        folder, _, _ = read_job(identifier)
        if name in {"detail.psd", "detail.png", "checks.json", "confirmation.json"}:
            folder /= "export"
        elif name not in {"preview.png", "manifest.json"}:
            raise InputError("file_not_found", "找不到该文件。", 404)
        path = folder / name
        if not path.is_file():
            raise InputError("file_not_found", "确认导出后才能获取成稿。", 404)
        return send_file(path, as_attachment=name == "detail.psd")

    return app


if __name__ == "__main__":
    from waitress import serve

    serve(create_app(), host="127.0.0.1", port=int(os.environ.get("REFRESH_PORT", "8772")), threads=4)
