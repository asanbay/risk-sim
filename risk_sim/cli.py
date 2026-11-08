from __future__ import annotations

import argparse
import functools
import json
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .io import InputError, load_inputs
from .model import DEFAULT_IFC_TYPES, import_ifc_model
from .physics import validate_physics
from .scenario import run_simulation
from .usd import export_openusd


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="risk-sim", description="Симуляция рисков строительных работ: поддержка решений, не расчёт по нормам")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Выполнить расчёт, Monte Carlo и собрать JSON/DOCX/PDF")
    run.add_argument("--data", default="data", help="Папка с works.xlsx, zones.csv, lifts.csv, wind.csv, rules.yaml и building.json")
    run.add_argument("--output", default="results", help="Папка результатов")
    run.add_argument("--viewer", default="viewer", help="Папка браузерной витрины")
    pilot = commands.add_parser("pilot", help="Расширенный месячный стенд: 11 правил, 6 подъёмов, агенты, отчёты и офлайн-вьюер")
    pilot.add_argument("--data", default="data/sample")
    pilot.add_argument("--output", default="results/pilot")
    pilot.add_argument("--mc-lifts", type=int, default=5000)
    pilot.add_argument("--mc-method", choices=["empirical", "weibull", "normal"], default="empirical")
    pilot.add_argument("--mc-schedule", type=int, default=2000)
    pilot.add_argument("--agents-mc", type=int, default=0)
    pilot.add_argument("--agent-dt", type=float, default=5.0)
    pilot.add_argument("--timeline-dt", type=float, default=10.0)
    pilot.add_argument("--swing-engine", choices=["analytic", "mujoco"], default="analytic")
    pilot.add_argument("--skip-lift-swing", action="store_true")
    pilot.add_argument("--clearance-margin", type=float, default=0.5)
    pilot.add_argument("--no-clearance", action="store_true")
    pilot.add_argument("--seed", type=int, default=42)
    pilot.add_argument("--no-report", action="store_true")
    pilot.add_argument("--no-viewer", action="store_true")
    pilot.add_argument("--gif", action="store_true")
    usd = commands.add_parser("export-usd", help="Создать слоистый OpenUSD и опционально flat-USD для Blender")
    usd.add_argument("--bundle", default="results/pilot/scene_bundle.json")
    usd.add_argument("--output", default="results/pilot/usd")
    usd.add_argument("--fps", type=float, default=24.0)
    usd.add_argument("--every", type=int, default=1, help="Брать каждый N-й кадр исходного таймлайна")
    usd.add_argument("--flatten", action="store_true", help="Дополнительно создать stage_flattened.usda через Usd.Stage.Flatten")
    usd.add_argument("--blender", help="Путь к исполняемому файлу Blender с pxr")
    validate = commands.add_parser("validate", help="Проверить формулы и маятник на известных случаях")
    validate.add_argument("--data", default="data")
    importer = commands.add_parser("import-ifc", help="Импортировать IFC и создать data/model.glb + model_import.json")
    importer.add_argument("input", help="Путь к IFC-файлу")
    importer.add_argument("--data", default="data", help="Папка входных данных проекта")
    importer.add_argument("--include-types", type=lambda value: tuple(item.strip() for item in value.split(",") if item.strip()), default=DEFAULT_IFC_TYPES, metavar="TYPES", help="Типы IFC через запятую")
    importer.add_argument("--max-triangles", type=int, default=200_000)
    serve = commands.add_parser("serve", help="Открыть интерактивный просмотр на localhost")
    serve.add_argument("--viewer", default="viewer")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-open", action="store_true", help="Не открывать браузер автоматически")
    stage = commands.add_parser("export-stage", help="scene_bundle.json → OpenUSD stage (единый формат платформы)")
    stage.add_argument("--bundle", default="results/dynamics/scene_bundle.json")
    stage.add_argument("--output", default="", help="Путь к scene.usda (по умолчанию рядом с бандлом)")
    stage.add_argument("--fps", type=float, default=24.0)
    stage.add_argument("--no-validate", action="store_true", help="Пропустить сверку stage с bundle")
    clearance = commands.add_parser("clearance", help="Клиренс грузового пути против 3D-модели (GLB) или зон")
    clearance.add_argument("--bundle", default="results/dynamics/scene_bundle.json")
    clearance.add_argument("--data", default="data/sample", help="Папка данных: model.glb и zones.csv")
    clearance.add_argument("--margin", type=float, default=0.5, help="Допустимый габарит, м")
    clearance.add_argument("--output", default="", help="Куда писать clearance_report.json (по умолчанию рядом с бандлом)")
    clearance.add_argument("--frame-step", type=int, default=1, help="Проверять каждый N-й кадр")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        if args.command == "run":
            result = run_simulation(args.data, args.output, args.viewer)
            print(f"Готово за {result['runtime_s']:.2f} с; событий: {result['event_count']}")
            for key in ("scenario", "conflicts", "report_docx", "report_pdf"):
                print(f"{key}: {result[key]}")
            print("Просмотр: risk-sim serve; затем http://127.0.0.1:8000")
            print("Результат симуляции — поддержка решений, а не расчёт по нормам.")
        elif args.command == "pilot":
            from .pilot import main as pilot_main
            argv = [
                "--data", args.data, "--out", args.output,
                "--mc-lifts", str(args.mc_lifts), "--mc-method", args.mc_method,
                "--mc-schedule", str(args.mc_schedule), "--agents-mc", str(args.agents_mc),
                "--agent-dt", str(args.agent_dt), "--timeline-dt", str(args.timeline_dt),
                "--swing-engine", args.swing_engine, "--seed", str(args.seed),
                "--clearance-margin", str(args.clearance_margin),
            ]
            if args.no_clearance:
                argv.append("--no-clearance")
            for flag in ("skip_lift_swing", "no_report", "no_viewer", "gif"):
                if getattr(args, flag):
                    argv.append(f"--{flag.replace('_', '-')}")
            return_code = pilot_main(argv)
            if return_code:
                raise SystemExit(return_code)
        elif args.command == "export-usd":
            result = export_openusd(args.bundle, args.output, args.fps, args.every, args.flatten, args.blender)
            print(f"Слоистый USD: {result['stage']}")
            print(f"Manifest: {result['manifest']}")
            if "flattened_stage" in result:
                print(f"Flat-USD для Blender: {result['flattened_stage']}")
                print(f"Проверка timeSamples: {result['flatten_validation']}")
        elif args.command == "validate":
            inputs = load_inputs(args.data)
            result = validate_physics(inputs.rules["physics"])
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            if not result["all_passed"]:
                raise SystemExit(1)
        elif args.command == "import-ifc":
            result = import_ifc_model(args.input, args.data, args.include_types, args.max_triangles)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print("Теперь выполните risk-sim run для обновления браузерной сцены.")
        elif args.command == "clearance":
            from .clearance import compute_clearance
            bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
            tl = bundle["timeline"]
            lifts = {lf.get("lift_id", "?"): {
                "load_name": lf.get("load_name", ""),
                "area_m2": (lf.get("input") or {}).get("area_m2", 3.0),
            } for lf in bundle.get("lifts", [])}
            data_dir = Path(args.data)
            model_path = data_dir / "model.glb"
            zones = [z for z in tl.get("zones", []) if z.get("type") in ("work", "storage")]
            result = compute_clearance(tl["frames"], tl["crane"], lifts, margin_m=args.margin,
                                       model_path=model_path if model_path.is_file() else None,
                                       zones=zones, frame_step=args.frame_step)
            info = result.as_dict()
            out_path = Path(args.output) if args.output else Path(args.bundle).parent / "clearance_report.json"
            out_path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"Кадров проверено: {info['checked_frames']}; событий: {len(info['events'])}; "
                  f"индекс: {info['index_mode']}; модель: {info['model_file'] or 'зоны'}")
            for ev in info["events"][:10]:
                print(f"  {ev['start']} → {ev['end']}  {ev['lift_id']}: {ev['min_dist_m']:.2f} м "
                      f"({ev['component']}, {ev['status']})")
            print(f"Отчёт: {out_path}")
        elif args.command == "export-stage":
            from .usd_stage import export_stage, validate_stage
            bundle = Path(args.bundle)
            out = Path(args.output) if args.output else bundle.parent / "scene.usda"
            info = export_stage(bundle, out, fps=args.fps)
            print(f"Stage: {info['stage']} · кадров {info['frames']} · fps {info['fps']} · "
                  f"типов грузов {len(info['kinds'])} · мешей здания {info['building_meshes']}")
            if not args.no_validate:
                rep = validate_stage(bundle, out)
                if rep["verdict"] != "PASS":
                    raise SystemExit(2)
        elif args.command == "serve":
            viewer = Path(args.viewer).resolve()
            if not (viewer / "scenario.json").exists():
                parser.error("viewer/scenario.json не найден: сначала выполните risk-sim run")
            handler = functools.partial(SimpleHTTPRequestHandler, directory=str(viewer))
            server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
            url = f"http://127.0.0.1:{args.port}"
            print(f"Просмотр: {url}. Остановка: Ctrl+C")
            if not args.no_open:
                threading.Timer(0.5, lambda: webbrowser.open(url)).start()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except RuntimeError as error:
        parser.exit(2, f"Ошибка выполнения:\n{error}\n")
    except (InputError, ValueError) as error:
        parser.exit(2, f"Ошибка входных данных:\n{error}\n")


if __name__ == "__main__":
    main()
