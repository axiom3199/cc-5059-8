"""调律资料迁移：旧弯音偏移格式 -> 显式比值/参考频率/级数映射新格式。"""

from .legacy import (
    FORMAT_VERSION_LEGACY,
    LegacyEvent,
    LegacyScore,
    LegacyTuning,
    parse_legacy_score,
    load_legacy_tuning,
)
from .channels import (
    ChannelEvent,
    BendChannelAllocator,
    simulate_channel_events,
)
from .migrator import (
    FORMAT_VERSION,
    MigrationManifest,
    MigratedNote,
    UnrepresentableNote,
    SafePoint,
    migrate_tuning,
    compare_switch_strategies,
    verify_legacy_example,
)
from .lock import (
    LOCK_FILENAME,
    write_format_lock,
    assert_lock_current,
    migration_header,
    ensure_clean_output_dir,
)

__all__ = [
    "FORMAT_VERSION_LEGACY",
    "LegacyEvent",
    "LegacyScore",
    "LegacyTuning",
    "parse_legacy_score",
    "load_legacy_tuning",
    "ChannelEvent",
    "BendChannelAllocator",
    "simulate_channel_events",
    "FORMAT_VERSION",
    "MigrationManifest",
    "MigratedNote",
    "UnrepresentableNote",
    "SafePoint",
    "migrate_tuning",
    "compare_switch_strategies",
    "verify_legacy_example",
    "LOCK_FILENAME",
    "write_format_lock",
    "assert_lock_current",
    "migration_header",
    "ensure_clean_output_dir",
]
