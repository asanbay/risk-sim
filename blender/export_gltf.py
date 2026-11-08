"""
blender/export_gltf.py — экспорт анимированной сцены в glTF/GLB для Three.js.

Запуск (после scene_setup.py + animate_pilot.py):
    blender -b results/pilot/building.blend -P blender/export_gltf.py -- \
        --out results/pilot/building.glb [--draco] [--only Crane,Zones,Crews,Load]

Зачем glTF: это единственный формат, который одинаково хорошо понимают
Three.js, Babylon, Unity/Unreal, Omniverse и Blender. Экспортировав один
раз, мы не привязаны к Blender — визуализацию можно заменить, а расчёты
(раздел 3 ТЗ) останутся в Python.

Что важно знать:
  * Blender Z-up -> glTF Y-up: экспортер поворачивает сцену автоматически,
    поэтому в Three.js «вверх» будет +Y, а координаты из JSON (x,y,z с Z вверх)
    нужно маппить как (x, z, -y) — см. viewer/pilot_three.html.
  * Анимация экспортируется как Action-ы; если объектов много, лучше
    прореживать кадры (--every в animate_pilot.py), иначе GLB раздувается.
  * Кастомные свойства (risk_type, base_color) уезжают в extras — ими
    Three.js-слой красит зоны риска, не зная ничего о правилах.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import bpy


def log(*a):
    print("[export_gltf]", *a, flush=True)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/pilot/building.glb")
    ap.add_argument("--draco", action="store_true", help="сжатие геометрии Draco")
    ap.add_argument("--only", default="", help="коллекции через запятую (иначе всё)")
    ap.add_argument("--no-anim", action="store_true")
    a = ap.parse_args(argv)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if a.only:
        wanted = {w.strip() for w in a.only.split(",") if w.strip()}
        for col in bpy.data.collections:
            col.hide_render = col.name not in wanted
        log(f"экспорт коллекций: {sorted(wanted)}")

    kw = dict(
        filepath=str(out),
        export_format="GLB",
        use_selection=False,
        export_apply=False,
        export_animations=not a.no_anim,
        export_animation_mode="ACTIONS",
        export_frame_range="SCENE",
        export_extras=True,          # кастомные свойства -> extras (risk_type и др.)
        export_cameras=True,
        export_lights=True,
        export_materials="EXPORT",
        export_normals=True,
        export_yup=True,
        export_skins=True,
        export_hierarchy_flattening="NO_FLATTENING",
    )
    if a.draco:
        kw.update(export_draco_mesh_compression_enable=True,
                  export_draco_mesh_compression_level=6)

    try:
        bpy.ops.export_scene.gltf(**kw)
    except TypeError as e:
        # часть параметров менялась между версиями Blender — режем лишнее и повторяем
        log(f"полный набор параметров не принят ({e}); пробую минимальный")
        bpy.ops.export_scene.gltf(filepath=str(out), export_format="GLB",
                                  export_animations=not a.no_anim, export_extras=True,
                                  export_yup=True)

    sz = out.stat().st_size / 1024 / 1024
    log(f"готово: {out} ({sz:.1f} МБ)")
    log("подключение в браузере: см. viewer/pilot_three.html (GLTFLoader + OrbitControls)")


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    main(argv)
