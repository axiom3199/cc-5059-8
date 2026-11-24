"""微分音编曲试听界面 —— 零依赖 Web 后端（仅标准库）。

运行：python3 -I microtone/web/server.py ，浏览器打开 http://localhost:8000

所有音高频率都由 microtone.core 在后端按当前调律实时计算后下发，
前端只负责把收到的频率值交给 Web Audio 振荡器，不做任何半音量化。
"""

from __future__ import annotations

import json
import os
import sys
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from microtone.core import (
    builtin_systems,
    TuningSystem,
    parse_score,
    render_score,
    write_wav,
    VoiceManager,
    verify_render,
    chord_ratios,
    compare_tuning_mappings,
)
from microtone.migration import (
    LegacyTuning,
    parse_legacy_score,
    migrate_tuning,
    FORMAT_VERSION,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SR = 44100


class ApiState:
    """跨请求共享的调律与实时声音状态。"""

    def __init__(self):
        self.systems = builtin_systems()
        self.current_name = "12-TET"
        self.policy = "hold_ratio"
        self.manager = VoiceManager(
            self.systems[self.current_name], sample_rate=SR,
            policy=self.policy)

    def tuning(self) -> TuningSystem:
        return self.systems[self.current_name]


STATE = ApiState()


def _json(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静一点
        sys.stderr.write("[web] " + fmt % args + "\n")

    # ---- 静态文件 --------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/app.js":
            return self._serve_file("app.js", "application/javascript; charset=utf-8")
        if path == "/style.css":
            return self._serve_file("style.css", "text/css; charset=utf-8")
        if path == "/api/tunings":
            return _json(self, {
                name: t.to_dict() for name, t in STATE.systems.items()})
        if path == "/api/current":
            t = STATE.tuning()
            q = parse_qs(parsed.query)
            lo = int(q.get("lo", ["-14"])[0])
            hi = int(q.get("hi", ["21"])[0])
            return _json(self, {
                "name": STATE.current_name,
                "policy": STATE.policy,
                "tuning": t.to_dict(),
                "freq_table": t.freq_table(lo, hi),
            })
        if path == "/api/table":
            q = parse_qs(parsed.query)
            name = q.get("name", [STATE.current_name])[0]
            t = STATE.systems.get(name)
            if t is None:
                return _json(self, {"error": f"未知调律 {name}"}, 404)
            lo = int(q.get("lo", ["-14"])[0])
            hi = int(q.get("hi", ["21"])[0])
            return _json(self, {
                "name": name, "tuning": t.to_dict(),
                "freq_table": t.freq_table(lo, hi)})
        if path == "/api/export-wav":
            return self._export_wav(parse_qs(parsed.query))
        self.send_error(404)

    def _serve_file(self, name: str, ctype: str):
        fp = os.path.join(HERE, name)
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- 动作 API --------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/select-tuning":
                return self._select_tuning()
            if path == "/api/update-tuning":
                return self._update_tuning()
            if path == "/api/note-on":
                return self._note_on()
            if path == "/api/note-off":
                return self._note_off()
            if path == "/api/gliss":
                return self._gliss()
            if path == "/api/set-policy":
                return self._set_policy()
            if path == "/api/render-score":
                return self._render_score()
            if path == "/api/verify":
                return self._verify()
            if path == "/api/migrate-legacy":
                return self._migrate_legacy()
            self.send_error(404)
        except Exception as exc:  # 任何后端错误回传，而不是静默
            _json(self, {"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _select_tuning(self):
        data = _read_json(self)
        name = data["name"]
        if name not in STATE.systems:
            return _json(self, {"error": f"未知调律 {name}"}, 404)
        # 调律表变化：已发声音按当前策略续接，并把续接记录返回给界面
        report = STATE.manager.apply_tuning_change(STATE.systems[name])
        STATE.current_name = name
        t = STATE.tuning()
        _json(self, {
            "name": name,
            "tuning": t.to_dict(),
            "freq_table": t.freq_table(-14, 21),
            "voice_continuation": report,
        })

    def _update_tuning(self):
        data = _read_json(self)
        # 编辑后的调律直接构造新 TuningSystem，比例字符串保留精确分数
        new_t = TuningSystem.from_dict(data["tuning"])
        save_name = data.get("name") or new_t.name or "custom"
        STATE.systems[save_name] = new_t
        report = STATE.manager.apply_tuning_change(new_t)
        STATE.current_name = save_name
        _json(self, {
            "name": save_name,
            "tuning": new_t.to_dict(),
            "freq_table": new_t.freq_table(-14, 21),
            "voice_continuation": report,
        })

    def _note_on(self):
        data = _read_json(self)
        degree = int(data["degree"])
        vel = float(data.get("velocity", 1.0))
        v = STATE.manager.note_on(degree, vel)
        t = STATE.tuning()
        if v is None:
            return _json(self, {
                "degree": degree,
                "freq_hz": t.degree_to_freq(degree),
                "muted": True,
                "reason": "超出音源有效范围",
            }, status=200)
        _json(self, {
            "degree": degree,
            "label": t.label_of(degree),
            "freq_hz": v.freq,
            "ratio": str(t.degree_ratio(degree)),
            "muted": False,
        })

    def _note_off(self):
        data = _read_json(self)
        degree = int(data["degree"])
        STATE.manager.note_off(degree)
        _json(self, {"degree": degree, "status": "off"})

    def _gliss(self):
        data = _read_json(self)
        d0 = int(data["degree_from"])
        d1 = int(data["degree_to"])
        dur = float(data.get("duration", 1.0))
        ok = STATE.manager.gliss_to(d0, d1, dur)
        t = STATE.tuning()
        _json(self, {
            "ok": ok,
            "degree_from": d0,
            "degree_to": d1,
            "freq_from": t.degree_to_freq(d0),
            "freq_to": t.degree_to_freq(d1),
            "ratio_to": str(t.degree_ratio(d1)),
        })

    def _set_policy(self):
        data = _read_json(self)
        STATE.policy = data["policy"]
        STATE.manager.set_policy(STATE.policy)
        _json(self, {"policy": STATE.policy})

    def _render_score(self):
        data = _read_json(self)
        score = parse_score(data["score_text"])
        tuning_name = data.get("tuning") or STATE.current_name
        tuning = STATE.systems[tuning_name]
        result = render_score(score, tuning, sample_rate=SR)
        checks = verify_render(result)
        # 和弦信息
        by_time = {}
        for m in result["mapping"]:
            by_time.setdefault(m["time"], []).append(m["freq_hz"])
        chords = []
        for tt, freqs in sorted(by_time.items()):
            if len(freqs) >= 2:
                chords.append({"time": tt, **chord_ratios(freqs)})
        _json(self, {
            "mapping": result["mapping"],
            "checks": checks,
            "chords": chords,
            "out_of_range": result["out_of_range"],
            "length_seconds": score.length_seconds,
            "tuning": tuning.to_dict(),
        })

    def _verify(self):
        data = _read_json(self)
        score = parse_score(data["score_text"])
        tuning = STATE.systems[data.get("tuning") or STATE.current_name]
        result = render_score(score, tuning, sample_rate=SR)
        _json(self, {"checks": verify_render(result),
                     "out_of_range": result["out_of_range"]})

    # ---- 调律资料迁移（旧弯音 -> 显式比值新格式） ------------------------

    def _migrate_legacy(self):
        data = _read_json(self)
        # 旧资料与旧谱面均来自请求体，后端按真实运算迁移，不接受手改频率
        legacy = LegacyTuning.from_dict(data["legacy_tuning"])
        legacy_score = parse_legacy_score(data["legacy_score_text"])
        release = float(data.get("release_seconds", 0.12))
        manifest = migrate_tuning(
            legacy, legacy_score, release_seconds=release,
            sample_rate=SR)

        # 迁移产物注册为可调可弹的新调律：琴键与渲染都走新映射发声
        save_name = f"migrated::{legacy.name}"
        new_tuning = TuningSystem.from_dict(manifest.tuning)
        STATE.systems[save_name] = new_tuning
        payload = manifest.to_dict()
        payload["registered_name"] = save_name
        payload["format_version_locked"] = FORMAT_VERSION
        _json(self, payload)

    def _export_wav(self, q):
        # GET /api/export-wav?name=...&tuning=...
        name = q.get("name", ["export"])[0]
        tuning_name = q.get("tuning", [STATE.current_name])[0]
        score_text = q.get("score", [""])[0]
        if not score_text:
            self.send_error(400, "缺少 score 参数")
            return
        score = parse_score(score_text)
        tuning = STATE.systems[tuning_name]
        result = render_score(score, tuning, sample_rate=SR)
        tmp = os.path.join("/tmp", f"{name}.wav")
        write_wav(tmp, result["pcm"], SR)
        with open(tmp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{name}_{tuning_name}.wav"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main(port: int = 8000):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"微分音编曲试听界面已启动: http://localhost:{port}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    main(p)
