"""
app_streamlit.py — панель прораба / инженера ПТО (Streamlit).

Запуск:
    streamlit run app_streamlit.py
    # или:  make panel

Принцип раздела 3 ТЗ: панель НЕ импортирует ядро и не считает сама. Она читает
только артефакты каталога results/pilot/ (scene_bundle.json, findings.json, *.csv,
*.png, simulation.gif). Поэтому панель можно открыть на любой машине, куда
скопировали results/pilot/, — даже без Python-ядра.

Если results/pilot/ пустой — панель подскажет запустить `make pilot`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

RESULTS = Path(st.session_state.get("results_dir", "results/pilot"))

st.set_page_config(page_title="Симуляция рисков — башня 400 м", page_icon="🏗️",
                   layout="wide", initial_sidebar_state="expanded")

DISCLAIMER = ("**Результат симуляции — поддержка решений, а не расчёт по нормам.** "
              "Допустимые значения — из ППР и паспортов оборудования. Решение принимает "
              "ответственный инженер.")


@st.cache_data(show_spinner=False)
def load_json(name: str):
    p = RESULTS / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


@st.cache_data(show_spinner=False)
def load_csv(name: str) -> pd.DataFrame:
    p = RESULTS / name
    return pd.read_csv(p) if p.exists() else pd.DataFrame()


def chart(name: str):
    p = RESULTS / "charts" / name
    if p.exists():
        st.image(str(p), width="stretch")
    else:
        st.info(f"нет графика {name} (запустите `make pilot`)")


# ---------------------------------------------------------------- заголовок
st.title("🏗️ Симуляция рисков строительных работ — башня 400 м, ярус L62")
bundle = load_json("scene_bundle.json")
if bundle is None:
    st.error("Нет results/pilot/scene_bundle.json. Запустите расчёт: `risk-sim pilot` или `make pilot`.")
    st.stop()

meta = bundle["timeline"]["meta"]
rules_meta = (bundle.get("rules") or {}).get("meta", {})
st.caption(f'{rules_meta.get("project", "")} · правила v{rules_meta.get("version", "?")} · '
           f'окно: {meta.get("t_start")} … {meta.get("t_end")}')
st.warning(DISCLAIMER, icon="⚠️")

findings = load_json("findings.json") or {}
flist = findings.get("findings", [])
fsum = bundle.get("findings_summary", {})
recs = load_csv("recommendations.csv")
mc = bundle.get("monte_carlo", {}) or {}
sched = mc.get("schedule") or {}
agents = bundle.get("agents") or {}

# ---------------------------------------------------------------- сайдбар
with st.sidebar:
    st.header("Фильтры и данные")
    sev_sel = st.multiselect("Критичность", ["critical", "high", "medium", "low", "info"],
                             default=["critical", "high", "medium"])
    fdf = pd.DataFrame(flist) if flist else pd.DataFrame()
    rule_sel = None
    if not fdf.empty:
        rule_sel = st.multiselect("Правила", sorted(fdf["rule_id"].unique()))
    st.divider()
    st.subheader("Артефакты")
    st.caption("Панель читает только results/pilot/ — ядро можно не устанавливать.")
    st.code("streamlit run app_streamlit.py", language="bash")
    if bundle.get("model"):
        model = bundle["model"]
        st.caption(
            f"IFC: {model['source_file']} · элементов: {model['element_count']} · "
            f"треугольников: {model['triangle_count']} · ошибок: {model['failed_element_count']}"
        )
    st.caption(rules_meta.get("disclaimer", DISCLAIMER))


def fview() -> pd.DataFrame:
    if fdf.empty:
        return fdf
    d = fdf
    if sev_sel:
        d = d[d["severity"].isin(sev_sel)]
    if rule_sel:
        d = d[d["rule_id"].isin(rule_sel)]
    return d


# ---------------------------------------------------------------- метрики
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Нарушений всего", fsum.get("total", len(flist)))
c2.metric("Critical", fsum.get("by_severity", {}).get("critical", 0))
c3.metric("Чел·мин, консервативно", f"{fsum.get('person_minutes_total', 0):,.0f}")
c4.metric("P(две бригады в зоне)",
          f"{(sched.get('rule_probabilities', {}) or {}).get('no_two_crews_same_zone', 0):.0%}"
          if sched else "—")
c5.metric("Подъёмов проверено", len(bundle.get("lifts", [])))
st.caption("Консервативная оценка = окна правил × все люди зоны; измеренная по агентным трекам — на вкладке «👷 Люди». Разница — запас безопасности, а не расхождение моделей.")

tabs = st.tabs(["📌 Резюме", "⚠️ Конфликты", "📅 Расписание", "🏗️ Подъёмы",
                "🧪 What-if", "🎲 Monte Carlo", "👷 Люди", "🌬️ Ветер",
                "🛠 Рекомендации", "🎬 Видео и 3D"])

# ---------------------------------------------------------------- 1 резюме
with tabs[0]:
    st.subheader("Сводка по сценарию")
    l, r = st.columns([1, 1])
    with l:
        by_rule = pd.DataFrame(fsum.get("by_rule", []))
        if not by_rule.empty:
            st.dataframe(by_rule[["rule_id", "severity", "n", "person_min"]]
                         .sort_values("person_min", ascending=False),
                         width="stretch", hide_index=True)
        chart("by_rule.png")
    with r:
        chart("heatmap.png")
        top = pd.DataFrame(fsum.get("top_findings", []))
        if not top.empty:
            st.subheader("Топ-10 по чел·мин")
            st.dataframe(top, width="stretch", hide_index=True)

# ---------------------------------------------------------------- 2 конфликты
with tabs[1]:
    st.subheader("Конфликты и нарушения")
    d = fview()
    if d.empty:
        st.success("Нарушений по выбранным фильтрам нет.")
    else:
        cols = [c for c in ["start", "end", "severity", "rule_id", "zone_ids",
                            "people_exposed", "exposure_person_min", "message", "source"]
                if c in d.columns]
        st.dataframe(d[cols].sort_values(["severity", "start"], ascending=[True, True]),
                     width="stretch", hide_index=True, height=420)
        st.caption(f"показано {len(d)} из {len(fdf)} записей findings.json")
        for _, row in d.sort_values("exposure_person_min", ascending=False).head(8).iterrows():
            with st.expander(f"[{row['severity']}] {row.get('rule_title','')} — {row.get('message','')[:90]}"):
                st.write(row.get("message", ""))
                st.caption(f"Источник правила: {row.get('source','')}")
                st.json({k: row[k] for k in ("zone_ids", "task_ids", "crew_ids", "people_exposed",
                                             "exposure_person_min", "start", "end") if k in row})

# ---------------------------------------------------------------- 2b расписание
with tabs[2]:
    st.subheader("Расписание: задачи, бригады, окна нарушений")
    import altair as alt
    tdf = pd.DataFrame(bundle["timeline"]["tasks"])
    if not tdf.empty:
        tdf["start"] = pd.to_datetime(tdf["start"]); tdf["end"] = pd.to_datetime(tdf["end"])
        g = alt.Chart(tdf).mark_bar(cornerRadius=2).encode(
            x=alt.X("start:T", title=None), x2=alt.X2("end:T"),
            y=alt.Y("zone_id:N", title=None, sort=alt.EncodingSortField("start")),
            color=alt.Color("crew_id:N", title="Бригада"),
            tooltip=["task_id", "name", "crew_id", "zone_id", "start", "end"]).properties(height=340)
        st.altair_chart(g, width="stretch")
        st.caption("Гант по зонам: видно совмещения бригад и работу друг над другом "
                   "(зоны одного уровня в один момент = конфликт по правилу no_two_crews_same_zone).")
    if not fdf.empty:
        wdf = fdf.dropna(subset=["start", "end"]).copy()
        wdf["start"] = pd.to_datetime(wdf["start"]); wdf["end"] = pd.to_datetime(wdf["end"])
        w2 = alt.Chart(wdf).mark_bar(opacity=0.75).encode(
            x=alt.X("start:T", title=None), x2=alt.X2("end:T"),
            y=alt.Y("rule_id:N", title=None),
            color=alt.Color("severity:N", scale=alt.Scale(
                domain=["critical", "high", "medium", "low"],
                range=["#c62828", "#e65100", "#b28704", "#2e7d32"])),
            tooltip=["rule_id", "severity", "start", "end", "message"]).properties(height=260)
        st.markdown("**Окна нарушений:**")
        st.altair_chart(w2, width="stretch")

# ---------------------------------------------------------------- 3 подъёмы
with tabs[3]:
    st.subheader("Проверка подъёмов (упрощённая физика + Monte Carlo)")
    lift_sum = load_csv("lift_summary.csv")
    if not lift_sum.empty:
        st.dataframe(lift_sum, width="stretch", hide_index=True)
    lifts = bundle.get("lifts", [])
    if lifts:
        sel = st.selectbox("Подъём", [l["lift_id"] for l in lifts],
                           format_func=lambda i: next(
                               f'{i} — {l["load_name"]} ({l["input"]["mass_kg"]:.0f} кг)'
                               for l in lifts if l["lift_id"] == i))
        L = next(l for l in lifts if l["lift_id"] == sel)
        c1, c2, c3, c4 = st.columns(4)
        s = L.get("summary", {})
        c1.metric("Макс. ветер операции", f'{s.get("v_work_max_ms", 0):.1f} м/с')
        c2.metric("Нагрузка на строп (исп.)", f'{s.get("sling_utilization_max", 0):.0%}')
        c3.metric("Исп. грузоподъёмности крана", f'{s.get("crane_utilization_max", 0):.0%}')
        c4.metric("Дефицит оттяжки", f'{s.get("tagline_deficit_max_N", 0):.0f} Н')
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Безопасные окна** (по оттяжке/ветру):")
            wins = s.get("safe_windows", [])
            if wins:
                st.dataframe(pd.DataFrame(wins), width="stretch", hide_index=True)
            else:
                st.warning("Окон нет — подъём в текущей оснастке не рекомендуется.")
        with c2:
            st.markdown("**Нарушения подъёма:**")
            v = L.get("violations", [])
            if v:
                st.dataframe(pd.DataFrame(v)[["rule_id", "severity", "message"]],
                             width="stretch", hide_index=True)
            else:
                st.success("Нарушений нет.")
        chart("lifts.png")
        chart("safe_wind.png")

# ---------------------------------------------------------------- 3b what-if
with tabs[4]:
    st.subheader("What-if: чувствительность и варианты оснастки")
    grid = load_csv("scenario_grid.csv")
    if not grid.empty:
        st.markdown("**Сетка «ветер × люди на оттяжке»: дефицит усилия, Н** "
                    "(красное — подъём не удерживается):")
        piv = grid.pivot(index="v_ref_ms", columns="tagline_people",
                         values="tagline_deficit_N")
        sty = piv.style.map(lambda v: "background-color:#c62828;color:#ffffff" if v > 0
                            else "background-color:#2e7d32;color:#ffffff")
        st.dataframe(sty, width="stretch")
        st.caption("Сетка построена для самого критичного подъёма сценария "
                   "(results/scenario_grid.csv).")
    tor = load_csv("tornado.csv")
    if not tor.empty:
        import altair as alt
        st.markdown("**Торнадо: что сильнее всего двигает дефицит оттяжки (±20%):**")
        c = alt.Chart(tor).mark_bar(color="#4da3ff").encode(
            x=alt.X("swing:Q", title="размах влияния, Н"),
            y=alt.Y("variable:N", sort="-x", title=None),
            tooltip=["variable", "base_value", "low_value", "high_value",
                     "deficit_at_low", "deficit_at_high", "swing"]).properties(height=220)
        st.altair_chart(c, width="stretch")
    mit = load_csv("mitigations.csv")
    if not mit.empty:
        st.markdown("**Варианты сделать подъём допустимым:**")
        for _, m in mit.iterrows():
            icon = "✅" if bool(m["feasible"]) else "❌"
            with st.expander(f"{icon} {m['option']} — {m['value']}", expanded=bool(m["feasible"])):
                st.write(m.get("note", ""))
    if grid.empty and tor.empty and mit.empty:
        st.info("Нет scenario_grid.csv / tornado.csv / mitigations.csv — "
                "пересоберите отчёт: `make report`.")
    chart("tornado.png")

# ---------------------------------------------------------------- 4 Monte Carlo
with tabs[5]:
    st.subheader("Monte Carlo: порывы, Cd, отклонения бригад")
    mcp = load_csv("mc_lift_probability.csv")
    if not mcp.empty:
        st.dataframe(mcp, width="stretch", hide_index=True)
    st.markdown("**Вероятности по сценарию (расписание):**")
    rp = sched.get("rule_probabilities", {}) or {}
    if rp:
        st.json(rp)
    eh = sched.get("exposure_person_hours", {}) or {}
    if eh:
        st.metric("Чел·ч воздействия, p95", f"{eh.get('p95', 0):,.0f}")
    pairs = sched.get("top_pairs", [])
    if pairs:
        st.markdown("**Наиболее вероятные пары конфликтов:**")
        st.dataframe(pd.DataFrame(pairs), width="stretch", hide_index=True)
    c1, c2 = st.columns(2)
    with c1:
        chart("mc.png")
    with c2:
        chart("tornado.png")
    chart("mc_pairs.png")

# ---------------------------------------------------------------- 5 люди
with tabs[6]:
    st.subheader("Бригады: маршруты, состояния, воздействия")
    asum = agents.get("summary", {})
    if asum:
        c1, c2, c3 = st.columns(3)
        c1.metric("Кадров треков", asum.get("frames", asum.get("n_frames", "—")))
        c2.metric("Событий воздействия", asum.get("events", "—"))
        c3.metric("Чел·мин", f"{asum.get('person_minutes', 0):,.0f}")
    ev = load_csv("agent_events.csv")
    if not ev.empty:
        st.dataframe(ev, width="stretch", hide_index=True, height=360)
    chart("exposure.png")

# ---------------------------------------------------------------- 6 ветер
with tabs[7]:
    st.subheader("Ветер: профиль, порывы, лимиты")
    frames = pd.DataFrame([
        {"t": f["t"], **{k: v for k, v in f.get("wind", {}).items()}}
        for f in bundle["timeline"]["frames"][::4]])
    if not frames.empty:
        frames["t"] = pd.to_datetime(frames["t"])
        frames = frames.set_index("t")
        lim = bundle["timeline"]["crane"].get("limits", {}).get("wind_limit_ops_ms")
        st.line_chart(frames[["gust_at_ref_ms", "gust_at_top_ms", "mean_at_ref_ms"]], height=300)
        if lim:
            st.caption(f"Лимит операций по паспорту крана: {lim} м/с (порыв на высоте оголовка). "
                       "Пересечение графика 'gust_at_top' с лимитом = окно остановки крана.")
    chart("wind.png")

# ---------------------------------------------------------------- 7 рекомендации
with tabs[8]:
    st.subheader("Рекомендации (что изменить в ППР/графике)")
    if recs.empty:
        st.info("recommendations.csv пуст.")
    else:
        for _, r in recs.sort_values("priority").iterrows():
            with st.expander(f"[приоритет {r['priority']}] {r['title']}", expanded=(r["priority"] == 1)):
                st.markdown(f"**Что:** {r['what']}")
                st.markdown(f"**Основание:** {r['basis']}")
                st.markdown(f"**Эффект:** {r['effect']}")
                opts = str(r.get("options", "")).split("|")
                st.markdown("**Варианты:** " + "; ".join(f"• {o.strip()}" for o in opts if o.strip()))
    ba = load_csv("before_after.csv")
    if not ba.empty:
        st.subheader("До / после предложенных изменений")
        st.dataframe(ba, width="stretch", hide_index=True)

# ---------------------------------------------------------------- 8 видео
with tabs[9]:
    st.subheader("Видео и 3D")
    gif = RESULTS / "simulation.gif"
    if gif.exists():
        st.image(str(gif), caption="Анимация недели (план + разрез), 1 кадр ≈ 2 ч",
                 width="stretch")
    else:
        st.code("python tools/make_gif.py --frames 64 --fps 8", language="bash")
    st.markdown("**Браузерный просмотр:**")
    st.code("python -m http.server 8000 -d viewer   # затем открыть http://localhost:8000",
            language="bash")
    st.markdown("**Blender (видео .mp4):**")
    st.code("./blender/render.sh        # EEVEE Next, 1280×720\n"
            "ENGINE=CYCLES SAMPLES=64 ./blender/render.sh", language="bash")
    st.markdown("**glTF для Three.js:**")
    st.code("blender -b scenes/building.blend -P blender/export_gltf.py -- --out scenes/building.glb",
            language="bash")
    st.warning(DISCLAIMER, icon="⚠️")
