#!/usr/bin/env python3
"""
risk_sim/pilot.py — сквозной прогон расширенного пайплайна.

  risk-sim pilot --data data/sample --output results/pilot [--mc-lifts 5000]
                 [--mc-schedule 2000] [--agents-mc 200]
                 [--swing-engine mujoco|analytic]

Что делает (по разделам спецификации):
  [2a] загрузка и валидация данных        -> results/pilot/context_info.json
  [2c] конфликты и зоны опасности         -> results/pilot/conflicts.csv, findings.json
  [2e] физика груза и крана               -> results/pilot/lift_report_*.json, lift_summary.csv
  [2d] агенты: бригады и маршруты         -> results/pilot/agent_events.csv, agent_tracks.json
  [2f] Monte Carlo                        -> results/pilot/mc_*.json
  [3]  промежуточные данные               -> results/pilot/scene_bundle.json
  [4]  отчёт и визуализация               -> results/pilot/report.html, viewer.html

Порядок намеренно фиксирован: физика считается ДО правил, потому что часть
правил (оттяжка, стропы, грузовая характеристика) читает её результат.
"""
from __future__ import annotations

import argparse
import subprocess
import json
import sys
import time
from pathlib import Path

import pandas as pd

from core.context import build_context
from core.export.writers import write_all, ensure_results_dir
from core.sim.rules import RuleEngine, analyze
from core.sim.timeline import build_timeline
from core.agents.crews import run_agent_sim, agent_monte_carlo
from core.sim.montecarlo import lift_monte_carlo, schedule_monte_carlo


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Симуляция рисков строительных работ — сквозной прогон")
    ap.add_argument("--data", default="data/sample", help="папка входных данных")
    ap.add_argument("--out", default="results/pilot", help="папка результатов")
    ap.add_argument("--mc-lifts", type=int, default=5000, help="прогонов Monte Carlo на подъём")
    ap.add_argument("--mc-method", default="empirical", choices=["empirical", "weibull", "normal"])
    ap.add_argument("--mc-schedule", type=int, default=2000, help="прогонов MC графика")
    ap.add_argument("--agents-mc", type=int, default=0,
                    help="прогонов MC агентов (0 = пропустить; 200 ≈ несколько минут)")
    ap.add_argument("--agent-dt", type=float, default=5.0, help="шаг агентной симуляции, мин")
    ap.add_argument("--timeline-dt", type=float, default=10.0, help="шаг кадров таймлайна, мин")
    ap.add_argument("--swing-engine", default="analytic", choices=["analytic", "mujoco"],
                    help="analytic — быстро (для MC), mujoco — для финального видео")
    ap.add_argument("--skip-lift-swing", action="store_true", help="не считать раскачку (быстрее)")
    ap.add_argument("--clearance-margin", type=float, default=0.5,
                    help="допустимый габарит грузового пути до конструкций, м")
    ap.add_argument("--no-clearance", action="store_true",
                    help="пропустить проверку клиренса грузового пути")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--gif", action="store_true",
                    help="собрать results/simulation.gif (без Blender)")
    ap.add_argument("--no-viewer", action="store_true")
    args = ap.parse_args(argv)

    t_start = time.time()
    out = ensure_results_dir(args.out)

    # -- [2a] данные ------------------------------------------------------
    print(f"[1/7] Загрузка данных из {args.data} …")
    ctx = build_context(args.data)
    print(f"      зон: {len(ctx.ds.zones)}  задач: {len(ctx.ds.tasks)}  подъёмов: {len(ctx.ds.lifts)}  "
          f"часов ветра: {len(ctx.wind.ts)}")
    for w in ctx.warnings:
        print(f"      ⚠ {w[:150]}")

    # -- [2e] физика ------------------------------------------------------
    print(f"[2/7] Физика подъёмов (движок раскачки: {args.swing_engine}) …")
    t0 = time.time()
    la = ctx.build_lift_assessments(run_swing=not args.skip_lift_swing, engine=args.swing_engine)
    for a in la:
        v = ", ".join(x["rule_id"] for x in a.violations) or "—"
        print(f"      {a.lift.lift_id:<6} {a.lift.load_name[:34]:<34} "
              f"Vраб={a.summary['v_work_max_ms']:5.1f} м/с  Fв={a.summary['wind_force_max_N']:7.0f} Н  "
              f"Uстроп={a.summary['sling_utilization_max']:.2f}  нарушения: {v}")
    print(f"      готово за {time.time()-t0:.1f} с")

    # -- [2c] правила и конфликты ----------------------------------------
    print("[3/7] Проверка правил и поиск конфликтов …")
    t0 = time.time()
    eng, findings, df = analyze(ctx.ds, ctx.cfg, ctx.wind, la)
    sev = df["severity"].value_counts().to_dict() if len(df) else {}
    print(f"      найдено {len(findings)} нарушений: {sev}  ({time.time()-t0:.1f} с)")
    if len(df):
        by_rule = df.groupby("rule_id").size().sort_values(ascending=False)
        for rid, n in by_rule.items():
            r = ctx.cfg.rule(rid)
            print(f"        {n:>3}  {r.severity:<8} {rid}")

    # -- [2d] агенты ------------------------------------------------------
    agent_res = None
    print(f"[4/7] Агенты: бригады и маршруты (шаг {args.agent_dt:g} мин) …")
    t0 = time.time()
    agent_res = run_agent_sim(ctx, dt_min=args.agent_dt, seed=args.seed, guardrail_engine=eng)
    s = agent_res.summary()
    print(f"      кадров: {s['steps']}, событий воздействия: {s['n_events']}, "
          f"человеко-минут: {s['total_person_minutes']:.0f}  ({time.time()-t0:.1f} с)")
    for rid, v in sorted(s["by_rule"].items()):
        print(f"        {rid:<32} событий {v['events']:>3}  чел·мин {v['person_minutes']:>9.0f}")

    # -- [2f] Monte Carlo -------------------------------------------------
    print(f"[5/7] Monte Carlo: {args.mc_lifts} прогонов на подъём, {args.mc_schedule} по графику …")
    t0 = time.time()
    mc_lifts = []
    for lf in ctx.ds.lifts:
        task = ctx.ds.task_by_id[lf.task_id]
        mc_lifts.append(lift_monte_carlo(ctx, lf, task, n=args.mc_lifts, seed=args.seed,
                                         method=args.mc_method))
    for r in mc_lifts:
        p = r.probabilities
        print(f"      {r.lift_id:<6} P(оттяжка)={p['P_tagline_force_exceeded']:6.1%}  "
              f"P(ветер>лимита)={p['P_wind_above_ppr_limit']:6.1%}  "
              f"P(строп)={p['P_sling_utilization_exceeded']:5.1%}  "
              f"P(критич. любое)={p['P_any_critical']:6.1%}")
    sched_mc = schedule_monte_carlo(ctx, n=args.mc_schedule, seed=args.seed) if args.mc_schedule else None
    if sched_mc is not None:
        print(f"      график: P(две бригады в зоне)={sched_mc.rule_probabilities.get('no_two_crews_same_zone', 0):.1%}, "
              f"P(работы друг над другом)={sched_mc.rule_probabilities.get('no_work_below_active_work', 0):.1%}, "
              f"чел·ч воздействия p95={sched_mc.exposure_person_hours['p95']}")
    print(f"      готово за {time.time()-t0:.1f} с")

    agents_mc = None
    if args.agents_mc:
        print(f"[5b] Monte Carlo агентов: {args.agents_mc} прогонов …")
        t0 = time.time()
        agents_mc = agent_monte_carlo(ctx, runs=args.agents_mc, dt_min=max(args.agent_dt, 10.0),
                                      guardrail_engine=eng, seed=args.seed)
        print(f"      готово за {time.time()-t0:.1f} с; P по правилам: "
              f"{ {k: f'{v:.0%}' for k, v in agents_mc['rule_probability'].items() if v > 0} }")
        (out / "mc_agents.json").write_text(json.dumps(agents_mc, ensure_ascii=False, indent=1),
                                            encoding="utf-8")

    # -- [3] таймлайн и экспорт ------------------------------------------
    print(f"[6/7] Таймлайн и экспорт в {out} …")
    t0 = time.time()
    tl = build_timeline(ctx, crew_tracks=agent_res.tracks_json(every=1) if agent_res else None,
                        hazard_windows=[f.as_dict() for f in findings], dt_min=args.timeline_dt)

    # -- [3b] клиренс грузового пути против 3D-модели ---------------------
    clearance_res = None
    if not args.no_clearance:
        from risk_sim.clearance import compute_clearance, events_to_findings
        t0c = time.time()
        model_path = ctx.data_dir / "model.glb"
        zones = [z.to_dict() for z in ctx.ds.zones if z.type in ("work", "storage")]
        lifts = {lf.lift_id: lf.to_dict() for lf in ctx.ds.lifts}
        clearance_res = compute_clearance(
            tl["frames"], tl["crane"], lifts, margin_m=args.clearance_margin,
            model_path=model_path if model_path.is_file() else None, zones=zones)
        if clearance_res.events:
            findings = findings + events_to_findings(clearance_res, ctx.ds)
        worst = clearance_res.as_dict().get("worst") or {}
        print(f"      клиренс: кадров {clearance_res.checked_frames}, событий {len(clearance_res.events)}, "
              f"худшая дистанция {worst.get('min_dist_m', '—')} м "
              f"(модель: {clearance_res.model_file or 'зоны'})  ({time.time()-t0c:.1f} с)")

    written = write_all(ctx, findings, la, tl, agent_res, mc_lifts, sched_mc, out,
                        clearance=clearance_res)
    print(f"      файлов: {sum(1 if isinstance(v, str) else len(v) for v in written.values())}"
          f"  ({time.time()-t0:.1f} с)")

    # -- [4] отчёт и просмотр --------------------------------------------
    if not args.no_report:
        print("[7/7] Отчёт …")
        try:
            from report.build_report import build_report
            paths = build_report(ctx, findings, la, agent_res, mc_lifts, sched_mc, agents_mc, out)
            for k, v in paths.items():
                print(f"      {k:<10} {v}")
        except Exception as exc:  # отчёт не должен ронять весь прогон
            print(f"      ⚠ отчёт не собран: {exc.__class__.__name__}: {exc}")
        try:
            from report.build_pdf import build_pdf
            p = build_pdf(out)
            print(f"      pdf        {p}")
        except Exception as exc:
            print(f"      ⚠ PDF не собран: {exc.__class__.__name__}: {exc}")
    if not args.no_viewer:
        try:
            from viewer.build_viewer import build_viewer
            p = build_viewer(out, out / "viewer.html")
            print(f"      viewer     {p}")
        except Exception as exc:
            print(f"      ⚠ просмотр не собран: {exc.__class__.__name__}: {exc}")

    if args.gif:
        try:
            r = subprocess.run([sys.executable, "-m", "tools.make_gif",
                                "--bundle", str(out / "scene_bundle.json"),
                                "--out", str(out / "simulation.gif")],
                               cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True)
            if r.returncode == 0:
                print(f"      gif        {out / 'simulation.gif'}")
            else:
                print(f"      ⚠ GIF не собран: {r.stderr.strip()[:200]}")
        except Exception as exc:
            print(f"      ⚠ GIF не собран: {exc.__class__.__name__}: {exc}")

    dt = time.time() - t_start
    print(f"\nГотово за {dt:.1f} с. Результаты: {out}/")
    print("Напоминание: это аналитическая поддержка решений, а не расчёт по нормам.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
