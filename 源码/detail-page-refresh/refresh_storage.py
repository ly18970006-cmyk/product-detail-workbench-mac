"""Resolve the optional local workspace profile for standalone refresh services."""

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def data_directory(configured, legacy_default, instance, *, local=True):
    if configured:
        return Path(configured).resolve()
    config = ROOT.parent / "detail-page-storage.local.json"
    if os.name != "nt" or not local or not config.is_file():
        return legacy_default
    archive = Path(json.loads(config.read_text(encoding="utf-8-sig"))["refresh"])
    if not archive.is_absolute() or not archive.is_dir():
        raise RuntimeError(f"无法访问 E 盘翻新成果目录，请确认磁盘已挂载：{archive}")
    return (archive / "运行数据" / instance).resolve()
