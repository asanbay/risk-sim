"""
tools/make_gif.py — анимированный GIF сценария (запасной «видео»-артефакт).

Зачем: полноценное видео рендерится в Blender (blender/render.sh), но для
быстрого инструктажа, переписки и вставки в отчёт достаточно GIF: открывается
везде, весит сотни КБ и показывает ТО ЖЕ, что считает ядро — кадры рисуются
из results/scene_bundle.json (tools/render_frame.py), без Blender и без ядра.

    python tools/make_gif.py [--frames 60] [--fps 10] [--width 960] \
        [--out results/simulation.gif] [--plan-only]

Кадры берутся равномерной подвыборкой таймлайна: 60 кадров на неделю работы —
это ~1 кадр на 2 ч. Нужна плавность — увеличивайте --frames (растёт размер).
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    import json
    from PIL import Image
    from tools.render_frame import FrameRenderer

    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default="results/scene_bundle.json")
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--width", type=int, default=960)
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--out", default="results/simulation.gif")
    a = ap.parse_args()

    bundle = json.loads(Path(a.bundle).read_text(encoding="utf-8"))
    height = int(a.width * 9 / 16)
    r = FrameRenderer(bundle, plan_only=a.plan_only)
    n = len(r.frames)
    k = max(2, min(a.frames, n))
    idxs = [round(i * (n - 1) / (k - 1)) for i in range(k)]

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    imgs = []
    for j, i in enumerate(idxs):
        png = r.render(i, width=a.width, height=height)
        imgs.append(Image.open(io.BytesIO(png)).convert("P", palette=Image.ADAPTIVE, colors=255))
        if (j + 1) % 15 == 0 or j + 1 == len(idxs):
            print(f"  кадр {j+1}/{len(idxs)} (t={r.frames[i]['t']})", flush=True)
    r.close()

    imgs[0].save(out, save_all=True, append_images=imgs[1:],
                 duration=int(1000 / a.fps), loop=0, optimize=True, disposal=2)
    print(f"[gif] {out} ({out.stat().st_size/1024:.0f} КБ, {len(imgs)} кадров, {a.fps} fps)")
    print("[gif] напоминание: результат симуляции — поддержка решений, а не расчёт по нормам")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
