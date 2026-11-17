"""Mount the owner-scoped refresh API behind the workbench's identity boundary."""

import hashlib
import importlib
import logging
import os
import re
import secrets
import sys
from pathlib import Path

from flask import abort, g, jsonify, request


PREFIX = '/api/refresh'
INTERNAL_ORIGIN = 'https://refresh-internal.invalid'
CODE = r'[0-9]{10,16}'
JOB = r'[a-f0-9]{32}'
REVISION = r'[a-f0-9]{64}'
PUBLIC_ROUTES = {
    'GET': re.compile(fr'(?:health|products/{CODE}(?:/(?:candidates(?:/(?:old_pages|bottles|logos)/{REVISION}/image)?|old-page|jobs))?|jobs/{JOB}(?:/files/(?:preview\.png|(?:baseline-)?comparison\.png|s[0-9]{{2}}-candidate\.png|detail\.psd|detail\.png|checks\.json))?)\Z'),
    'POST': re.compile(fr'(?:products/{CODE}/(?:registrations|plans)|previews|jobs/{JOB}/(?:confirm|query))\Z'),
}
APP_ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)


class RefreshAdapter:
    def __init__(self, data_root, asset_root):
        self.app = None
        self.proxy_token = secrets.token_urlsafe(32)
        self.handoff_token = secrets.token_urlsafe(32)
        bundled = APP_ROOT / 'detail-page-refresh'
        default = bundled if bundled.is_dir() else APP_ROOT.parent / 'detail-page-refresh'
        root = Path(os.environ.get('DETAIL_REFRESH_APP_ROOT', default)).resolve()
        if not (root / 'refresh_service.py').is_file():
            return
        try:
            # Load the trusted sibling as a private WSGI app. Its imports resolve
            # there, and the legacy edit service keeps its existing module path.
            sys.path.insert(0, str(root))
            try:
                service = importlib.import_module('refresh_service')
                # Health and company tasks import the gateway lazily inside
                # the sibling. Load it while its deployment root is visible.
                importlib.import_module('refresh_gateway')
            finally:
                sys.path.pop(0)
            if Path(service.__file__).resolve().parent != root:
                raise ImportError('Unexpected refresh service module root')
            self.app = self._create(service, data_root, asset_root)
        except (ImportError, OSError, ValueError, RuntimeError):
            LOGGER.exception('Refresh service unavailable')

    def _create(self, service, data_root, asset_root):
        # The native service captures this value at construction. It stays
        # separate from the public proxy token and never reaches the browser.
        name = 'REFRESH_V2_HANDOFF_TOKEN'
        prior = os.environ.get(name)
        os.environ[name] = self.handoff_token
        try:
            return service.create_app(
                data_root=data_root,
                asset_root=os.environ.get('REFRESH_V2_ASSETS', asset_root),
                proxy_token=self.proxy_token,
                public_origin=INTERNAL_ORIGIN,
            )
        finally:
            if prior is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prior

    @property
    def available(self):
        return self.app is not None

    def _request(self, owner, route, *, method='GET', data=None, query=None, key=None, handoff=False):
        if self.app is None:
            abort(503, '翻新服务尚未就绪，请联系维护人员。')
        headers = {'X-Detail-User': owner, 'X-Refresh-Proxy-Token': self.proxy_token}
        if handoff:
            headers['X-Refresh-Handoff-Token'] = self.handoff_token
        if key is not None:
            headers['Idempotency-Key'] = key
        client = self.app.test_client(use_cookies=False)
        return client.open(PREFIX + '/' + route, method=method, query_string=query,
                           data=data, content_type='application/json' if data is not None else None,
                           headers=headers, environ_overrides={'REMOTE_ADDR': '127.0.0.1'})

    @staticmethod
    def _public_task(task):
        result = task.get('result')
        if isinstance(result, dict):
            result.pop('handoff', None)

    def register(self, app):
        @app.route(PREFIX + '/<path:route>', methods=['GET', 'POST'])
        def refresh_api(route):
            method = request.method
            pattern = PUBLIC_ROUTES['GET' if method == 'HEAD' else method]
            if not pattern.fullmatch(route):
                abort(404, '找不到翻新接口。')
            request.max_content_length = 256 * 1024
            body = request.get_data() if method == 'POST' else None
            response = self._request(g.owner, route, method=method, data=body, query=request.args,
                                     key=request.headers.get('Idempotency-Key'))
            if response.status_code >= 500:
                response.close()
                abort(503, '翻新服务暂时不可用，请稍后重试。')
            if response.is_json and method != 'HEAD':
                with response:
                    payload = response.get_json()
                    if isinstance(payload, dict):
                        self._public_task(payload)
                        for task in payload.get('jobs', ()):
                            self._public_task(task)
                    return jsonify(payload), response.status_code
            return response

    def _json(self, owner, route, *, handoff=False, missing=False):
        with self._request(owner, route, handoff=handoff) as response:
            if missing and response.status_code == 404:
                return None
            if response.status_code >= 500:
                abort(503, '翻新服务暂时不可用，请稍后重试。')
            if response.status_code != 200:
                abort(response.status_code, '无法读取该员工的翻新版本。')
            # The native endpoint may send JSON as an attachment.
            payload = response.get_json(force=True, silent=True)
            if not isinstance(payload, dict):
                abort(409, '翻新服务返回的版本数据无效。')
            return payload

    def registration(self, owner, code):
        return self._json(owner, f'products/{code}', missing=True) if self.available and re.fullmatch(CODE, code) else None

    def discovery(self, owner, code):
        return self._json(owner, f'products/{code}/candidates', missing=True) if self.available and re.fullmatch(CODE, code) else None

    def check_confirmed(self, owner, code, job_id, revision):
        if (not isinstance(code, str) or not re.fullmatch(CODE, code)
                or not isinstance(job_id, str) or not re.fullmatch(JOB, job_id)
                or not isinstance(revision, str) or not re.fullmatch(REVISION, revision)):
            abort(400, '翻新交接需要有效的商品编码、确认任务和版本。')
        task = self._json(owner, f'jobs/{job_id}')
        if task.get('action') != 'confirm' or task.get('code') != code:
            abort(404, '找不到该商品的翻新确认稿。')
        result = task.get('result') or {}
        if (task.get('state') != 'succeeded' or result.get('revision') != revision
                or result.get('approval_status') != 'internal_layout_confirmed'):
            abort(409, '只有已确认且版本一致的翻新稿可以继续改字。')

    def copy_confirmed(self, owner, code, job_id, revision, stage):
        self.check_confirmed(owner, code, job_id, revision)
        manifest = self._json(owner, f'jobs/{job_id}/handoff', handoff=True)
        if (manifest.get('schema_version') != 1 or manifest.get('code') != code
                or manifest.get('job_id') != job_id or manifest.get('revision') != revision
                or manifest.get('approval_status') != 'internal_layout_confirmed'):
            abort(409, '翻新交接清单与确认版本不一致。')
        assets = manifest.get('assets')
        if not isinstance(assets, dict):
            abort(409, '翻新交接清单缺少确认文件。')
        for kind in ('psd', 'png'):
            item = assets.get(kind)
            name = f'detail.{kind}'
            if not isinstance(item, dict):
                abort(409, '翻新交接清单缺少确认文件。')
            digest = item.get('sha256')
            if item.get('file') != name or not isinstance(digest, str) or not re.fullmatch(REVISION, digest):
                abort(409, '翻新成稿引用不符合交接契约。')
            with self._request(owner, f'jobs/{job_id}/files/{name}') as response:
                if response.status_code != 200:
                    abort(409, '已确认的翻新文件暂不可用。')
                actual = hashlib.sha256()
                with (Path(stage) / name).open('xb') as target:
                    for block in response.iter_encoded():
                        actual.update(block)
                        target.write(block)
                if actual.hexdigest() != digest:
                    abort(409, '已确认的翻新文件与摘要不一致。')
        checks = self._json(owner, f'jobs/{job_id}/files/checks.json')
        try:
            layouts = [dict(path=[screen['group'], layer['name']], area=layer['area'])
                       for screen in checks['screens'] for layer in screen['layers'] if layer['kind'] == 'Type']
        except (KeyError, TypeError):
            abort(409, '翻新确认稿缺少有效的文字区记录。')
        return manifest, layouts
