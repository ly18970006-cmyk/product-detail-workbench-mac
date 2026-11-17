import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import webbrowser
from contextlib import closing
from http.client import HTTPConnection, HTTPException as ConnectionError
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

import renderer
from render_queue import QueueConflict, QueueFailure, QueueFull, RenderQueue


ROOT = Path(__file__).resolve().parent
SERVICE = 'detail-title-tool'
WINDOWS_SAMPLE_ROOT = Path('E:/工作/规划落地对话专用素材')
PRODUCT_CODE = re.compile(r'[0-9]{8,16}\Z')
JOB_ID = re.compile(r'[a-f0-9]{32}\Z')
REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{8,80}\Z')
EMPLOYEE_ID = re.compile(r'[A-Za-z0-9_.@-]{1,128}\Z')
PREVIEW_FILES = ('before.png', 'after.png', 'preview.png', 'candidate.psd')
DRAFT_BASELINE_FILES = ('baseline.psd', 'baseline.png')
DRAFT_PREVIEW_FILES = ('preview.png', 'candidate.psd')
ARTIFACTS = {
    'before': ('before.png', 'image/png'),
    'after': ('after.png', 'image/png'),
    'preview': ('preview.png', 'image/png'),
    'psd': ('detail.psd', 'application/vnd.adobe.photoshop'),
    'png': ('detail.png', 'image/png'),
}


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def local_storage(mode):
    config = ROOT.parent / 'detail-page-storage.local.json'
    if os.name == 'nt' and mode == 'local' and config.is_file():
        return json.loads(config.read_text(encoding='utf-8-sig'))
    return {}


def local_archive_data(path, instance):
    archive = Path(path)
    if not archive.is_absolute() or not archive.is_dir():
        raise RuntimeError(f'无法访问 E 盘工作台成果目录，请确认磁盘已挂载：{archive}')
    return archive / '运行数据' / instance


def configure_local_workbench(defaults, port):
    config = defaults.get('refresh_hero_lighting')
    overrides = ('DETAIL_DATA_DIR', 'DETAIL_ASSET_ROOT', 'DETAIL_REFRESH_APP_ROOT',
                 'REFRESH_V2_DATA', 'REFRESH_V2_ASSETS', 'REFRESH_V2_LOCAL_LAYOUTS')
    if not config or port != defaults['workbench_port'] or any(os.environ.get(name) for name in overrides):
        return

    layouts = Path(config['layouts'])
    if not layouts.is_absolute() or not layouts.is_file():
        print('找不到首屏光影工作台登记文件。', file=sys.stderr, flush=True)
        return
    os.environ['REFRESH_V2_LOCAL_LAYOUTS'] = str(layouts)

    gateway_overrides = ('REFRESH_V2_GATEWAY_CONFIG', 'REFRESH_V2_GATEWAY_BASE_URL',
                         'REFRESH_V2_GATEWAY_LAYOUT_TOKEN', 'REFRESH_V2_GATEWAY_IMAGE_TOKEN',
                         'REFRESH_V2_GATEWAY_IMAGE_MODEL')
    gateway_config = Path(config['gateway_config'])
    if any(os.environ.get(name) for name in gateway_overrides):
        return
    if not gateway_config.is_absolute() or not gateway_config.is_file():
        print('找不到公司网关的本机配置文件。', file=sys.stderr, flush=True)
        return
    os.environ['REFRESH_V2_GATEWAY_CONFIG'] = str(gateway_config)
    os.environ['REFRESH_V2_GATEWAY_IMAGE_MODEL'] = 'gpt-image-2.5-sunburst'
    os.environ['REFRESH_V2_HERO_LIGHTING_CODES'] = config['sku']
    os.environ['REFRESH_V2_REFERENCE_EDIT_LIMIT'] = str(config['limit'])


def create_app():
    app = Flask(__name__, static_folder='static', static_url_path='/static')
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024
    mode = os.environ.get('DETAIL_MODE', 'local')
    workbench_enabled = os.environ.get('DETAIL_WORKBENCH') == '1'
    if mode not in {'local', 'proxy'}:
        raise RuntimeError('DETAIL_MODE must be local or proxy.')
    proxy_token = os.environ.get('DETAIL_PROXY_TOKEN', '')
    origin = os.environ.get('DETAIL_PUBLIC_ORIGIN', '').rstrip('/')
    if mode == 'proxy' and (len(proxy_token) < 32 or not origin.startswith('https://')):
        raise RuntimeError('Proxy mode requires DETAIL_PROXY_TOKEN and an HTTPS DETAIL_PUBLIC_ORIGIN.')

    configured_data = os.environ.get('DETAIL_DATA_DIR')
    refresh_data = None
    defaults = local_storage(mode)
    if not configured_data and defaults:
        instance = '统一工作台' if workbench_enabled else '独立改字'
        configured_data = local_archive_data(defaults['edit'], instance)
        if workbench_enabled:
            refresh_data = os.environ.get('REFRESH_V2_DATA') or local_archive_data(defaults['refresh'], instance)
    data = Path(configured_data or ROOT / '.data').resolve()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    jobs = data / 'jobs'
    jobs.mkdir(exist_ok=True, mode=0o700)
    drafts = data / 'drafts'
    drafts.mkdir(exist_ok=True, mode=0o700)
    db = data / 'jobs.sqlite3'
    with closing(sqlite3.connect(db)) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, code TEXT NOT NULL,
            title TEXT NOT NULL, receipt TEXT NOT NULL, evidence TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'preview', result TEXT
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, code TEXT NOT NULL,
            evidence TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
            attempted_revision INTEGER NOT NULL DEFAULT 0, preview_job TEXT
        )''')
        connection.commit()

    roots = {
        'assets': Path(os.environ.get('DETAIL_ASSET_ROOT', WINDOWS_SAMPLE_ROOT
                                     if WINDOWS_SAMPLE_ROOT.is_dir() else ROOT / 'assets')),
        'workspace': Path(os.environ.get('DETAIL_WORKSPACE_ROOT', ROOT.parent)),
        'external': Path(os.environ.get('DETAIL_EXTERNAL_ROOT', Path.home() / 'Documents' / 'Codex')),
        'tool': ROOT,
    }
    roots = {name: path.resolve() for name, path in roots.items()}
    refresh = None
    if workbench_enabled:
        from refresh_adapter import RefreshAdapter

        refresh = RefreshAdapter(refresh_data or data / 'refresh-v2', roots['assets'] / '产品编码测验')
        refresh.register(app)
    catalog_path = Path(os.environ.get('DETAIL_CATALOG', ROOT / 'catalog.json')).resolve()
    local_cli = roots['workspace'] / 'photocraft-trial' / 'app' / 'photocraft-cli.exe'
    default_cli = shutil.which('photocraft-cli') or (local_cli if local_cli.is_file() else 'photocraft-cli')
    cli = Path(os.environ.get('DETAIL_RENDER_CLI', default_cli))
    render_queue = RenderQueue(
        workers=int(os.environ.get('DETAIL_RENDER_CONCURRENCY', '1')),
        capacity=int(os.environ.get('DETAIL_QUEUE_CAPACITY', '16')),
        per_owner=int(os.environ.get('DETAIL_QUEUE_PER_OWNER', '3')),
        wait_timeout=float(os.environ.get('DETAIL_QUEUE_WAIT_TIMEOUT', '120')),
    )
    publish_lock = threading.Lock()
    submission_lock = threading.Lock()

    def connection():
        if 'db' not in g:
            g.db = sqlite3.connect(db, timeout=10)
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_connection(_):
        database = g.pop('db', None)
        if database is not None:
            database.close()

    @app.before_request
    def identify():
        if request.remote_addr not in {'127.0.0.1', '::1'}:
            abort(403, '服务只接受本机浏览器或同机企业代理。')
        if mode == 'proxy':
            provided = request.headers.get('X-Detail-Proxy-Token', '')
            if not hmac.compare_digest(provided.encode('utf-8'), proxy_token.encode('utf-8')):
                abort(403, '需要通过企业登录入口访问。')
            employee = request.headers.get('X-Detail-User', '')
            if not EMPLOYEE_ID.fullmatch(employee):
                abort(403, '代理未提供有效的员工标识。')
            g.owner = employee
            expected_origin = origin
        else:
            if request.host.split(':')[0] not in {'127.0.0.1', 'localhost'}:
                abort(403, '请通过本机启动地址访问。')
            g.owner = 'local'
            expected_origin = f'http://{request.host}'
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            source = request.headers.get('Origin')
            if request.headers.get('Sec-Fetch-Site') == 'cross-site' or source and source != expected_origin:
                abort(403, '请求来源与当前入口不一致。')

    @app.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.errorhandler(HTTPException)
    def api_error(error):
        return jsonify(error=error.description), error.code

    def catalog():
        try:
            with catalog_path.open(encoding='utf-8-sig') as source:
                return json.load(source)['products']
        except (OSError, KeyError, ValueError):
            app.logger.exception('Cannot read product catalog')
            abort(503, '商品目录暂不可用。')

    def asset(reference):
        try:
            root = roots[reference['root']]
            name = Path(reference['path'])
            resolved = (root / name).resolve(strict=True)
            if name.is_absolute() or not resolved.is_relative_to(root) or not resolved.is_file():
                raise ValueError('asset is outside configured root')
            return resolved
        except (OSError, KeyError, ValueError):
            abort(409, '该商品的母版或瓶图尚未就绪。')

    def product(code):
        if not isinstance(code, str) or not PRODUCT_CODE.fullmatch(code):
            abort(400, '请输入完整商品编码。')
        selected = catalog().get(code)
        if selected is None:
            abort(404, '该编码尚未登记可编辑母版。')
        return selected

    def describe(code, selected):
        spec = selected['render']
        return dict(code=code, name=selected['name'], width=spec.get('expected_width'),
                    original_title=selected.get('original_title', ''),
                    sample_title=selected.get('sample_title', ''),
                    bottle=Path(selected['bottle']['path']).name,
                    bottle_url=f'/api/products/{code}/bottle',
                    template=Path(selected['template']['path']).name,
                    note=selected.get('note', ''))

    def render_inputs(selected):
        source_psd = asset(selected['template'])
        bottle = asset(selected['bottle'])
        spec = dict(selected['render'], source_psd=str(source_psd), product_image=str(bottle))
        source_files = {'template': source_psd, 'bottle': bottle}
        if spec.get('scene_replacement'):
            scene = asset(spec['scene_replacement']['image'])
            spec['scene_replacement'] = dict(spec['scene_replacement'], image=str(scene))
            source_files['scene'] = scene
        return spec, source_files

    def fingerprints(files):
        return {name: digest(path) for name, path in files.items()}

    def artifact_hashes(directory, names):
        return fingerprints({name: directory / name for name in names})

    def artifacts_match(directory, expected):
        return all((directory / name).is_file() and digest(directory / name) == sha
                   for name, sha in expected.items())

    def draft_row(draft_id):
        if not isinstance(draft_id, str) or not JOB_ID.fullmatch(draft_id):
            abort(404, '找不到该草稿。')
        row = connection().execute('SELECT * FROM drafts WHERE id=? AND owner=?',
                                   (draft_id, g.owner)).fetchone()
        if row is None:
            abort(404, '找不到该草稿。')
        return row

    def field_list(fields):
        return [dict(id=field['id'], path=field['path'], name=field['path'][-1],
                     screen=field['screen'], text=field['text'], bounds=field['bounds']) for field in fields]

    def baseline_url(draft_id):
        return f'/api/drafts/{draft_id}/files/before'

    def draft_response(draft_id, code, evidence, revision=0):
        report = evidence['baseline']
        url = baseline_url(draft_id)
        body = dict(id=draft_id, code=code, state='draft', revision=revision,
                    width=report['width'], height=report['height'], fields=field_list(report['fields']),
                    before_url=url, preview_url=url, changes=[])
        if 'source' in evidence:
            body['source'] = evidence['source']
        return body

    def request_revision(body):
        revision = body.get('revision') if isinstance(body, dict) else None
        if type(revision) is not int or not 0 < revision <= 2**53 - 1:
            abort(400, '草稿版本无效，请重新查找商品。')
        return revision

    def advance_draft(draft_id, revision, attempt):
        database = connection()
        with publish_lock, database:
            row = draft_row(draft_id)
            if revision < row['revision'] or attempt and revision <= row['attempted_revision']:
                abort(409, '草稿已有更新，请等待最新预览。')
            if not attempt and revision == row['revision']:
                return row
            database.execute('UPDATE drafts SET revision=?, attempted_revision=?, preview_job=NULL WHERE id=?',
                             (revision, revision if attempt else row['attempted_revision'], draft_id))
            return row

    def reserve_preview(draft_id, revision):
        row = advance_draft(draft_id, revision, attempt=False)
        render_queue.supersede(g.owner, draft_id, before_revision=revision)
        return row

    def job_row(job_id):
        if not JOB_ID.fullmatch(job_id):
            abort(404, '找不到该任务。')
        row = connection().execute('SELECT * FROM jobs WHERE id=? AND owner=?',
                                   (job_id, g.owner)).fetchone()
        if row is None:
            abort(404, '找不到该任务。')
        return row

    def job_response(row):
        base = f"/api/jobs/{row['id']}"
        evidence = json.loads(row['evidence'])
        body = dict(id=row['id'], code=row['code'], title=row['title'], state=row['state'],
                    receipt=row['receipt'], before_url=f'{base}/files/before',
                    after_url=f'{base}/files/after', preview_url=f'{base}/files/preview',
                    checks=evidence['checks'])
        if 'draft_id' in evidence:
            body.update(draft_id=evidence['draft_id'], revision=evidence['revision'],
                        before_url=baseline_url(evidence['draft_id']), after_url=body['preview_url'],
                        fields=field_list(evidence['checks']['fields']), changes=evidence['checks']['changes'])
            body['checks'] = {key: value for key, value in evidence['checks'].items() if key != 'fields'}
        if 'source' in evidence:
            body['source'] = evidence['source']
        if row['state'] == 'complete':
            body.update(psd_url=f'{base}/files/psd', png_url=f'{base}/files/png',
                        result=json.loads(row['result']))
        return body

    def save_job(job_id, code, title, evidence):
        connection().execute('INSERT INTO jobs (id,owner,code,title,receipt,evidence) VALUES (?,?,?,?,?,?)',
                             (job_id, g.owner, code, title, secrets.token_urlsafe(32),
                              json.dumps(evidence, ensure_ascii=False)))

    def render_error(error):
        app.logger.warning('Render failed: %s', error)
        status = 504 if isinstance(error, renderer.RenderTimeout) else 422
        return QueueFailure(str(error), status=status, field_id=error.field_id if status == 422 else None)

    def run_immediate(operation):
        try:
            with render_queue.immediate():
                result, status = operation()
                return jsonify(result), status
        except QueueFull as error:
            abort(429, str(error))
        except (renderer.RenderError, QueueFailure) as error:
            failure = render_error(error) if isinstance(error, renderer.RenderError) else error
            payload = {'error': str(failure)}
            if failure.field_id is not None:
                payload['field_id'] = failure.field_id
            return jsonify(payload), failure.status

    @app.get('/')
    def index():
        return app.send_static_file('workbench.html' if workbench_enabled else 'index.html')

    @app.get('/api/health')
    def health():
        return jsonify(service=SERVICE, render_queue=True, workbench=workbench_enabled)

    @app.get('/api/products')
    def list_products():
        return jsonify(products=[dict(code=code, name=entry['name'])
                                 for code, entry in catalog().items()])

    @app.get('/api/products/<code>')
    def get_product(code):
        selected = product(code)
        asset(selected['template'])
        asset(selected['bottle'])
        return jsonify(describe(code, selected))

    @app.get('/api/products/<code>/bottle')
    def get_bottle(code):
        return send_file(asset(product(code)['bottle']), conditional=True)

    if workbench_enabled:
        from workbench_catalog import describe_product, list_products, local_product_bottle

        @app.get('/api/workbench/products')
        def workbench_products():
            return jsonify(products=list_products(roots['assets'], catalog()))

        @app.get('/api/workbench/products/<code>')
        def workbench_product(code):
            if not PRODUCT_CODE.fullmatch(code):
                abort(400, '请输入完整商品编码。')
            registered = catalog().get(code)
            description = edit_error = None
            if registered is not None:
                try:
                    render_inputs(registered)
                    description = describe(code, registered)
                except HTTPException as error:
                    edit_error = error.description
            refresh_status = {'available': refresh.available, 'discovery': refresh.discovery(g.owner, code),
                              'registration': refresh.registration(g.owner, code)}
            report = describe_product(code, roots['assets'], registered, description, refresh_status, edit_error)
            if report is None:
                abort(404, '该编码在素材目录或工作台中没有记录。')
            report['capabilities']['refresh']['company_scope'] = {
                action: code in {entry.strip() for entry in os.environ.get(variable, '').split(',')}
                for action, variable in (('plan', 'REFRESH_V2_COMPANY_OLD_PAGE_CODES'),
                                         ('preview', 'REFRESH_V2_COMPANY_SCENE_CODES'))
            }
            return jsonify(report)

        @app.get('/api/workbench/products/<code>/bottle')
        def workbench_bottle(code):
            path = local_product_bottle(code, roots['assets']) if PRODUCT_CODE.fullmatch(code) else None
            if path is None:
                abort(404, '该编码没有可读取的商品瓶图。')
            return send_file(path, conditional=True)

    @app.post('/api/drafts')
    def create_draft():
        body = request.get_json()
        code = body.get('code') if isinstance(body, dict) else None
        selected = product(code)
        return run_immediate(lambda: render_baseline(code, selected))

    def render_baseline(code, selected):
        spec, source_files = render_inputs(selected)
        inputs = fingerprints(source_files)
        if selected.get('template_sha256', inputs['template']) != inputs['template']:
            abort(409, 'PSD 母版版本与登记不一致，请联系维护人员核对。')
        with tempfile.TemporaryDirectory(prefix='.baseline-', dir=drafts) as temporary:
            stage = Path(temporary)
            report = renderer.prepare_draft(cli, stage, spec)
            if inputs != fingerprints(source_files):
                abort(409, '加载期间商品素材已更新，请重新查找。')
            return store_draft(code, stage, spec, inputs, report)

    def render_handoff(code, job_id, revision):
        from refresh_handoff import draft_spec

        with tempfile.TemporaryDirectory(prefix='.handoff-', dir=drafts) as temporary:
            stage = Path(temporary)
            manifest, text_layouts = refresh.copy_confirmed(g.owner, code, job_id, revision, stage)
            try:
                spec = draft_spec(manifest, code, job_id, revision, text_layouts)
            except ValueError as error:
                abort(409, str(error))
            psd = stage / manifest['assets']['psd']['file']
            png = stage / manifest['assets']['png']['file']
            inputs = fingerprints({'psd': psd, 'png': png})
            if any(inputs[kind] != manifest['assets'][kind]['sha256'] for kind in inputs):
                abort(409, '翻新确认文件与摘要不一致。')
            report = renderer.prepare_confirmed_draft(cli, stage, spec, psd, png)
            source = dict(kind='refresh', job_id=job_id, revision=revision,
                          approval_status=manifest['approval_status'], assets=manifest['assets'],
                          versions=manifest['source'])
            return store_draft(code, stage, spec, inputs, report, source)

    def store_draft(code, stage, spec, inputs, report, source=None):
        draft_id = secrets.token_hex(16)
        directory = drafts / draft_id
        baseline = artifact_hashes(stage, DRAFT_BASELINE_FILES)
        directory.mkdir()
        for name in DRAFT_BASELINE_FILES:
            os.replace(stage / name, directory / name)
        evidence = dict(spec=spec, inputs=inputs, files=baseline, baseline=report)
        if source is not None:
            evidence['source'] = source
        with connection() as database:
            database.execute('INSERT INTO drafts (id,owner,code,evidence) VALUES (?,?,?,?)',
                             (draft_id, g.owner, code, json.dumps(evidence, ensure_ascii=False)))
        return draft_response(draft_id, code, evidence), 201

    @app.get('/api/drafts/<draft_id>/files/before')
    def draft_before(draft_id):
        draft_row(draft_id)
        return send_file(drafts / draft_id / 'baseline.png', mimetype='image/png', conditional=True)

    @app.post('/api/drafts/<draft_id>/revision')
    def invalidate_draft_preview(draft_id):
        revision = request_revision(request.get_json())
        with submission_lock:
            reserve_preview(draft_id, revision)
        return jsonify(revision=revision)

    @app.post('/api/drafts/<draft_id>/preview')
    def preview_draft(draft_id):
        request.max_content_length = 256 * 1024
        body = request.get_json()
        revision = request_revision(body)
        submitted = body.get('edits')
        with submission_lock:
            reserve_preview(draft_id, revision)
        return run_immediate(lambda: render_preview(draft_id, revision, submitted))

    def render_preview(draft_id, revision, submitted):
        job_id = secrets.token_hex(16)
        directory = jobs / job_id
        row = advance_draft(draft_id, revision, attempt=True)
        evidence = json.loads(row['evidence'])
        fields = evidence['baseline']['fields']
        originals = {field['id']: field['text'] for field in fields}
        if not isinstance(submitted, dict) or submitted.keys() - originals.keys():
            abort(400, '请选择当前母版可编辑的文字图层。')
        edits = {}
        for field_id, text in submitted.items():
            if not isinstance(text, str) or not text.strip() or len(text) > 512 or any(
                    not char.isprintable() and char != '\n' for char in text):
                raise QueueFailure('请输入可见文字，单个图层最多 512 字。', status=422, field_id=field_id)
            if text != originals[field_id]:
                edits[field_id] = text
        if not edits:
            return draft_response(draft_id, row['code'], evidence, revision), 200
        baseline = drafts / draft_id
        if not artifacts_match(baseline, evidence['files']):
            abort(409, '改前基准文件已变更，请重新查找商品。')
        with tempfile.TemporaryDirectory(prefix='.render-', dir=jobs) as temporary:
            stage = Path(temporary)
            checks = renderer.render_draft(cli, stage, evidence['spec'], baseline, edits, fields)
            preview = dict(draft_id=draft_id, revision=revision, checks=checks,
                           previews=artifact_hashes(stage, DRAFT_PREVIEW_FILES))
            if 'source' in evidence:
                preview['source'] = evidence['source']
            database = connection()
            with publish_lock, database:
                if draft_row(draft_id)['revision'] != revision:
                    raise QueueFailure('草稿已有更新，请等待最新预览。', status=409, superseded=True)
                directory.mkdir()
                for name in DRAFT_PREVIEW_FILES:
                    os.replace(stage / name, directory / name)
                save_job(job_id, row['code'], f'草稿-{draft_id[:8]}-v{revision}', preview)
                database.execute('UPDATE drafts SET preview_job=? WHERE id=?', (job_id, draft_id))
        return job_response(job_row(job_id)), 201

    @app.post('/api/jobs')
    def preview():
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400, '请提交商品编码和标题。')
        code = body.get('code')
        selected = product(code)
        title = body.get('title')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 40 or not title.isprintable():
            abort(400, '请输入 1–40 个可见字符的单行标题。')
        title = title.strip()
        spec, source_files = render_inputs(selected)
        return run_immediate(lambda: render_legacy(code, title, spec, source_files))

    def render_legacy(code, title, spec, source_files):
        job_id = secrets.token_hex(16)
        directory = jobs / job_id
        with tempfile.TemporaryDirectory(prefix='.legacy-', dir=jobs) as temporary:
            stage = Path(temporary)
            inputs = fingerprints(source_files)
            checks = renderer.prepare(cli, stage, spec, title)
            if inputs != fingerprints(source_files):
                abort(409, '生成期间商品素材已更新，请重新预览。')
            evidence = dict(spec=spec, inputs=inputs, checks=checks,
                            previews=artifact_hashes(stage, PREVIEW_FILES))
            directory.mkdir()
            for name in PREVIEW_FILES:
                os.replace(stage / name, directory / name)
        with connection():
            save_job(job_id, code, title, evidence)
        return job_response(job_row(job_id)), 201

    def queued_operation(owner, operation, *args):
        with app.app_context():
            g.owner = owner
            try:
                result, _ = operation(*args)
                return result
            except renderer.RenderError as error:
                raise render_error(error) from error
            except HTTPException as error:
                raise QueueFailure(error.description, status=error.code) from error

    def task_response(task):
        return dict(task, status_url=f"/api/render-tasks/{task['id']}")

    @app.post('/api/render-tasks')
    def submit_render_task():
        request.max_content_length = 256 * 1024
        body = request.get_json()
        if not isinstance(body, dict) or body.get('kind') not in ('draft', 'preview', 'refresh-edit'):
            abort(400, '请选择要执行的排版任务。')
        key = body.get('request_id')
        if not isinstance(key, str) or not REQUEST_ID.fullmatch(key):
            abort(400, '排版请求标识无效，请重试。')
        owner = g.owner
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        try:
            with submission_lock:
                existing = render_queue.replay(owner, key, fingerprint)
                if existing is not None:
                    return jsonify(task_response(existing)), 202
                draft_id = None
                revision = None
                if body['kind'] == 'draft':
                    code = body.get('code')
                    args = (code, product(code))
                    operation = render_baseline
                elif body['kind'] == 'refresh-edit':
                    if not workbench_enabled:
                        abort(404, '当前入口未启用翻新交接。')
                    code, job_id, source_revision = (body.get(key) for key in ('code', 'job_id', 'revision'))
                    refresh.check_confirmed(owner, code, job_id, source_revision)
                    args = (code, job_id, source_revision)
                    operation = render_handoff
                else:
                    draft_id = body.get('draft_id')
                    revision = request_revision(body)
                    submitted = body.get('edits')
                    row = reserve_preview(draft_id, revision)
                    if revision <= row['attempted_revision']:
                        abort(409, '该版本已开始排版，请使用当次任务或提交新版本。')
                    args = (draft_id, revision, submitted)
                    operation = render_preview
                task = render_queue.submit(owner, lambda: queued_operation(owner, operation, *args),
                                           key=key, fingerprint=fingerprint,
                                           coalesce=draft_id, revision=revision)
        except QueueFull as error:
            return jsonify(error=str(error)), 429, {'Retry-After': '3'}
        except QueueConflict as error:
            abort(409, str(error))
        return jsonify(task_response(task)), 202

    @app.get('/api/render-tasks/<task_id>')
    def get_render_task(task_id):
        task = render_queue.get(g.owner, task_id) if JOB_ID.fullmatch(task_id) else None
        if task is None:
            abort(404, '找不到排版任务或记录已过期，请重新提交。')
        return jsonify(task_response(task))

    @app.get('/api/jobs/<job_id>')
    def get_job(job_id):
        return jsonify(job_response(job_row(job_id)))

    @app.post('/api/jobs/<job_id>/confirm')
    def confirm(job_id):
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get('receipt'), str):
            abort(400, '请确认已查看当前预览。')
        row = job_row(job_id)
        if not hmac.compare_digest(body['receipt'].encode('utf-8'), row['receipt'].encode('utf-8')):
            abort(409, '预览版本已变更，请重新打开预览。')
        if row['state'] == 'complete':
            return jsonify(job_response(row))
        with publish_lock:
            row = job_row(job_id)
            if row['state'] == 'complete':
                return jsonify(job_response(row))
            directory = jobs / job_id
            evidence = json.loads(row['evidence'])
            if 'draft_id' in evidence:
                draft = draft_row(evidence['draft_id'])
                if draft['revision'] != evidence['revision'] or draft['preview_job'] != job_id:
                    abort(409, '文案已更新，请使用最新预览确认。')
                baseline = json.loads(draft['evidence'])
                if not artifacts_match(drafts / draft['id'], baseline['files']):
                    abort(409, '改前基准文件已变更，请重新查找商品。')
            if not artifacts_match(directory, evidence['previews']):
                abort(409, '预览文件已变化，请重新生成预览。')
            with tempfile.TemporaryDirectory(prefix='.publish-', dir=directory) as temporary:
                destination = Path(temporary)
                checks = {key: value for key, value in evidence['checks'].items() if key != 'fields'}
                result = renderer.publish(directory, destination, checks)
                if (digest(destination / 'detail.png') != evidence['previews']['preview.png']
                        or digest(destination / 'detail.psd') != evidence['previews']['candidate.psd']):
                    abort(409, '导出的文件与确认稿不一致。')
                for name in ('detail.psd', 'detail.png'):
                    os.replace(destination / name, directory / name)
            database = connection()
            database.execute("UPDATE jobs SET state='complete', result=? WHERE id=? AND owner=?",
                             (json.dumps(result, ensure_ascii=False), job_id, g.owner))
            database.commit()
            return jsonify(job_response(job_row(job_id)))

    @app.get('/api/jobs/<job_id>/files/<kind>')
    def download(job_id, kind):
        row = job_row(job_id)
        if kind not in ARTIFACTS or kind in {'psd', 'png'} and row['state'] != 'complete':
            abort(404, '该文件尚未开放下载。')
        if kind in {'before', 'after'}:
            evidence = json.loads(row['evidence'])
            if 'draft_id' in evidence:
                if kind == 'before':
                    return draft_before(evidence['draft_id'])
                kind = 'preview'
        filename, mime = ARTIFACTS[kind]
        return send_file(jobs / job_id / filename, mimetype=mime, conditional=True,
                         as_attachment=kind in {'psd', 'png'},
                         download_name=f"{row['code']}-{row['title']}.{kind}" if kind in {'psd', 'png'} else None)

    if mode == 'local' and workbench_enabled and os.environ.get('DETAIL_WEB_TRIAL_DIR'):
        from web_trial import register
        register(app, os.environ['DETAIL_WEB_TRIAL_DIR'])
    return app


def already_running(port):
    probe = HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        probe.request('GET', '/api/health')
        result = probe.getresponse()
        return result.status == 200 and json.loads(result.read(1024)).get('service') == SERVICE
    except (OSError, ValueError, AttributeError, ConnectionError):
        return False
    finally:
        probe.close()


if __name__ == '__main__':
    from waitress import create_server

    mode = os.environ.get('DETAIL_MODE', 'local')
    defaults = local_storage(mode) if os.environ.get('DETAIL_WORKBENCH') == '1' else {}
    default_port = 8768
    if defaults and not os.environ.get('DETAIL_DATA_DIR'):
        default_port = defaults['workbench_port']
    port = int(os.environ.get('DETAIL_PORT', default_port))
    url = f'http://127.0.0.1:{port}/'
    open_browser = '--open-browser' in sys.argv and os.environ.get('DETAIL_MODE', 'local') == 'local'
    if open_browser and already_running(port):
        webbrowser.open(url)
        sys.exit(0)
    configure_local_workbench(defaults, port)
    server = create_server(create_app(), host='127.0.0.1', port=port, threads=8)
    print(f'详情页改字工具 {url}', flush=True)
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    server.run()
