# RiskSim Engine — план развития до уровня NVIDIA Omniverse на Apple Silicon

> Цель: платформа класса Omniverse (USD-native, реалтайм-рендер, физика,
> коллаборация, нейрорендер) на Mac/Apple Silicon, открытой технологической
> базой. Дата: 2026-11-07. Статус ядра: этап 0 выполнен (см. §4).

---

## 1. Разбор «магии» Omniverse на подсистемы

Omniverse — это не один продукт, а 9 подсистем. Уровень «как у NVIDIA» =
воспроизвести каждую из них на открытой базе:

| # | Подсистема Omniverse | Что делает |
|---|---|---|
| 1 | OpenUSD core + схемы | единый формат сцены, композиция слоёв, ссылки |
| 2 | Hydra | абстракция «сцена → рендерер», плагины-делегаты |
| 3 | RTX Renderer | real-time path tracing на железе NVIDIA |
| 4 | PhysX 5 / Flow / Blast | жёсткая физика, жидкости, разрушение |
| 5 | MDL | физичные материалы, переносимость |
| 6 | Nucleus + Live Layers | коллаборация: живая синхронизация правок между DCC |
| 7 | Kit SDK + расширения | каркас приложений (USD Composer/Explorer), плагины на Python |
| 8 | Коннекторы (Blender, CAD, DCC) | импорт/экспорт в единый USD |
| 9 | AI: NuRec (Gaussian Splatting), DLSS | нейрорендер реальных объектов, апскейл/денойз |

---

## 2. Ключевой вывод исследования

Каждая подсистема имеет открытый эквивалент, работающий на Metal/Apple Silicon:

| # | У NVIDIA | Открытый эквивалент на Mac | Зрелость | Наш выбор |
|---|---|---|---|---|
| 1 | OpenUSD | **OpenUSD** (open source, Pixar/ASWF; pip-пакет `usd-core`) | ★★★★★ | уже совместимы (bundle→USD) |
| 2 | Hydra | **Hydra + Hgi** — Storm работает через OpenGL/**Metal**/Vulkan; примеры кастом-делегатов: hdEmbree, hdSt | ★★★★ | свой delegate на Metal (этап 3) |
| 3 | RTX (path tracing real-time) | **Metal RT (M3/M4) + MetalFX Denoiser** — Metal 4 делает path tracing практичным; + Blender Cycles Metal GPU | ★★★★ | RealityKit RT + MetalFX (этап 2–3) |
| 4 | PhysX 5 | **MuJoCo** (инженерная истина, уже есть) + **Jolt** (Swift Package xcframework, macOS/iOS/visionOS) для интерактива | ★★★★ | двухдвижковая схема: MuJoCo=истина, Jolt=интерактив |
| 5 | MDL | **MaterialX + OpenPBR** — открытый стандарт, поддержан Omniverse/Arnold/RenderMan, выравнен с OpenUSD 25.05 | ★★★★ | MaterialX |
| 6 | Nucleus | нет готового открытого аналога; есть **OpenUSDConnect** (live-sync между DCC) и работа Blender над USD live sync | ★★ | свой «Nucleus-лайт» на базе OpenUSDConnect-подхода (этап 4) |
| 7 | Kit SDK | **SwiftUI-хост RiskSim.app** (уже есть) + плагины на Python (наш CLI уже такой) | ★★★ | развить app до Kit-подобного хоста |
| 8 | Коннекторы | Blender (нативный USD), ifcopenshell (IFC→GLB/USD), наши экспортёры | ★★★★ | добавить USD-экспортёры |
| 9 | NuRec/DLSS | **3D Gaussian Splatting** (открытые репо, интеграция в USD обсуждается в AOUSD), **MetalFX Upscaling** | ★★★ | GS-захват реальных строек дроном (этап 4) |

Вывод: **«уровень Omniverse» на Mac достижим** — узкие места: коллаборация (6)
и собственный Hydra-делегат (2–3). Оба закрываются за 2 фазы.

---

## 3. Целевая архитектура RiskSim Engine

```text
┌──────────────────────────────────────────────────────────────────┐
│                    RiskSim.app (SwiftUI, Kit-роль)               │
│   Обзор · План · Конфликты · Подъёмы · Настройки · Плагины       │
└───────────────┬──────────────────────────────┬───────────────────┘
                │                              │
   ┌────────────▼────────────┐    ┌────────────▼─────────────────┐
   │  ИСТИНА (детерминизм)   │    │  РЕАЛТАЙМ (интерактив)       │
   │  Python: MuJoCo, MC,    │    │  Swift: RealityKit + Metal RT│
   │  правила, клиренс,      │    │  Jolt-интерактив, MetalFX,   │
   │  ветер, валидатор       │    │  Raycast-клиренс на GPU      │
   └────────────┬────────────┘    └────────────┬─────────────────┘
                │      scene_bundle.json        │
                ▼                               ▼
        ┌───────────────────────────────────────────────┐
        │        OpenUSD stage (единый формат)          │
        │  наши схемы: RiskSimLift, RiskSimClearance,   │
        │  RiskSimHazard (UsdShade/MaterialX)           │
        └───────┬───────────────┬───────────────┬───────┘
                │               │               │
        ┌───────▼──────┐ ┌──────▼───────┐ ┌─────▼─────────────┐
        │ Hydra        │ │ Blender      │ │ GS-двойник реальной│
        │ delegate     │ │ Cycles/EEVEE │ │ стройки (дрон →    │
        │ RiskSimMetal │ │ (оффлайн)    │ │ Gaussian Splat)    │
        └──────────────┘ └──────────────┘ └────────────────────┘
                │
        ┌───────▼───────────────────────────────────────┐
        │  RiskSim Live Sync (Nucleus-лайт): CRDT-слой  │
        │  правок USD между Mac/iPad/visionOS/CLI       │
        └───────────────────────────────────────────────┘
```

Принципы:
1. **MuJoCo — единственная истина физики** (сходимость, воспроизводимость,
   валидатор 0.0). Jolt — только для интерактива в реалтайме; расхождение
   ловится тем же валидатором.
2. **OpenUSD — единственный формат сцены.** scene_bundle.json остаётся
   входом симуляции, но визуализация читает только USD.
3. **MaterialX/OpenPBR** вместо MDL: открыто, поддержано индустрией.
4. Каждая новая возможность проходит **валидатор** — наш главный актив,
   которого нет у аналогов.

---

## 4. Этапы развития

### Этап 0 — Фундамент ✅ (выполнено, 2026-11)
- [x] Python-ядро: 11 правил, MuJoCo moving-anchor, Monte Carlo, агенты
- [x] Клиренс грузового пути против 3D-меша (trimesh/KDTree, маржа 0.5 м)
- [x] Blender-конвейер: реалистичная сцена, NOAA-солнце, приборы, панель
- [x] Валидатор точности: PASS 0.0 по гейтам, в конвейере рендера
- [x] RiskSim.app: SwiftUI, потоковый журнал, отмена, горячие клавиши
- [x] 70 pytest + 5 swift test — зелёные

### Этап 1 — USD как единый формат (1–2 месяца) — 🟡 ЯДРО ГОТОВО (2026-11-07)
Цель: сцена живёт в OpenUSD, bundle → stage без потерь.
- [x] `risk_sim/usd_stage.py`: scene_bundle → USD stage (timeSamples: кран,
      грузы с visibility по кадрам, NOAA-солнце; здание из GLB — 123 меша;
      зоны, маркеры клиренса, бригады, 3 камеры) — `export-stage`
- [x] Валидатор stage: `validate_stage` сверяет timeSamples с bundle —
      **PASS 0.0° / 0.0 м** (→ scene_stage_validation.json)
- [x] Blender импортирует наш stage (проверено: 401 объект) — конвейер
      «расчёт → USD → любой рендерер» замкнут
- [ ] Кастомные схемы: `RiskSimLift`, `RiskSimClearanceEvent`,
      `RiskSimHazardWindow` (по образцу OpenUSD-plugin-samples)
- [ ] MaterialX/OpenPBR-материалы взамен displayColor
- [ ] usdview/подписанный просмотрщик; стриминг stage
- **DoD**: ✅ частично — stage валидируется расчётом и читается Blender'ом;
  осталось: схемы + материалы + просмотрщик по умолчанию.

### Этап 2 — RiskSim Viewer: реалтайм на Metal (2–4 месяцы) — 🟡 V1 В ПРИЛОЖЕНИИ
Цель: интерактив 60 fps без Blender — то, что у NVIDIA делает RTX.
- [x] Страница «3D» в RiskSim.app: **SceneKit/Metal** — сцена недели работ
      строится из bundle (здание с этажами, кран, зоны, маркеры клиренса,
      бригады), встроенная орбита/зум (allowsCameraControl), покадровая
      связь со скраббингом и плеером, 4 типа грузов переключаются по
      `lift_id` (2026-11-07)
- [!] RealityKit на macOS не даёт управлять камерой (CameraEntity
      отсутствует) — v1 на SceneKit; миграция на RealityKit/RealityView
      когда Apple даст camera API на macOS
- [x] Живой клиренс в 3D: AABB-дистанция от габарита груза до здания на
      каждый кадр, HUD «Клиренс: X м · норма/близко/НАРУШЕНИЕ», груз краснеет
      при нарушении; зоны вспыхивают красным в окна активных findings
      (2026-11-07)
- [x] Физика клиренса выровнена: угол груза вращается по slew и в Python,
      и во вьюере; AABB-формула исправлена (bMin−aMax / aMin−bMax);
      детерминизм KDTree (seed 42). Худший реальныйevent 0.11 м (L-04)
- [!] Jolt (xcframework): пакет EvgenijLutz/JoltPhysics требует swift-tools
      6.3 (у нас 6.1.2) — отложено до обновления Xcode; интерактивное
      перетаскивание сделаем на SceneKit hitTest без Jolt
- [ ] Raycast-клиренс на GPU в реальном времени (Metal Ray Intersector):
      тащишь груз — дистанции до IFC-меша живьём, красный ореол при
      нарушении маржи
- [ ] MetalFX Upscaling на M1; на M3/M4 — MetalFX Denoiser + hardware RT
- [x] Подсветка активных нарушений в 3D (см. выше)
- [ ] Полировка: небо/тонмаппинг, лёгкие решётчатые текстуры крана
- **DoD**: на M1 — 60 fps сцены недели работ (достигнуто на static-сцене);
  клиренс в реалтайме совпадает с Python-валидатором ±5 см (следующий шаг).

### Этап 3 — Собственный Hydra delegate RiskSimMetal (4–7 месяцы)
Цель: свой «RTX» — наш рендерер внутри любого Hydra-приложения (usdview,
Houdini, Blender-плагин).
- [ ] Каркас по hdEmbree/hdSt: HdRenderDelegate на Metal (raster + RT)
- [ ] Path-traced режим с MetalFX Denoiser на M3/M4 (Metal 4)
- [ ] Материалы: MaterialX → Metal шейдерный транслятор
- [ ] plugInfo.json + установка в системный USD
- **DoD**: usdview рендерит сцену RiskSim нашим делегатом с RT-тенями
  и MaterialX; A/B против Cycles — расхождение цвета < 2 ΔE.

### Этап 4 — Коллаборация + нейрорендер (6–9 месяцы)
Цель: «Nucleus-лайт» + цифровой двойник реальной стройки.
- [ ] RiskSim Live Sync: сервер правок USD (WebSocket, CRDT-слой поверх
      слоёв USD), клиенты: app, CLI, Blender-плагин, iPad
- [ ] GS-конвейер: облёт дроном → COLMAP/3DGS → сцена GS в USD
      (по пути Adobe PLY-расширения) → наложение симуляции RiskSim на
      реальную площадку (наш аналог NuRec)
- [ ] Прогресс стройки: GS-снимки по неделям → диф против плана
- **DoD**: два Mac + iPad правят одну USD-сцену синхронно; реальная
  стройка (5 фото/видео с дрона) превращается в двойник с наложенными
  траекториями крана и клиренсом.

### Этап 5 — Streaming и расширения (9–12 месяцы)
- [ ] Thin-client: рендер на Mac-хосте → Metal-захват → H.265 на
      iPad/visionOS, управление обратно (аналог Omniverse Streaming)
- [ ] Kit-подобные расширения: плагины RiskSim на Python
      (`risksim.ext.*`), маркетплейс внутри app
- [ ] Публикация: подпись, нотаризация, сайт, документация

---

## 5. Железо

| Машина | Что даёт | Роль |
|---|---|---|
| M1 (текущая) | MetalFX Upscaling, CPU 8 ядер, GPU 7 ядер | разработка ядра, EEVEE/Cycles рендер, viewer 60 fps без RT |
| M3/M4 (цель) | **hardware RT + MetalFX Denoiser (Metal 4)** | real-time path tracing в viewer и Hydra-делегате |
| visionOS-устройство | RealityKit нативно | spatial-просмотр стройки (этап 5) |

---

## 6. Честные ограничения и наш ответ

| Ограничение NVIDIA | Почему не повторим 1:1 | Наш ответ |
|---|---|---|
| RTX-железо (100+ RT-ядер) | у Apple RT-блоки скромнее | MetalFX Denoiser + upscaling + LOD/instancing; оффлайн-истина — Cycles Metal |
| Экосистема плагинов Omniverse | годы накоплений | наш козырь: верифицируемая физика (валидатор), которой у развлекательных платформ нет |
| MDL | закрытый стандарт NVIDIA | MaterialX/OpenPBR — открытые и уже в Omniverse |
| Nucleus | закрытый сервер | Live Sync на CRDT — проще, открытее, без облака |

---

## 7. Быстрые победы первых 30 дней

1. scene_bundle → USD stage + usdview-проверка (этап 1, ядро)
2. RealityKit-прототип: USDZ сцены недели работ на M1, 60 fps
3. Raycast-клиренс прототип: живая дистанция при перетаскивании груза
4. GS-эксперимент: 100 фото любой площадки → 3DGS-модель → USD

---

## 8. Источники

- Omniverse Kit/USD/Hydra/PhysX: docs.omniverse.nvidia.com (Kit Overview, Omniverse Physics, Platform Overview)
- Hydra и кастом-делегаты: docs.nvidia.com/learn-openusd (Hydra), github.com/PixarAnimationStudios/OpenUSD (hdSt, hdEmbree)
- Metal RT/MetalFX: developer.apple.com/metal, Metal Feature Set Tables; WWDC23 «Your guide to Metal ray tracing»
- RealityKit/Reality Composer Pro: developer.apple.com/documentation/realitycomposerpro; AOUSD «USD Workflows for RealityKit/Vision Pro»
- Коллаборация: github.com/RoboticHuman/OpenUSDConnect; форумы OpenUSD (live sync отсутствует в ядре)
- Нейрорендер: developer.nvidia.com/omniverse/nurec (NuRec/3DGS); обсуждение GS в OpenUSD (openusd.org, фев 2025)
- Материалы: developer.nvidia.com/blog (OpenUSD+MaterialX+OpenPBR); «MaterialX in 2026»
- Jolt на Apple: github.com/EvgenijLutz/JoltPhysics; forums.swift.org
