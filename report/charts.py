"""
report/charts.py — графики для отчёта.

Все картинки пишутся в PNG и встраиваются в HTML (base64) и DOCX. Никакого
Plotly/D3 во внешнем отчёте: файл должен открываться где угодно, в том числе
в песочнице без сети и на компьютере инспектора без интернета.

Стиль намеренно «печатный»: светлый фон, крупные подписи единиц измерения,
обязательная линия лимита — чтобы график можно было вставить в ППР.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any, Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import Patch

from core.physics.wind import WindField, profile_factor

plt.rcParams.update({
    "figure.dpi": 130, "savefig.dpi": 130, "font.size": 8.5,
    "axes.grid": True, "grid.alpha": .28, "grid.linewidth": .6,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": "#8a94a3", "axes.labelcolor": "#2b3440",
    "text.color": "#1c2430", "xtick.color": "#4a5566", "ytick.color": "#4a5566",
    "font.family": "DejaVu Sans",
})
SEV_COLOR = {"critical": "#d6303a", "high": "#ef8a17", "medium": "#e8c33c", "low": "#3aa6a0"}


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def png_b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
def chart_wind(wind: WindField, tasks: Sequence[Any], z_work: float,
               limits: Sequence[float] = (8.0, 10.0, 12.0), out: Path = Path("results/charts/wind.png")) -> Path:
    """Ветер за неделю: на высоте измерения и на высоте работ + линии лимитов."""
    pf = profile_factor(z_work, wind.ref_height_m, wind.alpha)
    t = wind.ts
    fig, ax = plt.subplots(figsize=(9.2, 2.9))
    ax.fill_between(t, 0, wind.mean_ms, color="#4da3ff", alpha=.28, label="средняя (10 м)")
    ax.plot(t, wind.gust_ms, color="#1f6fb4", lw=1.5, label="порыв (10 м) — базис лимита ППР")
    ax.plot(t, wind.gust_ms * pf, color="#d6303a", lw=1.3, ls="--",
            label=f"порыв на {z_work:.0f} м (×{pf:.2f})")
    for lim in limits:
        ax.axhline(lim, color="#7a8593", lw=.9, ls=":")
        ax.text(t[0], lim + .25, f"лимит {lim:.0f} м/с", fontsize=7, color="#5a6472")
    # окна подъёмов
    for task in tasks:
        if "lift" in getattr(task, "hazard_tags", []) or "crane" in getattr(task, "hazard_tags", []):
            ax.axvspan(task.start, task.end, color="#ffb703", alpha=.13, lw=0)
    ax.set_ylabel("скорость ветра, м/с")
    ax.legend(loc="upper left", fontsize=7.4, framealpha=.92, ncol=2)
    ax.set_title("Ветер: измерение на 10 м и пересчёт на высоту работ. Жёлтым — окна подъёмов по графику",
                 fontsize=9.5, loc="left", pad=8)
    fig.autofmt_xdate(rotation=0, ha="center")
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d.%m"))
    return _save(fig, out)


def chart_heatmap(findings_df: pd.DataFrame, zones, out: Path = Path("results/charts/heatmap.png")) -> Path:
    """Тепловая карта: день × ярус, взвешенная по тяжести нарушения."""
    if findings_df.empty:
        fig, ax = plt.subplots(figsize=(6, 1.6)); ax.axis("off")
        ax.text(.5, .5, "нарушений не найдено", ha="center", va="center", fontsize=10)
        return _save(fig, out)
    df = findings_df.copy()
    df["day"] = pd.to_datetime(df["start"], errors="coerce").dt.strftime("%d.%m")
    lvl_of = {}
    for z in zones:
        lvl_of[z.zone_id] = z.level
    def lvl(s: str) -> str:
        if not isinstance(s, str) or not s:
            return "—"
        first = s.split(";")[0]
        return lvl_of.get(first, first.split("-")[-1] if "-" in first else "—")
    df["level"] = df["zone_ids"].map(lvl)
    rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    df["w"] = df["severity"].map(rank).fillna(1)
    piv = df.pivot_table(index="level", columns="day", values="w", aggfunc="sum", fill_value=0)
    piv = piv.reindex(sorted(piv.index, reverse=True))
    cmap = LinearSegmentedColormap.from_list("risk", ["#ffffff", "#ffe9b0", "#f7a44c", "#d6303a", "#7c1220"])
    fig, ax = plt.subplots(figsize=(max(4.2, 0.85 * len(piv.columns) + 2.2), max(1.5, 0.5 * len(piv) + 1.1)))
    im = ax.imshow(piv.values, cmap=cmap, aspect="auto", vmin=0)
    ax.set_xticks(range(len(piv.columns)), piv.columns, fontsize=8)
    ax.set_yticks(range(len(piv.index)), [f"ярус {i}" for i in piv.index], fontsize=8)
    for i in range(piv.shape[0]):
        for j in range(piv.shape[1]):
            v = piv.values[i, j]
            if v:
                ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=7.5,
                        color="white" if v > piv.values.max() * .55 else "#3b2b16")
    ax.set_title("Тепловая карта рисков: сумма весов тяжести по дням и ярусам "
                 "(critical=4, high=3, medium=2, low=1)", fontsize=9, loc="left", pad=8)
    ax.grid(False)
    fig.colorbar(im, ax=ax, shrink=.85, label="взвешенное число нарушений")
    return _save(fig, out)


def chart_findings_by_rule(findings_df: pd.DataFrame, cfg, out: Path = Path("results/charts/by_rule.png")) -> Path:
    if findings_df.empty:
        fig, ax = plt.subplots(figsize=(6, 1.4)); ax.axis("off")
        ax.text(.5, .5, "нарушений не найдено", ha="center", va="center")
        return _save(fig, out)
    g = (findings_df.groupby(["rule_id", "severity"]).size().reset_index(name="n")
         .sort_values("n"))
    titles = {r.id: r.title for r in cfg.rules}
    labels = [f"{titles.get(r, r)}" for r in g["rule_id"]]
    colors = [SEV_COLOR.get(s, "#888") for s in g["severity"]]
    fig, ax = plt.subplots(figsize=(8.4, max(1.8, 0.42 * len(g) + 1)))
    ax.barh(range(len(g)), g["n"], color=colors, height=.66)
    ax.set_yticks(range(len(g)), labels, fontsize=8)
    for i, v in enumerate(g["n"]):
        ax.text(v + .12, i, str(int(v)), va="center", fontsize=8, color="#3b4453")
    ax.set_xlabel("число нарушений за период")
    ax.set_title("Что именно нарушается (цвет = тяжесть)", fontsize=9.5, loc="left", pad=8)
    ax.legend(handles=[Patch(color=c, label=s) for s, c in SEV_COLOR.items() if s in set(g["severity"])],
              fontsize=7.5, loc="lower right")
    return _save(fig, out)


def chart_lift_physics(assessments: Sequence[Any], out: Path = Path("results/charts/lifts.png")) -> Path:
    """По каждому подъёму: сила ветра, потребность/возможность оттяжки, использование стропа."""
    ids = [a.lift.lift_id for a in assessments]
    x = np.arange(len(ids)); w = .27
    fmax = [a.summary["wind_force_max_N"] for a in assessments]
    treq = [max(a.summary["tagline_deficit_max_N"], 0) + a.limits["tagline_capacity_N"] for a in assessments]
    tcap = [a.limits["tagline_capacity_N"] for a in assessments]
    usling = [a.summary["sling_utilization_max"] * 100 for a in assessments]
    ucrane = [(a.summary.get("crane_utilization_max") or 0) * 100 for a in assessments]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.6, 2.9), gridspec_kw={"width_ratios": [1.25, 1]})
    ax1.bar(x - w / 2, fmax, w, label="сила ветра на груз, Н", color="#4da3ff")
    ax1.bar(x + w / 2, treq, w, label="нужно на оттяжке, Н", color="#d6303a")
    ax1.bar(x + w / 2, tcap, w, label="доступно у людей, Н", color="#3fb950", alpha=.85)
    ax1.set_xticks(x, ids, fontsize=8); ax1.set_ylabel("Н")
    ax1.legend(fontsize=7.2, loc="upper left")
    ax1.set_title("Ветровая нагрузка и оттяжка", fontsize=9, loc="left", pad=6)

    ax2.bar(x - w / 2, usling, w, color="#ef8a17", label="строп, % от WLL")
    ax2.bar(x + w / 2, ucrane, w, color="#7c5cff", label="кран, % от характеристики")
    ax2.axhline(80, color="#d6303a", ls=":", lw=1); ax2.text(len(ids) - .4, 81, "лимит стропа 80 %", fontsize=6.8, color="#a52028", ha="right")
    ax2.axhline(90, color="#7c1220", ls=":", lw=1); ax2.text(len(ids) - .4, 91, "лимит крана 90 %", fontsize=6.8, color="#7c1220", ha="right")
    ax2.set_xticks(x, ids, fontsize=8); ax2.set_ylabel("% от допустимого"); ax2.set_ylim(0, max(105, max(usling + ucrane) * 1.15))
    ax2.legend(fontsize=7.2, loc="upper right")
    ax2.set_title("Использование несущей способности", fontsize=9, loc="left", pad=6)
    return _save(fig, out)


def chart_mc_probability(mc: Sequence[Any], out: Path = Path("results/charts/mc.png")) -> Path:
    ids = [m.lift_id for m in mc]
    keys = [("P_tagline_force_exceeded", "отказ оттяжки"),
            ("P_wind_above_ppr_limit", "ветер выше лимита ППР"),
            ("P_sling_utilization_exceeded", "перегруз стропа"),
            ("P_crane_chart_exceeded", "выход за характеристику крана")]
    x = np.arange(len(ids)); w = .8 / len(keys)
    fig, ax = plt.subplots(figsize=(8.6, 2.7))
    cols = ["#d6303a", "#ef8a17", "#e8c33c", "#7c5cff"]
    for i, (k, lab) in enumerate(keys):
        vals = [m.probabilities.get(k, 0) * 100 for m in mc]
        ax.bar(x + (i - len(keys) / 2 + .5) * w, vals, w, label=lab, color=cols[i])
    ax.set_xticks(x, ids, fontsize=8); ax.set_ylabel("вероятность, %"); ax.set_ylim(0, 105)
    ax.legend(fontsize=7.2, ncol=2, loc="upper center")
    n = mc[0].n if mc else 0
    ax.set_title(f"Monte Carlo: вероятность превышения лимита за окно подъёма ({n} прогонов, "
                 f"метод «{mc[0].method if mc else '—'}»)", fontsize=9.5, loc="left", pad=8)
    return _save(fig, out)


def chart_safe_wind(mc_entry: dict, out: Path = Path("results/charts/safe_wind.png")) -> Path:
    """Кривая «скорость ветра -> вероятность отказа» для самого критичного подъёма."""
    curve = mc_entry.get("safe_wind_curve", [])
    if not curve:
        fig, ax = plt.subplots(figsize=(5, 1.5)); ax.axis("off"); return _save(fig, out)
    v = [c["v_ref_ms"] for c in curve]
    p = [c["P_tagline_exceeded"] * 100 for c in curve]
    f = [c["wind_force_N"] / 1000 for c in curve]
    fig, ax = plt.subplots(figsize=(5.4, 2.8))
    ax.plot(v, p, color="#d6303a", lw=2, marker="o", ms=3, label="P(оттяжка не удержит)")
    ax.axhline(50, color="#7a8593", ls=":", lw=.9)
    ax.text(v[0], 51.5, "50 % — граница планирования", fontsize=6.8, color="#5a6472")
    ax.set_xlabel("скорость ветра на 10 м, м/с"); ax.set_ylabel("вероятность отказа, %", color="#a52028")
    ax.set_ylim(0, 105); ax.tick_params(axis="y", labelcolor="#a52028")
    ax2 = ax.twinx(); ax2.grid(False)
    ax2.plot(v, f, color="#1f6fb4", lw=1.6, ls="--", label="сила ветра на груз")
    ax2.set_ylabel("сила ветра, кН", color="#1f6fb4"); ax2.tick_params(axis="y", labelcolor="#1f6fb4")
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=7.2, loc="upper left")
    ax.set_title(f"Подъём {mc_entry.get('lift_id','')}: граница безопасных условий", fontsize=9.5, loc="left", pad=8)
    return _save(fig, out)


def chart_tornado(rows: Sequence[dict], out: Path = Path("results/charts/tornado.png")) -> Path:
    if not rows:
        fig, ax = plt.subplots(figsize=(5, 1.4)); ax.axis("off"); return _save(fig, out)
    rows = list(rows)[::-1]
    y = np.arange(len(rows))
    base = rows[len(rows) // 2]["deficit_at_low"]
    lo = [r["deficit_at_low"] for r in rows]
    hi = [r["deficit_at_high"] for r in rows]
    m = max(abs(min(lo)), abs(max(hi)), 1)
    fig, ax = plt.subplots(figsize=(6.8, max(2.0, 0.42 * len(rows) + 1.1)))
    ax.barh(y, [l - 0 for l in lo], .55, color="#4da3ff", label="нижняя граница входа")
    ax.barh(y, hi, .55, color="#d6303a", alpha=.85, label="верхняя граница входа")
    ax.set_yticks(y, [r["variable"] for r in rows], fontsize=7.6)
    ax.axvline(0, color="#3b4453", lw=.9)
    ax.set_xlabel("дефицит усилия на оттяжке, Н (>0 — груз не удерживается)")
    ax.legend(fontsize=7.2, loc="lower right")
    ax.set_title("Чувствительность: что сильнее всего двигает результат", fontsize=9.5, loc="left", pad=8)
    for i, r in enumerate(rows):
        ax.text(max(r["deficit_at_high"], r["deficit_at_low"]) + m * .02, i,
                f"±{r['swing']:.0f} Н", va="center", fontsize=7, color="#3b4453")
    return _save(fig, out)


def chart_exposure(agent_df: pd.DataFrame, out: Path = Path("results/charts/exposure.png")) -> Path:
    """Человеко-минуты воздействия по бригадам и правилам."""
    if agent_df is None or agent_df.empty:
        fig, ax = plt.subplots(figsize=(5, 1.4)); ax.axis("off")
        ax.text(.5, .5, "агентная симуляция не запускалась", ha="center", va="center", fontsize=9)
        return _save(fig, out)
    df = agent_df.copy()
    df["rule_id"] = df["rule_id"].astype(str)
    piv = df.pivot_table(index="crew_id", columns="rule_id", values="person_minutes",
                         aggfunc="sum", fill_value=0)
    fig, ax = plt.subplots(figsize=(7.4, 2.6))
    bottom = np.zeros(len(piv))
    palette = ["#d6303a", "#ef8a17", "#e8c33c", "#3aa6a0", "#7c5cff", "#4da3ff"]
    for i, col in enumerate(piv.columns):
        ax.bar(piv.index, piv[col] / 60.0, bottom=bottom, label=col,
               color=palette[i % len(palette)], width=.62)
        bottom += (piv[col] / 60.0).values
    ax.set_ylabel("человеко-часы воздействия")
    ax.legend(fontsize=6.8, loc="upper left", ncol=1, framealpha=.9)
    ax.set_title("Кто и под каким воздействием (агентная симуляция)", fontsize=9.5, loc="left", pad=8)
    plt.setp(ax.get_xticklabels(), fontsize=8)
    return _save(fig, out)


def chart_mc_pairs(pair_df: pd.DataFrame, out: Path = Path("results/chairs/mc_pairs.png")) -> Path:
    pair_df = pair_df[pair_df["P_any"] > 0].head(12).iloc[::-1]
    if pair_df.empty:
        fig, ax = plt.subplots(figsize=(5, 1.4)); ax.axis("off")
        ax.text(.5, .5, "случайных пересечений не выявлено", ha="center", va="center", fontsize=9)
        return _save(fig, Path("results/charts/mc_pairs.png"))
    fig, ax = plt.subplots(figsize=(7.6, max(2.0, 0.36 * len(pair_df) + 1)))
    labs = [f"{a} × {b}" for a, b in zip(pair_df["task_a"], pair_df["task_b"])]
    cols = ["#d6303a" if p > .5 else "#ef8a17" if p > .15 else "#3fb950" for p in pair_df["P_any"]]
    ax.barh(range(len(pair_df)), pair_df["P_any"] * 100, color=cols, height=.62)
    ax.set_yticks(range(len(pair_df)), labs, fontsize=7.4)
    ax.set_xlabel("в каком доле прогонов возникает конфликт, %")
    ax.set_xlim(0, 108)
    for i, (p, h) in enumerate(zip(pair_df["P_any"], pair_df["mean_overlap_h"])):
        ax.text(p * 100 + 1.2, i, f"{p*100:.0f}% · {h:.1f} ч", va="center", fontsize=7, color="#3b4453")
    ax.set_title("Monte Carlo графика: структурные конфликты (100 %) против случайных", fontsize=9.5, loc="left", pad=8)
    return _save(fig, Path("results/charts/mc_pairs.png"))
