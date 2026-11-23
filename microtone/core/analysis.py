"""从实际渲染出的 PCM 信号反估主频、核对和弦比例与拍频。

全部基于纯标准库数学运算。估计流程：
加汉宁窗 -> DFT（限定目标频段以降低运算量）-> 抛物线插值求亚 bin 峰值。
"""

from __future__ import annotations

import math
from fractions import Fraction


def _goertzel_energy(pcm: list[float], freq: float, sr: int) -> float:
    """单频点 Goertzel 能量，用于精细扫频。"""
    w = 2.0 * math.pi * freq / sr
    coeff = 2.0 * math.cos(w)
    s_prev = 0.0
    s_prev2 = 0.0
    for x in pcm:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2


def estimate_fundamental(pcm: list[float], sr: int,
                         f_min: float = 20.0,
                         f_max: float = 8000.0) -> dict:
    """估计一段（近稳态）信号的主频。"""
    n = len(pcm)
    if n < 64:
        raise ValueError("样本过短，无法估计主频")
    # 汉宁窗
    windowed = [
        x * 0.5 * (1 - math.cos(2 * math.pi * i / (n - 1)))
        for i, x in enumerate(pcm)
    ]
    bin_hz = sr / n
    k_min = max(1, int(f_min / bin_hz))
    k_max = min(n // 2, int(f_max / bin_hz))
    if k_max <= k_min:
        k_max = min(n // 2, k_min + 4)

    # 粗扫 DFT 幅度
    best_k = k_min
    best_mag = -1.0
    mags = {}
    for k in range(k_min, k_max + 1):
        re = 0.0
        im = 0.0
        ang = 2.0 * math.pi * k / n
        # 递归旋转因子
        c = math.cos(ang)
        s = math.sin(ang)
        cw = 1.0
        sw = 0.0
        for x in windowed:
            re += x * cw
            im -= x * sw
            cw, sw = cw * c - sw * s, cw * s + sw * c
        mag = re * re + im * im
        mags[k] = math.sqrt(mag)
        if mag > best_mag:
            best_mag = mag
            best_k = k

    # 抛物线插值得到亚 bin 频率
    left = mags.get(best_k - 1, 0.0)
    center = mags[best_k]
    right = mags.get(best_k + 1, 0.0)
    denom = left - 2 * center + right
    delta = 0.5 * (left - right) / denom if denom != 0 else 0.0
    est_freq = (best_k + delta) * bin_hz

    # Goertzel 在估计点附近做一次精细确认
    fine = max(f_min, est_freq - bin_hz)
    step = bin_hz / 4
    probe = fine
    best_f = est_freq
    best_e = -1.0
    while probe <= min(f_max, est_freq + bin_hz):
        e = _goertzel_energy(windowed, probe, sr)
        if e > best_e:
            best_e = e
            best_f = probe
        probe += step

    return {
        "freq_hz": best_f,
        "coarse_bin_hz": best_k * bin_hz,
        "rms": math.sqrt(sum(x * x for x in pcm) / n),
        "samples": n,
    }


def steady_segment(pcm: list[float], sr: int,
                   start: float, duration: float = 0.4,
                   onset_skip: float = 0.08) -> list[float]:
    """取事件起始后稍靠中的稳态片段，避开起音包络。

    onset_skip 为额外的起音跳过时间；滑音等需要精确定位时可传 0。
    """
    s0 = int((start + onset_skip) * sr)
    n = int(duration * sr)
    s0 = min(s0, max(0, len(pcm) - n))
    return pcm[s0:s0 + n]


def chord_ratios(freqs: list[float]) -> list[dict]:
    """把一组同时发声的频率换算成相对最低音的比例与拍频信息。"""
    freqs = sorted(f for f in freqs if f > 0)
    if not freqs:
        return []
    base = freqs[0]
    out = []
    for i, f in enumerate(freqs):
        approx = Fraction(f / base).limit_denominator(64)
        out.append(
            {
                "freq_hz": f,
                "ratio_vs_lowest": f / base,
                "approx_simple_ratio": str(approx),
                "cents_vs_lowest": 1200 * math.log2(f / base),
            }
        )
    # 两两拍频
    pairs = []
    for i in range(len(freqs)):
        for j in range(i + 1, len(freqs)):
            f1, f2 = freqs[i], freqs[j]
            pairs.append(
                {
                    "pair": (f1, f2),
                    "beat_hz": abs(f2 - f1),
                    "interval_cents": 1200 * math.log2(f2 / f1),
                }
            )
    return {"voices": out, "pair_beats": pairs}


def beat_frequency(f1: float, f2: float) -> float:
    """两个相近正弦分量的一阶拍频。"""
    return abs(f1 - f2)


def compare_tuning_mappings(mapping_a: list[dict],
                            mapping_b: list[dict]) -> list[dict]:
    """同一谱面两种调律的逐事件音高映射差异。"""
    rows = []
    for a, b in zip(mapping_a, mapping_b):
        rows.append(
            {
                "time": a["time"],
                "degree": a["degree"],
                "label": a.get("label"),
                "freq_a_hz": a["freq_hz"],
                "freq_b_hz": b["freq_hz"],
                "delta_hz": b["freq_hz"] - a["freq_hz"],
                "delta_cents": 1200
                * math.log2(b["freq_hz"] / a["freq_hz"]),
            }
        )
    return rows


def estimate_fundamental_near_target(pcm: list[float], sr: int,
                                     target_hz: float,
                                     span: float = 0.03) -> dict:
    """在目标频率 ±span（默认 ±3%）邻域内找最强峰。

    复音和弦里全局主频会被最强分量主导，因此核对某个已知期望音时，
    必须在该音自己的频段内做局部 DFT 峰值搜索。
    """
    n = len(pcm)
    if n < 64:
        raise ValueError("样本过短，无法估计主频")
    windowed = [
        x * 0.5 * (1 - math.cos(2 * math.pi * i / (n - 1)))
        for i, x in enumerate(pcm)
    ]
    bin_hz = sr / n
    k_lo = max(1, int(target_hz * (1 - span) / bin_hz))
    k_hi = min(n // 2, max(k_lo + 2,
                           int(target_hz * (1 + span) / bin_hz)))
    best_k, best_mag, mags = k_lo, -1.0, {}
    for k in range(k_lo, k_hi + 1):
        re = im = 0.0
        ang = 2.0 * math.pi * k / n
        c, s = math.cos(ang), math.sin(ang)
        cw, sw = 1.0, 0.0
        for x in windowed:
            re += x * cw
            im -= x * sw
            cw, sw = cw * c - sw * s, cw * s + sw * c
        mag = re * re + im * im
        mags[k] = math.sqrt(mag)
        if mag > best_mag:
            best_mag, best_k = mag, k
    left = mags.get(best_k - 1, 0.0)
    center = mags[best_k]
    right = mags.get(best_k + 1, 0.0)
    denom = left - 2 * center + right
    delta = 0.5 * (left - right) / denom if denom != 0 else 0.0
    return {
        "freq_hz": (best_k + delta) * bin_hz,
        "rms": math.sqrt(sum(x * x for x in pcm) / n),
        "samples": n,
    }


def verify_render(render: dict, sr: int | None = None,
                  tolerance_cents: float = 3.0) -> list[dict]:
    """对渲染产物中的每个事件做“期望频率 vs 信号实测主频”核对。

    复音和弦中的每个音在其期望频率 ±3% 邻域内局部核对；
    单音事件同时给出一次全局主频估计。
    """
    sr = sr or render["sample_rate"]
    pcm = render["pcm"]
    checks = []
    # 记录每个起始时间上同时发声的音数，用于区分单音/复音
    from collections import Counter
    poly = Counter(m["time"] for m in render["mapping"])
    for m in render["mapping"]:
        if "gliss_to_freq_hz" in m:
            # 扫频在 70% 处完成，之后目标频率保持到 release；
            # 直接定位到保持段中部，跳过起音偏移
            end_time = m["time"] + m["duration"]
            seg = steady_segment(pcm, sr, end_time - 0.22, 0.10,
                                 onset_skip=0.0)
            if len(seg) < 64:
                continue
            local = estimate_fundamental_near_target(
                seg, sr, m["gliss_to_freq_hz"], span=0.06)
            err_cents = 1200 * math.log2(
                local["freq_hz"] / m["gliss_to_freq_hz"])
            checks.append({
                "degree": m["gliss_to_degree"],
                "label": m.get("label") + "→gliss",
                "expected_hz": m["gliss_to_freq_hz"],
                "estimated_hz": local["freq_hz"],
                "error_cents": err_cents,
                "polyphonic": False,
                "pass": abs(err_cents) <= tolerance_cents,
            })
            continue
        seg = steady_segment(pcm, sr, m["time"])
        if len(seg) < 64:
            continue
        local = estimate_fundamental_near_target(seg, sr, m["freq_hz"])
        err_cents = 1200 * math.log2(local["freq_hz"] / m["freq_hz"])
        row = {
            "degree": m["degree"],
            "label": m.get("label"),
            "expected_hz": m["freq_hz"],
            "estimated_hz": local["freq_hz"],
            "error_cents": err_cents,
            "polyphonic": poly[m["time"]] > 1,
            "pass": abs(err_cents) <= tolerance_cents,
        }
        if not row["polyphonic"]:
            est = estimate_fundamental(
                seg, sr,
                f_min=max(20, m["freq_hz"] * 0.5),
                f_max=m["freq_hz"] * 2.0,
            )
            row["global_estimate_hz"] = est["freq_hz"]
        checks.append(row)
    return checks
