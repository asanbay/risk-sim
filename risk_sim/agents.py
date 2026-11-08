from __future__ import annotations

from typing import Any

import networkx as nx
import numpy as np
import pandas as pd

PALETTE = ["#2e86de", "#10ac84", "#ee5253", "#ff9f43", "#5f27cd", "#01a3a4", "#c8d6e5", "#8395a7"]


def _path_position(points: np.ndarray, fraction: float) -> np.ndarray:
    if len(points) == 1:
        return points[0]
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    total = float(lengths.sum())
    if total == 0:
        return points[-1]
    target = float(np.clip(fraction, 0.0, 1.0)) * total
    cumulative = np.cumsum(lengths)
    index = int(np.searchsorted(cumulative, target, side="right"))
    index = min(index, len(lengths) - 1)
    previous = 0.0 if index == 0 else float(cumulative[index - 1])
    local = 0.0 if lengths[index] == 0 else (target - previous) / lengths[index]
    return points[index] + local * (points[index + 1] - points[index])


def build_agent_timeline(
    works: pd.DataFrame,
    zones: pd.DataFrame,
    building: dict[str, Any],
    config: dict[str, Any],
    timeline_step_s: int,
) -> dict[str, Any]:
    graph_data = building["route_graph"]
    graph = nx.Graph()
    for node, position in graph_data["nodes"].items():
        graph.add_node(node, pos=np.asarray(position, dtype=float))
    for left, right in graph_data["edges"]:
        distance = float(np.linalg.norm(graph.nodes[left]["pos"] - graph.nodes[right]["pos"]))
        graph.add_edge(left, right, weight=distance)
    origin = config.get("origin_node", "hoist")
    speed = float(config.get("walking_speed_m_s", 1.2))
    spacing = float(config.get("worker_spacing_m", 0.8))
    buffer_min = float(config.get("travel_buffer_min", 10.0))
    timeline_start = works.start.min() - pd.Timedelta(minutes=buffer_min)
    timeline_end = works.end.max() + pd.Timedelta(minutes=buffer_min)
    times = pd.date_range(timeline_start, timeline_end, freq=f"{timeline_step_s}s")
    if times[-1] < timeline_end:
        times = times.append(pd.DatetimeIndex([timeline_end]))
    zone_by_id = zones.set_index("zone_id")
    crew_colors = {crew: PALETTE[index % len(PALETTE)] for index, crew in enumerate(sorted(works.crew_id.unique()))}
    agents: list[dict[str, Any]] = []
    tracks: dict[str, np.ndarray] = {}
    states: dict[str, list[str]] = {}

    for task in works.itertuples():
        zone = zone_by_id.loc[task.zone_id]
        target_node = zone.get("route_node", "work")
        if pd.isna(target_node) or target_node not in graph:
            target_node = "work"
        nodes = nx.shortest_path(graph, origin, target_node, weight="weight")
        base_path = np.asarray([graph.nodes[node]["pos"] for node in nodes])
        for worker_index in range(int(task.crew_size)):
            agent_id = f"{task.crew_id}-{task.task_id}-{worker_index + 1:02d}"
            columns = int(np.ceil(np.sqrt(task.crew_size)))
            row, column = divmod(worker_index, columns)
            offset_x = (column - (columns - 1) / 2) * spacing
            rows = int(np.ceil(task.crew_size / columns))
            offset_y = (row - (rows - 1) / 2) * spacing
            center = np.array([(zone.x_min + zone.x_max) / 2, (zone.y_min + zone.y_max) / 2, zone.z_floor + 0.15])
            target = center + np.array([offset_x, offset_y, 0.0])
            target[0] = np.clip(target[0], zone.x_min + 0.2, zone.x_max - 0.2)
            target[1] = np.clip(target[1], zone.y_min + 0.2, zone.y_max - 0.2)
            path = np.vstack([base_path, target]) if np.linalg.norm(base_path[-1] - target) > 1e-6 else base_path
            distance = float(np.linalg.norm(np.diff(path, axis=0), axis=1).sum())
            travel_seconds = max(distance / speed, 1.0)
            departure = task.start - pd.Timedelta(seconds=travel_seconds)
            home_time = task.end + pd.Timedelta(seconds=travel_seconds)
            positions = np.empty((len(times), 3), dtype=float)
            agent_states = []
            for index, timestamp in enumerate(times):
                if timestamp < departure:
                    positions[index] = path[0]
                    state = "waiting"
                elif timestamp < task.start:
                    positions[index] = _path_position(path, (timestamp - departure).total_seconds() / travel_seconds)
                    state = "transition"
                elif timestamp <= task.end:
                    positions[index] = target
                    state = "work"
                elif timestamp <= home_time:
                    positions[index] = _path_position(path[::-1], (timestamp - task.end).total_seconds() / travel_seconds)
                    state = "transition"
                else:
                    positions[index] = path[0]
                    state = "rest"
                agent_states.append(state)
            agents.append({"id": agent_id, "crew_id": task.crew_id, "task_id": task.task_id, "color": crew_colors[task.crew_id]})
            tracks[agent_id] = positions
            states[agent_id] = agent_states

    frames = []
    for index, timestamp in enumerate(times):
        frames.append({
            "t": (timestamp - timeline_start).total_seconds(),
            "timestamp": timestamp.isoformat(),
            "agents": [
                [agent["id"], *np.round(tracks[agent["id"]][index], 4).tolist(), states[agent["id"]][index]]
                for agent in agents
            ],
        })
    return {
        "start": timeline_start.isoformat(),
        "end": timeline_end.isoformat(),
        "step_s": timeline_step_s,
        "agents": agents,
        "frames": frames,
        "state_values": ["waiting", "transition", "work", "rest"],
    }
