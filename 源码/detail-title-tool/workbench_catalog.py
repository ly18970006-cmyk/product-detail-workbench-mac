"""Describe products from the material library and the two workbench modes."""

import re
from pathlib import Path


CODE = re.compile(r'[0-9]{8,16}\Z')
REFRESH_CODE = re.compile(r'[0-9]{10,16}\Z')
IMAGES = {'.png', '.jpg', '.jpeg', '.webp'}
MISSING_REFRESH = {
    'old_page': '缺少可读的旧详情长图',
    'bottle': '缺少已登记的透明瓶图',
    'operations': '缺少已登记的运营需求表',
    'logo': '缺少已登记的品牌标志',
    'packaging': '包装版本待核准',
    'channel': '渠道待核准',
    'brand_rule': '品牌规范待核准',
    'approved_copy': '缺少核准文案',
}


def _library(asset_root):
    return Path(asset_root).resolve() / '产品编码测验'


def _children(directory):
    try:
        if directory.resolve() != directory:
            return []
        return sorted((child for child in directory.iterdir() if child.resolve() == child), key=lambda child: child.name)
    except OSError:
        return []


def _files(directory, suffixes):
    return [file for file in _children(directory) if file.suffix.lower() in suffixes and file.is_file()]


def _sheet_name(code, operations):
    match = re.fullmatch(rf'{code}[-－_](.+?)[-－_]详情页运营需求表', operations.stem)
    return match[1].strip() if match else None


def _library_assets(code, asset_root):
    if not isinstance(code, str) or not CODE.fullmatch(code):
        return None
    directory = _library(asset_root) / code
    if directory.resolve() != directory or not directory.is_dir():
        return None
    sheets = _files(directory, {'.xlsx'})
    names = {name for sheet in sheets if (name := _sheet_name(code, sheet))}
    return {
        'name': next(iter(names)) if len(names) == 1 else None,
        'old_pages': _files(directory, IMAGES) + _files(directory / '详情页', IMAGES),
        'bottles': _files(directory / '产品照片', IMAGES),
        'operations': sheets,
        'logos': _files(directory / '品牌标志', IMAGES),
        'psds': _files(directory, {'.psd'}),
    }


def local_product_bottle(code, asset_root):
    """Show a library reference only when the code has one image candidate."""
    files = (_library_assets(code, asset_root) or {}).get('bottles', [])
    return files[0] if len(files) == 1 else None


def list_products(asset_root, registered):
    library_codes = {item.name for item in _children(_library(asset_root)) if CODE.fullmatch(item.name) and item.is_dir()}
    products = []
    for code in sorted(library_codes | registered.keys()):
        name = registered.get(code, {}).get('name')
        if not name and code in library_codes:
            name = (_library_assets(code, asset_root) or {}).get('name')
        products.append({'code': code, 'name': name or f'商品 {code}'})
    return products


def _refresh_capability(code, status, materials):
    discovery = status.get('discovery')
    registration = status.get('registration')
    if discovery is not None:
        missing = set(discovery.get('missing', []))
    else:
        missing = {key for key, group in (('old_page', 'old_pages'), ('bottle', 'bottles'),
                                           ('operations', 'operations'), ('logo', 'logos')) if not materials[group]}
    if registration is not None:
        missing.update(registration.get('missing', []))
    if not registration or not registration.get('approved_copy'):
        missing.add('approved_copy')
    available = bool(status.get('available') and discovery is not None and 'old_page' not in missing)
    if not REFRESH_CODE.fullmatch(code):
        state, reason = 'unsupported_code', '当前翻新登记支持 10–16 位编码。'
    elif not status.get('available'):
        state, reason = 'service_unavailable', '翻新服务暂不可用。'
    elif 'old_page' in missing:
        state, reason = 'missing_old_page', '缺少可读的旧详情长图。'
    elif discovery is None:
        state, reason = 'missing_discovery', '翻新服务未找到该编码的素材目录。'
    elif registration is None:
        state, reason = 'registration_required', '可登记旧页并预览。登记资料及核准文案后可继续。'
    else:
        state, reason = 'ready', '可预览和确认翻新版式。'
    return {'available': available, 'state': state, 'reason': reason,
            'missing': sorted(missing), 'missing_reasons': [MISSING_REFRESH[key] for key in MISSING_REFRESH if key in missing],
            'registered': registration is not None}


def describe_product(code, asset_root, registered=None, edit_description=None, refresh_status=None, edit_error=None):
    status = refresh_status or {}
    discovery = status.get('discovery')
    registration = status.get('registration')
    assets = _library_assets(code, asset_root)
    if assets is None and registered is None and discovery is None and registration is None:
        return None
    materials = {key: [file.name for file in (assets or {}).get(key, [])]
                 for key in ('old_pages', 'bottles', 'operations', 'logos', 'psds')}
    if discovery is not None:
        for key in ('old_pages', 'bottles', 'operations', 'logos'):
            materials[key] = [candidate['name'] for candidate in discovery.get(key, [])]

    name = ((registered or {}).get('name') or (registration or {}).get('name') or
            (discovery or {}).get('name') or (assets or {}).get('name') or f'商品 {code}')
    if edit_description is not None:
        edit = {'available': True, 'state': 'ready', 'source': 'registered_master',
                'reason': '可编辑 PSD 的可见文字层。'}
    elif registered is not None:
        edit = {'available': False, 'state': 'registered_asset_unavailable',
                'reason': edit_error or '已登记的改字素材暂不可用。'}
    elif materials['psds']:
        edit = {'available': False, 'state': 'psd_unverified', 'reason': '有 PSD 文件，待校验和登记可编辑文字层。'}
    elif materials['old_pages']:
        edit = {'available': False, 'state': 'old_image_only', 'reason': '已有旧详情长图，缺可编辑 PSD 母版。'}
    else:
        edit = {'available': False, 'state': 'missing_psd', 'reason': '缺可编辑 PSD 母版。'}

    report = {'code': code, 'name': name, 'materials': materials,
              'capabilities': {'edit': edit, 'refresh': _refresh_capability(code, status, materials)}}
    if edit_description:
        report.update(edit_description)
    else:
        selected = (registration or {}).get('selection', {}).get('bottle')
        candidates = (discovery or {}).get('bottles', [])
        candidate = (next((item for item in candidates if selected and item['id'] == selected['id']), None)
                     or next((item for item in candidates if item.get('transparent')), None))
        if candidate and candidate.get('image_url', '').startswith(f'/api/refresh/products/{code}/candidates/bottles/'):
            report.update(bottle=candidate['name'], bottle_url=candidate['image_url'],
                          bottle_is_candidate=not selected or selected['id'] != candidate['id'])
        elif (bottle := local_product_bottle(code, asset_root)) is not None:
            report.update(bottle=bottle.name, bottle_url=f'/api/workbench/products/{code}/bottle')
    return report
