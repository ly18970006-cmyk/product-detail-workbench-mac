"""Stage an independent server bundle containing only configured production inputs."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import tempfile


ROOT = Path(__file__).resolve().parent
SAMPLE_ASSETS = Path('E:/工作/规划落地对话专用素材')
APPLICATION_FILES = (
    'app.py', 'renderer.py', 'render_queue.py', 'psd_text_alignment.py', 'requirements.txt', 'Start.cmd',
    'static/index.html', 'static/style.css', 'static/app.js',
)
WORKBENCH_FILES = (
    'Start-Workbench.cmd', 'workbench_catalog.py', 'refresh_adapter.py', 'refresh_handoff.py',
    'static/workbench.html', 'static/workbench.js', 'static/workbench.css',
    'WORKBENCH_API.md', 'WORKBENCH_REFRESH.md',
)
REFRESH_FILES = (
    'refresh_service.py', 'refresh_pipeline.py', 'refresh_store.py', 'refresh_storage.py', 'refresh_gateway.py',
    'refresh_import.py', 'refresh_layout.py', 'render.py', 'backgrounds.py', 'REFRESH_API.md',
    'templates/refresh-rules-v1.json', 'templates/refresh-layout-schema-v1.json',
    'templates/refresh-model-prompt-v1.md',
)
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}


def source_roots():
    return {
        'assets': Path(os.environ.get('DETAIL_ASSET_ROOT', SAMPLE_ASSETS if SAMPLE_ASSETS.is_dir() else ROOT / 'assets')),
        'workspace': Path(os.environ.get('DETAIL_WORKSPACE_ROOT', ROOT.parent)),
        'external': Path(os.environ.get('DETAIL_EXTERNAL_ROOT', Path.home() / 'Documents' / 'Codex')),
        'tool': ROOT,
    }


def resolve_input(reference, roots, allowed_suffixes):
    root = roots[reference['root']].resolve()
    name = reference['path']
    relative = PurePosixPath(name)
    windows_relative = PureWindowsPath(name)
    if (relative.is_absolute() or windows_relative.anchor or '..' in relative.parts
            or '..' in windows_relative.parts or '\\' in name):
        raise ValueError(f'Asset must use a relative catalog path: {name}')
    path = root.joinpath(*relative.parts).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in allowed_suffixes:
        raise ValueError(f'Invalid configured input: {path}')
    if relative.suffix.lower() not in allowed_suffixes:
        raise ValueError(f'Invalid configured input name: {name}')
    return path, relative.name


def relocate(catalog, roots):
    if catalog['version'] != 1:
        raise ValueError('Unsupported catalog version')

    transfers = []

    def include(code, role, reference, suffixes):
        source, filename = resolve_input(reference, roots, suffixes)
        target = PurePosixPath('inputs', code, role, filename)
        transfers.append((source, target))
        return {'root': 'tool', 'path': target.as_posix()}

    for code, entry in catalog['products'].items():
        if not re.fullmatch(r'[0-9]{1,24}', code):
            raise ValueError(f'Invalid code: {code}')
        entry['template'] = include(code, 'template', entry['template'], {'.psd'})
        entry['bottle'] = include(code, 'bottle', entry['bottle'], IMAGE_SUFFIXES)
        scene = entry['render'].get('scene_replacement')
        if scene is not None:
            scene['image'] = include(code, 'scene', scene['image'], IMAGE_SUFFIXES)
    return transfers


def pack(destination, workbench=False):
    if destination.is_symlink():
        raise ValueError('Output path must not be a symbolic link')
    destination = destination.resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('Output directory must be absent or empty')

    catalog_path = Path(os.environ.get('DETAIL_CATALOG', ROOT / 'catalog.json')).resolve()
    catalog = json.loads(catalog_path.read_text(encoding='utf-8-sig'))
    transfers = relocate(catalog, source_roots())
    filenames = APPLICATION_FILES + (('README.md',) if (ROOT / 'README.md').is_file() else ())
    if workbench:
        filenames += WORKBENCH_FILES
        refresh = Path(os.environ.get('DETAIL_REFRESH_APP_ROOT', ROOT.parent / 'detail-page-refresh')).resolve()
        transfers.extend((refresh / name, Path('detail-page-refresh') / name) for name in REFRESH_FILES)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.detail-title-pack-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'bundle'
        stage.mkdir()
        for source, relative in [(ROOT / name, Path(name)) for name in filenames] + transfers:
            output = stage / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)
        (stage / 'catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n',
                                           encoding='utf-8')
        if destination.exists():
            destination.rmdir()
        stage.rename(destination)
    return destination, len(transfers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, help='Absent or empty output directory')
    parser.add_argument('--workbench', action='store_true', help='Include the unified UI and refresh adapter')
    args = parser.parse_args()
    try:
        destination, files = pack(args.destination, args.workbench)
    except (KeyError, OSError, ValueError) as error:
        parser.exit(1, f'Bundle failed: {error}\n')
    print(f'Packed {files} configured inputs into {destination}')


if __name__ == '__main__':
    main()
