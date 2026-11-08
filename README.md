# Система симуляции рисков строительных работ

Объединённый пилот: Python-ядро, MuJoCo, IFC/GLB, Blender, Three.js, Streamlit,
Monte Carlo и автоматические отчёты. **Результат симуляции — поддержка решений,
а не расчёт по нормам.** Допустимые значения задаются из ППР и паспортов.

Все входы в `data/` сейчас демонстрационные и не предназначены для производства работ.

## Установка

```bash
python3.13 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e '.[bim,physics,test]'
```

Требуется Python 3.12+. Blender обнаруживается из Steam на этом Mac или задаётся
через `BLENDER=/полный/путь/к/Blender`.

## Два контура одного CLI

### Быстрый сценарий

Одиночный подъём, конфликты, Monte Carlo, MuJoCo, IFC/GLB, Three.js, DOCX/PDF:

```bash
.venv/bin/risk-sim validate --data data
.venv/bin/risk-sim run --data data --output results --viewer viewer
.venv/bin/risk-sim serve --viewer viewer --port 8765
```

Панель этого контура: `.venv/bin/streamlit run dashboard.py`.
Основной контракт: `results/scenario_*.json`.

### Расширенный пилот

Месячный график, 11 правил, 6 подъёмов, паспорт крана, профиль ветра по высоте,
агенты и экспозиция, сценарии смягчения, графики и автономный HTML:

```bash
.venv/bin/risk-sim pilot --data data/sample --output results/pilot \
  --swing-engine analytic --gif
```

Для финальной динамики подъёмов используйте `--swing-engine mujoco`.
Режим `moving_anchor` проигрывает полную траекторию опоры по фазам ППР,
а `collision_geometry` записывает контакты груза с перекрытиями и ядром,
интервал и максимальную нормальную силу. Шаг интегрирования задаётся в
`physics.moving_timestep_s`; для инженерной приёмки требуется проверка
сходимости по шагу, а не только успешный прогон.
`python run.py` сохранён как совместимый вход в этот же контур.

## Клиренс грузового пути против 3D-модели

`pilot` (и отдельная команда `clearance`) свипает путь груза, строповки, крюка,
стрелы и башни по кадрам таймлайна и меряет дистанцию до меша здания из
`data/model.glb` (после `import-ifc`; без модели — до объёмов зон work/storage).
Нарушение — дистанция меньше `--clearance-margin` (по умолчанию 0.5 м):

```bash
.venv/bin/risk-sim clearance --bundle results/dynamics/scene_bundle.json \
  --data data/sample --margin 0.5
```

События попадают в findings (rule `load_path_clearance`) и в блок
`scene_bundle["clearances"]`; Blender-сцена рисует ленту маркеров вдоль пути
груза: зелёный — норма, жёлтый — предупреждение (< 2×допуск), красный —
нарушение. Для синтетического sample-здания используйте
`python tools/make_sample_glb.py --data data/sample`.

## OpenUSD stage — единый формат платформы (этап 1 плана)

План развития до уровня Omniverse на Apple Silicon:
`docs/development-plan-omniverse-level.md`. Первый этап выполнен:

```bash
.venv/bin/risk-sim export-stage --bundle results/dynamics/scene_bundle.json
# → results/dynamics/scene.usda + scene_stage_validation.json (PASS)
```

Stage (937 кадров, timeSamples): башня/стрела/тележка/крюк, 4 типа грузов
с visibility по кадрам, 123 меша здания из GLB, зоны рисков, маркеры клиренса,
бригады-капсулы, NOAA-солнце (DistantLight), 3 камеры. Валидатор stage
(`validate_stage`) сверяет timeSamples с расчётом: допуск 0.5°/5 см — факт
0.0/0.0. Stage открывается в любом USD-приложении: Blender (File → Import →
USD), usdview, Houdini; следующий этап — RealityKit/Metal реалтайм-вьюер.

Панель расширенного пилота: `.venv/bin/streamlit run app_streamlit.py`.
Основной контракт: `results/pilot/scene_bundle.json`.
Результаты: `report.html`, `report.docx`, `report.pdf`, `recommendations.csv`,
`before_after.csv`, `viewer.html` и `simulation.gif`.
`viewer.html` открывается двойным щелчком без сервера и сети.

## Desktop (macOS)

Нативное приложение на SwiftUI без Electron и обязательного браузера:

```bash
cd desktop/RiskSim
scripts/build_app.sh
open dist/RiskSim.app
```

Приложение вызывает `.venv/bin/python -m risk_sim.cli` как бэкенд, читает
`scene_bundle.json`, показывает конфликты, подъёмы и таймлайн, запускает
Blender, открывает PDF/DOCX и каталог результатов. Исходники:
`desktop/RiskSim/Sources/RiskSimApp`.

## IFC

```bash
.venv/bin/risk-sim import-ifc /путь/к/модели.ifc --data data
```

Для расширенного пилота используйте `--data data/sample`.
Импорт создаёт `model.glb` и `model_import.json`, учитывает единицы IFC,
сохраняет SHA-256 и список ошибок геометрии. Рабочие зоны и маршруты не выводятся
из IFC автоматически: их задают и подтверждают в CSV/YAML отдельно.

## Blender

```bash
FRAMES=24 EVERY=40 FPS=12 \
  OUT=results/pilot/preview.mp4 SCENE=results/pilot/preview.blend \
  ./blender/render.sh
```

Без `FRAMES` рендерится вся сцена. `blender/animate.py` обслуживает быстрый
сценарий, `scene_setup.py` и `animate_pilot.py` — расширенный пилот.

## OpenUSD

```bash
.venv/bin/risk-sim export-usd \
  --bundle results/pilot/scene_bundle.json \
  --output results/pilot/usd --every 1 --flatten
```

`stage.usda` содержит слои `animation`, `risks`, `crews`, `crane`, `geometry`
в порядке от сильного к слабому. При наличии `model.glb` добавляется слой
`ifc_mesh`: вершины и треугольники IFC в метрах, исходные `GlobalId` и
`ifc_type`, SHA-256 GLB. В этом случае процедурная оболочка здания отключается.
`stage_flattened.usda` создаётся штатным `Usd.Stage.Flatten()` через `pxr`
в Blender и сохраняет `timeSamples` для Transform Cache. `manifest.json`
фиксирует единицы, ось Z, хэш источника, состав слоёв и ограничения.
Кран и грузы остаются визуальными прокси.

## Проверки

```bash
.venv/bin/python -m pytest tests -q
make check
cd desktop/RiskSim && swift test
```

Проверки включают V1–V11 исходного пилота, схемы визуализации, Streamlit,
импорт метеоданных/ППР, MuJoCo, IFC и OpenUSD. Подробности: `docs/methodology.md`,
`docs/data_dictionary.md`, `docs/pilot_plan.md`.

## Makefile

- `make scenario` — быстрый сценарий;
- `make pilot` — расширенный пилот с GIF;
- `make all` — оба контура;
- `make test` / `make check` — проверки;
- `make panel` / `make pilot-panel` — Streamlit;
- `make serve` — Three.js на порту 8765;
- `make video` — полный Blender-рендер расширенного пилота;
- `make usd` — слоистый OpenUSD и flat-USD для Blender.

## Лицензия

Код распространяется под лицензией **[GNU AGPL-3.0](LICENSE)** — копилефт,
как у Blender (GPL): любой может использовать, изучать и развивать проект,
но производные работы — включая запуск кода как веб-сервиса — обязаны
оставаться открытыми под той же лицензией. Закрытые коммерческие продукты
на основе этого кода запрещены.

Copyright © 2026 asanbay
