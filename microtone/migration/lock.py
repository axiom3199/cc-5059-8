"""调律资料格式的构建锁定。

迁移产物必须注明格式版本，且运行时核对锁文件与代码常量一致；
版本不一致直接拒绝迁移，防止新格式被旧解析器或被手改数据冒充。
锁文件随仓库提交，是“构建锁定变更”的可审计落点。
"""

from __future__ import annotations

import json
import os

from .migrator import FORMAT_VERSION
from .legacy import FORMAT_VERSION_LEGACY

LOCK_FILENAME = "tuning-format.lock.json"

# 代码侧锁定的契约。修改格式时必须同时提升版本并更新锁文件。
FORMAT_LOCK = {
    "new_format_version": FORMAT_VERSION,
    "legacy_format_version": FORMAT_VERSION_LEGACY,
    "frequency_source": "TuningSystem.degree_to_freq（显式 Fraction 比值）",
    "bend_path": "旧弯音仅用于迁移期通道模拟，不参与新格式发声",
    "contract": "新映射频率必须与旧资料真实目标频率在相对容差 1e-9 内一致",
}


def migration_header() -> dict:
    """写入每个迁移产物头部的版本信息。"""
    return {
        "format_version": FORMAT_VERSION,
        "legacy_format_version": FORMAT_VERSION_LEGACY,
        "locked_by": LOCK_FILENAME,
    }


def write_format_lock(directory: str) -> str:
    """在目录中写出锁文件。目录须由迁移流程新建或确认为净目录。"""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, LOCK_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(FORMAT_LOCK, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return path


def assert_lock_current(directory: str) -> dict:
    """核对目录中的锁文件与当前代码契约一致，不一致则拒绝继续。"""
    path = os.path.join(directory, LOCK_FILENAME)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"缺少构建锁 {path}，拒绝在未锁定目录消费迁移调律")
    with open(path, "r", encoding="utf-8") as f:
        locked = json.load(f)
    for key, expected in FORMAT_LOCK.items():
        if locked.get(key) != expected:
            raise ValueError(
                f"调律格式锁过期/被手改：{key} 锁内={locked.get(key)!r} "
                f"代码={expected!r}，请重新运行迁移而不是修改数据")
    return locked


def ensure_clean_output_dir(directory: str) -> None:
    """净目录约束：迁移输出目录必须不存在或为空，避免混入手改/陈旧资料。"""
    if os.path.exists(directory):
        if not os.path.isdir(directory):
            raise NotADirectoryError(f"输出路径不是目录: {directory}")
        if os.listdir(directory):
            raise FileExistsError(
                f"迁移输出目录非空，拒绝在非净目录执行: {directory}")
    else:
        os.makedirs(directory, exist_ok=True)
