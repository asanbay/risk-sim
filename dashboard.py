from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

RULE_TITLES = {
    "no_two_crews_same_zone": "Две бригады в одной зоне",
    "no_work_under_crane_hazard": "Работа в зоне крана",
    "edge_work_requires_guardrail": "Край без ограждения",
    "stop_crane_ops_above_wind_limit": "Ветер выше лимита",
}

st.set_page_config(page_title="Риски строительных работ", page_icon=None, layout="wide")


@st.cache_data
def load_scenario(path: str, modified: float) -> dict:
    del modified
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


st.title("Система симуляции рисков строительных работ")
st.caption("Панель руководителя · ядро и визуализация разделены через JSON")
paths = sorted(Path("results").glob("scenario_*.json"))
if not paths:
    st.error("Сначала выполните `.venv/bin/risk-sim run` из папки проекта.")
    st.stop()
selected = st.sidebar.selectbox("Сценарий", paths, format_func=lambda path: path.stem)
scenario = load_scenario(str(selected), selected.stat().st_mtime)
metadata = scenario["metadata"]
st.sidebar.caption(f"Версия модели: {metadata['model_version']}")
st.sidebar.caption(f"Сформировано: {metadata['generated_at']}")
st.warning(metadata["disclaimer"])
if metadata["source_type"] == "synthetic_demo":
    st.error("ДЕМОНСТРАЦИОННЫЕ ДАННЫЕ. Не использовать для производства работ.")

events = scenario["events"]
critical = sum(event["severity"] == "critical" for event in events)
schedule = scenario["monte_carlo"]["schedule"]
first_monte = scenario["monte_carlo"]["wind_and_lift"][0]
columns = st.columns(5)
columns[0].metric("События", len(events))
columns[1].metric("Критические", critical)
columns[2].metric("Календарный конфликт", f"{schedule['probability_any_schedule_conflict']['probability']:.1%}")
columns[3].metric("Ветер выше лимита", f"{first_monte['probabilities']['operational_wind_limit']['probability']:.1%}")
columns[4].metric("Время расчёта", f"{metadata.get('core_runtime_s', 0):.2f} c")

map_tab, conflicts_tab, lift_tab, monte_tab, assumptions_tab = st.tabs([
    "Карта рисков", "Конфликты", "Подъём", "Monte Carlo", "Допущения и отчёты",
])

with map_tab:
    st.subheader("Повторяемость и серьёзность по уровням и неделям")
    heatmap = scenario["risk_heatmap"]
    if heatmap:
        frame = pd.DataFrame(heatmap)
        weeks = sorted(frame.week.unique())
        levels = sorted(frame.level.unique(), reverse=True)
        scores = frame.pivot(index="level", columns="week", values="severity_score").reindex(index=levels, columns=weeks)
        counts = frame.pivot(index="level", columns="week", values="event_count").reindex(index=levels, columns=weeks)
        figure = go.Figure(go.Heatmap(
            z=scores.values, x=weeks, y=levels, text=counts.values,
            texttemplate="%{text} событий", colorscale="YlOrRd", colorbar={"title": "Балл"},
            hovertemplate="%{y}<br>%{x}<br>балл %{z}<extra></extra>",
        ))
        figure.update_layout(height=420, margin=dict(l=20, r=20, t=20, b=20))
        st.plotly_chart(figure, use_container_width=True)
    else:
        st.success("События по заданным правилам не обнаружены.")
    st.subheader("Наиболее повторяемые пары при отклонениях графика")
    pairs = pd.DataFrame(schedule["pair_probabilities"])
    if not pairs.empty:
        pairs["rule"] = pairs.rule.map(RULE_TITLES)
        pairs["probability"] = (100 * pairs.probability).map(lambda value: f"{value:.1f}%")
        st.dataframe(pairs[["rule", "task_a", "task_b", "zone_id", "probability", "exceedances"]], use_container_width=True, hide_index=True)

with conflicts_tab:
    st.subheader("Журнал событий")
    severities = st.multiselect("Серьёзность", sorted({event["severity"] for event in events}), default=sorted({event["severity"] for event in events}))
    rules = st.multiselect("Правила", sorted({event["rule"] for event in events}), default=sorted({event["rule"] for event in events}), format_func=lambda rule: RULE_TITLES[rule])
    filtered = [event for event in events if event["severity"] in severities and event["rule"] in rules]
    rows = [{
        "Начало": event["from"], "Конец": event["to"], "Уровень": event["level"], "Зона": event["zone_id"],
        "Серьёзность": event["severity"], "Правило": RULE_TITLES[event["rule"]],
        "Работа A": event["task_a"], "Работа B": event["task_b"],
        "Детали": json.dumps(event["details"], ensure_ascii=False), "Ссылка": event["reference"],
    } for event in filtered]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.download_button("Скачать все события CSV", Path("results/conflicts.csv").read_bytes(), "conflicts.csv", "text/csv")

with lift_tab:
    lift_ids = [lift["summary"]["lift_id"] for lift in scenario["lifts"]]
    lift_id = st.selectbox("Подъём", lift_ids)
    lift = next(item for item in scenario["lifts"] if item["summary"]["lift_id"] == lift_id)
    summary = lift["summary"]
    left, right = st.columns(2)
    left.subheader("Траектория центра масс")
    track = pd.DataFrame(lift["track"])
    positions = pd.DataFrame(track.pos.tolist(), columns=["x", "y", "z"])
    trajectory = go.Figure(go.Scatter3d(
        x=positions.x, y=positions.y, z=positions.z, mode="lines+markers",
        marker={"size": 2, "color": track.t, "colorscale": "Viridis"},
        line={"color": "#38bdf8", "width": 4}, hovertemplate="x %{x:.2f}<br>y %{y:.2f}<br>z %{z:.2f}<extra></extra>",
    ))
    trajectory.update_layout(height=460, scene=dict(xaxis_title="x, м", yaxis_title="y, м", zaxis_title="z, м", aspectmode="data"), margin=dict(l=0, r=0, t=10, b=0))
    left.plotly_chart(trajectory, use_container_width=True)
    right.subheader("Раскачка и усилия")
    figure = go.Figure()
    figure.add_trace(go.Scatter(x=track.t, y=track.swing_deg, name="Угол, °", line=dict(color="#38bdf8")))
    figure.add_trace(go.Scatter(x=track.t, y=track.wind_force_n / 1000, name="Ветер, кН", yaxis="y2", line=dict(color="#fb923c")))
    figure.add_hline(y=summary["tagline_capacity_n"] / 1000, yref="y2", line_dash="dash", line_color="#fb4d5e", annotation_text="Оттяжка, кН")
    figure.update_layout(height=460, yaxis=dict(title="Угол, °"), yaxis2=dict(title="Сила, кН", overlaying="y", side="right"), xaxis_title="Время, с", legend=dict(orientation="h"))
    right.plotly_chart(figure, use_container_width=True)
    metrics = pd.DataFrame([
        ["Максимальный порыв, м/с", summary["max_wind_ms"], summary["wind_limit_ms"]],
        ["Сила ветра, кН", summary["max_wind_force_n"] / 1000, summary["tagline_capacity_n"] / 1000],
        ["Усилие на ветвь, кН", summary["sling_tension_per_leg_n"] / 1000, summary["sling_wll_per_leg_n"] / 1000],
        ["Максимальный угол, °", summary["max_abs_swing_deg"], "наблюдаемый показатель"],
        ["Смещение, м", summary["max_horizontal_displacement_m"], "наблюдаемый показатель"],
    ], columns=["Показатель", "Расчёт", "Порог / пояснение"])
    st.dataframe(metrics, use_container_width=True, hide_index=True)
    st.subheader("Квазистатический анализ чувствительности")
    sensitivity = pd.DataFrame(lift["sensitivity"])
    st.dataframe(sensitivity[[
        "parameter", "change_percent", "value", "max_wind_force_n", "sling_utilization",
        "static_deflection_at_max_wind_deg", "small_angle_period_s", "threshold_conclusion_changed",
    ]].rename(columns={
        "parameter": "Параметр", "change_percent": "Изменение, %", "value": "Значение",
        "max_wind_force_n": "Сила ветра, Н", "sling_utilization": "Доля WLL",
        "static_deflection_at_max_wind_deg": "Статический угол, °",
        "small_angle_period_s": "Период малых колебаний, с",
        "threshold_conclusion_changed": "Вывод по порогу изменился",
    }), use_container_width=True, hide_index=True)
    st.caption("Динамика повторно не интегрируется; это аналитическая чувствительность сил, статического отклонения и периода малых колебаний.")

with monte_tab:
    st.subheader("Ветер и груз")
    monte = next(item for item in scenario["monte_carlo"]["wind_and_lift"] if item["lift_id"] == lift_id)
    probabilities = pd.DataFrame([
        ["Порог оттяжки", monte["probabilities"]["tagline_capacity"]],
        ["Рабочий лимит ветра", monte["probabilities"]["operational_wind_limit"]],
        ["Статический WLL строп", monte["probabilities"]["static_sling_wll"]],
        ["Любой заданный порог", monte["probabilities"]["any_defined_threshold"]],
    ], columns=["Порог", "Результат"])
    probabilities["Вероятность"] = probabilities.Результат.map(lambda result: f"{result['probability']:.1%}")
    probabilities["Wilson 95%"] = probabilities.Результат.map(lambda result: f"{result['wilson_95_low']:.1%} – {result['wilson_95_high']:.1%}")
    st.dataframe(probabilities[["Порог", "Вероятность", "Wilson 95%"]], use_container_width=True, hide_index=True)
    sample_path = selected.parent / "monte_carlo_samples.csv"
    if sample_path.exists():
        samples = pd.read_csv(sample_path)
        samples = samples[samples.lift_id == lift_id]
        figure = go.Figure(go.Histogram(x=samples.wind_force_n / 1000, nbinsx=45, marker_color="#38bdf8"))
        figure.add_vline(x=monte["thresholds"]["tagline_capacity_n"] / 1000, line_dash="dash", line_color="#fb4d5e", annotation_text="Порог оттяжки")
        figure.update_layout(height=400, xaxis_title="Квазистатическая сила ветра, кН", yaxis_title="Число прогонов")
        st.plotly_chart(figure, use_container_width=True)
    st.info(monte["interpretation"])
    st.json(monte["distributions"], expanded=False)
    st.subheader("Отклонения графика")
    st.json(schedule["distributions"], expanded=False)
    st.info(schedule["interpretation"])

with assumptions_tab:
    st.subheader("Проверка на известных случаях")
    checks = []
    for name, check in scenario["validation"]["checks"].items():
        checks.append({"Проверка": name, "Расчёт": check.get("actual_n", check.get("actual_s", check.get("actual_rad", check.get("actual_max_abs_difference_rad")))), "Ожидание": check.get("expected_n", check.get("expected_s", check.get("expected_rad"))), "Пройдено": bool(check["pass"])})
    st.dataframe(pd.DataFrame(checks), use_container_width=True, hide_index=True)
    st.subheader("Допущения")
    for assumption in scenario["assumptions"]:
        st.markdown(f"- {assumption}")
    st.subheader("Хеши входных файлов")
    st.json(metadata["source_sha256"], expanded=False)
    st.subheader("Автоматические отчёты")
    docx = selected.parent / scenario["artifacts"]["report_docx"]
    pdf = selected.parent / scenario["artifacts"]["report_pdf"]
    first, second = st.columns(2)
    first.download_button("Скачать Word", docx.read_bytes(), docx.name)
    second.download_button("Скачать PDF", pdf.read_bytes(), pdf.name)
    st.caption("Интерактивная 3D-страница: `.venv/bin/risk-sim serve` → http://127.0.0.1:8000")
