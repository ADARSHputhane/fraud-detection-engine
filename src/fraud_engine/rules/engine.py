"""Rules engine: deterministic, explainable checks that run before the ML model.

Rules live in YAML so an analyst can read and change them without touching code. The file is
hot-reloaded: when it changes on disk the next evaluation picks it up, and a broken edit is
logged and ignored, so the last good ruleset keeps running.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from time import monotonic
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..features import FEATURE_NAMES
from .expr import ConditionError, Expr, compile_condition

log = logging.getLogger(__name__)

Action = Literal["DENY", "REVIEW"]
_SEVERITY = {"REVIEW": 1, "DENY": 2}


class RulesError(ValueError):
    pass


class _YAMLLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans.

    YAML 1.1 reads yes/no/on/off as booleans, so a rule named "Off", or a country code "NO",
    silently becomes False. Only true/false are booleans here.
    """


_BOOL_TAG = "tag:yaml.org,2002:bool"
_YAMLLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != _BOOL_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_YAMLLoader.add_implicit_resolver(
    _BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


class _RuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^R\d{3}$")
    name: str
    condition: str
    action: Action
    weight: int = Field(ge=0, le=100, description="Contribution to the 0-100 rule score")
    reason: str = Field(description="Shown to analysts; {feature} placeholders are filled in")
    enabled: bool = True


class _RulesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[_RuleConfig]


@dataclass(frozen=True)
class RuleHit:
    id: str
    name: str
    action: Action
    weight: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "action": self.action,
            "weight": self.weight,
            "reason": self.reason,
        }


@dataclass
class RuleResult:
    hits: list[RuleHit]
    ruleset_version: str
    action: Action | None = None
    score: int = 0

    def __post_init__(self) -> None:
        if self.hits:
            self.action = max((h.action for h in self.hits), key=_SEVERITY.__getitem__)
        self.score = min(100, sum(h.weight for h in self.hits))


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    action: Action
    weight: int
    reason: str
    condition: str
    test: Expr = field(repr=False, compare=False)
    reads: frozenset[str] = frozenset()

    def explain(self, features: Mapping[str, Any]) -> str:
        return _render(self.reason, features)


def _render(template: str, features: Mapping[str, Any]) -> str:
    out = []
    for literal, name, spec, _conv in string.Formatter().parse(template):
        out.append(literal)
        if name is None:
            continue
        value = features.get(name)
        if value is None:
            out.append("n/a")
        else:
            try:
                out.append(format(value, spec or ""))
            except (TypeError, ValueError):
                out.append(str(value))
    return "".join(out)


def _check_template(rule_id: str, template: str) -> None:
    try:
        fields = [name for _, name, _, _ in string.Formatter().parse(template) if name is not None]
    except ValueError as exc:
        raise RulesError(f"{rule_id}: bad reason template: {exc}") from None
    for name in fields:
        # Only bare feature names: no attribute or index access in templates.
        if name not in FEATURE_NAMES:
            raise RulesError(f"{rule_id}: reason uses unknown placeholder {{{name}}}")


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    version: str
    source: str

    @classmethod
    def from_yaml(cls, text: str) -> RuleSet:
        try:
            raw = yaml.load(text, Loader=_YAMLLoader)  # noqa: S506 - a SafeLoader subclass
            config = _RulesFile.model_validate(raw)
        except yaml.YAMLError as exc:
            raise RulesError(f"invalid YAML: {exc}") from None
        except ValidationError as exc:
            raise RulesError(f"invalid rules file: {exc}") from None

        rules: list[Rule] = []
        seen: set[str] = set()
        for rc in config.rules:
            if rc.id in seen:
                raise RulesError(f"duplicate rule id {rc.id}")
            seen.add(rc.id)
            if not rc.enabled:
                continue
            try:
                test, reads = compile_condition(rc.condition, FEATURE_NAMES)
            except ConditionError as exc:
                raise RulesError(f"{rc.id}: {exc}") from None
            _check_template(rc.id, rc.reason)
            rules.append(
                Rule(rc.id, rc.name, rc.action, rc.weight, rc.reason, rc.condition, test, reads)
            )
        version = hashlib.sha256(text.encode()).hexdigest()[:12]
        return cls(rules=tuple(rules), version=version, source=text)

    def evaluate(self, features: Mapping[str, Any]) -> RuleResult:
        hits = [
            RuleHit(r.id, r.name, r.action, r.weight, r.explain(features))
            for r in self.rules
            if r.test(features)
        ]
        return RuleResult(hits=hits, ruleset_version=self.version)


DEFAULT_RULES = "default_rules.yml"


def default_rules_text() -> str:
    return resources.files(__package__).joinpath(DEFAULT_RULES).read_text(encoding="utf-8")


class RulesEngine:
    """A RuleSet loaded from a file and reloaded when the file changes."""

    def __init__(self, path: Path | None = None, check_interval: float = 2.0) -> None:
        self.path = path
        self.check_interval = check_interval
        self.last_error: str | None = None
        self._mtime: float | None = None
        self._checked = monotonic()
        self.ruleset = self._load()
        # Every version loaded in this process, so the audit log can store each one used.
        self.history: dict[str, RuleSet] = {self.ruleset.version: self.ruleset}

    def _load(self) -> RuleSet:
        if self.path is None:
            return RuleSet.from_yaml(default_rules_text())
        self._mtime = os.stat(self.path).st_mtime
        return RuleSet.from_yaml(self.path.read_text(encoding="utf-8"))

    def maybe_reload(self) -> bool:
        """Reload if the file changed. Returns True when a new ruleset was installed."""
        if self.path is None:
            return False
        now = monotonic()
        if now - self._checked < self.check_interval:
            return False
        self._checked = now
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError as exc:
            self.last_error = f"cannot read rules file: {exc}"
            return False
        if mtime == self._mtime:
            return False
        try:
            ruleset = self._load()
        except (RulesError, OSError) as exc:
            # Keep scoring with the last good rules; a typo must not take the engine down.
            self._mtime = mtime
            self.last_error = str(exc)
            log.error("rules reload failed, keeping version %s: %s", self.ruleset.version, exc)
            return False
        self.last_error = None
        changed = ruleset.version != self.ruleset.version
        self.ruleset = ruleset
        self.history[ruleset.version] = ruleset
        if changed:
            log.info("rules reloaded: version %s, %d rules", ruleset.version, len(ruleset.rules))
        return changed

    def evaluate(self, features: Mapping[str, Any]) -> RuleResult:
        self.maybe_reload()
        return self.ruleset.evaluate(features)
