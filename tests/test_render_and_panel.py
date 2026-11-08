"""
tests/test_render_and_panel.py — контракты визуального слоя и панели.

Здесь проверяется не физика (см. test_verification.py), а ДОГОВОРЁННОСТИ:
  1. scene_bundle.json держит схему, которую читают viewer, Blender и панель;
  2. серверный рендерер кадров (GIF/stills) работает и даёт валидный PNG;
  3. Streamlit-панель поднимается без исключений;
  4. Blender-скрипты и render.sh синтаксически исправны (их нельзя «сломать молча»).

Если тест провалился — сломан контракт между компонентами, а не «картинка».
"""
from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BUNDLE = os.path.join(ROOT, "results", "pilot", "scene_bundle.json")


# ---------------------------------------------------------------------------
# 1. схема бандла
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def bundle():
    if not os.path.exists(BUNDLE):
        pytest.skip("нет results/scene_bundle.json (сначала: make all)")
    with io.open(BUNDLE, encoding="utf-8") as f:
        return json.load(f)


def test_bundle_schema_timeline(bundle):
    frames = bundle["timeline"]["frames"]
    assert isinstance(frames, list) and len(frames) > 10
    f0 = frames[0]
    for key in ("t", "wind", "loads"):
        assert key in f0, f"кадр без ключа {key}"
    w = f0["wind"]
    for key in ("gust_at_ref_ms", "gust_at_top_ms"):
        assert isinstance(w[key], (int, float))
    assert isinstance(f0["loads"], list)
    for ld in f0["loads"][:5]:
        for key in ("lift_id", "mass_kg", "phase"):
            assert key in ld


def test_bundle_schema_crane_and_findings(bundle):
    lim = bundle["timeline"]["crane"]["limits"]
    assert isinstance(lim["wind_limit_ops_ms"], (int, float))
    assert isinstance(bundle["findings"], list)
    for f in bundle["findings"][:20]:
        assert "zone_ids" in f and "start" in f and "end" in f


def test_bundle_schema_zones_crew(bundle):
    for z in bundle["timeline"]["zones"]:
        for key in ("id", "type", "x_min", "x_max", "y_min", "y_max", "z_floor", "z_top"):
            assert key in z, f"зона без ключа {key}"
    for ct in bundle["timeline"]["crew_tracks"]:
        assert ct["points"], "crew_track без точек"


def test_bundle_loads_physics_fields(bundle):
    """Каждый груз держит поля раскачки/нагрузки, которые рисует viewer."""
    seen = 0
    for f in bundle["timeline"]["frames"]:
        for ld in f["loads"]:
            for key in ("swing_offset_m", "swing_theta_deg"):
                assert isinstance(ld[key], (int, float))
            seen += 1
            if seen > 50:
                return
    assert seen > 0


# ---------------------------------------------------------------------------
# 2. серверный рендерер кадров
# ---------------------------------------------------------------------------
def test_frame_renderer_produces_png():
    rf = pytest.importorskip("tools.render_frame", reason="нет tools/render_frame.py")
    if not os.path.exists(BUNDLE):
        pytest.skip("нет бандла для рендера")
    buf = io.BytesIO()
    rf.render_frame(BUNDLE, 0, size=(320, 180), out=buf)
    png = buf.getvalue()
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "рендер не дал PNG"
    assert len(png) > 2000


def test_frame_renderer_frames_differ():
    rf = pytest.importorskip("tools.render_frame", reason="нет tools/render_frame.py")
    if not os.path.exists(BUNDLE):
        pytest.skip("нет бандла для рендера")
    a, b = io.BytesIO(), io.BytesIO()
    rf.render_frame(BUNDLE, 0, size=(240, 140), out=a)
    rf.render_frame(BUNDLE, 40, size=(240, 140), out=b)
    assert a.getvalue() != b.getvalue(), "кадры 0 и 40 идентичны — таймлайн не едет"


def test_frame_renderer_levels(bundle):
    rf = pytest.importorskip("tools.render_frame", reason="нет tools/render_frame.py")
    r = rf.FrameRenderer(bundle)
    assert r._level_of(320) <= r._level_of(2400), "LOD не монотонен по ширине"
    assert r._level_of(640) in (0, 1, 2)
    assert r._level_of(1600) == 2, "полнокадровый рендер должен быть LOD 2"


# ---------------------------------------------------------------------------
# 3. Streamlit-панель
# ---------------------------------------------------------------------------
def test_streamlit_panel_runs_without_exceptions():
    st = pytest.importorskip("streamlit", reason="streamlit не установлен")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(os.path.join(ROOT, "app_streamlit.py"), default_timeout=120)
    at.run()
    assert not at.exception, f"панель упала: {at.exception}"


# ---------------------------------------------------------------------------
# 4. Blender-слой: синтаксис нельзя сломать молча
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["scene_setup.py", "animate.py", "animate_pilot.py", "export_gltf.py", "flatten_usd.py"])
def test_blender_scripts_parse(name):
    p = os.path.join(ROOT, "blender", name)
    if not os.path.exists(p):
        pytest.skip(f"нет blender/{name}")
    src = io.open(p, encoding="utf-8").read()
    ast.parse(src)  # SyntaxError = провал


def test_render_sh_syntax():
    p = os.path.join(ROOT, "blender", "render.sh")
    if not os.path.exists(p):
        pytest.skip("нет blender/render.sh")
    r = subprocess.run(["bash", "-n", p], capture_output=True, text=True)
    assert r.returncode == 0, f"render.sh: {r.stderr}"
