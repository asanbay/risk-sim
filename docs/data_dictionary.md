# Словарь данных (v1.0, 2026-11-02)

Назначение: неделя 7 пилота — замена `data/sample/*` на реальные входы без
чтения кода. Все поля, которые читает ядро, перечислены здесь; всё, чего здесь
нет, ядро игнорирует. Единицы — СИ, если не указано иное. Время — локальное
площадки, формат `YYYY-MM-DD HH:MM`.

## 1. Входы

### data/…/rules.yaml
| Раздел | Ключи | Смысл |
|---|---|---|
| meta | project, site_elevation_m, wind_ref_height_m, wind_terrain_alpha, wind_gust_policy, work_window, timezone, version, disclaimer | паспорт сценария; `disclaimer` печатается во всех артефактах |
| physics | g, rho_sea_level, air_density_model, default_cd_range, dynamic_amplification{smooth,normal,abrupt}, sling_included_angle_limit_deg | константы физики и коэффициенты из ППР |
| people | tagline_force_per_person_N, tagline_force_peak_per_person_N, tagline_effectiveness, max_tagline_people, walk_speed_ms{mean,sigma}, min_clearance_m | допуски по людям (ППР/охрана труда) |
| rules[] | id, title, severity, enabled, source, params{} | **source обязателен** (ссылка на ППР/норму), иначе загрузка падает (тест V11c) |

### data/…/crane.yaml + crane_load_chart.csv
| Файл | Поля |
|---|---|
| crane.yaml | crane{id, model, mast{x,y,base_z,top_z,section_m}, jib{length_m,counter_jib_m,head_z}, trolley{min_radius_m,max_radius_m,speed_ms}, hoist{max_speed_ms}, limits{max_load_kg, wind_limit_ops_ms, wind_limit_climbing_ms, slew_speed_dps}} |
| crane_load_chart.csv | radius_m, max_load_kg, source — точки грузовой характеристики; интерполяция монотонная |

### data/…/zones.csv
zone_id, level, type, x_min, y_min, x_max, y_max, z_floor, z_top, guardrail_installed, note.
`type` ∈ {work, edge, passage, storage, crane_hazard, node}; `guardrail_installed`
(true/false) — факт на НАЧАЛО окна работ; `level` — ярус (G, L61, L62…).

### data/…/crews.csv
crew_id, name, size, color (#RRGGBB), home_zone, role. `size` — людей в бригаде
(капсулы агентов и экспозиция считаются по нему).

### data/…/works.csv
task_id, name, zone_id, start, end, crew_id, crew_size, equipment_id,
hazard_tags (`height; crane; …`), wind_limit_ms, guardrail_required (bool),
installs_guardrail (bool), notes. `installs_guardrail=true` означает, что задача
СТАВИТ ограждение — порядок таких задач против работ у края проверяется правилом
edge_work_requires_guardrail.

### data/…/lifts.csv
Обязательные: lift_id, task_id, mass_kg, area_m2, sling_angle_deg, carrying_legs,
sling_wll_kg, radius_m, hook_height_m. Дополнительные: load_name, cd_min, cd_max
(иначе cd), sling_legs, pickup_x/y/z, dropoff_x/y/z, tagline_people,
wind_limit_ms, daf_mode ∈ {smooth, normal, abrupt}.
Проверка перед сдачей: `python tools/import_lift.py check <файл>`.

### data/…/wind.csv
timestamp, mean_ms, gust_ms, ref_height_m, direction_deg, source.
`gust_ms` обязателен для честной оценки: без него ядро предупреждает, что порыв
принят равным средней скорости (занижает риск). Приём реального ряда:
`python tools/import_wind.py …` (единицы m/s|km/h|kt, QC-отчёт рядом).

## 2. Выходы расширенного пилота (results/pilot/)

| Файл | Содержание |
|---|---|
| scene_bundle.json | контракт визуализаций: timeline{frames[], crane, zones, crews, tasks, lifts, crew_tracks, hazard_windows}, findings[], findings_summary, lifts[], agents{}, monte_carlo{}, rules{} |
| findings.json / conflicts.csv | нарушения: rule_id, severity, source, message, start/end, zone_ids(;), task_ids, crew_ids, people_exposed, exposure_person_min, metrics |
| lift_summary.csv / lift_report_*.json / lift_series_*.csv | физика подъёмов: скорости, силы, использования, раскачка, окна, нарушения |
| swing.moving_anchor / swing.collisions | траектория опоры, контакты: имя, интервал, глубина и максимальная нормальная сила |
| mc_lift_*.json / mc_lift_probability.csv / mc_safe_wind_curves.json | Monte Carlo подъёмов: вероятности, перцентили, кривая безопасного ветра |
| mc_schedule.json / mc_schedule_pairs.csv | Monte Carlo графика: P(правил), p95 чел·ч, топ-пары конфликтов |
| agent_events.csv / agent_tracks.json | события воздействия и треки бригад (измеренная экспозиция) |
| scenario_grid.csv / tornado.csv / mitigations.csv | what-if: сетка ветер×оттяжка, чувствительность, варианты оснастки |
| recommendations.csv / before_after.csv | рекомендации по ППР с основанием и эффектом |
| report.html / report.docx / report.pdf | отчёт (pdf — для рассылки; дисклеймер на каждой странице) |
| simulation.gif, stills/, charts/*.png | визуальные артефакты без Blender |
| viewer.html | автономный офлайн-вьюер в каталоге результатов |
| building.glb | экспортированная Blender-сцена; viewer/pilot_three.html — Three.js-просмотр |
| usd/stage.usda и usd/stage_flattened.usda | слоистый OpenUSD и flat-USD для Blender |
| usd/ifc_mesh.usda | при наличии IFC: вершины и треугольники в метрах, GlobalId и ifc_type |

## 3. Две метрики экспозиции (не смешивать)
* **консервативная** — findings: окно правила × все люди задействованных зон;
* **измеренная** — agent_events: фактическое присутствие по шагам симуляции.
Разница — запас безопасности; обе подписаны в отчёте и панели.

## 4. Версионирование
`rules.meta.version` и `scene_bundle.schema_version` растут при любом изменении
схемы; новые поля добавляются additive (старые рендереры не ломаются), тесты
схемы — tests/test_render_and_panel.py.
