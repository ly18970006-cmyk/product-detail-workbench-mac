"""Validate and freeze refresh jobs, then render and publish their confirmed artifacts."""

import io
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

import backgrounds
import refresh_import
import refresh_layout
import render
from refresh_store import canonical, file_hash, revision, stamp


PREFIX = "/api/refresh"
LOGGER = logging.getLogger("refresh.pipeline")
TASK_ID = re.compile(r"[0-9a-f]{32}\Z")
VERSION = re.compile(r"[0-9a-f]{64}\Z")
TEXT_KEY = re.compile(r"[a-zA-Z0-9_.:-]{1,96}\Z")
OFFSETS = {"text_x", "text_y", "product_x", "product_y"}
PROTECTED_SCENE_MODE = "protected_sunburst"


class RefreshError(Exception):
    def __init__(self, code, message, status=400, *, state="failed", evidence=None):
        super().__init__(code)
        self.code, self.message, self.status, self.state = code, message, status, state
        self.evidence = evidence

    def payload(self):
        return {"code": self.code, "message": self.message}


def short(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 and c != "\n" for c in value):
        raise RefreshError("invalid_field", f"{field}须为 1–{limit} 字的文本。")
    return value.strip()


def digest_value(value, name):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise RefreshError("invalid_revision", f"{name}版本无效。")
    return value


def write_json(path, value):
    path.write_bytes(canonical(value) + b"\n")


def read_json(path):
    return json.loads(path.read_bytes())


def protected_scene(row):
    return row.get("review_source") == "scene_edit" and row.get("scene_edit", {}).get("method") == "protected_lighting"


def lighting_mask(size, regions):
    if (not isinstance(regions, dict) or not regions or len(regions) > 24 or any(
            not isinstance(box, list) or len(box) != 4 or any(type(coord) is not int for coord in box)
            or not (0 <= box[0] < box[2] <= size[0] and 0 <= box[1] < box[3] <= size[1])
            for box in regions.values())):
        raise RefreshError("invalid_scene_mask", "首屏保护区域未登记或超出画面。", 422)
    mask = Image.new("L", size, 255)
    for box in regions.values():
        mask.paste(0, box)
    return mask


def protected_lighting(frozen, fitted, editable, regions):
    """Transfer broad brightness differences while retaining all original geometry."""
    sigma, transition, strength, max_delta = 23, 60, .77, 24
    neutral = Image.new("L", frozen.size, 128)
    diff = ImageChops.subtract(fitted.convert("L"), frozen.convert("L"), offset=128)
    weighted = Image.composite(diff, neutral, editable).filter(ImageFilter.GaussianBlur(sigma))
    support = editable.filter(ImageFilter.GaussianBlur(sigma))
    # Feather outside hard regions. Pixels inside these rectangles stay byte-identical.
    ramp = Image.new("L", frozen.size, 255)
    for x0, y0, x1, y1 in regions.values():
        lo, top = max(0, x0 - transition), max(0, y0 - transition)
        hi, bottom = min(frozen.width, x1 + transition), min(frozen.height, y1 + transition)
        dx = [max(x0 - x, x - x1 + 1, 0) for x in range(lo, hi)]
        dy = [max(y0 - y, y - y1 + 1, 0) for y in range(top, bottom)]
        values = bytearray()
        for y in dy:
            for x in dx:
                t = min(1, (x * x + y * y) ** .5 / transition)
                values.append(round(255 * t * t * (3 - 2 * t)))
        box = (lo, top, hi, bottom)
        fade = Image.frombytes("L", (hi - lo, bottom - top), bytes(values))
        ramp.paste(ImageChops.darker(fade, ramp.crop(box)), box)
    mapped = bytearray()
    pixels = frozen.tobytes()
    for i, (value, weight, opacity) in enumerate(zip(weighted.tobytes(), support.tobytes(), ramp.tobytes())):
        correction = 0
        if weight >= 24 and opacity:
            delta = (value - 128) * 255 / weight
            correction = round(max(-max_delta, min(max_delta, delta)) * opacity / 255 * strength)
        start = i * 3
        mapped.extend(max(0, min(255, channel + correction)) for channel in pixels[start:start + 3])
    composite = Image.frombytes("RGB", frozen.size, bytes(mapped))
    channels = ImageChops.difference(composite, frozen).split()
    delta = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    changed = lambda mask: sum(mask.histogram()[1:])
    checks = {"method": "low_frequency_luminance", "blur_px": sigma, "transition_px": transition,
              "max_input_delta": max_delta, "strength": strength, "changed_pixels": changed(delta),
              "protected_pixels": editable.histogram()[0],
              "protected_changed_pixels": changed(ImageChops.multiply(delta, ImageOps.invert(editable))),
              "region_changes": {name: changed(delta.crop(box)) for name, box in regions.items()}}
    return composite, checks


class Pipeline:
    def __init__(self, root, assets, store):
        self.root, self.assets, self.store = root, assets, store
        self.artifacts = root / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.processing = threading.Lock()
        profiles = os.environ.get("REFRESH_V2_LOCAL_LAYOUTS")
        profile_path = Path(profiles).resolve() if profiles else None
        self.profile_root = profile_path.parent if profile_path else None
        self.local_layouts = read_json(profile_path) if profile_path else {}
        self.scene_trial_limit = int(os.environ.get("REFRESH_V2_REFERENCE_EDIT_LIMIT", "0"))
        if not 0 <= self.scene_trial_limit <= 20:
            raise ValueError("参考图试点额度须在 0–20 之间")

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not TASK_ID.fullmatch(value):
            raise RefreshError("invalid_task", "任务编号无效。")
        return value

    @staticmethod
    def public_candidate(candidate):
        return {key: val for key, val in candidate.items() if key not in {"path", "absolute_path"}}

    def discovery(self, code):
        try:
            return refresh_import.discover(self.assets, code)
        except (FileNotFoundError, ValueError):
            raise RefreshError("product_not_found", "该编码没有可读取的商品素材目录。", 404) from None

    def public_discovery(self, code):
        found = self.discovery(code)
        response = {key: found[key] for key in ("code", "name", "missing")}
        for kind in ("old_pages", "bottles", "operations", "logos"):
            response[kind] = [self.public_candidate(item) for item in found.get(kind, [])]
            if kind != "operations":
                for candidate in response[kind]:
                    candidate["image_url"] = f"{PREFIX}/products/{code}/candidates/{kind}/{candidate['id']}/image"
        profile = self.local_layouts.get(code) or {}
        if any(row["sha256"] == profile.get("source_sha256") for row in found["old_pages"]):
            protected = next((row["id"] for row in profile["screens"] if protected_scene(row)), None)
            if protected:
                response["protected_scene_screen"] = protected
        return response

    def candidate_image(self, code, kind, candidate_id):
        if kind not in {"old_pages", "bottles", "logos"}:
            raise RefreshError("candidate_missing", "未登记该图像候选。", 404)
        found = self.discovery(code)
        entry = next((item for item in found.get(kind, []) if item["id"] == candidate_id), None)
        if not entry:
            raise RefreshError("candidate_missing", "未登记该图像候选。", 404)
        return self.source({"selection": {kind: entry}}, kind)

    def registration(self, code, body):
        found = self.discovery(code)
        selection = {}
        for key, kind in (("old_page", "old_pages"), ("bottle", "bottles"), ("operations", "operations"), ("logo", "logos")):
            candidate_id = body.get(key)
            if candidate_id is None and key != "old_page":
                continue
            candidate = next((entry for entry in found.get(kind, []) if candidate_id == entry["id"]), None)
            if candidate is None:
                raise RefreshError("candidate_missing", f"{key} 必须使用该编码的登记候选 ID。", 422)
            if key == "bottle" and not candidate.get("transparent"):
                raise RefreshError("bottle_not_transparent", "请选择已验证透明度的独立瓶图。", 422)
            selection[key] = candidate
        approved = body.get("approved_copy", {})
        if not isinstance(approved, dict) or len(approved) > 120:
            raise RefreshError("invalid_copy", "核准文案须按字段登记，并附审批依据。")
        for key, entry in approved.items():
            if not TEXT_KEY.fullmatch(key) or not isinstance(entry, dict) or set(entry) != {"text", "approval_ref"}:
                raise RefreshError("invalid_copy", "核准文案字段需要文字和审批依据。")
            short(entry["text"], "文案", 600)
            short(entry["approval_ref"], "审批依据", 240)
        profile = self.local_profile(code, selection)
        verified_assets = profile and all(selection.get(role, {}).get("sha256") == digest
                                          for role, digest in profile["required_assets"].items())
        facts = {}
        for field in ("packaging", "channel", "brand_rule"):
            entry = body.get(field)
            if entry is None and verified_assets and field in profile.get("required_facts", {}):
                entry = {"value": profile["required_facts"][field], "status": "approved",
                         "approval_ref": profile["approval_ref"]}
            if entry is None:
                facts[field] = {"status": "pending"}
                continue
            if not isinstance(entry, dict) or not {"value", "status"} <= entry.keys() <= {"value", "status", "approval_ref"}:
                raise RefreshError("invalid_fact", f"{field} 登记需要值、状态与适用审批依据。")
            if entry["status"] not in ("pending", "approved"):
                raise RefreshError("invalid_fact", "商品登记状态无效。")
            short(entry["value"], "资料", 300)
            if entry["status"] == "approved":
                short(entry.get("approval_ref"), "审批依据", 240)
            facts[field] = entry
        name = short(body.get("name", found["name"]), "商品名", 80)
        plan = refresh_import.build_review(found, selection.get("operations"), selection["old_page"])
        if profile:
            if profile.get("whole_review") and approved:
                raise RefreshError("fixed_reference_copy", "当前整页试跑锁定已核对的页面文案。", 422)
            public_fields = ("id", "name", "kind", "source_range", "items")
            plan = {"source": "verified_old_page", "screens": [
                {**{key: spec[key] for key in public_fields},
                 "range_status": spec.get("range_status", "verified")}
                for spec in profile["screens"]], "warnings": profile.get("warnings", []),
                **({"whole_review": True} if profile.get("whole_review") else {})}
        missing = sorted((set(found["missing"]) - selection.keys()) | {key for key in ("bottle", "operations", "logo") if key not in selection}
                         | {key for key, value in facts.items() if value["status"] != "approved"}
                         | ({"approved_copy"} if not approved else set()))
        return {"code": code, "name": name, "mode": "refresh", "selection": selection, "facts": facts,
                "approved_copy": approved, "review_plan": plan, "missing": missing, "approval_status": "internal_pending",
                **({"reference_profile": revision(profile)} if profile else {})}

    def public_registration(self, registration):
        old = f"{PREFIX}/products/{registration['code']}/old-page?version={registration['version']}"
        profile = self.local_profile(registration["code"], registration["selection"])
        protected = next((row["id"] for row in profile["screens"] if protected_scene(row)), None) if profile else None
        stale = bool(protected and revision(profile) != registration.get("reference_profile"))
        return {**registration, "selection": {key: self.public_candidate(value) for key, value in registration["selection"].items()},
                "old_page_url": old + ("&scope=review" if registration.get("reference_profile") else ""),
                **({"full_old_page_url": old} if registration["review_plan"].get("whole_review") else {}),
                **({"reference_profile_stale": stale, "protected_scene_screen": protected if not stale else None}
                   if protected else {})}

    def review_reference_image(self, registration, path, scope=None):
        screens = registration["review_plan"]["screens"]
        if scope is not None:
            screens = [screen for screen in screens if screen["id"] == scope]
            if not screens:
                raise RefreshError("invalid_screens", "找不到这个旧页分屏。", 404)
        with Image.open(path) as old:
            old = ImageOps.exif_transpose(old).convert("RGB")
            crops = [old.crop((0, top, old.width, bottom)).resize(
                (refresh_layout.WIDTH, round((bottom - top) * refresh_layout.WIDTH / old.width)), Image.Resampling.LANCZOS)
                for top, bottom in (screen["source_range"] for screen in screens)]
        proof = Image.new("RGB", (refresh_layout.WIDTH, sum(crop.height for crop in crops)))
        cursor = 0
        for crop in crops:
            proof.paste(crop, (0, cursor))
            cursor += crop.height
        encoded = io.BytesIO()
        proof.save(encoded, format="PNG")
        encoded.seek(0)
        return encoded

    def source(self, registration, key):
        entry = registration["selection"].get(key)
        if not entry:
            raise RefreshError("asset_missing", f"当前登记缺少 {key}。", 422)
        root = self.profile_root if entry.get("root") == "profile" else self.assets
        rel = Path(entry["path"])
        path = (root / rel).resolve() if root and not rel.is_absolute() and not rel.drive else None
        if path is None or not path.is_relative_to(root) or not path.is_file() or file_hash(path) != entry["sha256"]:
            raise RefreshError("asset_changed", f"登记素材 {key} 已变化，请重新扫描并登记。", 409)
        return path

    def company_status(self):
        from refresh_gateway import Gateway, GatewayError

        try:
            gateway = Gateway.from_env()
        except GatewayError as error:
            return {"configured": False, "reason": error.code}
        return {"configured": True, "layout_model": gateway.layout_model, "image_model": gateway.image_model}

    @staticmethod
    def approved_company_scope(kind, code):
        var = "REFRESH_V2_COMPANY_OLD_PAGE_CODES" if kind == "plan" else "REFRESH_V2_COMPANY_SCENE_CODES"
        return code in {part.strip() for part in os.environ.get(var, "").split(",")}

    @staticmethod
    def approved_protected_scene(code):
        return code in {part.strip() for part in os.environ.get("REFRESH_V2_HERO_LIGHTING_CODES", "").split(",")}

    def local_profile(self, code, selection):
        profile = self.local_layouts.get(code)
        if profile and profile["source_sha256"] == selection["old_page"]["sha256"]:
            return profile
        return None

    def validate_plan(self, owner, code, body):
        version = digest_value(body["registration"], "登记")
        registration = self.store.get_registration(owner, code, version)
        if not registration:
            raise RefreshError("registration_required", "找不到该员工的登记版本。", 404)
        body.setdefault("mode", "local_review")
        if body["mode"] not in ("local_review", "company"):
            raise RefreshError("invalid_mode", "计划模式无效。")
        if body["mode"] == "company" and registration.get("reference_profile"):
            raise RefreshError("invalid_mode", "已核对的本地版式使用指定本地素材。", 422)
        if body["mode"] == "company" and not self.approved_company_scope("plan", code):
            raise RefreshError("material_scope_required", "公司网关尚未获准处理该编码的实际旧页。", 403)

    def required_task(self, owner, identifier, action):
        task = self.store.get(owner, self.identifier(identifier))
        if task is None or task["action"] != action:
            raise RefreshError("task_not_found", "找不到该任务。", 404)
        if task["state"] != "succeeded":
            raise RefreshError("task_unfinished", "任务还没有可用结果。", 409)
        return task

    def validate_preview(self, owner, parent, body):
        parent = self.required_task(owner, parent["id"], "plan")
        plan = parent["result"]["plan"]
        screens = {screen["id"]: screen for screen in plan["screens"]}
        selected = body.setdefault("screens", list(screens))
        if (not isinstance(selected, list) or not selected or len(selected) > len(screens)
                or any(not isinstance(entry, str) or entry not in screens for entry in selected)
                or len(set(selected)) != len(selected)):
            raise RefreshError("invalid_screens", "请选择计划中的屏幕，编号不能重复。")
        body["screens"] = [key for key in screens if key in selected]
        base = None
        if "base_job_id" in body:
            base = self.required_task(owner, body["base_job_id"], "preview")
            if base["body"]["plan_job_id"] != parent["id"]:
                raise RefreshError("different_revision", "背景复用须对应同一分屏计划。", 409)
        body.setdefault("mode", "reuse" if base else "local_review")
        if body["mode"] == PROTECTED_SCENE_MODE:
            if (not plan.get("whole_review") or len(selected) != 1 or not protected_scene(screens[selected[0]])
                    or any(key in body for key in ("base_job_id", "edits", "offsets", "redo_screen"))):
                raise RefreshError("invalid_protected_scene", "首屏光影优化只能使用已锁定的单屏。", 422)
            return
        if plan.get("whole_review"):
            if body["mode"] not in ("whole_review", "local_review", "reuse") or any(
                    key in body for key in ("edits", "offsets", "redo_screen")):
                raise RefreshError("fixed_reference_copy", "整页审稿使用已核对的分屏与素材。", 422)
            if body["mode"] == "reuse":
                runs = (base or {}).get("result", {}).get("background_runs", {})
                if not any(screens[key]["review_source"] == "scene_edit" and not protected_scene(screens[key])
                           and runs.get(key, {}).get("candidate_pending_visual_review") for key in selected):
                    raise RefreshError("scene_candidate_required", "请选用本计划已冻结的场景候选。", 422)
            elif base:
                raise RefreshError("invalid_mode", "采用已有候选时请使用复用模式。", 422)
            if body["mode"] == "whole_review" and sum(
                    screens[key]["review_source"] == "scene_edit" and not protected_scene(screens[key]) for key in selected) > 1:
                raise RefreshError("pilot_single_screen", "每次整页试跑最多提交一个有参考图的场景屏。", 422)
            return
        if body["mode"] not in ("local_review", "company", "reuse") or (body["mode"] == "reuse" and not base):
            raise RefreshError("invalid_mode", "背景模式无效。")
        if body["mode"] == "reuse" and not set(body["screens"]) <= set(base["body"]["screens"]):
            raise RefreshError("invalid_screens", "纯复用模式只接受已有背景的屏幕。", 409)
        redo = body.get("redo_screen")
        if "redo_screen" in body and (not base or not isinstance(redo, str) or redo not in selected
                                      or screens[redo]["kind"] != "visual" or body["mode"] == "reuse"):
            raise RefreshError("invalid_redo", "逐屏重做需要已有预览、目标视觉屏和新背景模式。")
        if body["mode"] == "company":
            if "reference_version" in plan:
                raise RefreshError("invalid_mode", "已核对的本地版式使用指定本地素材。", 422)
            if not self.approved_company_scope("preview", parent["code"]):
                raise RefreshError("material_scope_required", "公司网关尚未获准处理该编码的场景提示。", 403)
            count = 1 if redo else sum(screens[key]["kind"] == "visual" for key in selected)
            if count != 1:
                raise RefreshError("pilot_single_screen", "每次付费试点只允许提交一个视觉屏。", 422)
        for field, keys in (("edits", "items"), ("offsets", None)):
            per_screen = body.get(field, {})
            if not isinstance(per_screen, dict) or not per_screen.keys() <= set(selected):
                raise RefreshError("invalid_edit", "编辑内容须属于选中的屏。")
            for screen_id, updates in per_screen.items():
                permitted = {item["key"] for item in screens[screen_id][keys]} if keys else OFFSETS
                if not isinstance(updates, dict) or not updates.keys() <= permitted:
                    raise RefreshError("invalid_edit", "编辑字段未在本屏登记。")
                for value in updates.values():
                    if keys:
                        short(value, "草稿文字", 600)
                    elif type(value) is not int or abs(value) > 32:
                        raise RefreshError("invalid_offset", "位置微调须为 -32 到 32 像素的整数。")

    @staticmethod
    def validate_confirmation(task, submitted):
        digest_value(submitted, "草图")
        if task["state"] != "succeeded" or task["result"]["revision"] != submitted:
            raise RefreshError("revision_mismatch", "请确认当前可用的草图版本。", 409)
        if task["result"].get("review_only"):
            raise RefreshError("review_only", "当前整页供审稿，待确认画面后再安排可编辑 PSD。", 409)

    def folder(self, task_id):
        return self.artifacts / self.identifier(task_id)

    @staticmethod
    def file_url(task_id, filename):
        return f"{PREFIX}/jobs/{task_id}/files/{filename}"

    def download(self, task, filename, *, handoff=False):
        allowed = {"preview": {"preview.png", "checks.json"}, "confirm": {"detail.psd", "detail.png", "checks.json"}}
        if (task.get("result") or {}).get("review_only"):
            allowed["preview"].add("comparison.png")
            if task["body"].get("mode") == PROTECTED_SCENE_MODE:
                allowed["preview"].add("baseline-comparison.png")
            if re.fullmatch(r"s[0-9]{2}-candidate\.png", filename):
                allowed["preview"].add(filename)
        if handoff:
            valid = task["action"] == "confirm" and filename == "handoff.json"
        else:
            valid = filename in allowed.get(task["action"], ())
        path = self.folder(task["id"]) / filename if valid else None
        if task["state"] != "succeeded" or path is None or not path.is_file():
            raise RefreshError("file_not_found", "当前版本没有可下载的文件。", 404)
        return path

    def check_can_query(self, task):
        call = (task["result"] or {}).get("pending_call", {})
        if not call.get("task_id"):
            raise RefreshError("query_unavailable", "网关未返回可查询的任务 ID，请用原请求记录人工核对。", 409)

    @staticmethod
    def vision_image(path):
        with Image.open(path) as image:
            # Keep the entire page legible by sending consecutive views of the same source.
            rgb = image.convert("RGB")
            scaled_width = min(960, rgb.width)
            strip = max(1, round(rgb.width * 2.2))
            overlap = round(rgb.width * .12)
            if rgb.height > (strip - overlap) * 15 + overlap:
                raise RefreshError("vision_source_too_long", "旧页过长，需登记分段参考后再提交。", 422)
            for top in range(0, rgb.height, strip - overlap):
                view = rgb.crop((0, top, rgb.width, min(rgb.height, top + strip)))
                height = max(1, round(view.height * scaled_width / view.width))
                target = io.BytesIO()
                view.resize((scaled_width, height), Image.Resampling.LANCZOS).save(target, "JPEG", quality=86)
                yield target.getvalue()
                if top + strip >= rgb.height:
                    break

    def process(self, task, *, query=False):
        with self.processing:
            try:
                handler = {"plan": self.plan, "preview": self.preview, "confirm": self.confirm}[task["action"]]
                result = handler(task, query=query)
                self.store.finish(task["id"], "succeeded", result)
            except RefreshError as error:
                self.store.finish(task["id"], error.state, error.evidence, error.payload())
            except render.RenderError as error:
                self.store.finish(task["id"], "failed", error={"code": "layout_failed", "message": str(error)})
            except Exception as error:
                # Provider errors must be sanitized before reaching storage or application logs.
                LOGGER.error("Refresh task %s failed with %s", task["id"], type(error).__name__)
                self.store.finish(task["id"], "failed", error={"code": "processing_failed", "message": "处理失败，请核对素材及服务配置。"})

    def frozen(self, task, filename):
        manifest = read_json(self.folder(task["id"]) / filename)
        for name, sha in manifest["files"].items():
            path = self.folder(task["id"]) / name
            if Path(name).name != name or not path.is_file() or file_hash(path) != sha:
                raise RefreshError("snapshot_changed", "冻结的草图素材已改变。", 409)
        return manifest

    def gateway_call(self, task, kind, *, query, prompt=None, image=None):
        from refresh_gateway import GatewayError, Gateway

        key = f"refresh-{task['id']}-{kind}"
        pending = (task["result"] or {}).get("pending_call", {})
        try:
            gateway = Gateway.from_env()
            if query:
                result = gateway.query(pending["task_id"], kind=kind, request_id=key)
            elif kind == "plan":
                result = gateway.layout(prompt, images=tuple(self.vision_image(image)), request_id=key, idempotency_key=key)
            else:
                result = gateway.image(prompt, width=1024, height=1536, request_id=key, idempotency_key=key)
        except GatewayError as error:
            state = "unknown" if error.status == "unknown" else "failed"
            raise RefreshError(error.code, "公司模型调用未成功，详见调用状态记录。", 502, state=state,
                               evidence={"pending_call" if state == "unknown" else "call": error.evidence}) from None
        evidence = {name: value for name, value in result.items() if name not in {"text", "image"}}
        if result["status"] != "succeeded":
            if result["status"] in ("queued", "pending", "running", "unknown"):
                raise RefreshError("provider_pending", "已提交，查询原任务即可取回结果。", 202,
                                   state="unknown", evidence={"pending_call": evidence})
            raise RefreshError("provider_failed", "公司网关返回失败。", 502, evidence={"call": evidence})
        self.store.checkpoint(task["id"], {"pending_call": evidence})
        return {"value": result.get("text") if kind == "plan" else result.get("image"), "evidence": evidence}

    def plan(self, task, *, query=False):
        params = task["body"]
        registration = self.store.get_registration(task["owner"], task["code"], params["registration"])
        old_page = self.source(registration, "old_page")
        evidence = None
        if params["mode"] == "company":
            if not self.approved_company_scope("plan", task["code"]):
                raise RefreshError("material_scope_required", "该编码的旧页发送权限未启用。", 403)
            folder = self.folder(task["id"])
            folder.mkdir(exist_ok=True)
            recovered = None if query else self.store.recoverable_plan(task)
            if recovered and (self.folder(recovered["id"]) / "model-layout.json").is_file():
                proposed = read_json(self.folder(recovered["id"]) / "model-layout.json")
                evidence = {**recovered["result"]["call"], "recovered_from": recovered["id"]}
            else:
                prompt = refresh_layout.model_prompt(registration)
                result = self.gateway_call(task, "plan", query=query, prompt=prompt, image=old_page)
                evidence = result["evidence"]
                result_text = result["value"].strip()
                if result_text.startswith("```json\n") and result_text.endswith("```"):
                    result_text = result_text[8:-3].strip()
                try:
                    proposed = json.loads(result_text)
                except ValueError:
                    raise RefreshError("invalid_model_layout", "模型返回的分屏结果不是有效 JSON。", 422,
                                       evidence={"call": evidence}) from None
            evidence["source_sha256"] = registration["selection"]["old_page"]["sha256"]
            write_json(folder / "model-layout.json", proposed)
            try:
                layout = refresh_layout.reconcile_layout(proposed, registration)
            except (TypeError, ValueError, render.RenderError) as error:
                message = str(error) if isinstance(error, render.RenderError) else "模型分屏结果未通过结构校验。"
                raise RefreshError("invalid_model_layout", message, 422,
                                   evidence={"call": evidence}) from None
        else:
            profile = self.local_profile(task["code"], registration["selection"])
            if registration.get("reference_profile"):
                if not profile or revision(profile) != registration["reference_profile"]:
                    raise RefreshError("layout_changed", "本地版式已变化，请重新登记。", 409)
                for role, digest in profile["required_assets"].items():
                    if registration["selection"].get(role, {}).get("sha256") != digest:
                        raise RefreshError("layout_asset_required", f"此旧页的核对版式需要选择指定的 {role} 素材。", 422)
                for key, value in profile.get("required_facts", {}).items():
                    fact = registration["facts"].get(key, {})
                    if fact.get("status") != "approved" or fact.get("value") != value:
                        raise RefreshError("layout_fact_required", f"请核对并登记此版式的 {key} 决定。", 422)
                layout = refresh_layout.reference_layout(registration, profile)
            else:
                layout = refresh_layout.derive_layout(registration)
        protected = [screen["id"] for screen in layout["screens"] if protected_scene(screen)]
        if layout.get("whole_review") and protected:
            if len(protected) != 1:
                raise RefreshError("invalid_protected_scene", "光影试点只接受一个登记首屏。", 422)
            layout["protected_scene_screen"] = protected[0]
        return {"plan": layout, "registration": registration["version"], "mode": params["mode"], "call": evidence}

    def freeze_reference_art(self, screen, folder, files):
        layers = []
        for index, asset in enumerate(screen["reference_art"], 1):
            original = self.source({"selection": {"art": asset}}, "art")
            filename = f"{screen['id']}-art-{index:02d}.png"
            shutil.copyfile(original, folder / filename)
            files[filename] = file_hash(folder / filename)
            if files[filename] != asset["sha256"]:
                raise RefreshError("asset_changed", "核对版式的图像素材已变化。", 409)
            layers.append({**asset, "file": filename})
        screen["reference_art"] = layers

    def reference_scene(self, task, screen, old_screen, folder, files):
        from refresh_gateway import Gateway, GatewayError

        spec = screen["scene_edit"]
        read = lambda key: self.source({"selection": {key: spec[key]}}, key)
        reference = read("reference").read_bytes()
        expected = old_screen.size
        with Image.open(io.BytesIO(reference)) as image:
            if image.size != expected or image.format != "PNG":
                raise RefreshError("invalid_scene_reference", "茶汤屏的引导图与分屏尺寸不符。", 422)
        with Image.open(read("mask")) as image:
            mask = image.convert("L")
        if mask.size != expected or not mask.getbbox() or any(mask.crop(box).getbbox() for box in spec["protected"]):
            raise RefreshError("invalid_scene_mask", "茶汤屏遮罩超出场景区或碰到页面文案。", 422)
        key = f"refresh-{task['id']}-reference"
        attachment = hashlib.sha256(reference).hexdigest()
        call = {"request_id": key, "endpoint": "/v1/images/edits", "input_sha256": attachment,
                "input_description": spec["input_description"], "count": 1}
        try:
            gate = Gateway.from_env()
        except GatewayError as error:
            return old_screen, {**call, "source": "old_page_retained", "status": "unavailable",
                                "error": error.code, "count": 0}
        self.store.checkpoint(task["id"], {"whole_review": True, "pending_call": call})
        try:
            result = gate.edit_image(spec["prompt"], reference, request_id=key, idempotency_key=key)
        except GatewayError as error:
            state = "lookup_failed" if error.evidence.get("request_id", "").startswith("discover-") else error.status
            return old_screen, {**call, **error.evidence, "request_id": key, "status": state,
                                "source": "old_page_retained", "error": error.code,
                                "count": 0 if state == "lookup_failed" else 1}
        blob = result.pop("image", None)
        call.update(result)
        if result["status"] != "succeeded" or not blob:
            return old_screen, {**call, "source": "old_page_retained"}
        raw_name = f"{screen['id']}-gateway.png"
        try:
            with Image.open(io.BytesIO(blob)) as source:
                if source.width * source.height > 25_000_000 or getattr(source, "n_frames", 1) != 1:
                    raise ValueError("incorrect scene dimensions or animation")
                candidate = source.convert("RGB")
        except (OSError, ValueError, Image.DecompressionBombError):
            return old_screen, {**call, "source": "old_page_retained", "status": "invalid_image"}
        candidate.save(folder / raw_name)
        files[raw_name] = file_hash(folder / raw_name)
        # Candidate and original stay with the same draft revision. Publish only
        # the isolated blend for review; the retained screen protects exact copy.
        fitted = ImageOps.fit(candidate, expected, Image.Resampling.LANCZOS)
        blended = Image.composite(fitted, old_screen, mask)
        name = f"{screen['id']}-candidate.png"
        blended.save(folder / name)
        files[name] = file_hash(folder / name)
        return old_screen, {**call, "source": "old_page_retained", "candidate_pending_visual_review": True,
                            "candidate_sha256": files[name], "candidate": self.file_url(task["id"], name),
                            "provider_image_sha256": hashlib.sha256(blob).hexdigest(), "candidate_size": list(candidate.size)}

    def protected_reference_scene(self, task, screen, frozen, folder, files):
        from refresh_gateway import Gateway, GatewayError

        spec = screen["scene_edit"]
        baseline_sha = screen["review_asset"]["sha256"]
        call = {"source": "confirmed_png_retained", "effect_applied": False, "baseline_sha256": baseline_sha,
                "changed_pixels": 0, "protected_changed_pixels": 0, "count": 0}
        if not self.approved_protected_scene(task["code"]):
            return frozen, {**call, "status": "reference_edit_not_authorized"}
        company = self.company_status()
        if not company.get("configured"):
            return frozen, {**call, "status": "unavailable", "reason": company.get("reason")}
        if company.get("image_model") != "gpt-image-2.5-sunburst":
            return frozen, {**call, "status": "invalid_model", "model": company.get("image_model")}
        blocked = task["body"].get("blocked_reference_call")
        if blocked:
            return frozen, {**call, "status": "prior_request_unresolved", "prior_request": blocked}
        if not task["body"].get("reference_edit"):
            return frozen, {**call, "status": "pilot_limit"}
        path = self.source({"selection": {"reference": spec["reference"]}}, "reference")
        reference = path.read_bytes()
        if hashlib.sha256(reference).hexdigest() != baseline_sha:
            raise RefreshError("invalid_scene_reference", "公司参考图与已确认的首屏不是同一版。", 422)
        with Image.open(io.BytesIO(reference)) as source:
            if source.size != frozen.size or source.format != "PNG" or source.convert("RGB").tobytes() != frozen.tobytes():
                raise RefreshError("invalid_scene_reference", "公司参考图与已确认的首屏像素不一致。", 422)
        regions = spec["protected"]
        mask = lighting_mask(frozen.size, regions)
        if not mask.getbbox():
            raise RefreshError("invalid_scene_mask", "未给首屏光影留下可编辑范围。", 422)
        key = f"refresh-{task['id']}-reference"
        call.update(request_id=key, model=company["image_model"], endpoint="/v1/images/edits",
                    input_sha256=baseline_sha, input_description=spec["input_description"], count=1)
        self.store.checkpoint(task["id"], {"whole_review": True, "pending_call": call})
        try:
            gate = Gateway.from_env()
            result = gate.edit_image(spec["prompt"], reference, request_id=key, idempotency_key=key)
        except GatewayError as error:
            state = "lookup_failed" if error.evidence.get("request_id", "").startswith("discover-") else error.status
            return frozen, {**call, **error.evidence, "request_id": key, "status": state,
                            "error": error.code, "count": 0 if state == "lookup_failed" else 1}
        blob = result.pop("image", None)
        call.update(result)
        if call["status"] != "succeeded" or not blob:
            return frozen, call
        raw_name = f"{screen['id']}-gateway.png"
        try:
            with Image.open(io.BytesIO(blob)) as source:
                if source.width * source.height > 25_000_000 or getattr(source, "n_frames", 1) != 1:
                    raise ValueError("incorrect scene dimensions or animation")
                candidate = source.convert("RGB")
        except (OSError, ValueError, Image.DecompressionBombError):
            return frozen, {**call, "status": "invalid_image"}
        call.update(provider_image_sha256=hashlib.sha256(blob).hexdigest(), candidate_size=list(candidate.size))
        if abs(candidate.width / candidate.height - frozen.width / frozen.height) > .025:
            return frozen, {**call, "status": "invalid_scene_geometry"}
        candidate.save(folder / raw_name)
        files[raw_name] = file_hash(folder / raw_name)
        fitted = ImageOps.fit(candidate, frozen.size, Image.Resampling.LANCZOS, centering=(.5, 0))
        blended, checks = protected_lighting(frozen, fitted, mask, regions)
        call.update(checks)
        if checks["protected_changed_pixels"]:
            return frozen, {**call, "status": "protection_failed"}
        if not checks["changed_pixels"]:
            return frozen, {**call, "status": "no_lighting_change"}
        name = f"{screen['id']}-candidate.png"
        blended.save(folder / name)
        files[name] = file_hash(folder / name)
        return blended, {**call, "source": "company_scene_edit", "effect_applied": True,
                         "candidate_sha256": files[name], "candidate": self.file_url(task["id"], name)}

    def whole_page_review(self, task, parent, registration, *, query=False):
        if query:
            raise RefreshError("query_unavailable", "使用原网关请求号核对，不重新提交。", 409)
        start = time.monotonic()
        params = task["body"]
        screens = [row for row in parent["result"]["plan"]["screens"] if row["id"] in params["screens"]]
        folder = self.folder(task["id"])
        folder.mkdir(exist_ok=True)
        old_path = self.source(registration, "old_page")
        with Image.open(old_path) as source:
            old_page = source.convert("RGB")
        width = refresh_layout.WIDTH
        height = sum(row["height"] for row in screens)
        new = Image.new("RGB", (width, height))
        label_width = 28
        pair = Image.new("RGB", (width * 2 + label_width, height + 60), "#f5f2ec")
        pen = ImageDraw.Draw(pair)
        font_path = render._font_path()
        heading = ImageFont.truetype(font_path, 24)
        number = ImageFont.truetype(font_path, 14)
        pen.text((20, 10), "翻新草稿 · 待审", font=heading, fill="#28231e")
        pen.text((width + label_width + 20, 10), "上线旧页 · 对齐实际首屏", font=heading, fill="#28231e")
        files, runs, records = {}, {}, []
        baseline_comparison = None
        base = self.required_task(task["owner"], params["base_job_id"], "preview") if params.get("base_job_id") else None
        frozen_runs = self.frozen(base, "manifest.json")["runs"] if base else {}
        top = 0
        for row in screens:
            screen_start = time.monotonic()
            y0, y1 = row["old_range"]
            old = old_page.crop((0, y0, width, y1))
            run = {"source": row["review_source"], "count": 0}
            if protected_scene(row):
                path = self.source({"selection": {"png": row["review_asset"]}}, "png")
                with Image.open(path) as file:
                    frozen = file.convert("RGB")
                view = frozen
                run = {"source": "confirmed_png_retained", "count": 0, "effect_applied": False,
                       "baseline_sha256": row["review_asset"]["sha256"]}
                if frozen.size != (width, row["height"]):
                    raise RefreshError("invalid_reference_size", "已确认首屏尺寸与登记不符。", 422)
                if params["mode"] == PROTECTED_SCENE_MODE:
                    view, run = self.protected_reference_scene(task, row, frozen, folder, files)
                    baseline_comparison = Image.new("RGB", (width * 2 + label_width, height + 60), "#f5f2ec")
                    labels = ImageDraw.Draw(baseline_comparison)
                    labels.text((20, 10), "已确认首屏 · 本次基准", font=heading, fill="#28231e")
                    labels.text((width + label_width + 20, 10),
                                "保护合成 · 光影候选" if run["effect_applied"] else "本次保留已确认首屏",
                                font=heading, fill="#28231e")
                    baseline_comparison.paste(frozen, (0, 60))
                    baseline_comparison.paste(view, (width + label_width, 60))
            elif row["review_source"] in ("confirmed_png", "reused_png"):
                path = self.source({"selection": {"png": row["review_asset"]}}, "png")
                with Image.open(path) as file:
                    view = file.convert("RGB")
                run["source_sha256"] = row["review_asset"]["sha256"]
            elif row["review_source"] in ("old_crop", "scene_edit"):
                view = old
                run["source"] = "old_page_retained"
                if row["review_source"] == "scene_edit":
                    prior_run = frozen_runs.get(row["id"], {})
                    if prior_run.get("candidate_pending_visual_review"):
                        name = f"{row['id']}-candidate.png"
                        asset = self.folder(base["id"]) / name
                        if not asset.is_file() or file_hash(asset) != prior_run["candidate_sha256"]:
                            raise RefreshError("snapshot_changed", "已冻结的场景候选已改变。", 409)
                        with Image.open(asset) as file:
                            view = file.convert("RGB")
                        run = {"source": "reused_scene_candidate", "count": 0, "original_task_id": base["id"],
                               "model": prior_run["model"], "provider_request_id": prior_run.get("provider_request_id"),
                               "candidate_sha256": prior_run["candidate_sha256"]}
                    elif params.get("reference_edit"):
                        view, run = self.reference_scene(task, row, old, folder, files)
                    else:
                        company = self.company_status()
                        run["status"] = "reference_edit_not_submitted"
                        run["reason"] = (company.get("reason") if not company.get("configured") else
                                         "reference_edit_not_requested_or_budget_used")
            elif row["review_source"] == "asset_patch":
                asset = self.source({"selection": {"png": row["review_asset"]}}, "png")
                with Image.open(asset) as file:
                    patch = file.convert("RGBA")
                if patch.size != old.size or not patch.getchannel("A").getbbox():
                    raise RefreshError("invalid_reference_size", "产品贴片与核对分屏尺寸不符。", 422)
                if any(patch.getchannel("A").crop(box).getbbox() for box in row["protected"]):
                    raise RefreshError("invalid_scene_mask", "产品贴片碰到锁定的文案区域。", 422)
                view = Image.alpha_composite(old.convert("RGBA"), patch).convert("RGB")
                run["source_sha256"] = row["review_asset"]["sha256"]
                run["patch_bbox"] = list(patch.getchannel("A").getbbox())
                run["provenance"] = "指定真瓶叠回，原版文案、卡片和位置保留"
            else:
                raise RefreshError("invalid_reference_screen", "分屏缺少核对后的画面来源。", 422)
            if view.size != (width, row["height"]):
                raise RefreshError("invalid_reference_size", "冻结分屏尺寸与登记不符。", 422)
            screen_path = folder / f"{row['id']}-preview.png"
            view.save(screen_path)
            files[screen_path.name] = file_hash(screen_path)
            new.paste(view, (0, top))
            pair.paste(view, (0, top + 60))
            pair.paste(old, (width + label_width, top + 60))
            pen.text((width + 3, top + 64), row["id"][1:], font=number, fill="#564b3f")
            delta = ImageChops.difference(view, old)
            runs[row["id"]] = run
            records.append({"id": row["id"], "name": row["name"], "source_range": [y0, y1],
                            "new_range": [top, top + row["height"]], "size": list(view.size),
                            "same_as_old": delta.getbbox() is None, "png_sha256": files[screen_path.name],
                            "elapsed_ms": round((time.monotonic() - screen_start) * 1000), "run": run})
            top += row["height"]
        for name, image in (("preview.png", new), ("comparison.png", pair)):
            image.save(folder / name)
            files[name] = file_hash(folder / name)
        if baseline_comparison:
            name = "baseline-comparison.png"
            baseline_comparison.save(folder / name)
            files[name] = file_hash(folder / name)
        checks = {"width": width, "height": height, "screens": records, "review_only": True}
        write_json(folder / "checks.json", checks)
        files["checks.json"] = file_hash(folder / "checks.json")
        manifest = {"files": files, "plan_job_id": parent["id"], "registration_version": registration["version"],
                    "source_old_sha256": registration["selection"]["old_page"]["sha256"], "runs": runs, "screens": records}
        write_json(folder / "manifest.json", manifest)
        return {"revision": revision(manifest), "preview": self.file_url(task["id"], "preview.png"),
                "checks": self.file_url(task["id"], "checks.json"), "comparison": self.file_url(task["id"], "comparison.png"),
                "review_only": True, "background_runs": runs, "plan_job_id": parent["id"], "screens": params["screens"],
                **({"baseline_comparison": self.file_url(task["id"], "baseline-comparison.png")}
                   if baseline_comparison else {}),
                "timings_ms": {"total_ms": round((time.monotonic() - start) * 1000)}}

    def preview(self, task, *, query=False):
        start = time.monotonic()
        params = task["body"]
        parent = self.required_task(task["owner"], params["plan_job_id"], "plan")
        registration = self.store.get_registration(task["owner"], task["code"], parent["result"]["registration"])
        layout = parent["result"]["plan"]
        if layout.get("whole_review"):
            return self.whole_page_review(task, parent, registration, query=query)
        folder = self.folder(task["id"])
        folder.mkdir(exist_ok=True)
        base = self.required_task(task["owner"], params["base_job_id"], "preview") if params.get("base_job_id") else None
        previous = self.frozen(base, "manifest.json") if base else None
        selected = set(params["screens"])
        selected_layout = {**layout, "screens": []}
        files, runs, image_hashes, timings = {}, {}, {}, {}
        recovery = None if query else self.store.recoverable_preview(task)

        if recovery and (self.folder(recovery["id"]) / "bottle.png").is_file():
            shutil.copyfile(self.folder(recovery["id"]) / "bottle.png", folder / "bottle.png")
            files["bottle.png"] = file_hash(folder / "bottle.png")
        elif "bottle" in registration["selection"]:
            bottle = self.source(registration, "bottle")
            with Image.open(bottle) as image:
                image.convert("RGBA").save(folder / "bottle.png")
            files["bottle.png"] = file_hash(folder / "bottle.png")
        if ("logo" in registration["selection"] and registration["facts"]["brand_rule"]["status"] == "approved"):
            logo = self.source(registration, "logo")
            if "reference_version" in layout and logo.suffix.lower() == ".png":
                shutil.copyfile(logo, folder / "logo.png")
            else:
                with Image.open(logo) as image:
                    image.convert("RGBA").save(folder / "logo.png")
            files["logo.png"] = file_hash(folder / "logo.png")

        old_screens = {screen["id"]: screen for screen in previous["layout"]["screens"]} if previous else {}
        for original in layout["screens"]:
            screen_id = original["id"]
            if screen_id not in selected:
                continue
            screen_start = time.monotonic()
            prior = old_screens.get(screen_id, {})
            updates = params.get("edits", {}).get(screen_id, {})
            offsets = {**prior.get("offsets", {}), **params.get("offsets", {}).get(screen_id, {})}
            screen = {**original, "offsets": offsets, "items": [
                {**item, "text": updates[item["key"]], "status": "pending", "source": "internal_revision"}
                if item["key"] in updates else item for item in prior.get("items", original["items"])
            ]}
            screen.update(bottle="bottle.png" if "bottle.png" in files else None,
                          logo="logo.png" if "logo.png" in files else None, background=None)
            if "reference_art" in screen:
                if params["mode"] == "company":
                    raise RefreshError("invalid_mode", "已核对的本地版式使用指定本地素材。", 422)
                if prior and params["mode"] == "reuse":
                    screen["reference_art"] = prior["reference_art"]
                    for asset in screen["reference_art"]:
                        filename = asset["file"]
                        shutil.copyfile(self.folder(base["id"]) / filename, folder / filename)
                        files[filename] = previous["files"][filename]
                else:
                    self.freeze_reference_art(screen, folder, files)
                runs[screen_id] = {"source": "local_reference_art", "provider": "local", "count": 0}
                timings.setdefault("screen_assets_ms", {})[screen_id] = round((time.monotonic() - screen_start) * 1000)
            elif screen["kind"] == "visual":
                name = f"scene-{len(selected_layout['screens']):02d}.png"
                target = folder / name
                keep = prior.get("background") and (params["mode"] in {"reuse", "local_review"} or bool(params.get("redo_screen"))) and params.get("redo_screen") != screen_id
                saved_run = (recovery["result"] or {}).get("background_runs", {}).get(screen_id) if recovery else None
                saved_scene = self.folder(recovery["id"]) / name if saved_run else None
                if saved_scene and saved_scene.is_file():
                    expected = (recovery["result"] or {}).get("backgrounds", {}).get(screen_id)
                    if expected and file_hash(saved_scene) != expected:
                        raise RefreshError("snapshot_changed", "上次任务的场景文件已变化。", 409)
                    shutil.copyfile(saved_scene, target)
                    runs[screen_id] = saved_run
                elif keep:
                    shutil.copyfile(self.folder(base["id"]) / prior["background"], target)
                    runs[screen_id] = previous["background_runs"][screen_id]
                elif params["mode"] == "company":
                    if not self.approved_company_scope("preview", task["code"]):
                        raise RefreshError("material_scope_required", "该编码的场景提示发送权限未启用。", 403)
                    if not query:
                        local_start = time.monotonic()
                        preflight = {**selected_layout, "screens": [{**screen, "background": None}]}
                        with tempfile.TemporaryDirectory(prefix="refresh-preflight-", dir=folder) as work:
                            refresh_layout.preview(preflight, folder, Path(work) / "proof.png")
                        timings["native_preflight_ms"] = round((time.monotonic() - local_start) * 1000)
                    result = self.gateway_call(task, "image", query=query, prompt=refresh_layout.scene_prompt(screen))
                    with Image.open(io.BytesIO(result["value"])) as image:
                        ImageOps.fit(image.convert("RGB"), (layout["width"], screen["height"]), Image.Resampling.LANCZOS).save(target)
                    runs[screen_id] = {**result["evidence"], "scene_prompt_sha256": revision(refresh_layout.scene_prompt(screen)),
                                       "source": "company_scene_prompt_only", "count": 1, "requested_size": [1024, 1536]}
                else:
                    runs[screen_id] = backgrounds.make(screen["id"], layout["width"], screen["height"], "warm", target)
                screen["background"] = name
                files[name] = image_hashes[screen_id] = file_hash(target)
            selected_layout["screens"].append(screen)

        try:
            local_start = time.monotonic()
            checks = refresh_layout.preview(selected_layout, folder, folder / "preview.png")
            timings["local_render_ms"] = round((time.monotonic() - local_start) * 1000)
        except render.RenderError as error:
            raise RefreshError("layout_failed", str(error), 422,
                               evidence={"background_runs": runs, "backgrounds": image_hashes}) from None
        write_json(folder / "checks.json", checks)
        files["preview.png"] = file_hash(folder / "preview.png")
        manifest = {"layout": selected_layout, "files": files, "background_runs": runs,
                    "registration_version": registration["version"], "plan_job_id": parent["id"],
                    "source": {key: entry["sha256"] for key, entry in registration["selection"].items()},
                    "mode": params["mode"], "approval_status": "internal_pending"}
        write_json(folder / "manifest.json", manifest)
        timings["total_ms"] = round((time.monotonic() - start) * 1000)
        return {"revision": revision(manifest), "preview": self.file_url(task["id"], "preview.png"),
                "checks": self.file_url(task["id"], "checks.json"), "backgrounds": image_hashes, "background_runs": runs,
                "approval_status": manifest["approval_status"], "recovered_from": recovery["id"] if recovery else None,
                "timings_ms": timings}

    def confirm(self, task, *, query=False):
        if query:
            raise RefreshError("query_unavailable", "排版任务没有外部查询接口。", 409)
        source = self.required_task(task["owner"], task["body"]["preview_job_id"], "preview")
        self.validate_confirmation(source, task["body"]["revision"])
        manifest = self.frozen(source, "manifest.json")
        if revision(manifest) != task["body"]["revision"]:
            raise RefreshError("snapshot_changed", "冻结的草图清单已改变。", 409)
        folder = self.folder(task["id"])
        folder.mkdir(exist_ok=True)
        checks = refresh_layout.export(manifest["layout"], self.folder(source["id"]), folder / "detail.psd", folder / "detail.png")
        compare = render.compare(self.folder(source["id"]) / "preview.png", folder / "detail.png", checks["screens"])
        if any(screen["changed_pixels"] for screen in compare):
            raise RefreshError("preview_export_mismatch", "确认稿与重开 PSD 导出的 PNG 存在差异。", 422)
        checks["preview_export"] = compare
        write_json(folder / "checks.json", checks)
        approved_assets = {ext: {"file": f"detail.{ext}", "sha256": file_hash(folder / f"detail.{ext}")} for ext in ("psd", "png")}
        layout = manifest["layout"]
        handoff = {"schema_version": 1, "code": task["code"], "job_id": task["id"],
                   "revision": task["body"]["revision"], "approval_status": "internal_layout_confirmed",
                   "width": checks["width"], "height": checks["height"], "screens": [
                       {"id": row["id"], "name": row["name"], "y": check["top"], "height": check["height"]}
                       for row, check in zip(layout["screens"], checks["screens"])], "assets": approved_assets,
                   "source": {"template": layout["rules_version"], "layout": revision(layout),
                              "copy": revision([row["items"] for row in layout["screens"]]),
                              "bottle": manifest["source"].get("bottle")}}
        write_json(folder / "handoff.json", handoff)
        write_json(folder / "confirmation.json", {"employee": task["owner"], "confirmed_at": stamp(),
                                                    "preview_job_id": source["id"], "revision": handoff["revision"],
                                                    "approval_status": handoff["approval_status"]})
        return {"revision": handoff["revision"], "preview_job_id": source["id"], "approval_status": handoff["approval_status"],
                "preview_matches_export": True, "handoff": f"{PREFIX}/jobs/{task['id']}/handoff",
                "files": {name: self.file_url(task["id"], name) for name in ("detail.psd", "detail.png", "checks.json")}}
