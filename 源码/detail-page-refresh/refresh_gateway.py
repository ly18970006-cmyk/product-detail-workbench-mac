"""Server-only transport for the explicitly configured company model gateway."""

import base64
import binascii
import json
import os
import re
import time
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


LAYOUT_MODEL = "gpt-6.1-sol"
IMAGE_MODELS = {"gpt-image-2.5-sunburst", "gpt-image-2.5-flare"}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
FILE_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{15,}\b")
IMAGE_STATUS = re.compile(r"/(?:[A-Za-z0-9_-]+/)+\{id\}\Z")
RESPONSE_LIMIT = 32 * 1024 * 1024
IMAGE_LIMIT = 16 * 1024 * 1024
KNOWN_USAGE = {"input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"}


class GatewayError(Exception):
    def __init__(self, code, *, status="failed", evidence=None):
        super().__init__(code)
        self.code, self.status, self.evidence = code, status, evidence or {}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _api_base(value):
    try:
        url = urlsplit(value)
        valid = (url.scheme == "https" and url.hostname and not url.username and not url.password
                 and not url.query and not url.fragment and url.path.rstrip("/") in ("", "/v1")
                 and url.port in (None, 443) and re.fullmatch(r"[a-z0-9.-]+", url.hostname))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise GatewayError("gateway_config_invalid")
    return f"https://{url.hostname}/v1"


def _file_config(filename):
    try:
        with Path(filename).open(encoding="utf-8-sig") as file:
            contents = file.read(8193)
    except (OSError, UnicodeError):
        raise GatewayError("gateway_config_unreadable") from None
    if len(contents) > 8192:
        raise GatewayError("gateway_config_invalid")
    urls = re.findall(r"https://[^\s\"'<>]+", contents)
    if len(urls) != 1:
        raise GatewayError("gateway_config_invalid")
    tokens = []
    image_token = None
    for line in contents.splitlines():
        for token in FILE_TOKEN.findall(line):
            if token not in tokens:
                tokens.append(token)
            if "image" in FILE_TOKEN.sub("", line).lower():
                image_token = token
    if not tokens or len(tokens) > 2 or (len(tokens) == 2 and image_token is None):
        raise GatewayError("gateway_config_invalid")
    if len(tokens) == 1:
        return _api_base(urls[0]), tokens[0], tokens[0]
    return _api_base(urls[0]), next(token for token in tokens if token != image_token), image_token


def _media_type(blob):
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    raise GatewayError("gateway_invalid_image")


def _attached_image(value):
    if isinstance(value, Path):
        try:
            with value.open("rb") as source:
                value = source.read(IMAGE_LIMIT + 1)
        except OSError:
            raise GatewayError("gateway_invalid_image") from None
    if not isinstance(value, bytes) or len(value) > IMAGE_LIMIT:
        raise GatewayError("gateway_invalid_image")
    mime = _media_type(value)
    return {"type": "input_image", "image_url": f"data:{mime};base64,{base64.b64encode(value).decode('ascii')}",
            "detail": "high"}


class Gateway:
    """Disable redirects, fail closed on missing exact model, and never replay a POST."""

    def __init__(self, base, layout_token, image_token, *, image_model="gpt-image-2.5-sunburst", image_status_path=None):
        self.base = _api_base(base)
        self.layout_model = LAYOUT_MODEL
        if image_model not in IMAGE_MODELS or (image_status_path and not IMAGE_STATUS.fullmatch(image_status_path)):
            raise GatewayError("gateway_config_invalid")
        if any(not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{16,512}", token)
               for token in (layout_token, image_token)):
            raise GatewayError("gateway_config_invalid")
        self.image_model, self.image_status_path = image_model, image_status_path
        self._tokens = {"plan": layout_token, "image": image_token}
        self._opener = build_opener(_NoRedirect())

    @classmethod
    def from_env(cls):
        filename = os.environ.get("REFRESH_V2_GATEWAY_CONFIG")
        base = os.environ.get("REFRESH_V2_GATEWAY_BASE_URL")
        layout_token = os.environ.get("REFRESH_V2_GATEWAY_LAYOUT_TOKEN")
        image_token = os.environ.get("REFRESH_V2_GATEWAY_IMAGE_TOKEN")
        if filename:
            if base or layout_token or image_token:
                raise GatewayError("gateway_config_ambiguous")
            base, layout_token, image_token = _file_config(filename)
        if not base or not layout_token or not image_token:
            raise GatewayError("gateway_unconfigured")
        return cls(base, layout_token, image_token,
                   image_model=os.environ.get("REFRESH_V2_GATEWAY_IMAGE_MODEL", "gpt-image-2.5-sunburst"),
                   image_status_path=os.environ.get("REFRESH_V2_GATEWAY_IMAGE_STATUS_PATH"))

    def describe(self):
        return {"host": urlsplit(self.base).hostname, "layout_model": self.layout_model, "image_model": self.image_model,
                "image_query_configured": bool(self.image_status_path)}

    def _safe(self, value):
        if isinstance(value, str) and SAFE_ID.fullmatch(value) and not value.startswith("sk-"):
            if not any(token in value for token in self._tokens.values()):
                return value
        return None

    def _model(self, kind):
        if kind not in self._tokens:
            raise GatewayError("gateway_invalid_kind")
        return self.layout_model if kind == "plan" else self.image_model

    def _request(self, kind, path, request_id, *, body=None, idempotency_key=None, timeout=15):
        model = self._model(kind)
        if not self._safe(request_id) or (body is not None and not self._safe(idempotency_key)):
            raise GatewayError("gateway_invalid_request_id")
        headers = {"Authorization": f"Bearer {self._tokens[kind]}", "Accept": "application/json", "X-Request-ID": request_id}
        data = None
        if body is not None:
            if isinstance(body, tuple):
                data, content_type = body
            else:
                data, content_type = json.dumps(body).encode(), "application/json"
            headers.update({"Content-Type": content_type, "Idempotency-Key": idempotency_key})
        request = Request(self.base + path, data=data, headers=headers)
        evidence = {"request_id": request_id, "idempotency_key": idempotency_key, "model": model, "task_id": None,
                    "provider_request_id": None, "http_status": None, "elapsed_ms": None, "usage": None}
        start = time.monotonic()
        try:
            with self._opener.open(request, timeout=timeout) as response:
                evidence.update(http_status=response.status, provider_request_id=self._safe(response.headers.get("x-request-id")))
                raw = response.read(RESPONSE_LIMIT + 1)
            if len(raw) > RESPONSE_LIMIT:
                raise ValueError
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
            return payload, evidence
        except HTTPError as error:
            evidence.update(http_status=error.code, provider_request_id=self._safe(error.headers.get("x-request-id")))
            try:
                payload = json.loads(error.read(32 * 1024))
            except (OSError, ValueError):
                payload = {}
            finally:
                error.close()
            if isinstance(payload, dict):
                details = payload.get("error")
                if isinstance(details, dict):
                    evidence["provider_error_code"] = self._safe(details.get("code"))
                    evidence["provider_error_type"] = self._safe(details.get("type"))
                evidence["task_id"] = self._safe(payload.get("id") or payload.get("task_id"))
            ambiguous = body is not None and (error.code in (408, 409) or error.code >= 500)
            raise GatewayError("gateway_result_unknown" if ambiguous else "gateway_http_error",
                               status="unknown" if ambiguous else "failed", evidence=evidence) from None
        except (OSError, URLError, ValueError, HTTPException):
            submitted = body is not None
            raise GatewayError("gateway_result_unknown" if submitted else "gateway_lookup_unavailable",
                               status="unknown" if submitted else "failed", evidence=evidence) from None
        finally:
            evidence["elapsed_ms"] = round((time.monotonic() - start) * 1000)

    def discover(self):
        evidence = {}
        for kind in self._tokens:
            model = self._model(kind)
            payload, call = self._request(kind, "/models/" + model, f"discover-{uuid.uuid4().hex}")
            if payload.get("id") != model:
                raise GatewayError("gateway_model_unavailable", evidence=call)
            evidence[kind] = call
        return {**self.describe(), "discovery": evidence}

    def _post(self, kind, path, body, request_id, idempotency_key):
        model = self._model(kind)
        payload, discover = self._request(kind, "/models/" + model, f"discover-{uuid.uuid4().hex}")
        if payload.get("id") != model:
            raise GatewayError("gateway_model_unavailable", evidence=discover)
        payload, evidence = self._request(kind, path, request_id, body=body, idempotency_key=idempotency_key, timeout=170)
        return self._result(kind, payload, evidence)

    def layout(self, prompt, *, images=(), request_id, idempotency_key):
        if not isinstance(prompt, str) or not 0 < len(prompt) <= 60_000 or len(images) > 15:
            raise GatewayError("gateway_invalid_layout_input")
        attachments = [_attached_image(image) for image in images]
        if sum(len(item["image_url"]) for item in attachments) > 19 * 1024 * 1024:
            raise GatewayError("gateway_invalid_layout_input")
        content = [{"type": "input_text", "text": prompt}, *attachments]
        body = {"model": self.layout_model, "input": [{"role": "user", "content": content}],
                "max_output_tokens": 12000, "store": False}
        return self._post("plan", "/responses", body, request_id, idempotency_key)

    def image(self, prompt, *, width, height, request_id, idempotency_key):
        if not isinstance(prompt, str) or not 0 < len(prompt) <= 8_000:
            raise GatewayError("gateway_invalid_image_prompt")
        if (width, height) not in ((1024, 1024), (1024, 1536), (1536, 1024)):
            raise GatewayError("gateway_invalid_image_size")
        body = {"model": self.image_model, "prompt": prompt, "n": 1, "size": f"{width}x{height}",
                "quality": "low", "output_format": "png"}
        result = self._post("image", "/images/generations", body, request_id, idempotency_key)
        return {**result, "requested_size": [width, height], "count": 1}

    def edit_image(self, prompt, reference, *, request_id, idempotency_key):
        if (self.image_model != "gpt-image-2.5-sunburst" or not 0 < len(prompt) <= 8_000
                or len(reference) > IMAGE_LIMIT or _media_type(reference) != "image/png"):
            raise GatewayError("gateway_invalid_image_edit")
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("model", self.image_model), ("prompt", prompt), ("n", "1")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="scene.png"\r\n'
                     'Content-Type: image/png\r\n\r\n'.encode())
        parts.extend((reference, f"\r\n--{boundary}--\r\n".encode()))
        body = b"".join(parts), f"multipart/form-data; boundary={boundary}"
        model, evidence = self._request("image", "/models/" + self.image_model, f"discover-{uuid.uuid4().hex}")
        if model.get("id") != self.image_model:
            raise GatewayError("gateway_model_unavailable", evidence=evidence)
        payload, call = self._request("image", "/images/edits", request_id, body=body,
                                      idempotency_key=idempotency_key, timeout=180)
        call.update(endpoint="/v1/images/edits", model_discovery_ms=evidence["elapsed_ms"],
                    returned_model=self._safe(payload.get("model")))
        return self._result("image", payload, call)

    def query(self, task_id, *, kind, request_id):
        model = self._model(kind)
        if not self._safe(task_id):
            raise GatewayError("gateway_query_unavailable", status="unknown")
        if kind == "image" and not self.image_status_path:
            raise GatewayError("gateway_query_unconfigured", status="unknown",
                               evidence={"task_id": task_id, "model": model, "request_id": request_id})
        path = f"/responses/{task_id}" if kind == "plan" else self.image_status_path.format(id=task_id)
        try:
            payload, evidence = self._request(kind, path, request_id)
        except GatewayError as error:
            raise GatewayError(error.code, status="unknown",
                               evidence={**error.evidence, "task_id": task_id}) from None
        return self._result(kind, payload, {**evidence, "task_id": task_id})

    def _result(self, kind, payload, evidence):
        task_id = self._safe(payload.get("id") or payload.get("task_id")) or evidence.get("task_id")
        provider_state = self._safe(payload.get("status"))
        usage = payload.get("usage")
        usage = {key: usage[key] for key in KNOWN_USAGE if type(usage.get(key)) is int and usage[key] >= 0} if isinstance(usage, dict) else None
        evidence = {**evidence, "task_id": task_id, "provider_state": provider_state, "usage": usage,
                    "billing_state": "usage_reported" if usage else "not_reported", "actual_cost": None}
        if provider_state in ("failed", "cancelled", "canceled", "expired", "incomplete") or payload.get("error"):
            raise GatewayError("gateway_provider_failed", evidence=evidence)
        if provider_state in ("pending", "queued", "running", "processing", "in_progress"):
            return {**evidence, "status": "pending" if task_id else "unknown"}
        returned_model = payload.get("model")
        if returned_model and returned_model != self._model(kind):
            # Some gateways include a provider-specific effective version. Treat any mismatch as unverified.
            evidence["returned_model"] = self._safe(returned_model)
            raise GatewayError("gateway_model_mismatch", evidence=evidence)
        if kind == "plan":
            output = payload.get("output")
            text = []
            if isinstance(output, list):
                for entry in output:
                    content = entry.get("content") if isinstance(entry, dict) else None
                    if isinstance(content, list):
                        text.extend(part["text"] for part in content if isinstance(part, dict)
                                    and part.get("type") == "output_text" and isinstance(part.get("text"), str))
            if text:
                return {**evidence, "status": "succeeded", "text": "".join(text)}
        else:
            data = payload.get("data")
            if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
                item = data[0]
                raw = item.get("b64_json")
                if isinstance(raw, str) and len(raw) <= (IMAGE_LIMIT * 4 // 3) + 4:
                    try:
                        image = base64.b64decode(raw, validate=True)
                        if len(image) > IMAGE_LIMIT:
                            raise ValueError
                        mime = _media_type(image)
                    except (binascii.Error, ValueError, GatewayError):
                        raise GatewayError("gateway_invalid_image", evidence=evidence) from None
                    return {**evidence, "status": "succeeded", "image": image, "mime": mime}
                if item.get("url"):
                    raise GatewayError("gateway_external_image_url", evidence=evidence)
        return {**evidence, "status": "unknown"}
