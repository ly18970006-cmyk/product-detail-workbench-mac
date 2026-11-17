"""Mac-only deployment wrapper. No changes to upstream application code."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

BASE = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8905/'
APP = BASE / '源码/detail-title-tool/app.py'
PYTHON = BASE / '.venv/bin/python'
PIDFILE = BASE / '运行数据/service.json'
CLI = Path('/Applications/PhotoCraft.app/Contents/MacOS/photocraft-cli')


def material_paths():
    config_file = BASE / '配置/本机路径.json'
    library = BASE / '素材/产品编码测验'
    if config_file.is_file():
        config = json.loads(config_file.read_text())
        configured = config.get('product_directory')
        if not isinstance(configured, str) or not Path(configured).is_absolute():
            raise RuntimeError('商品目录配置必须是完整本地路径。')
        library = Path(configured).resolve(strict=True)
        if not library.is_dir() or library.name != '产品编码测验':
            raise RuntimeError('商品目录不可用，当前工作台要求目录名称为“产品编码测验”。')
    return library.parent, library


def font_settings():
    config_file = BASE / '配置/本机路径.json'
    config = json.loads(config_file.read_text()) if config_file.is_file() else {}
    font_file, family = config.get('font_file'), config.get('font_family')
    if font_file is None and family is None:
        return {}
    if not isinstance(font_file, str) or not Path(font_file).is_absolute() or not Path(font_file).is_file():
        raise RuntimeError('本地排版字体文件不可用，请检查配置/本机路径.json。')
    if not isinstance(family, str) or not family.strip():
        raise RuntimeError('本地排版字体名称未配置。')
    return {'REFRESH_FONT': font_file, 'REFRESH_FONT_FAMILY': family}


def environment():
    asset_root, product_library = material_paths()
    env = {k: v for k, v in os.environ.items() if not k.startswith(('DETAIL_', 'REFRESH_'))}
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env.update({
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
        'DETAIL_MODE': 'local', 'DETAIL_WORKBENCH': '1', 'DETAIL_PORT': '8905',
        'DETAIL_ASSET_ROOT': str(asset_root),
        'DETAIL_WORKSPACE_ROOT': str(BASE / '源码'),
        'DETAIL_EXTERNAL_ROOT': str(BASE / '素材'),
        'DETAIL_CATALOG': str(BASE / '配置/catalog.json'),
        'DETAIL_DATA_DIR': str(BASE / '运行数据/改字'),
        'DETAIL_WEB_TRIAL_DIR': str(BASE / '运行数据/网页首屏试点'),
        'DETAIL_RENDER_CLI': str(CLI),
        'REFRESH_PHOTOCRAFT': str(CLI),
        'REFRESH_V2_DATA': str(BASE / '运行数据/翻新'),
        'REFRESH_V2_ASSETS': str(product_library),
        'REFRESH_V2_PILOT_LIMIT': '0', 'REFRESH_V2_REFERENCE_EDIT_LIMIT': '0',
    })
    env.update(font_settings())
    return env


def health(path='api/health'):
    with urlopen(URL + path, timeout=2) as response:
        return json.load(response)


def owned_process():
    try:
        saved = json.loads(PIDFILE.read_text())
        pid = saved['pid']
        if not isinstance(pid, int) or pid <= 1 or os.getpgid(pid) != pid:
            return None
        ps_env = {**os.environ, 'LC_ALL': 'en_US.UTF-8'}
        command = subprocess.run(['/bin/ps', '-ww', '-p', str(pid), '-o', 'command='], env=ps_env, capture_output=True, text=True, check=True).stdout.strip()
        started = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='], env=ps_env, capture_output=True, text=True, check=True).stdout.strip()
        if str(APP) not in command or saved.get('started') != started:
            return None
        return pid
    except (OSError, KeyError, ValueError, subprocess.SubprocessError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status'], nargs='?', default='start')
    args = parser.parse_args()
    with (BASE / '运行数据/launcher.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pid = owned_process()
        if args.action == 'stop':
            if not pid:
                print('没有找到本工作台正在运行的服务；不会停止其他程序。')
                return
            os.killpg(pid, signal.SIGTERM)
            for _ in range(50):
                if not owned_process():
                    print('详情页工作台服务已停止。')
                    return
                time.sleep(0.1)
            raise RuntimeError('停止尚未完成；请检查本工作台服务日志。')
        if args.action == 'status':
            if not pid:
                print('详情页工作台未运行。')
                return
            print(json.dumps({'url': URL, 'health': health(), 'refresh': health('api/refresh/health')}, ensure_ascii=False, indent=2))
            return
        if pid:
            if health().get('service') != 'detail-title-tool':
                raise RuntimeError('本工作台服务未就绪，请检查日志。')
            print('工作台已在运行：' + URL)
            return
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', 8905)) == 0:
                raise RuntimeError('8905端口已被其他服务使用；未启动或停止任何其他程序。')
        if not PYTHON.is_file() or not CLI.is_file():
            raise RuntimeError('独立Python环境或PhotoCraft路径不可用。')
        with (BASE / '日志/服务.log').open('ab') as log:
            child = subprocess.Popen([str(PYTHON), '-B', str(APP)], cwd=APP.parent,
                                     env=environment(), stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True)
        try:
            started = subprocess.run(['/bin/ps', '-p', str(child.pid), '-o', 'lstart='], env={**os.environ, 'LC_ALL': 'en_US.UTF-8'}, capture_output=True, text=True, check=True).stdout.strip()
            PIDFILE.write_text(json.dumps({'pid': child.pid, 'started': started, 'url': URL}, ensure_ascii=False, indent=2))
            for _ in range(80):
                if child.poll() is not None:
                    raise RuntimeError('服务启动失败，请查看 日志/服务.log。')
                try:
                    if health().get('workbench') and health('api/refresh/health').get('mode') == 'refresh':
                        print('详情页工作台已启动：' + URL)
                        print('公司模型调用已关闭；关闭浏览器不会停止服务，可双击“停止详情页工作台.command”。')
                        return
                except (OSError, ValueError):
                    pass
                time.sleep(0.25)
            raise RuntimeError('服务启动超时，请查看 日志/服务.log。')
        except Exception:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=10)
            raise


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('操作失败：' + str(error), file=sys.stderr)
        sys.exit(1)
