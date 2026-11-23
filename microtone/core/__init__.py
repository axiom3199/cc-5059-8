"""微分音音乐编配工具 —— 纯标准库核心。"""

from .tuning import (
    TuningSystem,
    builtin_systems,
    equal_temperament,
    just_scale,
    pythagorean_scale,
    meantone_scale,
    bohlen_pierce,
    quarter_tone_scale,
    ratio_to_cents,
)
from .score import Score, parse_score, NoteEvent, ChordEvent, GlissEvent, RestEvent
from .renderer import (
    render_score,
    write_wav,
    write_wav_stereo_compare,
    Voice,
    VoiceManager,
)
from .analysis import (
    estimate_fundamental,
    estimate_fundamental_near_target,
    chord_ratios,
    beat_frequency,
    verify_render,
    compare_tuning_mappings,
)

__all__ = [
    "TuningSystem",
    "builtin_systems",
    "equal_temperament",
    "just_scale",
    "pythagorean_scale",
    "meantone_scale",
    "bohlen_pierce",
    "quarter_tone_scale",
    "ratio_to_cents",
    "Score",
    "parse_score",
    "NoteEvent",
    "ChordEvent",
    "GlissEvent",
    "RestEvent",
    "render_score",
    "write_wav",
    "write_wav_stereo_compare",
    "Voice",
    "VoiceManager",
    "estimate_fundamental",
    "estimate_fundamental_near_target",
    "chord_ratios",
    "beat_frequency",
    "verify_render",
    "compare_tuning_mappings",
]
