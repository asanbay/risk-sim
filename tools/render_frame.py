"""
tools/render_frame.py — серверный рендер кадров симуляции в PNG (matplotlib).

Зачем он нужен, если есть браузерный viewer и Blender:
  * GIF/превью для отчёта и переписки без Blender (tools/make_gif.py);
  * «кадр дня» для вставки в Word/PDF;
  * проверка, что scene_bundle.json самодостаточен: этот модуль НЕ импортирует
    ядро — только читает JSON. Если кадр нарисовался, значит и внешний
    рендерер (Blender/Three.js/Unreal) получит всё необходимое.

    python tools/render_frame.py --frames 0,120,300,480 --out results/stills
    python tools/make_gif.py --frames 60 --fps 10
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrow, Rectangle

BG = "#0d1117"
FG = "#e6edf3"
MUTED = "#8b949e"
ZCOL = {
    "work": "#4da3ff", "edge": "#ff4d4f", "passage": "#ffd166",
    "storage": "#a855f7", "crane_hazard": "#ff8c42", "node": "#7ee787",
}
SEV = {"critical": "#ff4d4f", "high": "#ff8c42", "medium": "#ffd166",
       "low": "#7ee787", "info": "#8b949e"}


def _ts(t: str) -> datetime:
    return datetime.strptime(t, "%Y-%m-%d %H:%M")


class FrameRenderer:
    """Держит данные бандла и рисует один кадр за раз (фигура переиспользуется)."""

    def __init__(self, bundle: dict[str, Any], *, plan_only: bool = False):
        self.b = bundle
        self.tl = bundle["timeline"]
        self.frames = self.tl["frames"]
        self.zones = self.tl["zones"]
        self.crane = self.tl["crane"]
        self.tracks = {c["crew_id"]: c for c in (self.tl.get("crew_tracks") or [])}
        self.findings = bundle.get("findings", [])
        self.lim = self.crane.get("limits", {})
        self.plan_only = plan_only

        xs = [z["x_min"] for z in self.zones] + [z["x_max"] for z in self.zones]
        ys = [z["y_min"] for z in self.zones] + [z["y_max"] for z in self.zones]
        R = self.crane["jib"]["length_m"]
        mx, my = self.crane["mast"]["x"], self.crane["mast"]["y"]
        self.xlim = (min(xs + [mx - R]) - 6, max(xs + [mx + R]) + 6)
        self.ylim = (min(ys + [my - R]) - 6, max(ys + [my + R]) + 6)
        self.zlim = (0, self.crane["mast"]["top_z"] + 25)
        self._fig = None

    # -- вспомогательное ----------------------------------------------------
    def _active_findings(self, t: datetime) -> list[dict]:
        out = []
        for f in self.findings:
            try:
                s, e = _ts(f["start"]), _ts(f["end"])
            except (KeyError, ValueError):
                continue
            if s <= t <= e:
                out.append(f)
        return out

    def _crew_at(self, cid: str, t: datetime) -> tuple[float, float, float, str] | None:
        c = self.tracks.get(cid)
        if not c:
            return None
        pts = c["points"]
        if not pts:
            return None
        lo, hi = 0, len(pts) - 1
        if t <= _ts(pts[0]["t"]):
            p = pts[0]
            return p["x"], p["y"], p["z"], p.get("state", "")
        if t >= _ts(pts[-1]["t"]):
            p = pts[-1]
            return p["x"], p["y"], p["z"], p.get("state", "")
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if _ts(pts[mid]["t"]) <= t:
                lo = mid
            else:
                hi = mid
        a, bb = pts[lo], pts[hi]
        t0, t1 = _ts(a["t"]), _ts(bb["t"])
        dt = max(1e-6, (t1 - t0).total_seconds())
        k = (t - t0).total_seconds() / dt
        return (a["x"] + (bb["x"] - a["x"]) * k, a["y"] + (bb["y"] - a["y"]) * k,
                a["z"] + (bb["z"] - a["z"]) * k, a.get("state", ""))

    # -- публичный API ------------------------------------------------------
    def render(self, i: int, *, width: int = 960, height: int = 540) -> bytes:
        f = self.frames[i]
        self._lod = self._level_of(width)
        if self._fig is None:
            ncols = 1 if self.plan_only else 2
            wr = [1.0] if self.plan_only else [1.25, 1.0]
            self._fig, axes = plt.subplots(
                1, ncols, figsize=(width / 100, height / 100), dpi=100,
                gridspec_kw={"width_ratios": wr, "left": 0.05, "right": 0.985,
                             "top": 0.80, "bottom": 0.075, "wspace": 0.14})
            self._axes = [axes] if self.plan_only else list(axes)
            self._fig.patch.set_facecolor(BG)
            for ax in self._axes:
                ax.set_facecolor(BG)
        for ax in self._axes:
            ax.clear()
            ax.set_facecolor(BG)
        self._draw_plan(self._axes[0], f)
        if not self.plan_only:
            self._draw_elev(self._axes[1], f)
        self._draw_hud(f, i)
        import io
        buf = io.BytesIO()
        self._fig.savefig(buf, format="png", facecolor=BG)
        return buf.getvalue()

    def close(self):
        if self._fig is not None:
            plt.close(self._fig)
            self._fig = None

    # -- план ---------------------------------------------------------------
    def _draw_plan(self, ax, f: dict):
        lvl = self._active_level(f)
        ax.set_title(f"ПЛАН (вид сверху) · ярус {lvl}", color=FG, fontsize=9, loc="left", pad=6)
        ax.set_xlim(self.xlim); ax.set_ylim(self.ylim)
        ax.set_aspect("equal"); ax.set_xlabel("X, м", color=MUTED, fontsize=7)
        ax.set_ylabel("Y, м", color=MUTED, fontsize=7)
        ax.tick_params(colors=MUTED, labelsize=6)
        for s in ax.spines.values():
            s.set_color("#30363d")
        ax.grid(color="#21262d", lw=0.5, alpha=0.8)

        for z in self.zones:
            if z.get("level") != lvl:
                continue
            c = ZCOL.get(z.get("type", "work"), ZCOL["work"])
            al = {"crane_hazard": 0.08, "edge": 0.30, "passage": 0.18}.get(z.get("type"), 0.22)
            ax.add_patch(Rectangle((z["x_min"], z["y_min"]), z["x_max"] - z["x_min"],
                                   z["y_max"] - z["y_min"], facecolor=c, alpha=al,
                                   edgecolor=c, lw=0.9))
            if self._lod >= 2:
                ax.text((z["x_min"] + z["x_max"]) / 2, (z["y_min"] + z["y_max"]) / 2,
                        z.get("id", "").replace("-" + lvl, ""), color=c, fontsize=5.5,
                        ha="center", va="center", alpha=0.95)

        mx, my = self.crane["mast"]["x"], self.crane["mast"]["y"]
        R = self.crane["jib"]["length_m"]
        ax.add_patch(Circle((mx, my), R, fill=False, color="#f0883e", lw=0.7, ls=(0, (4, 3)), alpha=0.6))
        ax.plot([mx], [my], marker="s", ms=5, color="#f0883e", zorder=5)

        cr = f.get("crane") or {}
        if cr and cr.get("slew_deg") is not None:
            a = math.radians(float(cr["slew_deg"]))
            r = float(cr.get("radius_m", R))
            jx, jy = mx + R * math.cos(a), my + R * math.sin(a)
            cjl = self.crane["jib"].get("counter_jib_m", R / 3)
            ax.plot([mx - cjl * math.cos(a), jx], [my - cjl * math.sin(a), jy],
                    color="#f0883e", lw=2.0, solid_capstyle="round", zorder=6)
            ax.add_patch(FancyArrow(mx, my, r * math.cos(a), r * math.sin(a),
                                    width=0.35, head_width=1.6, head_length=1.6,
                                    length_includes_head=True, color="#ffd166", alpha=0.55, zorder=5))
        for ld in f.get("loads") or []:
            ax.plot([ld["x"]], [ld["y"]], marker="D", ms=7, color="#ff7b4d",
                    mec=FG, mew=0.7, zorder=8)
            off = float(ld.get("swing_offset_m", 0.0) or 0.0)
            if off > 0.05:
                a = math.radians(float(cr.get("slew_deg", 0.0))) if cr else 0.0
                ax.plot([ld["x"], ld["x"] + off * math.cos(a)],
                        [ld["y"], ld["y"] + off * math.sin(a)],
                        color="#ff4d4f", lw=1.4, zorder=9)
        if self._lod >= 1:
            for cid, c in self.tracks.items():
                p = self._crew_at(cid, self._t(f))
                if not p:
                    continue
                ax.plot([p[0]], [p[1]], marker="o", ms=4.5, color=c.get("color", "#7ee787"),
                        mec="#0d1117", mew=0.5, zorder=7)
        # активные нарушения — красные метки у зон
        for fd in self._active_findings(self._t(f)):
            for z in self._zids(fd):
                zz = next((q for q in self.zones if q.get("id") == z), None)
                if zz and zz.get("level") == lvl:
                    ax.add_patch(Rectangle((zz["x_min"], zz["y_min"]), zz["x_max"] - zz["x_min"],
                                           zz["y_max"] - zz["y_min"], fill=False,
                                           edgecolor=SEV.get(fd.get("severity"), "#ff4d4f"),
                                           lw=2.0, ls="-", zorder=10))

    # -- разрез -------------------------------------------------------------
    def _draw_elev(self, ax, f: dict):
        ax.set_title("РАЗРЕЗ (X–Z)", color=FG, fontsize=9, loc="left", pad=6)
        ax.set_xlim(self.xlim); ax.set_ylim(self.zlim)
        ax.set_xlabel("X, м", color=MUTED, fontsize=7); ax.set_ylabel("Z, м", color=MUTED, fontsize=7)
        ax.tick_params(colors=MUTED, labelsize=6)
        for s in ax.spines.values():
            s.set_color("#30363d")
        ax.grid(color="#21262d", lw=0.5, alpha=0.8)

        x0 = min(z["x_min"] for z in self.zones); x1 = max(z["x_max"] for z in self.zones)
        seen = set()
        for z in self.zones:
            k = (z.get("level"), round(z["z_floor"], 1))
            if k in seen:
                continue
            seen.add(k)
            ax.add_patch(Rectangle((x0 - 2, z["z_floor"] - 0.45), (x1 - x0) + 4, 0.45,
                                   facecolor="#30363d", edgecolor="none"))
            c = ZCOL.get(z.get("type", "work"), ZCOL["work"])
            ax.add_patch(Rectangle((z["x_min"], z["z_floor"]), z["x_max"] - z["x_min"],
                                   min(1.4, max(0.35, z["z_top"] - z["z_floor"])),
                                   facecolor=c, alpha=0.30, edgecolor=c, lw=0.6))
        mx, top = self.crane["mast"]["x"], self.crane["mast"]["top_z"]
        s = self.crane["mast"]["section_m"]
        ax.add_patch(Rectangle((mx - s / 2, self.crane["mast"]["base_z"]), s, top,
                               facecolor="#f0883e", alpha=0.85, edgecolor="none"))
        head_z = self.crane["jib"].get("head_z", top - 4)
        R = self.crane["jib"]["length_m"]
        cjl = self.crane["jib"].get("counter_jib_m", R / 3)
        ax.plot([mx - cjl, mx + R], [head_z, head_z], color="#f0883e", lw=2.2, zorder=6)
        ax.plot([mx, mx], [top, top + 9], color="#f0883e", lw=1.2, zorder=6)

        cr = f.get("crane") or {}
        loads = f.get("loads") or []
        if cr and cr.get("hook_z_m") is not None:
            hz = float(cr["hook_z_m"])
            lx = loads[0]["x"] if loads else mx + float(cr.get("radius_m", 20))
            ax.plot([lx, lx], [head_z, hz], color="#8b949e", lw=0.9, zorder=7)
            ax.plot([lx], [hz], marker="_", ms=8, color="#ffd166", mew=1.6, zorder=8)
        for ld in loads:
            ax.plot([ld["x"]], [ld["z"]], marker="D", ms=7, color="#ff7b4d",
                    mec=FG, mew=0.7, zorder=9)
            ax.text(ld["x"] + 2, ld["z"], f'{ld.get("lift_id","")}\nz={ld["z"]:.0f} м',
                    color=FG, fontsize=6, va="center")
        for cid, c in self.tracks.items():
            p = self._crew_at(cid, self._t(f))
            if p:
                ax.plot([p[0]], [p[2] + 0.9], marker="o", ms=4, color=c.get("color", "#7ee787"),
                        mec="#0d1117", mew=0.5, zorder=7)
        # лимиты ветра
        wl = self.lim.get("wind_limit_ops_ms")
        if wl:
            w = f.get("wind", {})
            g = float(w.get("gust_at_top_ms", 0.0))
            ax.text(0.02, 0.965,
                    f'порыв на верху: {g:.1f} м/с  (лимит {wl:.0f})',
                    transform=ax.transAxes, fontsize=6.5, va="top",
                    color="#ff4d4f" if g > wl else "#7ee787")

    # -- HUD ----------------------------------------------------------------
    def _draw_hud(self, f: dict, i: int):
        fig = self._fig
        for t in list(fig.texts):      # фигура переиспользуется — убираем старый HUD
            t.remove()
        t = self._t(f)
        w = f.get("wind", {})
        act = self._active_findings(t)
        sev_count: dict[str, int] = {}
        for a in act:
            sev_count[a.get("severity", "info")] = sev_count.get(a.get("severity", "info"), 0) + 1
        sev_txt = "  ".join(f"{k}:{v}" for k, v in sorted(sev_count.items(),
                                                           key=lambda kv: -{"critical": 4, "high": 3, "medium": 2, "low": 1}.get(kv[0], 0)))
        lift_txt = ""
        if f.get("loads"):
            l0 = f["loads"][0]
            lift_txt = (f'   |   {l0.get("lift_id","")} {l0.get("load_name","")[:28]} '
                        f'{l0.get("mass_kg",0):.0f} кг, фаза: {l0.get("phase","")}')
        fig.text(0.05, 0.955, f["t"], color=FG, fontsize=13, fontweight="bold")
        fig.text(0.05, 0.905,
                 f'ветер: порыв {w.get("gust_at_ref_ms",0):.1f} м/с @10 м · '
                 f'{w.get("gust_at_top_ms",0):.1f} м/с на верху · средний {w.get("mean_at_ref_ms",0):.1f}'
                 + lift_txt, color=MUTED, fontsize=8)
        fig.text(0.05, 0.862, f"активных нарушений: {len(act)}   {sev_txt}",
                 color=SEV.get("critical") if sev_count.get("critical") else FG, fontsize=8.5)
        if act:
            msg = act[0].get("message", "")[:150]
            fig.text(0.05, 0.828, f'— {act[0].get("rule_title","")}: {msg}',
                     color=SEV.get(act[0].get("severity", "info"), MUTED), fontsize=7)
        fig.text(0.985, 0.828, f"кадр {i+1}/{len(self.frames)}", color=MUTED,
                 fontsize=7, ha="right")
        fig.text(0.05, 0.018,
                 "Результат симуляции — поддержка решений, а не расчёт по нормам. "
                 "Допустимые значения — из ППР и паспортов оборудования.",
                 color=MUTED, fontsize=6.2, style="italic")

    # -- мелочи -------------------------------------------------------------
    def _t(self, f: dict) -> datetime:
        return _ts(f["t"])

    def _level_of(self, width: int) -> int:
        """Уровень детализации по ширине кадра (LOD): 0 < 480 px, 1 < 900 px, 2 — полно."""
        return 0 if width < 480 else (1 if width < 900 else 2)

    def _active_level(self, f: dict) -> str:
        """Ярус, который сейчас в работе: по зонам активных задач, иначе — где люди."""
        act = set(f.get("tasks_active") or [])
        zlev = {z.get("id"): z.get("level") for z in self.zones}
        counts: dict[str, int] = {}
        for t in self.tl.get("tasks", []):
            if t.get("task_id") in act:
                lv = zlev.get(t.get("zone_id"))
                if lv:
                    counts[lv] = counts.get(lv, 0) + 1
        if counts:
            return max(counts.items(), key=lambda kv: kv[1])[0]
        t = self._t(f)
        for cid in self.tracks:
            p = self._crew_at(cid, t)
            if p:
                lv = zlev.get(self._zone_of(cid, t))
                if lv:
                    counts[lv] = counts.get(lv, 0) + 1
        if counts:
            return max(counts.items(), key=lambda kv: kv[1])[0]
        lv = sorted({z.get("level") for z in self.zones} - {"G"})
        return lv[-1] if lv else "G"

    def _zone_of(self, cid: str, t: datetime) -> str | None:
        pts = (self.tracks.get(cid) or {}).get("points") or []
        for p in reversed(pts):
            if _ts(p["t"]) <= t:
                return p.get("zone")
        return pts[0].get("zone") if pts else None

    @staticmethod
    def _zids(fd: dict) -> list[str]:
        z = fd.get("zone_ids") or []
        if isinstance(z, str):
            return [q for q in z.split(";") if q]
        return list(z)


def render_frame(bundle, i: int, *, size: tuple[int, int] = (960, 540),
                 out=None, plan_only: bool = False) -> bytes:
    """Одноразовый рендер кадра i в PNG.

    bundle — dict бандла ИЛИ путь к scene_bundle.json; out — путь или file-like,
    куда дополнительно записать байты. Возвращает PNG-байты.
    """
    if not isinstance(bundle, dict):
        bundle = json.loads(Path(bundle).read_text(encoding="utf-8"))
    r = FrameRenderer(bundle, plan_only=plan_only)
    try:
        png = r.render(i, width=size[0], height=size[1])
    finally:
        r.close()
    if out is not None:
        if hasattr(out, "write"):
            out.write(png)
        else:
            Path(out).write_bytes(png)
    return png


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default="results/scene_bundle.json")
    ap.add_argument("--frames", default="0", help="индексы или 'every:N'")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--out", default="results/stills")
    a = ap.parse_args()

    bundle = json.loads(Path(a.bundle).read_text(encoding="utf-8"))
    r = FrameRenderer(bundle, plan_only=a.plan_only)
    n = len(r.frames)
    if a.frames.startswith("every:"):
        step = max(1, int(a.frames.split(":")[1]))
        idxs = list(range(0, n, step))
    else:
        idxs = [min(max(0, int(x)), n - 1) for x in a.frames.split(",") if x.strip() != ""]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    for i in idxs:
        png = r.render(i, width=a.width, height=a.height)
        p = out / f"frame_{i:04d}.png"
        p.write_bytes(png)
        print(f"  {p} ({len(png)/1024:.0f} КБ)")
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
