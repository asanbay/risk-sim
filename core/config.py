"""Загрузка правил (rules.yaml) и общие настройки проекта."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Rule:
    id: str
    title: str
    severity: str
    source: str
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    @property
    def weight(self) -> int:
        """Числовой вес тяжести — для сортировки и тепловой карты."""
        return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(self.severity, 1)


class RulesConfig:
    """Обёртка над rules.yaml с валидацией обязательных полей."""

    REQUIRED_RULE_FIELDS = ("id", "title", "severity", "source")

    def __init__(self, raw: dict[str, Any], path: Path | None = None):
        self.path = path
        self.raw = copy.deepcopy(raw)
        self.meta: dict[str, Any] = raw.get("meta", {})
        self.physics: dict[str, Any] = raw.get("physics", {})
        self.people: dict[str, Any] = raw.get("people", {})
        self.rules: list[Rule] = [self._make_rule(r) for r in raw.get("rules", [])]
        self._validate()

    # -- helpers ---------------------------------------------------------
    def _make_rule(self, r: dict[str, Any]) -> Rule:
        missing = [k for k in self.REQUIRED_RULE_FIELDS if not r.get(k)]
        if missing:
            raise ValueError(f"Правило {r.get('id', '<без id>')} не содержит обязательных полей: {missing}. "
                             f"Каждое правило обязано ссылаться на ППР/норму (поле source).")
        return Rule(
            id=r["id"], title=r["title"], severity=r["severity"], source=r["source"],
            params=r.get("params", {}) or {}, enabled=bool(r.get("enabled", True)),
        )

    def _validate(self) -> None:
        ids = [r.id for r in self.rules]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"Дублирующиеся id правил: {sorted(dupes)}")

    # -- accessors -------------------------------------------------------
    def rule(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(f"Правило '{rule_id}' не найдено в {self.path}")

    def enabled_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.enabled]

    def param(self, rule_id: str, key: str, default: Any = None) -> Any:
        return self.rule(rule_id).params.get(key, default)

    @property
    def g(self) -> float:
        return float(self.physics.get("g", 9.80665))

    @property
    def disclaimer(self) -> str:
        return str(self.meta.get("disclaimer", "Результат симуляции — поддержка решений, а не расчёт по нормам."))

    def as_dict(self) -> dict[str, Any]:
        return self.raw


def load_rules(path: str | Path) -> RulesConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"rules.yaml не найден: {p}")
    with p.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return RulesConfig(raw, path=p)
