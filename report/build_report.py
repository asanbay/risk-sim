"""
report/build_report.py — автоматическая сборка отчёта (раздел 12 спецификации).

Порядок разделов фиксирован и совпадает с ТЗ:
  титул -> резюме -> конфликты -> тепловая карта -> подъёмы ->
  рекомендации -> «до и после» -> допущения и ограничения -> приложение

Два формата из одного источника:
  results/report.html  — самодостаточный (картинки в base64), открывается где угодно;
  results/report.docx  — для передачи в ПТО и подписи;
  results/report.pdf   — если установлен WeasyPrint (необязательно).

Каждая рекомендация обязана иметь: что сделать, на каком основании
(ссылка на правило и его source), и какой количественный эффект ожидается.
Рекомендация без численного эффекта — это мнение, а не рекомендация.
"""
from __future__ import annotations

import html as _html
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from report import charts as C
from core.sim.sensitivity import mitigation_options, scenario_grid, tornado


# ===========================================================================
# рекомендации
# ===========================================================================
def make_recommendations(ctx, findings, assessments, mc_lifts, sched_mc) -> list[dict[str, Any]]:
    """Генерирует конкретные предложения из найденных нарушений."""
    recs: list[dict[str, Any]] = []
    by_rule: dict[str, list] = {}
    for f in findings:
        by_rule.setdefault(f.rule_id, []).append(f)

    ds = ctx.ds

    # --- 1. оттяжка / парусность ---------------------------------------
    tag = by_rule.get("tagline_force_within_capacity", [])
    if tag:
        worst = None
        for a in assessments:
            if any(v["rule_id"] == "tagline_force_within_capacity" for v in a.violations):
                if worst is None or a.summary["tagline_deficit_max_N"] > worst.summary["tagline_deficit_max_N"]:
                    worst = a
        opts = mitigation_options(ctx, worst.lift, worst.summary["v_ref_max_ms"]) if worst else []
        recs.append({
            "priority": 1, "rule": "tagline_force_within_capacity",
            "title": "Заменить удержание груза людьми на технологическое решение",
            "what": (f"Подъём {worst.lift.lift_id} ({worst.lift.load_name}, {worst.lift.mass_kg:.0f} кг, "
                     f"парусность {worst.lift.area_m2:.1f} м²) на высоте {worst.lift.hook_height_m:.0f} м "
                     f"не удерживается оттяжкой: требуется {worst.summary['tagline_required_N'] if 'tagline_required_N' in worst.summary else worst.summary['wind_force_max_N']:.0f} Н, "
                     f"доступно {worst.limits['tagline_capacity_N']:.0f} Н "
                     f"({worst.lift.tagline_people} чел.)."),
            "options": opts,
            "basis": ctx.cfg.rule("tagline_force_within_capacity").source,
            "effect": (f"Устраняет {len(tag)} нарушений. Предельная безопасная скорость ветра "
                       f"поднимается с {worst.limits.get('safe_wind_ms_by_tagline_at_ref_height')} м/с "
                       f"до лимита ППР {worst.lift.wind_limit_ms:.0f} м/с, то есть число пригодных "
                       f"для подъёма окон за неделю вырастает с {len(worst.summary.get('safe_windows', []))} "
                       f"до практически всех рабочих часов."),
            "source_lift": worst.lift.lift_id,
        })

    # --- 2. ограждение края до начала работ ----------------------------
    edge = by_rule.get("edge_work_requires_guardrail", [])
    if edge:
        rows = []
        for f in edge:
            t0 = pd.Timestamp(f.start) if f.start is not None else None
            ready = f.metrics.get("guardrail_ready")
            rows.append({"zone": (f.zone_ids or ["—"])[0], "task": (f.task_ids or ["—"])[0],
                         "start": t0.strftime("%d.%m %H:%M") if t0 is not None else "—",
                         "guardrail_ready": ready or "не запланировано",
                         "people": f.people_exposed})
        first = min([pd.Timestamp(f.start) for f in edge if f.start is not None], default=None)
        recs.append({
            "priority": 1, "rule": "edge_work_requires_guardrail",
            "title": "Перенести монтаж ограждения ДО начала работ у края",
            "what": ("Ограждение отсутствует в момент начала работ на следующих участках: " +
                     "; ".join(f"{r['zone']} (задача {r['task']} с {r['start']}, ограждение — {r['guardrail_ready']})"
                               for r in rows) + "."),
            "options": [{"option": "изменить последовательность", "value":
                         f"поставить монтаж ограждения не позднее {first.strftime('%d.%m %H:%M')}" if first is not None else "",
                         "feasible": True,
                         "note": "работа «ограждение -> всё остальное» вместо параллельной"},
                        {"option": "альтернативная защита", "value":
                         "страховочная система с анкерной линией + настил", "feasible": True,
                         "note": "применяется, если ограждение физически невозможно установить до работ"}],
            "basis": ctx.cfg.rule("edge_work_requires_guardrail").source,
            "effect": f"Устраняет {len(edge)} нарушений категории critical, "
                      f"суммарно {sum(f.people_exposed for f in edge)} чел. выводится из зоны падения.",
            "table": rows,
        })

    # --- 3. траектория груза над людьми --------------------------------
    path = by_rule.get("load_path_over_people", [])
    under = by_rule.get("no_work_under_crane_hazard", [])
    if path or under:
        lifts_hit = sorted({f.lift_id for f in (path + under) if f.lift_id})
        people_max = max([f.people_exposed for f in (path + under)] or [0])
        recs.append({
            "priority": 1, "rule": "load_path_over_people",
            "title": "Развести подъёмы и работы людей во времени, либо изменить траекторию",
            "what": (f"Траектории подъёмов {', '.join(lifts_hit)} проходят над зонами, где в это же время "
                     f"находятся люди (до {people_max} чел. одновременно)."),
            "options": [
                {"option": "окно тишины", "value": "остановка работ в зоне на время подъёма (15-40 мин)",
                 "feasible": True, "note": "самое дешёвое решение; вводится сигналом и записью в наряд-допуске"},
                {"option": "изменить точку приёмки", "value": "перенести приёмку на край яруса со стороны крана",
                 "feasible": True, "note": "убирает пересечение с рабочей зоной, но удлиняет горизонтальную подачу"},
                {"option": "изменить вылет/расположение склада", "value": "склад под стрелой, приёмка по кратчайшей хорде",
                 "feasible": True, "note": "требует перепланировки ПОС"},
            ],
            "basis": ctx.cfg.rule("load_path_over_people").source,
            "effect": f"Устраняет {len(path) + len(under)} нарушений critical; "
                      f"число людей под грузом снижается до нуля при введении окна тишины.",
        })

    # --- 4. ветер: перенос в безопасные окна ---------------------------
    wind = by_rule.get("stop_crane_ops_above_wind_limit", [])
    if wind:
        tot_h = sum(f.metrics.get("hours_over_limit", 0) for f in wind)
        wlist = []
        for a in assessments:
            wins = a.summary.get("safe_windows", [])
            if wins:
                wlist.append({"lift": a.lift.lift_id,
                              "limit_ref_ms": round(min(a.limits.get("safe_wind_ms_by_tagline_at_ref_height") or 99,
                                                        a.lift.wind_limit_ms), 1),
                              "windows": len(wins),
                              "hours": round(sum(w["hours"] for w in wins), 1),
                              "nearest": wins[0]["start"], "nearest_h": wins[0]["hours"]})
        recs.append({
            "priority": 2, "rule": "stop_crane_ops_above_wind_limit",
            "title": "Планировать подъёмы по ветровым окнам, а не по календарю",
            "what": (f"В {len(wind)} случаях крановая операция попадает на ветер выше лимита ППР; "
                     f"суммарно {tot_h:.0f} ч работ в недопустимых условиях."),
            "options": [{"option": "перенести в окно", "value":
                         "; ".join(f"{w['lift']}: {w['windows']} окон / {w['hours']} ч, ближайшее {w['nearest']} ({w['nearest_h']} ч)"
                                   for w in wlist[:4]),
                         "feasible": True,
                         "note": "окна рассчитаны по фактическому ряду ветра с учётом и лимита ППР, и возможности оттяжки"}],
            "basis": ctx.cfg.rule("stop_crane_ops_above_wind_limit").source,
            "effect": f"Устраняет {len(wind)} нарушений critical без изменения объёма работ — меняется только время.",
            "table": wlist,
        })

    # --- 5. перекрытый проход ------------------------------------------
    pas = by_rule.get("passage_must_stay_clear", [])
    if pas:
        recs.append({
            "priority": 2, "rule": "passage_must_stay_clear",
            "title": "Убрать складирование с путей движения и эвакуации",
            "what": "; ".join(f"{(f.zone_ids or ['—'])[0]}: {f.message}" for f in pas[:3]),
            "options": [{"option": "перенести склад", "value": "на LAYDOWN-зону яруса или на площадку у земли",
                         "feasible": True, "note": "проверить допустимую нагрузку на перекрытие у конструктора"},
                        {"option": "оставить коридор", "value": "минимум 1.2 м свободного прохода (0.8 м для носилок)",
                         "feasible": True, "note": "ширину задать в ПОС и контролировать ежедневно"}],
            "basis": ctx.cfg.rule("passage_must_stay_clear").source,
            "effect": f"Устраняет {len(pas)} нарушений high; восстанавливает путь эвакуации с яруса.",
        })

    # --- 6. совмещение бригад / ярусов ---------------------------------
    crews = by_rule.get("no_two_crews_same_zone", [])
    stacked = by_rule.get("no_work_below_active_work", [])
    if crews or stacked:
        p_struct = (sched_mc.rule_probabilities.get("no_two_crews_same_zone") if sched_mc else None)
        recs.append({
            "priority": 2, "rule": "no_two_crews_same_zone",
            "title": "Разделить бригады по захваткам либо по времени",
            "what": (f"{len(crews)} случаев одновременной работы двух бригад в пересекающихся зонах"
                     + (f", {len(stacked)} случаев работ друг над другом." if stacked else ".")),
            "options": [{"option": "сдвинуть по времени", "value":
                         "минимум 1 смена разрыва между захватками", "feasible": True,
                         "note": ("Monte Carlo показывает: конфликт возникает в "
                                  f"{p_struct*100:.0f} % прогонов с опозданиями — он заложен в самом графике, "
                                  "а не является следствием случайности" if p_struct else
                                  "проверьте вероятность в mc_schedule_pairs.csv")},
                        {"option": "разделить физически", "value": "ограждение/настил между захватками",
                         "feasible": True, "note": "применяется, когда сдвиг по времени срывает сроки"}],
            "basis": ctx.cfg.rule("no_two_crews_same_zone").source,
            "effect": f"Устраняет {len(crews) + len(stacked)} нарушений; "
                      f"снижает число людей в одной зоне на {max([f.people_exposed for f in (crews + stacked)] or [0])} чел.",
        })

    # --- 7. бетонирование под грузом ------------------------------------
    pour = by_rule.get("no_simultaneous_lift_and_pour", [])
    if pour:
        recs.append({
            "priority": 2, "rule": "no_simultaneous_lift_and_pour",
            "title": "Не подавать материалы краном над зоной бетонирования",
            "what": f"{len(pour)} случаев подачи груза над работающим бетононасосом/бетонщиками.",
            "options": [{"option": "приоритет насосу", "value": "подача краном только при остановке бетонирования",
                         "feasible": True, "note": "фиксируется в наряд-допуске и сигнальной схеме"},
                        {"option": "резервная схема", "value": "дополнительный лоток/второй насос",
                         "feasible": True, "note": "убирает саму необходимость подъёма бадьи над людьми"}],
            "basis": ctx.cfg.rule("no_simultaneous_lift_and_pour").source,
            "effect": f"Устраняет {len(pour)} нарушений high; до {max([f.people_exposed for f in pour] or [0])} чел. "
                      f"выводится из-под груза.",
        })

    recs.sort(key=lambda r: (r["priority"], r["rule"]))
    for i, r in enumerate(recs, 1):
        r["n"] = i
    return recs


def before_after(findings, recs, assessments) -> pd.DataFrame:
    """Страница «до и после» для руководства: что было и что меняется."""
    rows = []
    for r in recs:
        n = sum(1 for f in findings if f.rule_id == r["rule"])
        rows.append({
            "Проблема": r["title"],
            "Было в графике": f"{n} нарушений",
            "Что меняем": r["options"][0]["option"] if r.get("options") else "—",
            "Станет": "0 нарушений по этому правилу",
            "Приоритет": {1: "критический", 2: "высокий", 3: "средний"}.get(r["priority"], "низкий"),
        })
    return pd.DataFrame(rows)


def assumptions(ctx, assessments) -> list[dict[str, str]]:
    """Список допущений. Каждый отчёт обязан его содержать (раздел 11)."""
    w = ctx.wind
    a = [
        {"what": "Аэродинамика груза", "value": "квазистатическая формула F = 0.5·ρ·Cd·A·V²",
         "why": "достаточно для панельных грузов при V < 25 м/с; не учитывает срыв потока и галопирование",
         "risk": "при Cd ниже расчётного сила занижается; для сетчатых/ажурных грузов нужна продувка"},
        {"what": "Коэффициент Cd", "value": f"диапазон {ctx.cfg.physics.get('default_cd_range')}",
         "why": "берётся из справочника; точное значение зависит от геометрии и ориентации",
         "risk": "главный источник неопределённости — см. диаграмму чувствительности"},
        {"what": "Плотность воздуха", "value": f"{w.rho:.4f} кг/м³ на отметке {w.elevation_m:.0f} м",
         "why": "барометрическая формула ISA",
         "risk": "отклонение ±3 % даёт ±3 % к силе ветра"},
        {"what": "Профиль ветра по высоте", "value": f"степенной, α = {w.alpha}",
         "why": "стандартная аппроксимация для пограничного слоя",
         "risk": f"экстраполяция с {w.ref_height_m:.0f} м на {ctx.max_work_height_m():.0f} м — за пределами "
                 f"калибровки; для высотного здания обязателен ветроинженерный отчёт"},
        {"what": "Порыв", "value": f"политика «{w.gust_policy}»",
         "why": "измеренный порыв переносится профилем; модель EN 1991-1-4 используется как перекрёстная проверка",
         "risk": "если данных порывов нет, риск занижается"},
        {"what": "Базис лимита ветра", "value": str(ctx.cfg.meta.get("wind_limit_basis", "ref_height")),
         "why": "лимиты паспорта крана нормированы к высоте 10 м; сравнивать их со скоростью на 250 м — ошибка",
         "risk": "при смене базиса все выводы по ветру меняются — проверьте, что написано в вашем ППР"},
        {"what": "Раскачка груза", "value": "маятник с аэродинамическим демпфированием",
         "why": "проверено на периоде и статическом отклонении (тесты V4-V7)",
         "risk": "не учитывает податливость строп и динамику тележки/лебёдки крана"},
        {"what": "Усилие человека на оттяжке", "value":
            f"{ctx.cfg.people.get('tagline_force_per_person_N')} Н длительно, "
            f"КПД {ctx.cfg.people.get('tagline_effectiveness')}",
         "why": "оценочное значение; ОБЯЗАТЕЛЬНО калибруется с ПТО и бригадирами",
         "risk": "самая мягкая цифра в модели: при 150 Н вместо 200 Н выводы ужесточатся"},
        {"what": "Несущие ветви стропа", "value": "carrying_legs из lifts.csv",
         "why": "при 4 ветвях на жёстком грузе реально нагружены 2-3; точное число даёт схема строповки",
         "risk": "занижение числа ветвей завышает усилие (консервативно)"},
        {"what": "Люди", "value": "агенты-капсулы без физической модели",
         "why": "нужно знать «кто где когда», а не биомеханику",
         "risk": "маршруты идеализированы; реальное поведение уточняется по данным обходов"},
        {"what": "Модель здания", "value": "зоны-прямоугольники по zones.csv",
         "why": "для поиска конфликтов достаточно плановой геометрии",
         "risk": "не учитывает реальные препятствия, проёмы, фактическую конфигурацию опалубки"},
    ]
    return a


# ===========================================================================
# HTML
# ===========================================================================
def _esc(s: Any) -> str:
    return _html.escape(str(s if s is not None else ""))


def _img(path: Path, alt: str = "") -> str:
    p = Path(path)
    if not p.exists():
        return f'<div class="miss">график не построен: {p.name}</div>'
    return f'<img alt="{_esc(alt)}" src="data:image/png;base64,{C.png_b64(p)}"/>'


def build_html(ctx, findings, assessments, agent_res, mc_lifts, sched_mc, agents_mc,
               recs, charts: dict[str, Path], outdir: Path) -> Path:
    df = pd.DataFrame([f.as_row() for f in findings]) if findings else pd.DataFrame()
    meta = ctx.cfg.meta
    sev_counts = df["severity"].value_counts().to_dict() if len(df) else {}

    def finding_row(f):
        return (f'<tr class="sev-{_esc(f.severity)}"><td>{_esc(f.start.strftime("%d.%m %H:%M") if f.start is not None else "—")}'
                f'–{_esc(f.end.strftime("%H:%M") if f.end is not None else "")}</td>'
                f'<td><span class="pill {_esc(f.severity)}">{_esc(f.severity)}</span></td>'
                f'<td><b>{_esc(f.rule_id)}</b><div class="dim">{_esc(f.metrics.get("reason") or "")}</div></td>'
                f'<td>{_esc(", ".join(f.zone_ids))}</td><td>{_esc(", ".join(f.task_ids))}</td>'
                f'<td class="num">{_esc(f.people_exposed)}</td>'
                f'<td class="num">{_esc(round(f.duration_min,1) if f.duration_min else "—")}</td>'
                f'<td class="msg">{_esc(f.message)}<div class="dim src">{_esc(f.source)}</div></td></tr>')

    # --- таблица подъёмов
    lift_rows = ""
    for a in assessments:
        s, lim, sw = a.summary, a.limits, a.swing
        u = s.get("sling_utilization_max", 0) or 0
        uc = s.get("crane_utilization_max") or 0
        def cls(x, hi, mid): return "bad" if x > hi else "warn" if x > mid else "ok"
        lift_rows += (
            f'<tr><td><b>{_esc(a.lift.lift_id)}</b><div class="dim">{_esc(a.lift.load_name)}</div></td>'
            f'<td class="num">{a.lift.mass_kg:.0f}</td><td class="num">{a.lift.area_m2:.1f}</td>'
            f'<td class="num">{a.lift.radius_m:.0f}</td><td class="num">{a.lift.hook_height_m:.0f}</td>'
            f'<td class="num">{s.get("v_ref_max_ms",0):.1f}<div class="dim">→ {s.get("v_work_max_ms",0):.1f} на высоте</div></td>'
            f'<td class="num">{s.get("wind_force_max_N",0):.0f}</td>'
            f'<td class="num {cls(u,0.8,0.6)}">{u*100:.0f}%</td>'
            f'<td class="num {cls(uc,0.9,0.7)}">{uc*100:.0f}%</td>'
            f'<td class="num { "bad" if s.get("tagline_deficit_max_N",0)>0 else "ok"}">'
            f'{lim.get("tagline_capacity_N",0):.0f} / {s.get("tagline_deficit_max_N",0)+lim.get("tagline_capacity_N",0):.0f}</td>'
            f'<td class="num">{(sw.get("steady_offset_m") or 0):.2f}</td>'
            f'<td class="num">{lim.get("safe_wind_ms_by_tagline_at_ref_height") if lim.get("safe_wind_ms_by_tagline_at_ref_height") is not None else "—"}</td>'
            f'<td class="num">{len(s.get("safe_windows", []))}<div class="dim">'
            f'{sum(w["hours"] for w in s.get("safe_windows", [])):.0f} ч</div></td>'
            f'<td class="num {("bad" if a.violations else "ok")}">{len(a.violations)}</td></tr>')

    collision_html = ""
    moving = [a for a in assessments if a.swing.get("moving_anchor")]
    if moving:
        rows = ""
        for a in moving:
            events = a.swing.get("structural_collisions", [])
            detail = "нет контакта с перекрытиями или ядром"
            if events:
                first = events[0]
                detail = f"{first['name']}: {first['start_s']:.1f}–{first['end_s']:.1f} с, глубина {first['max_depth_m']:.3f} м, скорость {first['max_impact_speed_m_s']:.2f} м/с"
            rows += (f'<tr><td><b>{_esc(a.lift.lift_id)}</b></td>'
                     f'<td class="num">{len(events)}</td>'
                     f'<td class="num">{a.swing.get("contact_max_impact_speed_m_s", 0):.2f}</td>'
                     f'<td>{_esc(detail)}</td></tr>')
        collision_html = ("<h3>Контакты с перекрытиями и ядром (MuJoCo)</h3>"
                          "<table><tr><th>Подъём</th><th>Контакты</th><th>Макс. скорость, м/с</th>"
                          "<th>Первый контакт</th></tr>" + rows + "</table>"
                          "<p class='dim'>Контакт с землёй на складе — отдельная опора, а не столкновение. "
                          "Численный контактный импульс не сходится по шагу и для инженерной оценки не используется.</p>")

    # --- рекомендации
    rec_html = ""
    for r in recs:
        opts = "".join(f'<li><b>{_esc(o["option"])}</b>: {_esc(o["value"])}'
                       f'<div class="dim">{_esc(o.get("note",""))}{" · НЕ РЕАЛИЗУЕМО" if o.get("feasible") is False else ""}</div></li>'
                       for o in r.get("options", []))
        tbl = ""
        if r.get("table"):
            cols = list(r["table"][0].keys())
            tbl = ("<table class='inner'><tr>" + "".join(f"<th>{_esc(c)}</th>" for c in cols) + "</tr>"
                   + "".join("<tr>" + "".join(f"<td>{_esc(row.get(c,''))}</td>" for c in cols) + "</tr>"
                             for row in r["table"][:12]) + "</table>")
        rec_html += (f'<div class="rec p{r["priority"]}"><div class="rech">'
                     f'<span class="num-badge">{r["n"]}</span><h4>{_esc(r["title"])}</h4>'
                     f'<span class="prio">приоритет {r["priority"]}</span></div>'
                     f'<p>{_esc(r["what"])}</p><ul>{opts}</ul>'
                     f'<p class="basis"><b>Основание:</b> {_esc(r["basis"])}</p>'
                     f'<p class="eff"><b>Ожидаемый эффект:</b> {_esc(r["effect"])}</p>{tbl}</div>')

    # --- Monte Carlo
    mc_rows = ""
    for m in (mc_lifts or []):
        p = m.probabilities
        def pc(v): return f'<span class="{"bad" if v>.5 else "warn" if v>.15 else "ok"}">{v*100:.0f}%</span>'
        mc_rows += (f'<tr><td><b>{_esc(m.lift_id)}</b></td><td>{pc(p["P_tagline_force_exceeded"])}</td>'
                    f'<td>{pc(p["P_wind_above_ppr_limit"])}</td><td>{pc(p["P_sling_utilization_exceeded"])}</td>'
                    f'<td>{pc(p["P_crane_chart_exceeded"])}</td><td>{pc(p["P_any_critical"])}</td>'
                    f'<td class="num">{m.percentiles["static_offset_m"]["p95"]:.2f}</td>'
                    f'<td class="num">{m.percentiles["wind_force_N"]["p95"]:.0f}</td></tr>')

    pairs_html = ""
    if sched_mc is not None:
        pd_ = sched_mc.pair_probabilities[sched_mc.pair_probabilities["P_any"] > 0].head(12)
        pairs_html = ("<table class='inner'><tr><th>Пара работ</th><th>Зоны</th><th>P конфликта</th>"
                      "<th>Среднее перекрытие</th><th>Характер</th></tr>"
                      + "".join(f"<tr><td>{_esc(r.task_a)} × {_esc(r.task_b)}</td>"
                                f"<td>{_esc(r.zone_a)} / {_esc(r.zone_b)}</td>"
                                f"<td class='num'>{r.P_any*100:.0f}%</td><td class='num'>{r.mean_overlap_h:.1f} ч</td>"
                                f"<td>{'структурный' if r.P_any>.9 else 'вероятностный' if r.P_any>.15 else 'редкий'}</td></tr>"
                                for r in pd_.itertuples()) + "</table>")

    agent_html = ""
    if agent_res is not None:
        s = agent_res.summary()
        adf = agent_res.events_df()
        rows = "".join(f'<tr><td>{_esc(k)}</td><td class="num">{v["events"]}</td>'
                       f'<td class="num">{v["person_minutes"]/60:.1f}</td><td class="num">{v["people_max"]}</td></tr>'
                       for k, v in sorted(s["by_rule"].items(), key=lambda kv: -kv[1]["person_minutes"]))
        agent_html = (f'<table class="inner"><tr><th>Правило [тяжесть]</th><th>Событий</th>'
                      f'<th>Чел·часов</th><th>Макс. людей</th></tr>{rows}</table>'
                      f'<p class="dim">Шаг моделирования {s["dt_min"]:g} мин, {s["steps"]} кадров, '
                      f'seed {s["seed"]}. Всего {s["total_person_minutes"]/60:.0f} человеко-часов воздействия.</p>')

    warn_html = "".join(f"<li>{_esc(w)}</li>" for w in ctx.warnings)
    assum_rows = "".join(f'<tr><td><b>{_esc(a["what"])}</b></td><td>{_esc(a["value"])}</td>'
                         f'<td>{_esc(a["why"])}</td><td class="dim">{_esc(a["risk"])}</td></tr>'
                         for a in assumptions(ctx, assessments))
    ba = before_after(findings, recs, assessments)
    ba_html = ("<table class='inner'><tr>" + "".join(f"<th>{_esc(c)}</th>" for c in ba.columns) + "</tr>"
               + "".join("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>"
                         for row in ba.values.tolist()) + "</table>") if len(ba) else ""

    # самый критичный подъём для кривой безопасности
    worst_mc = None
    if mc_lifts:
        worst_mc = max((m.as_dict() for m in mc_lifts),
                       key=lambda d: d["probabilities"]["P_any_critical"])

    top = df.nlargest(12, "exposure_person_min") if len(df) else df
    top_html = "".join(finding_row_from_row(r) for _, r in top.iterrows()) if len(df) else ""
    all_html = "".join(finding_row(f) for f in sorted(findings, key=lambda x: (-x.severity_rank, x.start or pd.Timestamp.max)))

    html = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Отчёт по симуляции рисков — {_esc(meta.get('project',''))}</title>
<style>
@page{{size:A4;margin:16mm}}
body{{font:13.5px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
 color:#1c2430;margin:0;background:#f2f4f7}}
.page{{max-width:1000px;margin:0 auto;background:#fff;padding:34px 42px 60px;box-shadow:0 1px 12px #0002}}
h1{{font-size:23px;margin:0 0 4px;letter-spacing:-.2px}}
h2{{font-size:16px;margin:30px 0 10px;padding-bottom:5px;border-bottom:2px solid #e3e8ef;color:#12202f}}
h3{{font-size:13.5px;margin:16px 0 6px;color:#28374a}}
h4{{margin:0;font-size:14px}}
.dim{{color:#6b7787;font-size:11.5px}} .src{{font-style:italic}}
table{{border-collapse:collapse;width:100%;font-size:11.8px;margin:8px 0}}
th{{background:#f4f6f9;text-align:left;padding:6px 7px;border-bottom:2px solid #d8dfe8;
 font-weight:600;color:#3b4655;font-size:11px;text-transform:uppercase;letter-spacing:.3px}}
td{{padding:5px 7px;border-bottom:1px solid #eef1f5;vertical-align:top}}
td.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
td.msg{{min-width:280px}}
tr.sev-critical td{{background:#fff6f6}} tr.sev-high td{{background:#fffbf3}}
.ok{{color:#1a7f37;font-weight:600}} .warn{{color:#b06a00;font-weight:600}} .bad{{color:#c0202a;font-weight:600}}
.pill{{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10px;font-weight:700;color:#fff}}
.pill.critical{{background:#d6303a}} .pill.high{{background:#ef8a17}}
.pill.medium{{background:#c9a227}} .pill.low{{background:#3aa6a0}}
.kpi{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:14px 0}}
.kpi>div{{border:1px solid #e3e8ef;border-radius:8px;padding:10px 12px;background:#fbfcfe}}
.kpi b{{display:block;font-size:22px;line-height:1.1}}
.kpi span{{font-size:10.5px;color:#6b7787;text-transform:uppercase;letter-spacing:.4px}}
.rec{{border:1px solid #e3e8ef;border-left:4px solid #ef8a17;border-radius:0 8px 8px 0;
 padding:11px 15px;margin:11px 0;background:#fffdfa}}
.rec.p1{{border-left-color:#d6303a;background:#fffafa}}
.rech{{display:flex;align-items:center;gap:9px;margin-bottom:5px}}
.num-badge{{width:22px;height:22px;border-radius:50%;background:#28374a;color:#fff;
 display:flex;align-items:center;justify-content:center;font-size:11.5px;font-weight:700;flex:none}}
.prio{{margin-left:auto;font-size:10.5px;color:#6b7787;border:1px solid #e3e8ef;border-radius:9px;padding:1px 8px}}
.rec ul{{margin:6px 0 6px 18px;padding:0}} .rec li{{margin-bottom:4px}}
.basis,.eff{{margin:5px 0;font-size:12.2px}}
.basis{{color:#4a5566}} .eff{{color:#1a5c2e;background:#f2fbf4;border-radius:5px;padding:5px 8px}}
.note{{background:#fff8e8;border:1px solid #f0dca8;border-left:4px solid #e8b93c;
 padding:9px 13px;border-radius:0 6px 6px 0;margin:12px 0;font-size:12.4px}}
.disc{{background:#f4f6f9;border:1px solid #dfe5ec;border-radius:7px;padding:11px 14px;font-size:12.2px;color:#3b4655}}
img{{max-width:100%;border:1px solid #e8ecf2;border-radius:6px;margin:6px 0}}
.miss{{color:#a33;font-size:11px}}
.cover{{border-bottom:3px solid #28374a;padding-bottom:16px;margin-bottom:8px}}
.meta{{display:grid;grid-template-columns:180px 1fr;gap:3px 12px;font-size:12.2px;margin-top:12px}}
.meta dt{{color:#6b7787}} .meta dd{{margin:0;font-weight:600}}
code{{background:#f2f4f7;padding:1px 5px;border-radius:4px;font-size:11.5px}}
.small{{font-size:11.5px}}
footer{{margin-top:34px;padding-top:12px;border-top:1px solid #e3e8ef;font-size:11px;color:#6b7787}}
@media print{{body{{background:#fff}}.page{{box-shadow:none;max-width:none;padding:0}}}}
</style></head><body><div class="page">

<div class="cover">
<h1>Отчёт по симуляции рисков строительных работ</h1>
<div class="dim">{_esc(ctx.cfg.disclaimer)}</div>
<dl class="meta">
<dt>Объект / проект</dt><dd>{_esc(meta.get('project',''))}</dd>
<dt>Период анализа</dt><dd>{_esc(meta.get('work_window',['—'])[0])} … {_esc(meta.get('work_window',['—','—'])[1])}</dd>
<dt>Версия методики</dt><dd>{_esc(meta.get('version',''))}</dd>
<dt>Набор данных</dt><dd>{_esc(ctx.data_dir)}</dd>
<dt>Отчёт собран</dt><dd>{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}</dd>
<dt>Ядро</dt><dd>risk-sim {pd.Timestamp.now().year} · Python, без проприетарных зависимостей</dd>
<dt>Автор / ответственный</dt><dd>_______________ (подпись ответственного инженера)</dd>
</dl>
</div>

<div class="note"><b>Статус документа.</b> Это результат компьютерной симуляции, а не расчёт по нормам.
Все предельные значения (WLL строп, грузовая характеристика крана, лимиты ветра, допустимое усилие
человека на оттяжке) взяты из входных файлов, которые заполняются по ППР, паспортам и нормативам.
Корректность входных данных подтверждает ПТО; выводы утверждает ответственный инженер.</div>

<h2>1. Резюме</h2>
<div class="kpi">
<div><b>{len(findings)}</b><span>нарушений найдено</span></div>
<div><b class="bad">{sev_counts.get('critical',0)}</b><span>критических</span></div>
<div><b class="warn">{sev_counts.get('high',0)}</b><span>высоких</span></div>
<div><b>{len(assessments)}</b><span>подъёмов просчитано</span></div>
<div><b>{len(recs)}</b><span>рекомендаций</span></div>
</div>
<p><b>Главный вывод.</b> {_esc(_headline(findings, assessments, recs, ctx))}</p>
<p class="dim">Проанализировано {len(ctx.ds.tasks)} работ, {len(ctx.ds.lifts)} подъёмов,
{len(ctx.ds.zones)} зон, {len(ctx.ds.crews)} бригад, {len(ctx.wind.ts)} часов метеоданных.
Найдено {len(findings)} нарушений по {len({f.rule_id for f in findings})} правилам.
Максимальное число людей в одной опасной ситуации: {max([f.people_exposed for f in findings] or [0])} чел.</p>

<h3>Наиболее значимые по суммарному воздействию</h3>
<table><tr><th>Когда</th><th>Тяжесть</th><th>Правило</th><th>Зона</th><th>Работы</th>
<th>Людей</th><th>Мин</th><th>Что произошло и на каком основании</th></tr>{top_html}</table>

<h2>2. Конфликты и опасные ситуации</h2>
{_img(charts.get('by_rule'), 'нарушения по правилам')}
<h3>Полный перечень ({len(findings)})</h3>
<div style="max-height:520px;overflow:auto">
<table><tr><th>Когда</th><th>Тяжесть</th><th>Правило</th><th>Зона</th><th>Работы</th>
<th>Людей</th><th>Мин</th><th>Что произошло и на каком основании</th></tr>{all_html}</table></div>

<h2>3. Тепловая карта рисков</h2>
{_img(charts.get('heatmap'), 'тепловая карта')}
<p class="dim">Читается так: чем темнее клетка, тем больше взвешенных нарушений в этот день на этом ярусе.
Карта отвечает на вопрос «куда ставить контроль», а не «кто виноват».</p>

<h2>4. Сценарии подъёмов: ветер, стропы, оттяжка</h2>
{_img(charts.get('wind'), 'ветер')}
{_img(charts.get('lifts'), 'физика подъёмов')}
<table><tr><th>Подъём</th><th>кг</th><th>м²</th><th>Вылет</th><th>Высота</th>
<th>Ветер 10 м<br>→ на высоте</th><th>F ветра, Н</th><th>Строп</th><th>Кран</th>
<th>Оттяжка<br>есть/нужно, Н</th><th>Смещение<br>груза, м</th><th>Безоп. ветер<br>на 10 м</th>
<th>Окон</th><th>Наруш.</th></tr>{lift_rows}</table>
{collision_html}
<p class="dim">«Окон» — сколько периодов за неделю, когда подъём допустим и по лимиту ППР, и по
возможности оттяжки. «Безоп. ветер» — расчётная предельная скорость на высоте 10 м, при которой
люди ещё удерживают груз: это не норматив, а обратная задача от усилия оттяжки.</p>
{_img(charts.get('safe_wind'), 'граница безопасных условий')}
<h3>Чувствительность результата к неопределённости входов</h3>
{_img(charts.get('tornado'), 'чувствительность')}
<p class="dim">Диаграмма показывает, какую неопределённость уточнять в первую очередь.
Если размах от Cd больше всех остальных — спорить о массе груза до килограмма бессмысленно,
нужно уточнять аэродинамику.</p>

<h2>5. Вероятностная оценка (Monte Carlo)</h2>
{_img(charts.get('mc'), 'Monte Carlo')}
<table><tr><th>Подъём</th><th>P(отказ оттяжки)</th><th>P(ветер &gt; лимита)</th>
<th>P(перегруз стропа)</th><th>P(выход за хар-ку крана)</th><th>P(любое критическое)</th>
<th>Смещение p95, м</th><th>F ветра p95, Н</th></tr>{mc_rows}</table>
<p class="dim">Метод сэмплирования ветра: <b>{_esc(mc_lifts[0].method if mc_lifts else '—')}</b>
({mc_lifts[0].n if mc_lifts else 0} прогонов на подъём). Cd сэмплируется из диапазона,
масса — с допуском, число людей на оттяжке — биномиально (часть может быть занята),
DAF — с разбросом ±3 %.</p>
<h3>Monte Carlo графика работ</h3>
{pairs_html}
{_img(charts.get('mc_pairs'), 'MC графика')}
<p class="dim">Конфликт с вероятностью ~100 % — <b>структурный</b>: он заложен в самом графике и не
исчезнет от дисциплины. Конфликт с вероятностью 20-60 % — <b>вероятностный</b>: его снимает
буфер времени или разделение захваток.</p>

<h2>6. Люди: кто, где и под чем</h2>
{_img(charts.get('exposure'), 'воздействие на бригады')}
{agent_html}

<h2>7. Рекомендации</h2>
{rec_html or '<p class="dim">Нарушений не найдено — рекомендации не требуются.</p>'}

<h2>8. До и после</h2>
{ba_html or '<p class="dim">Нет данных.</p>'}
<p class="dim">Страница для руководства: что было в графике, что меняем и сколько нарушений уходит.
Стоимость мер намеренно не оценивается — это делается вместе с ПТО после согласования.</p>

<h2>9. Допущения и ограничения</h2>
<table><tr><th>Что</th><th>Принято</th><th>Почему так</th><th>Что это может исказить</th></tr>{assum_rows}</table>
<h3>Предупреждения о качестве входных данных</h3>
<ul class="small">{warn_html or '<li>предупреждений нет</li>'}</ul>

<h2>10. Приложение</h2>
<ul class="small">
<li>Интерактивный просмотр: <code>viewer/index.html</code> (открывается двойным щелчком, данные встроены в файл)</li>
<li>Таблица нарушений для Excel: <code>results/conflicts.csv</code></li>
<li>Расчёт каждого подъёма: <code>results/lift_report_*.json</code>, серии по времени <code>results/lift_series_*.csv</code></li>
<li>События по людям: <code>results/agent_events.csv</code>, траектории <code>results/agent_tracks.json</code></li>
<li>Monte Carlo: <code>results/mc_lift_*.json</code>, <code>results/mc_schedule_pairs.csv</code>,
    кривые <code>results/mc_safe_wind_curves.json</code></li>
<li>Пакет для визуализации (Blender/Three.js/Unreal): <code>results/scene_bundle.json</code></li>
<li>Видео: <code>blender/render.sh</code> → <code>renders/</code></li>
<li>Проверка достоверности: <code>python -m pytest tests/</code> (33 сверки, включая MuJoCo ↔ аналитика)</li>
</ul>

<div class="disc"><b>Оговорка.</b> {_esc(ctx.cfg.disclaimer)} Симуляция воспроизводит геометрию,
время и упрощённую физику; она не заменяет расчёты ППР, паспорт крана, схемы строповки,
нормативные документы и решение ответственного инженера. Числа в этом отчёте следует читать
как диапазоны с указанной неопределённостью, а не как точные значения.</div>

<footer>risk-sim · отчёт собран автоматически {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')} ·
версия методики {_esc(meta.get('version',''))} · данные {_esc(str(ctx.data_dir))} ·
git-ревизия: __________</footer>
</div></body></html>"""
    p = Path(outdir) / "report.html"
    p.write_text(html, encoding="utf-8")
    return p


def finding_row_from_row(r) -> str:
    return (f'<tr class="sev-{_esc(r["severity"])}"><td>{_esc(str(r["start"])[5:])}</td>'
            f'<td><span class="pill {_esc(r["severity"])}">{_esc(r["severity"])}</span></td>'
            f'<td><b>{_esc(r["rule_id"])}</b></td><td>{_esc(r["zone_ids"])}</td>'
            f'<td>{_esc(r["task_ids"])}</td><td class="num">{_esc(r["people_exposed"])}</td>'
            f'<td class="num">{_esc(r["duration_min"])}</td>'
            f'<td class="msg">{_esc(r["message"])}<div class="dim src">{_esc(r["source"])}</div></td></tr>')


def _headline(findings, assessments, recs, ctx) -> str:
    """Одно предложение для руководства — суть, а не пересказ таблиц."""
    if not findings:
        return "Нарушений в проанализированном периоде не выявлено."
    crit = [f for f in findings if f.severity == "critical"]
    parts = []
    tag = [a for a in assessments if any(v["rule_id"] == "tagline_force_within_capacity" for v in a.violations)]
    if tag:
        w = max(tag, key=lambda a: a.summary["tagline_deficit_max_N"])
        sv = w.limits.get("safe_wind_ms_by_tagline_at_ref_height")
        parts.append(f"главное ограничение — удержание груза оттяжкой: для {w.lift.lift_id} "
                     f"({w.lift.load_name}) оно возможно только при ветре до {sv} м/с на высоте 10 м, "
                     f"что даёт {len(w.summary.get('safe_windows', []))} пригодных окон за неделю")
    edge = [f for f in findings if f.rule_id == "edge_work_requires_guardrail"]
    if edge:
        parts.append(f"{len(edge)} случая работ у края без ограждения (до {max(f.people_exposed for f in edge)} чел.)")
    ppl = [f for f in findings if f.rule_id in ("load_path_over_people", "no_work_under_crane_hazard")]
    if ppl:
        parts.append(f"{len(ppl)} случаев нахождения людей под грузом или на его траектории")
    wnd = [f for f in findings if f.rule_id == "stop_crane_ops_above_wind_limit"]
    if wnd:
        parts.append(f"{len(wnd)} крановых операций попадают на ветер выше лимита ППР")
    lead = f"Выявлено {len(findings)} нарушений, из них {len(crit)} критических. "
    return lead + "; ".join(parts) + "."


# ===========================================================================
# DOCX
# ===========================================================================
def build_docx(ctx, findings, assessments, agent_res, mc_lifts, sched_mc, agents_mc,
               recs, charts: dict[str, Path], outdir: Path) -> Path:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    meta = ctx.cfg.meta
    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Cm(1.8)
        s.top_margin = s.bottom_margin = Cm(1.6)
    st = doc.styles["Normal"]
    st.font.name = "Helvetica Neue"
    st.font.size = Pt(9.5)

    def h(txt, lvl=1):
        p = doc.add_heading(txt, level=lvl)
        for r in p.runs:
            r.font.color.rgb = RGBColor(0x12, 0x20, 0x2f)
        return p

    def para(txt, bold=False, size=9.5, italic=False, color=None):
        p = doc.add_paragraph()
        r = p.add_run(txt); r.bold = bold; r.italic = italic; r.font.size = Pt(size)
        if color: r.font.color.rgb = color
        return p

    def table(headers, rows, widths=None, font=8):
        t = doc.add_table(rows=1, cols=len(headers))
        t.style = "Light Grid Accent 1"
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        for i, hd in enumerate(headers):
            c = t.rows[0].cells[i]; c.text = ""
            r = c.paragraphs[0].add_run(str(hd)); r.bold = True; r.font.size = Pt(font)
        for row in rows:
            cells = t.add_row().cells
            for i, v in enumerate(row):
                cells[i].text = ""
                r = cells[i].paragraphs[0].add_run("" if v is None else str(v))
                r.font.size = Pt(font)
        if widths:
            for i, w in enumerate(widths):
                for row in t.rows:
                    row.cells[i].width = Cm(w)
        return t

    def pic(key, width_cm=17.0):
        p = charts.get(key)
        if p and Path(p).exists():
            doc.add_picture(str(p), width=Cm(width_cm))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

    # --- титул
    para("ОТЧЁТ ПО СИМУЛЯЦИИ РИСКОВ СТРОИТЕЛЬНЫХ РАБОТ", bold=True, size=16)
    para(str(meta.get("project", "")), size=11)
    para("")
    table(["Параметр", "Значение"], [
        ["Период анализа", " … ".join(str(x) for x in meta.get("work_window", ["—"]))],
        ["Версия методики", meta.get("version", "")],
        ["Набор данных", str(ctx.data_dir)],
        ["Дата сборки отчёта", pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")],
        ["Ответственный инженер", ""],
        ["Подпись / дата", ""],
    ], widths=[5.5, 11.5])
    doc.add_paragraph()
    para(ctx.cfg.disclaimer, italic=True, size=9)

    # --- 1 резюме
    h("1. Резюме", 1)
    sev = {}
    for f in findings:
        sev[f.severity] = sev.get(f.severity, 0) + 1
    table(["Показатель", "Значение"], [
        ["Всего нарушений", len(findings)],
        ["критических", sev.get("critical", 0)],
        ["высоких", sev.get("high", 0)],
        ["средних", sev.get("medium", 0)],
        ["Подъёмов просчитано", len(assessments)],
        ["Макс. людей в одной ситуации", max([f.people_exposed for f in findings] or [0])],
        ["Рекомендаций", len(recs)],
    ], widths=[8, 5])
    para(_headline(findings, assessments, recs, ctx), bold=True)

    h("Наиболее значимые нарушения", 2)
    df = pd.DataFrame([f.as_row() for f in findings])
    if len(df):
        top = df.nlargest(10, "exposure_person_min")
        table(["Когда", "Тяжесть", "Правило", "Зона", "Людей", "Суть"],
              [[r["start"], r["severity"], r["rule_id"], r["zone_ids"], r["people_exposed"], r["message"]]
               for _, r in top.iterrows()], widths=[2.6, 1.8, 3.6, 2.4, 1.2, 8.4], font=7.5)

    # --- 2 конфликты
    doc.add_page_break(); h("2. Конфликты и опасные ситуации", 1)
    pic("by_rule")
    if len(df):
        table(["Начало", "Конец", "Тяжесть", "Правило", "Зоны", "Работы", "Людей", "Основание (ППР/норма)", "Описание"],
              [[r["start"], r["end"], r["severity"], r["rule_id"], r["zone_ids"], r["task_ids"],
                r["people_exposed"], r["source"], r["message"]] for _, r in df.iterrows()],
              widths=[2.2, 2.2, 1.5, 3.2, 2.2, 2.4, 1.0, 4.2, 6.5], font=6.8)

    # --- 3 тепловая карта
    doc.add_page_break(); h("3. Тепловая карта рисков", 1)
    pic("heatmap")
    para("Сумма весов тяжести (critical=4, high=3, medium=2, low=1) по дням и ярусам. "
         "Карта показывает, куда ставить контроль.", italic=True, size=8.5)

    # --- 4 подъёмы
    doc.add_page_break(); h("4. Сценарии подъёмов", 1)
    pic("wind")
    pic("lifts")
    rows = []
    for a in assessments:
        s, lim, sw = a.summary, a.limits, a.swing
        rows.append([a.lift.lift_id, a.lift.load_name, f"{a.lift.mass_kg:.0f}", f"{a.lift.area_m2:.1f}",
                     f"{a.lift.radius_m:.0f}", f"{s.get('v_ref_max_ms',0):.1f}",
                     f"{s.get('v_work_max_ms',0):.1f}", f"{s.get('wind_force_max_N',0):.0f}",
                     f"{(s.get('sling_utilization_max',0) or 0)*100:.0f}%",
                     f"{(s.get('crane_utilization_max',0) or 0)*100:.0f}%",
                     f"{lim.get('tagline_capacity_N',0):.0f}",
                     f"{s.get('tagline_deficit_max_N',0):.0f}",
                     f"{(sw.get('steady_offset_m') or 0):.2f}",
                     lim.get("safe_wind_ms_by_tagline_at_ref_height") if lim.get("safe_wind_ms_by_tagline_at_ref_height") is not None else "—",
                     len(s.get("safe_windows", [])), len(a.violations)])
    table(["ID", "Груз", "кг", "м²", "Вылет", "V10", "Vвыс", "Fветра", "Строп", "Кран",
           "Оттяжка есть", "Дефицит", "Смещ.", "Безоп.V", "Окон", "Наруш."],
          rows, font=7)
    moving = [a for a in assessments if a.swing.get("moving_anchor")]
    if moving:
        h("Контакты с перекрытиями и ядром (MuJoCo)", 2)
        collision_rows = []
        for a in moving:
            events = a.swing.get("structural_collisions", [])
            detail = "нет контакта с перекрытиями или ядром"
            if events:
                first = events[0]
                detail = f"{first['name']}: {first['start_s']:.1f}–{first['end_s']:.1f} с, глубина {first['max_depth_m']:.3f} м, скорость {first['max_impact_speed_m_s']:.2f} м/с"
            collision_rows.append([a.lift.lift_id, len(events), f"{a.swing.get('contact_max_impact_speed_m_s', 0):.2f}", detail])
        table(["Подъём", "Контакты", "Скорость, м/с", "Первый контакт"], collision_rows, font=7)
        para("Контакт с землёй на складе — отдельная опора, а не столкновение. Численный контактный импульс "
             "не сходится по шагу и для инженерной оценки не используется. Геометрия перекрытий и груза упрощена; "
             "выводы проверяет ответственный инженер.", italic=True, size=8)
    para("V10 — порыв на высоте 10 м (базис лимита паспорта крана), Vвыс — пересчитанный порыв на "
         "высоте работ. «Безоп.V» — обратная задача: при каком ветре на 10 м люди ещё удерживают груз.",
         italic=True, size=8)
    pic("safe_wind", 12.5)
    h("Чувствительность", 2); pic("tornado", 14)

    # --- 5 Monte Carlo
    doc.add_page_break(); h("5. Вероятностная оценка (Monte Carlo)", 1)
    pic("mc")
    if mc_lifts:
        table(["Подъём", "P(оттяжка)", "P(ветер)", "P(строп)", "P(кран)", "P(критич.)",
               "Смещение p95, м", "F ветра p95, Н"],
              [[m.lift_id, f"{m.probabilities['P_tagline_force_exceeded']*100:.0f}%",
                f"{m.probabilities['P_wind_above_ppr_limit']*100:.0f}%",
                f"{m.probabilities['P_sling_utilization_exceeded']*100:.0f}%",
                f"{m.probabilities['P_crane_chart_exceeded']*100:.0f}%",
                f"{m.probabilities['P_any_critical']*100:.0f}%",
                f"{m.percentiles['static_offset_m']['p95']:.2f}",
                f"{m.percentiles['wind_force_N']['p95']:.0f}"] for m in mc_lifts], font=8)
        para(f"Метод сэмплирования ветра: {mc_lifts[0].method}, {mc_lifts[0].n} прогонов на подъём.",
             italic=True, size=8)
    if sched_mc is not None:
        h("Monte Carlo графика работ", 2)
        pd_ = sched_mc.pair_probabilities[sched_mc.pair_probabilities["P_any"] > 0].head(12)
        table(["Пара работ", "Зоны", "P", "Перекрытие, ч", "Характер"],
              [[f"{r.task_a} × {r.task_b}", f"{r.zone_a} / {r.zone_b}", f"{r.P_any*100:.0f}%",
                f"{r.mean_overlap_h:.1f}",
                "структурный" if r.P_any > .9 else "вероятностный" if r.P_any > .15 else "редкий"]
               for r in pd_.itertuples()], font=7.5)
        pic("mc_pairs", 15)

    # --- 6 люди
    doc.add_page_break(); h("6. Люди: кто, где и под чем", 1)
    pic("exposure")
    if agent_res is not None:
        s = agent_res.summary()
        table(["Правило [тяжесть]", "Событий", "Чел·часов", "Макс. людей"],
              [[k, v["events"], f"{v['person_minutes']/60:.1f}", v["people_max"]]
               for k, v in sorted(s["by_rule"].items(), key=lambda kv: -kv[1]["person_minutes"])], font=8)
        para(f"Шаг моделирования {s['dt_min']:g} мин, {s['steps']} кадров, seed {s['seed']}. "
             f"Всего {s['total_person_minutes']/60:.0f} человеко-часов воздействия.", italic=True, size=8)

    # --- 7 рекомендации
    doc.add_page_break(); h("7. Рекомендации", 1)
    for r in recs:
        h(f"{r['n']}. {r['title']}  (приоритет {r['priority']})", 2)
        para(r["what"])
        for o in r.get("options", []):
            para(f"• {o['option']}: {o['value']}" + (f" — {o['note']}" if o.get("note") else "")
                 + ("" if o.get("feasible", True) else "  [НЕ РЕАЛИЗУЕМО]"), size=9)
        para(f"Основание: {r['basis']}", italic=True, size=8.5)
        para(f"Ожидаемый эффект: {r['effect']}", size=9)
        if r.get("table"):
            cols = list(r["table"][0].keys())
            table(cols, [[row.get(c, "") for c in cols] for row in r["table"][:12]], font=7.5)

    # --- 8 до и после
    doc.add_page_break(); h("8. До и после", 1)
    ba = before_after(findings, recs, assessments)
    if len(ba):
        table(list(ba.columns), ba.values.tolist(), font=8)

    # --- 9 допущения
    h("9. Допущения и ограничения", 1)
    table(["Что", "Принято", "Почему", "Что может исказить"],
          [[a["what"], a["value"], a["why"], a["risk"]] for a in assumptions(ctx, assessments)], font=7.5)
    h("Предупреждения о качестве входных данных", 2)
    for w in ctx.warnings:
        para("• " + w, size=8.5)

    # --- 10 приложение
    h("10. Приложение", 1)
    for line in [
        "Интерактивный просмотр: viewer/index.html (данные встроены в файл, открывается без сервера)",
        "Таблица нарушений: results/conflicts.csv",
        "Расчёты подъёмов: results/lift_report_*.json, results/lift_series_*.csv",
        "События по людям: results/agent_events.csv, траектории: results/agent_tracks.json",
        "Monte Carlo: results/mc_lift_*.json, results/mc_schedule_pairs.csv",
        "Пакет для визуализации: results/scene_bundle.json",
        "Проверка достоверности: python -m pytest tests/ (33 сверки)",
    ]:
        para("• " + line, size=8.5)
    doc.add_paragraph()
    para(ctx.cfg.disclaimer, italic=True, size=9)

    p = Path(outdir) / "report.docx"
    doc.save(str(p))
    return p


def build_pdf(outdir: Path) -> Path | None:
    """PDF через WeasyPrint, если он установлен (необязательная зависимость)."""
    src = Path(outdir) / "report.html"
    if not src.exists():
        return None
    try:
        from weasyprint import HTML
    except Exception:
        return None
    p = Path(outdir) / "report.pdf"
    HTML(string=src.read_text(encoding="utf-8"), base_url=str(src.parent)).write_pdf(str(p))
    return p


# ===========================================================================
def build_report(ctx, findings, assessments, agent_res, mc_lifts, sched_mc, agents_mc,
                 outdir: str | Path = "results") -> dict[str, Any]:
    out = Path(outdir); ch = out / "charts"; ch.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([f.as_row() for f in findings]) if findings else pd.DataFrame()
    z_work = ctx.max_work_height_m()

    lift_tasks = [t for t in ctx.ds.tasks if {"crane", "lift"} & set(t.hazard_tags)]
    charts = {
        "wind": C.chart_wind(ctx.wind, lift_tasks, z_work,
                             limits=sorted({l.wind_limit_ms for l in ctx.ds.lifts
                                            if np.isfinite(l.wind_limit_ms)}) or [10.0],
                             out=ch / "wind.png"),
        "heatmap": C.chart_heatmap(df, ctx.ds.zones, out=ch / "heatmap.png"),
        "by_rule": C.chart_findings_by_rule(df, ctx.cfg, out=ch / "by_rule.png"),
        "lifts": C.chart_lift_physics(assessments, out=ch / "lifts.png"),
        "mc": C.chart_mc_probability(mc_lifts or [], out=ch / "mc.png"),
        "tornado": C.chart_tornado(tornado(ctx, assessments[0].lift, 8.0) if assessments else [],
                                   out=ch / "tornado.png"),
        "exposure": C.chart_exposure(agent_res.events_df() if agent_res else None, out=ch / "exposure.png"),
        "mc_pairs": C.chart_mc_pairs(sched_mc.pair_probabilities if sched_mc else pd.DataFrame(),
                                     out=ch / "mc_pairs.png"),
    }
    # кривая безопасности для самого критичного подъёма
    if mc_lifts:
        worst = max((m.as_dict() for m in mc_lifts), key=lambda d: d["probabilities"]["P_any_critical"])
        charts["safe_wind"] = C.chart_safe_wind(worst, out=ch / "safe_wind.png")
        tor = tornado(ctx, next(a.lift for a in assessments if a.lift.lift_id == worst["lift_id"]), 8.0)
        charts["tornado"] = C.chart_tornado(tor, out=ch / "tornado.png")
        worst_a = next(a for a in assessments if a.lift.lift_id == worst["lift_id"])
        grid = scenario_grid(ctx, worst_a.lift)
        grid.to_csv(out / "scenario_grid.csv", index=False)
        pd.DataFrame(tor).to_csv(out / "tornado.csv", index=False)
        pd.DataFrame(mitigation_options(ctx, worst_a.lift,
                                        worst_a.summary["v_ref_max_ms"])).to_csv(
            out / "mitigations.csv", index=False)

    recs = make_recommendations(ctx, findings, assessments, mc_lifts, sched_mc)

    rec_df = pd.DataFrame([{
        "n": r["n"], "priority": r["priority"], "rule": r["rule"], "title": r["title"],
        "what": r["what"], "basis": r["basis"], "effect": r["effect"],
        "options": " | ".join(f"{o['option']}: {o['value']}" for o in r.get("options", [])),
    } for r in recs])
    rec_df.to_csv(out / "recommendations.csv", index=False)
    ba_df = before_after(findings, recs, assessments)
    ba_df.to_csv(out / "before_after.csv", index=False)

    out_files = {
        "html": str(build_html(ctx, findings, assessments, agent_res, mc_lifts, sched_mc,
                               agents_mc, recs, charts, out)),
        "docx": str(build_docx(ctx, findings, assessments, agent_res, mc_lifts, sched_mc,
                               agents_mc, recs, charts, out)),
        "recommendations": str(out / "recommendations.csv"),
        "before_after": str(out / "before_after.csv"),
        "charts": str(ch),
    }
    pdf = build_pdf(out)
    if pdf:
        out_files["pdf"] = str(pdf)
    return out_files
