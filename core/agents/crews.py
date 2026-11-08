"""
core/agents/crews.py — бригады как агенты.

Модель намеренно простая (раздел 10 спецификации): люди — это не физические
тела, а носители состояния «кто, где, когда». Физика каждого щита опалубки
не нужна; нужно уметь ответить на вопросы:

  * кто находился под грузом и сколько минут;
  * кто работал у открытого края и в какое время;
  * кто оказался в зоне крана при активном подъёме;
  * перекрывал ли кто-то путь эвакуации.

Маршруты строятся по графу проходов (networkx): лестничные клетки,
подъёмник, коридоры. Между уровнями агент обязан пройти через вертикальный
узел — «телепортация» на этаж выше запрещена, иначе симуляция начнёт
придумывать несуществующие пути.

Случайные отклонения (для Monte Carlo):
  * опоздание начала задачи;
  * индивидуальная скорость (логнормально);
  * перерывы;
  * p_shortcut — вероятность того, что бригада срежет путь через зону
    опасности вместо обхода. Это главный источник «неочевидных» конфликтов:
    на планёрке все соглашаются обходить, а по факту идут ближе.
    Параметр ОБЯЗАТЕЛЬНО калибруется с бригадирами, иначе цифра выдумана.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import networkx as nx
import numpy as np
import pandas as pd

from core.context import Context
from core.io.loaders import Dataset, WorkTask, Zone
from core.sim import geometry as geom
from core.sim.rules import RuleEngine
from core.sim.timeline import LoadTrajectory, crane_state

WALKABLE = {"passage", "work", "storage", "node"}

_SEV_ORDER = ["low", "medium", "high", "critical"]


def _downgrade(sev: str) -> str:
    """Снизить тяжесть на одну ступень (для «проходил мимо», а не «работал»)."""
    i = _SEV_ORDER.index(sev) if sev in _SEV_ORDER else 0
    return _SEV_ORDER[max(0, i - 1)]




# ---------------------------------------------------------------------------
# граф проходов
# ---------------------------------------------------------------------------
def build_route_graph(ds: Dataset, *, link_gap_m: float = 10.0,
                      walk_speed_ms: float = 1.1, climb_speed_ms: float = 0.35,
                      hoist_speed_ms: float = 1.0,
                      vertical_max_gap_m: float = 300.0) -> nx.Graph:
    """Граф маршрутов: узлы — проходимые зоны, рёбра — переходы.

    Вес ребра — ВРЕМЯ в секундах (а не расстояние), чтобы лестница и
    подъёмник сравнивались честно: подняться на 240 м в кабине быстрее,
    чем пройти 264 м пешком, и кратчайший путь должен это понимать.

    Горизонтальные рёбра: зоны одного яруса, чьи контуры пересекаются или
    находятся в пределах link_gap_m.
    Вертикальные: зоны типа passage на разных ярусах с совпадающими/близкими
    контурами (лестничная клетка или шахта подъёмника). Если в модели
    представлены не все этажи, связь всё равно строится — вес считается по
    фактическому перепаду высот.

    «Телепортация» между ярусами вне вертикальных узлов запрещена: иначе
    симуляция начнёт придумывать несуществующие пути, и выводы о времени
    пребывания в опасной зоне станут фикцией.
    """
    G = nx.Graph()
    zones = [z for z in ds.zones if z.type in WALKABLE]
    for z in zones:
        G.add_node(z.zone_id, level=z.level, z_floor=z.z_floor, type=z.type,
                   center=z.center, zone=z)

    for i, a in enumerate(zones):
        for b in zones[i + 1:]:
            gap = _rect_gap(a, b)
            same_level = abs(a.z_floor - b.z_floor) < 0.6
            if same_level:
                if gap <= link_gap_m:
                    d = math.dist(a.center[:2], b.center[:2])
                    G.add_edge(a.zone_id, b.zone_id,
                               weight=d / max(walk_speed_ms, 0.1), dist=d, kind="h")
            else:
                if not (a.type == "passage" and b.type == "passage"):
                    continue
                dz = abs(a.z_floor - b.z_floor)
                if dz > vertical_max_gap_m or gap > link_gap_m:
                    continue
                is_hoist = ("hoist" in a.zone_id.lower() or "hoist" in b.zone_id.lower()
                            or "подъёмник" in (a.note + b.note).lower())
                v = hoist_speed_ms if is_hoist else climb_speed_ms
                G.add_edge(a.zone_id, b.zone_id,
                           weight=dz / max(v, 0.05), dist=dz, kind="v",
                           vertical=True, hoist=is_hoist)
    return G


def _rect_gap(a: Zone, b: Zone) -> float:
    dx = max(b.x_min - a.x_max, a.x_min - b.x_max, 0.0)
    dy = max(b.y_min - a.y_max, a.y_min - b.y_max, 0.0)
    return math.hypot(dx, dy)


# ---------------------------------------------------------------------------
# события воздействия
# ---------------------------------------------------------------------------
@dataclass
class ExposureEvent:
    rule_id: str
    title: str
    severity: str
    source: str
    crew_id: str
    people: int
    zone_id: str
    start: pd.Timestamp
    end: pd.Timestamp
    message: str
    lift_id: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0

    @property
    def person_minutes(self) -> float:
        return self.minutes * self.people

    def as_row(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id, "title": self.title, "severity": self.severity,
            "source": self.source, "crew_id": self.crew_id, "people": self.people,
            "zone_id": self.zone_id, "lift_id": self.lift_id or "",
            "start": self.start.strftime("%Y-%m-%d %H:%M"),
            "end": self.end.strftime("%Y-%m-%d %H:%M"),
            "minutes": round(self.minutes, 1),
            "person_minutes": round(self.person_minutes, 1),
            "message": self.message,
            "metrics": "; ".join(f"{k}={v}" for k, v in self.metrics.items()),
        }


# ---------------------------------------------------------------------------
# симуляция
# ---------------------------------------------------------------------------
@dataclass
class CrewState:
    crew_id: str
    people: int
    pos: tuple[float, float, float]
    zone_id: str
    state: str                       # idle | transit | working | break
    task_id: str | None
    path: list[str]                  # оставшиеся узлы маршрута
    speed_ms: float
    until_break: float               # минут до перерыва
    delay_min: float


class AgentSim:
    def __init__(self, ctx: Context, *, dt_min: float = 2.0, seed: int = 1,
                 p_shortcut: float = 0.0, link_gap_m: float = 10.0,
                 guardrail_engine: RuleEngine | None = None):
        self.ctx = ctx
        self.ds = ctx.ds
        self.cfg = ctx.cfg
        self.wind = ctx.wind
        self.dt_min = float(dt_min)
        self.rng = np.random.default_rng(seed)
        self.seed = seed
        self.p_shortcut = float(p_shortcut)
        self.G = build_route_graph(self.ds, link_gap_m=link_gap_m)
        self.lifts = {lf.lift_id: LoadTrajectory(lf, self.ds.task_by_id[lf.task_id])
                      for lf in self.ds.lifts}
        self._guard = guardrail_engine
        self._min_clear = float(ctx.cfg.people.get("min_clearance_to_load_m", 5.0))
        cw = float(ctx.cfg.param("no_work_under_crane_hazard", "corridor_width_m", 8.0))
        er = float(ctx.cfg.param("no_work_under_crane_hazard", "end_radius_m", 6.0))
        # коридоры траекторий считаем один раз — они не зависят от времени
        self._lift_polys = {lf.lift_id: geom.lift_hazard_polygon(lf, cw, er)[0]
                            for lf in self.ds.lifts}
        self.tracks: dict[str, list[dict[str, Any]]] = {}
        self.raw_events: list[tuple[pd.Timestamp, str, ExposureEvent]] = []
        self.warnings: list[str] = []

    # -- ограждения -----------------------------------------------------
    def guardrail_ok(self, zone_id: str, at: pd.Timestamp) -> bool:
        if self._guard is not None:
            return self._guard.guardrail_ok(zone_id, at)
        z = self.ds.zone_by_id.get(zone_id)
        return True if z is None else bool(z.guardrail_installed)

    # -- маршруты -------------------------------------------------------
    def route(self, src: str, dst: str) -> list[str] | None:
        if src == dst:
            return [dst]
        if src not in self.G or dst not in self.G:
            return None
        try:
            return nx.shortest_path(self.G, src, dst, weight="weight")
        except nx.NetworkXNoPath:
            return None

    def _point_in_zone(self, zone_id: str, jitter: float = 0.35) -> tuple[float, float, float]:
        z = self.ds.zone_by_id[zone_id]
        rx = (z.x_max - z.x_min) / 2.0 * jitter
        ry = (z.y_max - z.y_min) / 2.0 * jitter
        cx, cy, cz = z.center
        return (float(cx + self.rng.uniform(-rx, rx)),
                float(cy + self.rng.uniform(-ry, ry)),
                float(z.z_floor + 0.0))

    # -- запуск ---------------------------------------------------------
    def run(self, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> "AgentSimResult":
        ds, cfg = self.ds, self.cfg
        t0 = pd.Timestamp(start) if start is not None else min(t.start for t in ds.tasks)
        t1 = pd.Timestamp(end) if end is not None else max(t.end for t in ds.tasks)
        times = pd.date_range(t0, t1, freq=f"{self.dt_min:g}min")

        walk = cfg.people.get("walk_speed_ms", {}) or {}
        v_mean = float(walk.get("mean", 1.1))
        v_sigma = float(walk.get("sigma", 0.25))
        break_every = float(cfg.people.get("break_every_min", 120))
        delay_cfg = cfg.people.get("task_start_delay_min", {}) or {}
        edge_warn = float(cfg.people.get("edge_proximity_warn_m", 2.0))

        # инициализация бригад
        states: dict[str, CrewState] = {}
        tasks_by_crew: dict[str, list[WorkTask]] = {}
        for c in ds.crews:
            home = c.home_zone if c.home_zone in self.G else self._nearest_walkable(c.home_zone)
            if home is None:
                self.warnings.append(f"Бригада {c.crew_id}: домашняя зона '{c.home_zone}' вне графа проходов — "
                                     f"взята первая проходимая зона.")
                home = next(iter(self.G.nodes))
            states[c.crew_id] = CrewState(
                crew_id=c.crew_id, people=c.size, pos=self._point_in_zone(home), zone_id=home,
                state="idle", task_id=None, path=[home],
                speed_ms=float(np.clip(self.rng.lognormal(math.log(v_mean), 0.35), 0.35, 1.8)),
                until_break=break_every * float(self.rng.uniform(0.7, 1.0)),
                delay_min=float(delay_cfg.get("mean", 15) + self.rng.exponential(float(delay_cfg.get("scale", 10))))
                if delay_cfg else 0.0,
            )
            tasks_by_crew[c.crew_id] = sorted([t for t in ds.tasks if t.crew_id == c.crew_id],
                                              key=lambda t: t.start)

        for c in ds.crews:
            self.tracks[c.crew_id] = []

        # кэш активных задач на шаг
        for ts in times:
            for c in ds.crews:
                st = states[c.crew_id]
                self._step_crew(st, ts, tasks_by_crew[c.crew_id])
                self.tracks[c.crew_id].append({
                    "t": ts.strftime("%Y-%m-%d %H:%M"),
                    "x": round(st.pos[0], 2), "y": round(st.pos[1], 2), "z": round(st.pos[2], 2),
                    "zone": st.zone_id, "state": st.state, "task": st.task_id or "",
                })
                self._evaluate_hazards(st, ts, edge_warn)

        events = self._merge_events()
        return AgentSimResult(sim=self, times=times, events=events,
                              tracks=self.tracks, warnings=self.warnings)

    # ------------------------------------------------------------------
    def _nearest_walkable(self, zone_id: str) -> str | None:
        z = self.ds.zone_by_id.get(zone_id)
        if z is None:
            return None
        best, bd = None, float("inf")
        for nid, nd in self.G.nodes(data=True):
            d = _rect_gap(z, nd["zone"])
            if d < bd:
                best, bd = nid, d
        return best

    def _step_crew(self, st: CrewState, ts: pd.Timestamp, my_tasks: list[WorkTask]) -> None:
        """Один шаг агента: перерыв / работа / переход."""
        if st.people <= 0:
            st.state = "idle"
            return

        # активная задача (с учётом опоздания)
        active = None
        for t in my_tasks:
            s = t.start + pd.Timedelta(minutes=st.delay_min)
            if s <= ts <= t.end:
                active = t
                break

        # перерыв
        st.until_break -= self.dt_min
        if st.until_break <= 0 and st.state != "break":
            st.state = "break"
            st.until_break = float(self.cfg.people.get("break_every_min", 120)) * \
                float(self.rng.uniform(0.7, 1.0))
            st.path = [st.zone_id]
            return
        if st.state == "break":
            st.until_break -= self.dt_min
            if st.until_break <= -15.0:      # перерыв ~15 минут
                st.state = "idle"
                st.until_break = float(self.cfg.people.get("break_every_min", 120))
            return

        if active is None:
            # нет работы: уходим в домашнюю зону
            st.state = "idle"
            st.task_id = None
            home = self.ds.crew_by_id[st.crew_id].home_zone
            if home in self.G and st.zone_id != home:
                st.path = self.route(st.zone_id, home) or [st.zone_id]
            self._advance(st)
            return

        st.task_id = active.task_id
        target = active.zone_id
        if target not in self.G:
            tgt = self._nearest_walkable(target)
            if tgt is None:
                st.state = "idle"
                return
            target = tgt

        if st.zone_id != target or (st.path and st.path[-1] != target):
            new_path = self.route(st.zone_id, target)
            if new_path is None:
                msg = f"Бригада {st.crew_id}: нет маршрута {st.zone_id} -> {target} (граф проходов несвязен)."
                if msg not in self.warnings:
                    self.warnings.append(msg)
                st.state = "idle"
                return
            st.path = new_path
            st.state = "transit"

        if st.state == "transit":
            self._advance(st)
            if st.zone_id == target and len(st.path) <= 1:
                st.state = "working"
                st.pos = self._point_in_zone(target, jitter=0.45)
        else:
            st.state = "working"
            # небольшое перемещение внутри зоны (реалистично и дёшево)
            p = self._point_in_zone(target, jitter=0.25)
            st.pos = (0.85 * st.pos[0] + 0.15 * p[0], 0.85 * st.pos[1] + 0.15 * p[1], p[2])

    def _advance(self, st: CrewState) -> None:
        """Продвигает бригаду вдоль маршрута. Бюджет — ВРЕМЯ шага, а не метры:
        горизонтальный переход и подъём по лестнице/в кабине сравниваются честно.
        """
        if len(st.path) < 2:
            if st.path:
                st.zone_id = st.path[-1]
            return
        budget_s = self.dt_min * 60.0
        walk = max(st.speed_ms, 0.1)

        # срез пути через зону опасности (поведенческое отклонение, калибруется с ПТО)
        if self.p_shortcut > 0 and len(st.path) > 2 and self.rng.random() < self.p_shortcut:
            st.path = [st.path[0], st.path[-1]]

        while budget_s > 0 and len(st.path) >= 2:
            za = self.ds.zone_by_id[st.path[0]]
            zb = self.ds.zone_by_id[st.path[1]]
            a, b = za.center, zb.center
            edge = self.G.get_edge_data(st.path[0], st.path[1]) or {}
            if edge.get("kind") == "v":
                dz = abs(a[2] - b[2])
                v = (1.0 if edge.get("hoist") else 0.35) * max(walk / 1.1, 0.3)
                need = dz / max(v, 0.05)
                length = dz
            else:
                length = math.dist(a[:2], b[:2])
                need = length / walk
            if need < 1e-6:
                st.path.pop(0)
                st.zone_id = st.path[0] if st.path else st.zone_id
                continue
            if budget_s >= need:
                budget_s -= need
                st.path.pop(0)
                st.zone_id = st.path[0]
                st.pos = (b[0], b[1], float(self.ds.zone_by_id[st.zone_id].z_floor))
            else:
                f = budget_s / need
                st.pos = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f,
                          a[2] + (b[2] - a[2]) * f)
                budget_s = 0.0
        if len(st.path) == 1:
            st.zone_id = st.path[0]

    # ------------------------------------------------------------------
    def _evaluate_hazards(self, st: CrewState, ts: pd.Timestamp,
                          edge_warn: float) -> None:
        """Проверка опасных состояний бригады в момент ts.

        Состояния `break` и `idle` не учитываются: работа не выполняется,
        людей в зоне риска по факту нет (бригада в месте сбора). Если на
        объекте место сбора само по себе опасно — это отдельное замечание
        к ПОС, а не к конкретному часу работ.
        """
        if st.state in ("break", "idle") or st.people <= 0:
            return
        x, y, z = st.pos
        cfg = self.cfg

        # 1) активные подъёмы: под грузом / в коридоре траектории
        min_clear = self._min_clear
        for lf in self.ds.lifts:
            task = self.ds.task_by_id[lf.task_id]
            if not (task.start <= ts <= task.end) or task.crew_id == st.crew_id:
                continue
            pos = self.lifts[lf.lift_id].at(ts)
            same_level = abs(z - pos["z"]) < 12.0 or abs(z - lf.pickup[2]) < 3.0
            if not same_level:
                continue
            d_load = math.hypot(x - pos["x"], y - pos["y"])
            poly = self._lift_polys.get(lf.lift_id)
            in_corridor = bool(poly is not None and geom.point_in_polygon(x, y, poly))
            if d_load <= min_clear:
                self._emit(ts, self.cfg.rule("no_work_under_crane_hazard"), st,
                           f"Бригада {st.crew_id} ({st.people} чел.) в {d_load:.1f} м от груза "
                           f"{lf.lift_id} ({lf.load_name}, {lf.mass_kg:.0f} кг) на отметке "
                           f"{pos['z']:.0f} м — непосредственно под грузом.",
                           lift_id=lf.lift_id, check="under_load",
                           distance_to_load_m=round(d_load, 2), load_z_m=round(pos["z"], 1),
                           phase=pos["phase"])
            elif in_corridor:
                self._emit(ts, self.cfg.rule("no_work_under_crane_hazard"), st,
                           f"Бригада {st.crew_id} ({st.people} чел.) находится в коридоре "
                           f"траектории груза {lf.lift_id} ({lf.load_name}); до груза {d_load:.0f} м.",
                           lift_id=lf.lift_id, check="in_corridor",
                           distance_to_load_m=round(d_load, 2), load_z_m=round(pos["z"], 1),
                           phase=pos["phase"])

        # 2) открытый край
        cur = self.ds.zone_by_id.get(st.zone_id)
        edges = [e for e in self.ds.zones if e.type == "edge"
                 and abs(e.z_floor - z) < 1.5 and not self.guardrail_ok(e.zone_id, ts)]
        for e in edges:
            d = _rect_gap_pt(x, y, e)
            inside = e.contains_xy(x, y)
            if inside or d <= edge_warn:
                # Работа у края и ПРОХОД мимо края — разная тяжесть:
                # в первом случае люди заняты там весь смену, во втором
                # пересекают участок. Смешивать их в одну цифру нельзя —
                # иначе человеко-часы риска теряют смысл.
                working_here = st.state == "working"
                base_sev = cfg.rule("edge_work_requires_guardrail").severity
                sev = base_sev if working_here else _downgrade(base_sev)
                act = "работает" if working_here else ("идёт через участок" if st.state == "transit"
                                                       else "находится")
                self._emit(ts, cfg.rule("edge_work_requires_guardrail"), st,
                           f"Бригада {st.crew_id} ({st.people} чел.) {act} "
                           f"{'в зоне открытого края' if inside else f'в {d:.1f} м от открытого края'} "
                           f"{e.zone_id}: ограждение не установлено.",
                           zone_override=e.zone_id, severity_override=sev,
                           distance_to_edge_m=round(d, 2), inside_edge=inside,
                           agent_state=st.state, fall_height_m=round(e.z_floor, 1))

        # 3) ветер выше лимита текущей задачи
        task = self.ds.task_by_id.get(st.task_id) if st.task_id else None
        if task is not None and math.isfinite(task.wind_limit_ms):
            zt = self.ds.zone_by_id[task.zone_id]
            z_work = max(zt.z_top, zt.z_floor + 2.0)
            basis = str(cfg.meta.get("wind_limit_basis", "ref_height"))
            z_cmp = self.wind.ref_height_m if basis == "ref_height" else z_work
            v = self.wind.gust_at(ts, z_cmp)
            if v > task.wind_limit_ms:
                self._emit(ts, cfg.rule("stop_crane_ops_above_wind_limit"), st,
                           f"Бригада {st.crew_id} продолжает '{task.name}' при ветре {v:.1f} м/с "
                           f"(лимит ППР {task.wind_limit_ms:.0f} м/с, базис {basis} @ {z_cmp:.0f} м).",
                           wind_ms=round(v, 2), limit_ms=task.wind_limit_ms, basis=basis)

        # 4) работа в зоне перекрытого прохода
        for zz in self.ds.zones:
            if zz.type != "passage" or not zz.contains_xy(x, y):
                continue
            blockers = [t for t in self.ds.tasks
                        if t.start <= ts <= t.end and t.zone_id == zz.zone_id]
            for b in blockers:
                self._emit(ts, cfg.rule("passage_must_stay_clear"), st,
                           f"Бригада {st.crew_id} идёт через проход {zz.zone_id}, "
                           f"перекрытый задачей '{b.name}'.",
                           zone_override=zz.zone_id, blocking_task=b.task_id)

    def _emit(self, ts: pd.Timestamp, rule, st: CrewState, message: str, *,
              lift_id: str | None = None, zone_override: str | None = None,
              severity_override: str | None = None, **metrics: Any) -> None:
        ev = ExposureEvent(
            rule_id=rule.id, title=rule.title,
            severity=severity_override or rule.severity, source=rule.source,
            crew_id=st.crew_id, people=st.people, zone_id=zone_override or st.zone_id,
            start=ts, end=ts + pd.Timedelta(minutes=self.dt_min), message=message,
            lift_id=lift_id, metrics=metrics,
        )
        self.raw_events.append((ts, f"{ev.rule_id}|{ev.crew_id}|{ev.zone_id}|{ev.lift_id}", ev))

    def _merge_events(self) -> list[ExposureEvent]:
        """Склеивает соседние шаги с одинаковым ключом в интервалы."""
        out: list[ExposureEvent] = []
        self.raw_events.sort(key=lambda r: (r[1], r[0]))
        for key, grp in _groupby(self.raw_events, key=lambda r: r[1]):
            grp = sorted(grp, key=lambda r: r[0])
            cur = grp[0][2]
            for _, _, ev in grp[1:]:
                if ev.start <= cur.end:
                    cur.end = max(cur.end, ev.end)
                    cur.metrics = ev.metrics
                    cur.message = ev.message
                else:
                    out.append(cur)
                    cur = ev
            out.append(cur)
        out.sort(key=lambda e: (e.start, -e.person_minutes))
        return out


def _groupby(seq: Iterable, key):
    """Мини-itertools.groupby без требования предварительной сортировки."""
    buckets: dict[Any, list] = {}
    order: list = []
    for item in seq:
        k = key(item)
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(item)
    return [(k, buckets[k]) for k in order]


def _rect_gap_pt(x: float, y: float, z: Zone) -> float:
    dx = max(z.x_min - x, 0.0, x - z.x_max)
    dy = max(z.y_min - y, 0.0, y - z.y_max)
    return math.hypot(dx, dy)


# ---------------------------------------------------------------------------
# результаты
# ---------------------------------------------------------------------------
@dataclass
class AgentSimResult:
    sim: AgentSim
    times: pd.DatetimeIndex
    events: list[ExposureEvent]
    tracks: dict[str, list[dict[str, Any]]]
    warnings: list[str]

    def events_df(self) -> pd.DataFrame:
        return pd.DataFrame([e.as_row() for e in self.events])

    def tracks_json(self, every: int = 1) -> list[dict[str, Any]]:
        """Формат для Blender/Three.js: один массив точек на бригаду."""
        out = []
        for crew_id, pts in self.tracks.items():
            c = self.sim.ds.crew_by_id.get(crew_id)
            out.append({
                "crew_id": crew_id,
                "name": c.name if c else crew_id,
                "size": c.size if c else 0,
                "color": c.color if c else "#888888",
                "points": pts[::max(1, every)],
            })
        return out

    def summary(self) -> dict[str, Any]:
        df = self.events_df()
        by_rule: dict[str, Any] = {}
        if len(df):
            g = (df.groupby(["rule_id", "severity"])
                   .agg(events=("minutes", "size"), minutes=("minutes", "sum"),
                        person_minutes=("person_minutes", "sum"), people_max=("people", "max"))
                   .reset_index())
            for _, r in g.iterrows():
                by_rule[f"{r['rule_id']} [{r['severity']}]"] = {
                    "events": int(r["events"]), "minutes": round(float(r["minutes"]), 1),
                    "person_minutes": round(float(r["person_minutes"]), 1),
                    "people_max": int(r["people_max"]),
                }
        by_crew: dict[str, Any] = {}
        if len(df):
            gc = df.groupby("crew_id").agg(events=("minutes", "size"),
                                           person_minutes=("person_minutes", "sum"))
            by_crew = {k: {"events": int(v["events"]), "person_minutes": round(float(v["person_minutes"]), 1)}
                       for k, v in gc.T.to_dict().items()}
        return {
            "steps": int(len(self.times)),
            "dt_min": self.sim.dt_min,
            "seed": self.sim.seed,
            "n_events": len(self.events),
            "total_person_minutes": round(float(df["person_minutes"].sum()), 1) if len(df) else 0.0,
            "by_rule": by_rule,
            "by_crew": by_crew,
            "warnings": self.warnings,
        }


def run_agent_sim(ctx: Context, *, dt_min: float = 2.0, seed: int = 1,
                  guardrail_engine: RuleEngine | None = None,
                  start: pd.Timestamp | None = None, end: pd.Timestamp | None = None,
                  **kw: Any) -> AgentSimResult:
    sim = AgentSim(ctx, dt_min=dt_min, seed=seed, guardrail_engine=guardrail_engine, **kw)
    return sim.run(start=start, end=end)


def agent_monte_carlo(ctx: Context, *, runs: int = 200, dt_min: float = 10.0,
                      p_shortcut: float = 0.02, seed: int = 100,
                      guardrail_engine: RuleEngine | None = None,
                      progress: bool = True) -> dict[str, Any]:
    """Много прогонов агентной модели: где риск повторяется чаще всего.

    Возвращает вероятность того, что в прогоне возникнет каждое правило,
    распределение человеко-минут воздействия и топ «горячих» зон.
    """
    rule_hits: dict[str, list[int]] = {}
    person_min: list[float] = []
    zone_hits: dict[str, list[float]] = {}
    crew_hits: dict[str, list[float]] = {}
    t0 = pd.Timestamp.now()

    for i in range(runs):
        sim = AgentSim(ctx, dt_min=dt_min, seed=seed + i, p_shortcut=p_shortcut,
                       guardrail_engine=guardrail_engine)
        res = sim.run()
        df = res.events_df()
        rules_here = set(df["rule_id"]) if len(df) else set()
        for r in {rr.id for rr in ctx.cfg.rules}:
            rule_hits.setdefault(r, []).append(1 if r in rules_here else 0)
        pm = float(df["person_minutes"].sum()) if len(df) else 0.0
        person_min.append(pm)
        if len(df):
            for zid, v in df.groupby("zone_id")["person_minutes"].sum().items():
                zone_hits.setdefault(str(zid), []).append(float(v))
            for cid, v in df.groupby("crew_id")["person_minutes"].sum().items():
                crew_hits.setdefault(str(cid), []).append(float(v))
        if progress and (i + 1) % max(1, runs // 10) == 0:
            print(f"  [agents MC] {i+1}/{runs} прогонов, {pd.Timestamp.now()-t0}")

    def agg(d: dict[str, list[float]]) -> dict[str, dict[str, float]]:
        return {k: {"runs": len(v), "mean": round(float(np.mean(v)), 1),
                    "p95": round(float(np.percentile(v, 95)), 1),
                    "max": round(float(np.max(v)), 1)} for k, v in sorted(d.items())}

    return {
        "runs": runs, "dt_min": dt_min, "p_shortcut": p_shortcut, "seed": seed,
        "elapsed_s": round((pd.Timestamp.now() - t0).total_seconds(), 1),
        "rule_probability": {k: round(float(np.mean(v)), 4) for k, v in sorted(rule_hits.items())},
        "rule_events_mean": {k: round(float(np.mean(v)), 2) for k, v in sorted(rule_hits.items())},
        "person_minutes": {"mean": round(float(np.mean(person_min)), 1),
                           "p50": round(float(np.percentile(person_min, 50)), 1),
                           "p95": round(float(np.percentile(person_min, 95)), 1),
                           "max": round(float(np.max(person_min)), 1)},
        "zone_exposure": agg(zone_hits),
        "crew_exposure": agg(crew_hits),
    }
