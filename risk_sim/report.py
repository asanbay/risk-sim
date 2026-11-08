from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import DISCLAIMER

RULE_TITLES = {
    "no_two_crews_same_zone": "Две бригады в одной зоне",
    "no_work_under_crane_hazard": "Работа в зоне опасности крана",
    "edge_work_requires_guardrail": "Край без подтверждённого ограждения",
    "stop_crane_ops_above_wind_limit": "Ветер выше рабочего лимита",
}
RECOMMENDATIONS = {
    "no_two_crews_same_zone": "Развести бригады по времени или зонам; проверить транспортные и проходные зоны после изменения графика.",
    "no_work_under_crane_hazard": "Исключить посторонние работы в зоне подъёма, назначить ограждение и ответственного; повторить прогон после переноса.",
    "edge_work_requires_guardrail": "До начала работ подтвердить ограждение по ППР; статус «не задан» не считать наличием ограждения.",
    "stop_crane_ops_above_wind_limit": "Перенести подъём или пересмотреть окно по высотному прогнозу; порог остановки принять только из паспорта и ППР.",
}


def _time(event: dict[str, Any]) -> str:
    return f"{event['from'][11:16]}–{event['to'][11:16]} {event['from'][:10]}"


def _validation_values(check: dict[str, Any]) -> list[str]:
    actual = next((check[key] for key in ("actual_n", "actual_s", "actual_rad", "actual_max_abs_difference_rad") if key in check), None)
    expected = next((check[key] for key in ("expected_n", "expected_s", "expected_rad") if key in check), None)
    return ["—" if value is None else f"{value:.6g}" for value in (actual, expected)]


def _details(event: dict[str, Any]) -> str:
    details = event["details"]
    if event["rule"] == "stop_crane_ops_above_wind_limit":
        return f"максимум {details['max_wind_ms']:.1f} м/с; лимит {details['limit_ms']:.1f} м/с"
    if event["rule"] == "no_two_crews_same_zone":
        return f"бригады {details['crew_a']} и {details['crew_b']}; всего {details['people_total']} чел."
    if event["rule"] == "no_work_under_crane_hazard":
        return f"пересечение зон {details['intersection_area_m2']:.1f} м²"
    if event["rule"] == "edge_work_requires_guardrail":
        return "ограждение: " + ("не задано" if details["guardrail_status"] == "not_declared" else "отсутствует")
    return str(details)


def _recommendations(events: list[dict[str, Any]]) -> list[str]:
    return [RECOMMENDATIONS[rule] for rule in dict.fromkeys(event["rule"] for event in events)]


def _docx_table(document: Document, headers: list[str], rows: list[list[str]], font_size: float = 8.5) -> None:
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for index, header in enumerate(headers):
        table.rows[0].cells[index].text = header
    for row in rows:
        cells = table.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = value
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(font_size)
                    run.font.name = "Arial"


def _generate_docx(scenario: dict[str, Any], path: Path) -> None:
    metadata = scenario["metadata"]
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Cm(1.5)
    section.left_margin = section.right_margin = Cm(1.5)
    for style in document.styles:
        if hasattr(style, "font"):
            style.font.name = "Arial"
            style.element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
    document.styles["Normal"].font.size = Pt(10)
    footer = section.footer.paragraphs[0]
    footer.text = DISCLAIMER
    footer.style = document.styles["Normal"]
    footer.runs[0].font.size = Pt(7)
    footer.runs[0].font.color.rgb = RGBColor(100, 100, 100)
    document.core_properties.title = f"Симуляция рисков: {metadata['object_name']}"
    document.core_properties.subject = "Аналитическая поддержка, не расчёт по нормам"
    document.core_properties.author = metadata["author"]

    title = document.add_heading("Система симуляции рисков строительных работ", 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle = document.add_paragraph(metadata["object_name"])
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    document.add_paragraph(f"Сценарий: {metadata['name']}")
    document.add_paragraph(f"Версия модели: {metadata['model_version']}; программа: {metadata['software_version']}")
    document.add_paragraph(f"Сформировано: {metadata['generated_at']}")
    document.add_paragraph(f"Автор: {metadata['author']}")
    warning = document.add_paragraph("ДЕМОНСТРАЦИОННЫЕ ДАННЫЕ — НЕ ДЛЯ ПРОИЗВОДСТВА РАБОТ")
    warning.runs[0].bold = True
    warning.runs[0].font.color.rgb = RGBColor(180, 0, 0)
    document.add_heading("Резюме", level=1)
    counts = Counter(event["severity"] for event in scenario["events"])
    document.add_paragraph(
        f"Обнаружено событий: {len(scenario['events'])}; критических: {counts.get('critical', 0)}; "
        f"высокой серьёзности: {counts.get('high', 0)}. Это срабатывания заданных правил, а не прогноз происшествий."
    )
    for recommendation in _recommendations(scenario["events"])[:4]:
        document.add_paragraph(recommendation, style="List Bullet")
    document.add_paragraph(DISCLAIMER)

    document.add_page_break()
    document.add_heading("1. Конфликты и опасные ситуации", level=1)
    _docx_table(document, ["Интервал", "Зона", "Правило", "Работы", "Показатель"], [
        [_time(event), f"{event['level']} / {event['zone_id']}", RULE_TITLES[event["rule"]],
         " ↔ ".join(filter(None, [event["task_a"], event["task_b"]])), _details(event)]
        for event in scenario["events"]
    ])
    document.add_heading("Ссылки на правила", level=2)
    for rule in dict.fromkeys(event["rule"] for event in scenario["events"]):
        event = next(item for item in scenario["events"] if item["rule"] == rule)
        document.add_paragraph(f"{RULE_TITLES[rule]}: {event['reference']}")
    document.add_heading("Тепловая карта по уровням и неделям", level=2)
    _docx_table(document, ["Уровень", "Неделя", "События", "Взвешенный балл"], [
        [row["level"], row["week"], str(row["event_count"]), str(row["severity_score"])]
        for row in scenario["risk_heatmap"]
    ])

    document.add_page_break()
    document.add_heading("2. Подъём: ветер, стропы и оттяжка", level=1)
    for lift in scenario["lifts"]:
        source, result = lift["input"], lift["summary"]
        document.add_heading(f"{source['lift_id']}: {source['load_name']}", level=2)
        _docx_table(document, ["Показатель", "Результат", "Порог / пояснение"], [
            ["Максимальный порыв в окне", f"{result['max_wind_ms']:.2f} м/с", f"рабочий лимит {result['wind_limit_ms']:.2f} м/с"],
            ["Квазистатическая сила ветра", f"{result['max_wind_force_n'] / 1000:.2f} кН", f"оттяжка {result['tagline_capacity_n'] / 1000:.2f} кН"],
            ["Максимальная амплитуда", f"{result['max_abs_swing_deg']:.2f}°", f"смещение {result['max_horizontal_displacement_m']:.2f} м"],
            ["Статическое усилие на ветвь", f"{result['sling_tension_per_leg_n'] / 1000:.2f} кН", f"WLL {result['sling_wll_per_leg_n'] / 1000:.2f} кН; использование {result['sling_utilization']:.1%}"],
            ["Ветер по порогу оттяжки", f"{result['tagline_wind_capacity_ms_by_cd']['cd_nominal']:.2f} м/с", f"диапазон Cd: {result['tagline_wind_capacity_ms_by_cd']['cd_max']:.2f}–{result['tagline_wind_capacity_ms_by_cd']['cd_min']:.2f} м/с"],
        ])
        monte = next(item for item in scenario["monte_carlo"]["wind_and_lift"] if item["lift_id"] == source["lift_id"])
        document.add_paragraph(
            f"Monte Carlo ({monte['samples']} прогонов): превышение ветрового лимита "
            f"{monte['probabilities']['operational_wind_limit']['probability']:.1%}; порога оттяжки "
            f"{monte['probabilities']['tagline_capacity']['probability']:.1%}; статического WLL "
            f"{monte['probabilities']['static_sling_wll']['probability']:.1%}. "
            "Доверительные интервалы и параметры распределений находятся в monte_carlo.json."
        )
    schedule = scenario["monte_carlo"]["schedule"]
    document.add_paragraph(
        f"График, {schedule['samples']} прогонов: хотя бы один временной конфликт в "
        f"{schedule['probability_any_schedule_conflict']['probability']:.1%}; среднее число событий "
        f"{schedule['expected_conflicts_per_run']:.2f}. Это повторяемость конфликтов, не частота аварий."
    )

    document.add_page_break()
    document.add_heading("3. Рекомендации и границы применения", level=1)
    for recommendation in _recommendations(scenario["events"]):
        document.add_paragraph(recommendation, style="List Bullet")
    document.add_heading("Проверка известных случаев", level=2)
    _docx_table(document, ["Проверка", "Расчёт", "Ожидание", "Статус"], [
        [name, *_validation_values(check), "пройдено" if check["pass"] else "НЕ ПРОЙДЕНО"]
        for name, check in scenario["validation"]["checks"].items()
    ])
    document.add_heading("Формулы и чувствительность", level=2)
    document.add_paragraph("Fветра = 0,5·ρ·Cd·A·v²; Tветви = m·g/(n·sin α), α — угол ветви к горизонтали; T₀ = 2π·√(L/g); θстат = atan(Fветра/(m·g)).")
    document.add_paragraph("Для Cd, массы, угла строп и длины подвеса рассчитаны варианты ±10% и ±30% (16 строк на подъём). Квазистатические результаты и изменения вывода по порогам сохранены в scenario_*.json и lift_report.json.")
    document.add_heading("Допущения и ограничения", level=2)
    for assumption in scenario["assumptions"]:
        document.add_paragraph(assumption, style="List Bullet")
    document.add_heading("Материалы", level=2)
    document.add_paragraph("Интерактивный просмотр: viewer/index.html через команду risk-sim serve.")
    document.add_paragraph("Машинные результаты: scenario_*.json, conflicts.csv, lift_report.json, monte_carlo.json.")
    document.add_paragraph(DISCLAIMER)
    document.save(path)


def _font() -> str:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            pdfmetrics.registerFont(TTFont("RiskSans", candidate))
            return "RiskSans"
    raise RuntimeError("Для кириллицы в PDF нужен Arial или Helvetica; не найден ни один системный шрифт")


def _generate_pdf(scenario: dict[str, Any], path: Path) -> None:
    font = _font()
    base = getSampleStyleSheet()
    normal = ParagraphStyle("RiskNormal", parent=base["BodyText"], fontName=font, fontSize=9, leading=12, spaceAfter=5)
    small = ParagraphStyle("RiskSmall", parent=normal, fontSize=7.5, leading=9)
    heading = ParagraphStyle("RiskHeading", parent=base["Heading1"], fontName=font, fontSize=16, leading=20, spaceBefore=6, spaceAfter=10)
    subheading = ParagraphStyle("RiskSubheading", parent=base["Heading2"], fontName=font, fontSize=11, leading=14, spaceBefore=6, spaceAfter=6)
    title = ParagraphStyle("RiskTitle", parent=heading, fontSize=19, leading=24, alignment=TA_CENTER, spaceAfter=16)
    warning = ParagraphStyle("RiskWarning", parent=normal, textColor=colors.darkred, fontSize=11, leading=14)

    def p(text: str, style: ParagraphStyle = normal) -> Paragraph:
        return Paragraph(escape(text), style)

    def table(headers: list[str], rows: list[list[str]], widths: list[float] | None = None) -> Table:
        data = [[p(item, small) for item in headers]] + [[p(item, small) for item in row] for row in rows]
        result = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        result.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbeafe")),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#94a3b8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return result

    def footer(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#475569"))
        canvas.drawString(1.4 * cm, 0.8 * cm, DISCLAIMER[:125])
        canvas.drawRightString(A4[0] - 1.4 * cm, 0.8 * cm, f"стр. {document.page}")
        canvas.restoreState()

    metadata = scenario["metadata"]
    story = [p("Система симуляции рисков строительных работ", title), p(metadata["object_name"], title)]
    story += [
        p(f"Сценарий: {metadata['name']}"), p(f"Версия модели: {metadata['model_version']}; программа: {metadata['software_version']}"),
        p(f"Сформировано: {metadata['generated_at']}"), p(f"Автор: {metadata['author']}"), Spacer(1, 10),
        p("ДЕМОНСТРАЦИОННЫЕ ДАННЫЕ — НЕ ДЛЯ ПРОИЗВОДСТВА РАБОТ", warning), p("Резюме", heading),
    ]
    counts = Counter(event["severity"] for event in scenario["events"])
    story.append(p(f"Обнаружено событий: {len(scenario['events'])}; критических: {counts.get('critical', 0)}; высокой серьёзности: {counts.get('high', 0)}. Это срабатывания заданных правил, а не прогноз происшествий."))
    story += [p("• " + recommendation) for recommendation in _recommendations(scenario["events"])[:4]]
    story += [p(DISCLAIMER, warning), PageBreak(), p("1. Конфликты и опасные ситуации", heading)]
    story.append(table(["Интервал", "Зона", "Правило", "Работы", "Показатель"], [
        [_time(event), f"{event['level']} / {event['zone_id']}", RULE_TITLES[event["rule"]],
         " ↔ ".join(filter(None, [event["task_a"], event["task_b"]])), _details(event)]
        for event in scenario["events"]
    ], [2.6 * cm, 2.4 * cm, 3.5 * cm, 4.1 * cm, 5.2 * cm]))
    story.append(p("Ссылки на правила", subheading))
    for rule in dict.fromkeys(event["rule"] for event in scenario["events"]):
        event = next(item for item in scenario["events"] if item["rule"] == rule)
        story.append(p(f"{RULE_TITLES[rule]}: {event['reference']}", small))
    story.append(p("Тепловая карта", subheading))
    story.append(table(["Уровень", "Неделя", "События", "Балл"], [
        [row["level"], row["week"], str(row["event_count"]), str(row["severity_score"])]
        for row in scenario["risk_heatmap"]
    ]))
    story += [PageBreak(), p("2. Подъём: ветер, стропы и оттяжка", heading)]
    for lift in scenario["lifts"]:
        source, result = lift["input"], lift["summary"]
        story.append(p(f"{source['lift_id']}: {source['load_name']}", subheading))
        story.append(table(["Показатель", "Результат", "Порог / пояснение"], [
            ["Максимальный порыв", f"{result['max_wind_ms']:.2f} м/с", f"лимит {result['wind_limit_ms']:.2f} м/с"],
            ["Сила ветра", f"{result['max_wind_force_n'] / 1000:.2f} кН", f"оттяжка {result['tagline_capacity_n'] / 1000:.2f} кН"],
            ["Максимальная амплитуда", f"{result['max_abs_swing_deg']:.2f}°", f"смещение {result['max_horizontal_displacement_m']:.2f} м"],
            ["Усилие на ветвь (статика)", f"{result['sling_tension_per_leg_n'] / 1000:.2f} кН", f"WLL {result['sling_wll_per_leg_n'] / 1000:.2f} кН; {result['sling_utilization']:.1%}"],
            ["Ветер по порогу оттяжки", f"{result['tagline_wind_capacity_ms_by_cd']['cd_nominal']:.2f} м/с", f"Cd-диапазон {result['tagline_wind_capacity_ms_by_cd']['cd_max']:.2f}–{result['tagline_wind_capacity_ms_by_cd']['cd_min']:.2f} м/с"],
        ], [5.0 * cm, 4.0 * cm, 8.8 * cm]))
        monte = next(item for item in scenario["monte_carlo"]["wind_and_lift"] if item["lift_id"] == source["lift_id"])
        story.append(p(f"Monte Carlo ({monte['samples']}): ветер выше лимита {monte['probabilities']['operational_wind_limit']['probability']:.1%}; порог оттяжки {monte['probabilities']['tagline_capacity']['probability']:.1%}; статический WLL {monte['probabilities']['static_sling_wll']['probability']:.1%}. Это превышения порогов, не вероятность происшествия."))
    schedule = scenario["monte_carlo"]["schedule"]
    story.append(p(f"График ({schedule['samples']} прогонов): хотя бы один конфликт в {schedule['probability_any_schedule_conflict']['probability']:.1%}; среднее число {schedule['expected_conflicts_per_run']:.2f}."))
    story += [PageBreak(), p("3. Рекомендации и границы применения", heading)]
    story += [p("• " + recommendation) for recommendation in _recommendations(scenario["events"])]
    story.append(p("Проверка известных случаев", subheading))
    story.append(table(["Проверка", "Расчёт", "Ожидание", "Статус"], [
        [name, *_validation_values(check), "пройдено" if check["pass"] else "НЕ ПРОЙДЕНО"]
        for name, check in scenario["validation"]["checks"].items()
    ]))
    story.append(p("Формулы и чувствительность", subheading))
    story.append(p("Fветра = 0,5·ρ·Cd·A·v²; Tветви = m·g/(n·sin α), α — угол к горизонтали; T₀ = 2π·√(L/g); θстат = atan(Fветра/(m·g))."))
    story.append(p("Для Cd, массы, угла строп и длины подвеса рассчитаны варианты ±10% и ±30% (16 строк на подъём). Квазистатические результаты сохранены в scenario_*.json и lift_report.json.", small))
    story.append(p("Допущения и ограничения", subheading))
    story += [p("• " + assumption, small) for assumption in scenario["assumptions"]]
    story += [
        p("Материалы", subheading),
        p("Интерактив: viewer/index.html через risk-sim serve. Данные: scenario_*.json, conflicts.csv, lift_report.json, monte_carlo.json."),
        p(DISCLAIMER, warning),
    ]
    document = SimpleDocTemplate(
        str(path), pagesize=A4, rightMargin=1.4 * cm, leftMargin=1.4 * cm,
        topMargin=1.3 * cm, bottomMargin=1.3 * cm,
        title=f"Симуляция рисков: {metadata['object_name']}", author=metadata["author"],
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)


def generate_reports(scenario: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    scenario_id = scenario["metadata"]["scenario_id"]
    paths = {"docx": output_dir / f"report_{scenario_id}.docx", "pdf": output_dir / f"report_{scenario_id}.pdf"}
    _generate_docx(scenario, paths["docx"])
    _generate_pdf(scenario, paths["pdf"])
    return paths
