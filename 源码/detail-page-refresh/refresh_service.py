"""Owner-scoped refresh API. The legacy review server remains independent."""

import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request, send_file

import refresh_pipeline as pipeline
from refresh_storage import data_directory
from refresh_store import Store, StoreConflict


ROOT = Path(__file__).resolve().parent
CODE = re.compile(r"[0-9]{10,16}\Z")
JOB = re.compile(r"[0-9a-f]{32}\Z")
REVISION = re.compile(r"[0-9a-f]{64}\Z")
EMPLOYEE = re.compile(r"[A-Za-z0-9._@-]{1,128}\Z")
IDEMPOTENCY = re.compile(r"[A-Za-z0-9_.:-]{16,128}\Z")
PREFIX = "/api/refresh"


def create_app(*, data_root=None, asset_root=None, proxy_token=None, public_origin=None, company_limit=None):
    assets = Path(asset_root or os.environ.get("REFRESH_V2_ASSETS", "E:/工作/规划落地对话专用素材/产品编码测验")).resolve()
    token = proxy_token if proxy_token is not None else os.environ.get("REFRESH_V2_PROXY_TOKEN", "")
    root = data_directory(data_root or os.environ.get("REFRESH_V2_DATA"), ROOT / ".data" / "refresh-v2",
                          "独立v2", local=not token)
    handoff_token = os.environ.get("REFRESH_V2_HANDOFF_TOKEN", "")
    origin = public_origin if public_origin is not None else os.environ.get("REFRESH_V2_PUBLIC_ORIGIN", "")
    quota = company_limit if company_limit is not None else int(os.environ.get("REFRESH_V2_PILOT_LIMIT", "0"))
    if token and (len(token) < 32 or not origin.startswith("https://")):
        raise RuntimeError("企业模式需要至少 32 字符的代理令牌和 HTTPS 统一入口。")
    if handoff_token and (len(handoff_token) < 32 or secrets.compare_digest(handoff_token, token)):
        raise RuntimeError("交接令牌须独立设置，长度至少 32 字符。")
    if not 0 <= quota <= 20:
        raise RuntimeError("试点额度须在 0–20 之间。")
    store = Store(root)
    store.recover()
    engine = pipeline.Pipeline(root, assets, store)
    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    wake = threading.Event()

    def worker():
        while True:
            wake.clear()
            while (task := store.claim()) is not None:
                engine.process(task)
            wake.wait(2)

    threading.Thread(target=worker, name="refresh-worker", daemon=True).start()

    @app.errorhandler(pipeline.RefreshError)
    def error_response(error):
        return jsonify(error.payload()), error.status

    @app.errorhandler(413)
    def too_large(_):
        return {"code": "request_too_large", "message": "登记请求超过大小限制。"}, 413

    @app.after_request
    def headers(response):
        response.headers.update({"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer"})
        return response

    @app.before_request
    def identity():
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            raise pipeline.RefreshError("proxy_required", "翻新服务仅接受本机或可信代理。", 403)
        if token:
            received = request.headers.get("X-Refresh-Proxy-Token", "").encode()
            user = request.headers.get("X-Detail-User", "")
            if not hmac.compare_digest(received, token.encode()) or not EMPLOYEE.fullmatch(user):
                raise pipeline.RefreshError("proxy_required", "请通过统一企业登录入口访问。", 403)
            expected = origin
            g.owner = user
        else:
            if urlsplit("//" + request.host).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise pipeline.RefreshError("local_only", "请使用环回地址。", 403)
            if request.headers.get("X-Detail-User") or request.headers.get("X-Refresh-Proxy-Token"):
                raise pipeline.RefreshError("proxy_required", "本机模式不接受身份标头。", 403)
            g.owner = "local"
            expected = "http://" + request.host
        source = request.headers.get("Origin")
        if (request.method not in {"GET", "HEAD", "OPTIONS"}
                and (request.headers.get("Sec-Fetch-Site") == "cross-site" or source and source != expected)):
            raise pipeline.RefreshError("cross_origin", "请求来源与入口不一致。", 403)

    def code(value):
        if not isinstance(value, str) or not CODE.fullmatch(value):
            raise pipeline.RefreshError("invalid_code", "请输入有效商品编码。")
        return value

    def body(allowed, required=()):
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or not value.keys() <= set(allowed) or not set(required) <= value.keys():
            raise pipeline.RefreshError("invalid_request", "请求字段与接口契约不符。")
        return value

    def job(task_id, action=None):
        record = store.get(g.owner, task_id) if JOB.fullmatch(task_id) else None
        if record is None or (action and record["action"] != action):
            raise pipeline.RefreshError("task_not_found", "找不到该任务。", 404)
        return record

    def public_task(record):
        fields = ("id", "code", "action", "state", "saved_at", "updated_at", "result", "error")
        response = {**{key: record[key] for key in fields}, "url": f"{PREFIX}/jobs/{record['id']}"}
        payload = record["body"]
        if record["action"] == "preview" and payload.get("mode") == pipeline.PROTECTED_SCENE_MODE:
            response.update(review_kind=pipeline.PROTECTED_SCENE_MODE, plan_job_id=payload["plan_job_id"],
                            screens=payload["screens"])
        result = response["result"] or {}
        plan = result.get("plan") or {}
        if plan.get("reference_version"):
            screens = [{key: value for key, value in screen.items() if key not in ("reference_art", "review_asset", "scene_edit")}
                       for screen in plan["screens"]]
            response["result"] = {**result, "plan": {**plan, "screens": screens}}
        return response

    def queue(sku, action, payload, *, company=False, reference_edit=False):
        key = request.headers.get("Idempotency-Key", "")
        if not IDEMPOTENCY.fullmatch(key):
            raise pipeline.RefreshError("idempotency_required", "请提供 16–128 字符的 Idempotency-Key。")
        try:
            task_id = store.enqueue(g.owner, sku, action, key, payload, company=company,
                                    company_limit=engine.scene_trial_limit if reference_edit else quota,
                                    reference_edit=reference_edit)
        except StoreConflict as exc:
            reason = str(exc)
            messages = {"idempotency_conflict": "同一幂等键已经绑定其他输入。", "pilot_limit": "已达到本次联调的模型调用额度。",
                        "queue_full": "该员工的任务队列已满，请稍后重试。"}
            raise pipeline.RefreshError(reason, messages[reason], 429 if reason != "idempotency_conflict" else 409) from None
        wake.set()
        return public_task(store.get(g.owner, task_id)), 202

    @app.get(PREFIX + "/health")
    def health():
        return {"schema_version": 1, "mode": "refresh", "identity": "proxy" if token else "local",
                "company": engine.company_status(), "pilot_limit": quota,
                **({"reference_edit_limit": engine.scene_trial_limit} if engine.scene_trial_limit else {})}

    @app.get(PREFIX + "/products/<sku>/candidates")
    def discover(sku):
        return engine.public_discovery(code(sku))

    @app.post(PREFIX + "/products/<sku>/registrations")
    def register(sku):
        payload = body(("old_page", "bottle", "operations", "logo", "brand_rule", "packaging", "channel", "name", "approved_copy"),
                       ("old_page",))
        record = store.save_registration(g.owner, engine.registration(code(sku), payload))
        return engine.public_registration(record), 201

    @app.get(PREFIX + "/products/<sku>")
    def product(sku):
        sku = code(sku)
        record = store.get_registration(g.owner, sku)
        if not record:
            raise pipeline.RefreshError("registration_required", "请先登记旧详情页和商品素材。", 404)
        return engine.public_registration(record)

    @app.get(PREFIX + "/products/<sku>/old-page")
    def old_page(sku):
        version = request.args.get("version")
        if version is not None and not REVISION.fullmatch(version):
            raise pipeline.RefreshError("invalid_revision", "登记版本无效。")
        registration = store.get_registration(g.owner, code(sku), version)
        if not registration:
            raise pipeline.RefreshError("registration_required", "找不到该员工的素材登记。", 404)
        path = engine.source(registration, "old_page")
        if request.args.get("scope") == "review" and registration.get("reference_profile"):
            return send_file(engine.review_reference_image(registration, path, request.args.get("screen")), mimetype="image/png")
        return send_file(path)

    @app.get(PREFIX + "/products/<sku>/candidates/<kind>/<candidate_id>/image")
    def candidate_image(sku, kind, candidate_id):
        return send_file(engine.candidate_image(code(sku), kind, candidate_id))

    @app.post(PREFIX + "/products/<sku>/plans")
    def plan(sku):
        payload = body(("registration", "mode"), ("registration",))
        sku = code(sku)
        engine.validate_plan(g.owner, sku, payload)
        return queue(sku, "plan", payload, company=payload["mode"] == "company")

    @app.post(PREFIX + "/previews")
    def preview():
        payload = body(("plan_job_id", "screens", "mode", "base_job_id", "redo_screen", "edits", "offsets"),
                       ("plan_job_id",))
        parent = job(engine.identifier(payload["plan_job_id"]), "plan")
        engine.validate_preview(g.owner, parent, payload)
        plan = parent["result"]["plan"]
        selected = set(payload["screens"])
        reference_edit = False
        if engine.scene_trial_limit:
            if payload["mode"] == pipeline.PROTECTED_SCENE_MODE:
                company = engine.company_status()
                reference_edit = bool(engine.approved_protected_scene(parent["code"]) and company.get("configured")
                                      and company.get("image_model") == "gpt-image-2.5-sunburst")
            elif payload["mode"] == "whole_review":
                reference_edit = bool(engine.approved_company_scope("preview", parent["code"])
                                      and engine.company_status().get("configured")
                                      and any(row.get("review_source") == "scene_edit" and not pipeline.protected_scene(row)
                                              and row["id"] in selected for row in plan["screens"]))
        return queue(parent["code"], "preview", payload, company=payload["mode"] == "company",
                     reference_edit=reference_edit)

    @app.post(PREFIX + "/jobs/<task_id>/confirm")
    def confirm(task_id):
        source = job(task_id, "preview")
        payload = body(("revision",), ("revision",))
        engine.validate_confirmation(source, payload["revision"])
        return queue(source["code"], "confirm", {"preview_job_id": task_id, "revision": payload["revision"]})

    @app.get(PREFIX + "/products/<sku>/jobs")
    def history(sku):
        return {"jobs": [public_task(record) for record in store.list(g.owner, code(sku))]}

    @app.get(PREFIX + "/jobs/<task_id>")
    def status(task_id):
        return public_task(job(task_id))

    @app.post(PREFIX + "/jobs/<task_id>/query")
    def query(task_id):
        current = job(task_id)
        if current["state"] != "unknown":
            return public_task(current)
        if (current["result"] or {}).get("whole_review"):
            raise pipeline.RefreshError("query_unavailable", "请使用已记录的原请求 ID 向网关核对。当前入口不会重发。", 409)
        engine.check_can_query(current)
        claimed = store.take_for_query(g.owner, task_id)
        if claimed is not None:
            threading.Thread(target=engine.process, args=(claimed,), kwargs={"query": True}, daemon=True).start()
        return public_task(store.get(g.owner, task_id)), 202

    @app.get(PREFIX + "/jobs/<task_id>/files/<name>")
    def download(task_id, name):
        return send_file(engine.download(job(task_id), name), as_attachment=name.endswith(".psd"))

    @app.get(PREFIX + "/jobs/<task_id>/handoff")
    def handoff(task_id):
        current = job(task_id, "confirm")
        supplied = request.headers.get("X-Refresh-Handoff-Token", "")
        if not token or not handoff_token or not hmac.compare_digest(supplied.encode(), handoff_token.encode()):
            raise pipeline.RefreshError("proxy_required", "交接清单仅由可信后端读取。", 403)
        return send_file(engine.download(current, "handoff.json", handoff=True))

    return app


if __name__ == "__main__":
    from waitress import serve

    serve(create_app(), host="127.0.0.1", port=int(os.environ.get("REFRESH_V2_PORT", "8775")), threads=6)
