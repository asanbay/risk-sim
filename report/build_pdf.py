"""
report/build_pdf.py — PDF-версия отчёта (раздел 10 ТЗ: «авто-отчёт Word/PDF»).

    python -m report.build_pdf --out results/report.pdf
    # или:  make pdf        (вызывается и из run.py --pdf)

Принцип тот же, что у панели: PDF собирается ИЗ results/ (findings.json,
*.csv, charts/*.png, scene_bundle.json), без ядра. Word-версия (report.docx)
и HTML (report.html) остаются основными для правок; PDF — для рассылки и
подшивки к ППР: он неизменяем и везде открывается одинаково.

Кириллица: регистрируем DejaVuSans из поставки matplotlib (лицензия свободная),
иначе встроенные шрифты reportlab дают квадраты вместо букв.

ВАЖНО: каждая страница несёт дисклеймер — результат симуляции является
поддержкой решений, а не расчётом по нормам.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

DISCLAIMER = ("Результат симуляции — аналитическая поддержка решений. Он НЕ заменяет расчёты ППР, "
              "паспорт крана, схемы строповки, нормативные документы и решение ответственного инженера.")

SEV_COLORS = {"critical": colors.HexColor("#c62828"), "high": colors.HexColor("#e65100"),
              "medium": colors.HexColor("#b28704"), "low": colors.HexColor("#2e7d32"),
              "info": colors.HexColor("#546e7a")}


def register_fonts() -> str:
    import matplotlib
    ttf = Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf"
    pdfmetrics.registerFont(TTFont("DejaVu", str(ttf / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(ttf / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVu-It", str(ttf / "DejaVuSans-Oblique.ttf")))
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold", italic="DejaVu-It")
    return "DejaVu"


def styles(font: str):
    ss = getSampleStyleSheet()
    def st(name, **kw):
        base = dict(fontName=font, fontSize=9, leading=12.5, textColor=colors.HexColor("#1a1a1a"))
        base.update(kw)
        return ParagraphStyle(name, **base)
    return {
        "h1": st("h1", fontSize=17, leading=21, fontName=f"{font}-Bold", spaceAfter=4),
        "h2": st("h2", fontSize=12.5, leading=16, fontName=f"{font}-Bold",
                 spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#0d47a1")),
        "body": st("body", spaceAfter=4),
        "small": st("small", fontSize=7.5, leading=10, textColor=colors.HexColor("#555555")),
        "warn": st("warn", fontSize=8.5, leading=12, fontName=f"{font}-Bold",
                   textColor=colors.HexColor("#8a1f1f")),
        "th": st("th", fontSize=7.6, leading=9.5, fontName=f"{font}-Bold",
                 textColor=colors.white),
        "td": st("td", fontSize=7.4, leading=9.4),
    }


def table(data: list[list], S, widths=None, align_right=()):
    t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), f"{S['td'].fontName}-Bold" if False else "DejaVu-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.4),
        ("FONTNAME", (0, 1), (-1, -1), "DejaVu"),
        ("LEADING", (0, 0), (-1, -1), 9.4),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b0bec5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eceff1")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    for c in align_right:
        style.append(("ALIGN", (c, 1), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def fmt_pct(x):
    try:
        return f"{float(x):.1%}"
    except (TypeError, ValueError):
        return str(x)


def build_pdf(results_dir: str | Path = "results", out: str | Path | None = None) -> Path:
    R = Path(results_dir)
    out = Path(out) if out else R / "report.pdf"
    font = register_fonts()
    S = styles(font)

    bundle = json.loads((R / "scene_bundle.json").read_text(encoding="utf-8"))
    findings = (json.loads((R / "findings.json").read_text(encoding="utf-8"))
                if (R / "findings.json").exists() else {"findings": []})["findings"]
    fsum = bundle.get("findings_summary", {})
    meta = bundle["timeline"]["meta"]
    rmeta = (bundle.get("rules") or {}).get("meta", {})
    ctx = json.loads((R / "context_info.json").read_text(encoding="utf-8")) \
        if (R / "context_info.json").exists() else {}
    lift_sum = pd.read_csv(R / "lift_summary.csv") if (R / "lift_summary.csv").exists() else pd.DataFrame()
    mc_p = pd.read_csv(R / "mc_lift_probability.csv") if (R / "mc_lift_probability.csv").exists() else pd.DataFrame()
    recs = pd.read_csv(R / "recommendations.csv") if (R / "recommendations.csv").exists() else pd.DataFrame()
    ba = pd.read_csv(R / "before_after.csv") if (R / "before_after.csv").exists() else pd.DataFrame()
    ev = pd.read_csv(R / "agent_events.csv") if (R / "agent_events.csv").exists() else pd.DataFrame()
    sched = (bundle.get("monte_carlo") or {}).get("schedule") or {}

    story: list = []
    story.append(Paragraph("Симуляция рисков строительных работ", S["h1"]))
    story.append(Paragraph(f"{rmeta.get('project', meta.get('project', ''))} · "
                           f"правила v{rmeta.get('version', '?')} · окно {meta.get('t_start')} … {meta.get('t_end')}",
                           S["small"]))
    story.append(Spacer(1, 3 * mm))
    warn = Table([[Paragraph("⚠ " + DISCLAIMER, S["warn"])]], colWidths=[170 * mm])
    warn.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1.2, SEV_COLORS["critical"]),
                              ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fdecea")),
                              ("LEFTPADDING", (0, 0), (-1, -1), 6),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                              ("TOPPADDING", (0, 0), (-1, -1), 5),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(warn)
    story.append(Spacer(1, 4 * mm))

    # -- 1 резюме -----------------------------------------------------------
    story.append(Paragraph("1. Резюме", S["h2"]))
    by_sev = fsum.get("by_severity", {})
    rows = [["Показатель", "Значение"],
            ["Нарушений всего", str(fsum.get("total", len(findings)))],
            ["Critical / high", f"{by_sev.get('critical', 0)} / {by_sev.get('high', 0)}"],
            ["Чел·мин воздействия, консервативно", f"{fsum.get('person_minutes_total', 0):,.0f}"],
            ["Макс. людей в нарушении", str(fsum.get("people_exposed_max", 0))],
            ["Подъёмов проверено", str(len(bundle.get("lifts", [])))],
            ["P(две бригады в зоне), MC", fmt_pct((sched.get("rule_probabilities", {}) or {})
                                                 .get("no_two_crews_same_zone"))],
            ["Чел·ч воздействия p95, MC",
             f"{(sched.get('exposure_person_hours', {}) or {}).get('p95', 0):,.0f}"]]
    story.append(table(rows, S, widths=[70 * mm, 40 * mm]))
    story.append(Spacer(1, 3 * mm))
    by_rule = pd.DataFrame(fsum.get("by_rule", []))
    if not by_rule.empty:
        rows = [["Правило", "Категория", "Событий", "Чел·мин", "Источник (ППР/норма)"]]
        for _, r in by_rule.sort_values("person_min", ascending=False).iterrows():
            rows.append([str(r["rule_id"]), str(r["severity"]), str(int(r["n"])),
                         f"{float(r['person_min']):,.0f}", str(r.get("source", ""))[:90]])
        story.append(table(rows, S, widths=[42 * mm, 18 * mm, 14 * mm, 16 * mm, 80 * mm],
                           align_right=(2, 3)))

    # -- 2 конфликты --------------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("2. Конфликты и нарушения (топ по чел·мин)", S["h2"]))
    fdf = pd.DataFrame(findings)
    if not fdf.empty:
        top = fdf.sort_values("exposure_person_min", ascending=False).head(24)
        rows = [["Начало", "Категория", "Правило / зоны", "Людей", "Чел·мин", "Сообщение"]]
        for _, r in top.iterrows():
            rows.append([str(r.get("start", ""))[:16], str(r.get("severity", "")),
                         f'{r.get("rule_id", "")}\n{str(r.get("zone_ids", ""))[:34]}',
                         str(int(r.get("people_exposed", 0))),
                         f'{float(r.get("exposure_person_min", 0)):,.0f}',
                         str(r.get("message", ""))[:110]])
        story.append(table(rows, S, widths=[24 * mm, 16 * mm, 34 * mm, 12 * mm, 14 * mm, 70 * mm],
                           align_right=(3, 4)))
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(f"Всего записей: {len(fdf)}. Полная таблица — results/findings.json "
                               f"и conflicts.csv.", S["small"]))

    # -- 3 подъёмы ----------------------------------------------------------
    story.append(Paragraph("3. Проверка подъёмов", S["h2"]))
    if not lift_sum.empty:
        cols = [("lift_id", "Подъём"), ("load_name", "Груз"), ("mass_kg", "Масса, кг"),
                ("v_work_max_ms", "V раб, м/с"), ("wind_force_max_N", "F ветра, Н"),
                ("sling_utilization_max", "Строп, исп."), ("crane_utilization_max", "Кран, исп."),
                ("tagline_deficit_max_N", "Дефицит оттяжки, Н"), ("violations", "Нарушений")]
        rows = [[h for _, h in cols]]
        for _, r in lift_sum.iterrows():
            row = []
            for c, _ in cols:
                v = r.get(c)
                if c in ("sling_utilization_max", "crane_utilization_max"):
                    row.append(f"{float(v):.0%}")
                elif c in ("mass_kg", "wind_force_max_N", "tagline_deficit_max_N"):
                    row.append(f"{float(v):,.0f}")
                elif c == "v_work_max_ms":
                    row.append(f"{float(v):.1f}")
                else:
                    row.append(str(v)[:26])
            rows.append(row)
        story.append(table(rows, S, widths=[14 * mm, 40 * mm, 16 * mm, 15 * mm, 17 * mm,
                                            15 * mm, 15 * mm, 24 * mm, 14 * mm],
                           align_right=(2, 3, 4, 5, 6, 7, 8)))
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph("Использование стропа и крана — относительно паспортных значений; "
                               "дефицит оттяжки = ветровая сила минус способность людей на оттяжке "
                               "(ППР). Подробности по каждому подъёму — lift_report_*.json.", S["small"]))

    # -- 4 Monte Carlo ------------------------------------------------------
    story.append(Paragraph("4. Monte Carlo (порывы, Cd, пуск; N=5000 на подъём)", S["h2"]))
    if not mc_p.empty:
        cols = [("lift_id", "Подъём"), ("P_wind_above_ppr_limit", "P(ветер>ППР)"),
                ("P_tagline_force_exceeded", "P(оттяжка)"),
                ("P_sling_utilization_exceeded", "P(строп)"),
                ("P_load_offset_over_1m", "P(снос>1 м)"), ("P_any_critical", "P(критич.)")]
        rows = [[h for _, h in cols]]
        for _, r in mc_p.iterrows():
            rows.append([str(r["lift_id"])] + [fmt_pct(r.get(c)) for c, _ in cols[1:]])
        story.append(table(rows, S, widths=[18 * mm, 28 * mm, 26 * mm, 24 * mm, 28 * mm, 26 * mm],
                           align_right=(1, 2, 3, 4, 5)))
    rp = sched.get("rule_probabilities", {}) or {}
    if rp:
        rows = [["Событие расписания (MC, N=2000)", "Вероятность"]]
        for k, v in rp.items():
            rows.append([str(k), fmt_pct(v)])
        story.append(Spacer(1, 2 * mm))
        story.append(table(rows, S, widths=[90 * mm, 30 * mm], align_right=(1,)))

    # -- 5 люди -------------------------------------------------------------
    story.append(Paragraph("5. Люди и воздействия", S["h2"]))
    story.append(Paragraph("Ниже — ИЗМЕРЕННАЯ экспозиция по агентным трекам (фактическое присутствие "
                           "людей). Консервативная оценка резюме (окна правил × все люди зоны) выше — "
                           "разница есть запас безопасности, а не расхождение моделей.", S["small"]))
    asum = (bundle.get("agents") or {}).get("summary", {})
    if asum:
        rows = [["Кадров треков", "Событий воздействия", "Чел·мин"]]
        rows.append([str(asum.get("frames", asum.get("n_frames", "—"))),
                     str(asum.get("events", "—")), f"{asum.get('person_minutes', 0):,.0f}"])
        story.append(table(rows, S, widths=[40 * mm, 40 * mm, 40 * mm], align_right=(0, 1, 2)))
    if not ev.empty:
        top = ev.sort_values("person_minutes", ascending=False).head(12) \
            if "person_minutes" in ev.columns else ev.head(12)
        rows = [["Бригада", "Зона", "Правило", "Начало", "Чел·мин"]]
        for _, r in top.iterrows():
            rows.append([str(r.get("crew_id", "")), str(r.get("zone_id", "")),
                         str(r.get("rule_id", "")), str(r.get("start", ""))[:16],
                         f"{float(r.get('person_minutes', 0)):,.0f}"])
        story.append(Spacer(1, 2 * mm))
        story.append(table(rows, S, widths=[18 * mm, 30 * mm, 52 * mm, 28 * mm, 18 * mm],
                           align_right=(4,)))

    # -- 6 рекомендации -----------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("6. Рекомендации по ППР и графику", S["h2"]))
    if recs.empty:
        story.append(Paragraph("Рекомендаций нет.", S["body"]))
    for _, r in recs.sort_values("priority").iterrows():
        story.append(Paragraph(f"{int(r['priority'])}. {r['title']}", S["body"]))
        story.append(Paragraph(f"<b>Что:</b> {r['what']}", S["small"]))
        story.append(Paragraph(f"<b>Основание:</b> {r['basis']}", S["small"]))
        story.append(Paragraph(f"<b>Эффект:</b> {r['effect']}", S["small"]))
        opts = " · ".join(o.strip() for o in str(r.get("options", "")).split("|") if o.strip())
        if opts:
            story.append(Paragraph(f"<b>Варианты:</b> {opts}", S["small"]))
        story.append(Spacer(1, 2 * mm))
    if not ba.empty:
        story.append(Paragraph("До / после", S["body"]))
        rows = [list(ba.columns)] + ba.astype(str).values.tolist()
        rows[0] = [str(c) for c in rows[0]]
        story.append(table(rows, S, widths=[52 * mm, 26 * mm, 34 * mm, 34 * mm, 24 * mm]))

    # -- 7 графики ----------------------------------------------------------
    charts = sorted((R / "charts").glob("*.png")) if (R / "charts").is_dir() else []
    if charts:
        story.append(PageBreak())
        story.append(Paragraph("7. Графики", S["h2"]))
        for c in charts:
            story.append(Image(str(c), width=168 * mm, height=168 * mm * 9 / 16))
            story.append(Paragraph(c.stem.replace("_", " "), S["small"]))
            story.append(Spacer(1, 3 * mm))

    # -- 8 допущения --------------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("8. Допущения и источники данных", S["h2"]))
    w = ctx.get("wind", {}) if isinstance(ctx, dict) else {}
    rows = [["Элемент", "Источник / статус"]]
    rows.append(["Данные проекта", str(ctx.get("data_dir", "")) +
                 ("  (СИНТЕТИКА, заменить реальными)" if "sample" in str(ctx.get("data_dir", "")) else "")])
    rows.append(["Ветер", str(w.get("source", "—"))])
    rows.append(["Кран", str(ctx.get("crane", ""))[:120] or "см. data/…/crane.yaml"])
    rows.append(["Правила", f"v{rmeta.get('version', '?')}, каждое правило со ссылкой на ППР/норму"])
    rows.append(["Физика", "ESDU-профиль + порывы Durst; маятник груза сверен с MuJoCo (V4–V7)"])
    rows.append(["Границы", "без CFD-аэродинамики здания, без упругости стрелы, без IFC-геометрии"])
    story.append(table(rows, S, widths=[30 * mm, 140 * mm]))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("⚠ " + DISCLAIMER, S["warn"]))

    # -- сборка -------------------------------------------------------------
    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(15 * mm, 10 * mm, "Симуляция рисков — поддержка решений, а не расчёт по нормам")
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"стр. {doc.page}")
        canvas.setStrokeColor(colors.HexColor("#b0bec5"))
        canvas.line(15 * mm, 13 * mm, A4[0] - 15 * mm, 13 * mm)
        canvas.restoreState()

    doc = SimpleDocTemplate(str(out), pagesize=A4,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=14 * mm, bottomMargin=18 * mm,
                            title="Симуляция рисков строительных работ — отчёт",
                            author="risk-sim pilot")
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    print(f"[pdf] {out} ({out.stat().st_size/1024:.0f} КБ)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    build_pdf(a.results, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
