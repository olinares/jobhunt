"""Load and validate config/roles.yaml.

The YAML file drives title matching (filter.py) and location/region matching
(location.py). This module is only responsible for reading the file, checking
its shape, and handing back a small, typed, immutable structure that the rest
of the package can rely on without re-validating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when config/roles.yaml is missing, malformed, or invalid."""


@dataclass(frozen=True)
class Region:
    """One named region and the raw match phrases that identify it."""

    name: str
    match: tuple[str, ...]


@dataclass(frozen=True)
class RolesConfig:
    """Validated contents of config/roles.yaml."""

    titles: tuple[str, ...]
    seniority_ok: tuple[str, ...]
    exclude_title_terms: tuple[str, ...]
    regions: tuple[Region, ...] = field(default_factory=tuple)

    def region_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.regions)


def load_config(path: str | Path) -> RolesConfig:
    """Read and validate the roles config at `path`.

    Raises ConfigError with a clear, specific message for any missing or
    malformed key. Never raises a bare KeyError/TypeError from bad YAML.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    try:
        raw_text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if data is None:
        raise ConfigError(f"{path} is empty")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping at the top level, got {type(data).__name__}")

    return _parse_config(data, source=str(path))


def parse_config(data: dict[str, Any], *, source: str = "<config>") -> RolesConfig:
    """Validate an already-parsed dict (e.g. in tests). See `load_config`."""
    return _parse_config(data, source=source)


def _parse_config(data: dict[str, Any], *, source: str) -> RolesConfig:
    titles = _require_str_list(data, "titles", source)
    seniority_ok = _require_str_list(data, "seniority_ok", source, allow_empty_items=True)
    exclude_title_terms = _require_str_list(data, "exclude_title_terms", source)
    regions = _parse_regions(data, source)

    return RolesConfig(
        titles=tuple(t.lower() for t in titles),
        seniority_ok=tuple(s.lower() for s in seniority_ok),
        exclude_title_terms=tuple(t.lower() for t in exclude_title_terms),
        regions=regions,
    )


def _require_str_list(
    data: dict[str, Any],
    key: str,
    source: str,
    *,
    allow_empty_items: bool = False,
) -> list[str]:
    if key not in data:
        raise ConfigError(f"{source}: missing required key '{key}'")
    value = data[key]
    if not isinstance(value, list):
        raise ConfigError(f"{source}: '{key}' must be a list, got {type(value).__name__}")
    if not value:
        raise ConfigError(f"{source}: '{key}' must not be empty")

    result: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(
                f"{source}: '{key}[{i}]' must be a string, got {type(item).__name__}: {item!r}"
            )
        if not allow_empty_items and not item.strip():
            raise ConfigError(f"{source}: '{key}[{i}]' must not be blank")
        result.append(item)
    return result


def _parse_regions(data: dict[str, Any], source: str) -> tuple[Region, ...]:
    if "regions" not in data:
        raise ConfigError(f"{source}: missing required key 'regions'")
    regions_raw = data["regions"]
    if not isinstance(regions_raw, dict):
        raise ConfigError(
            f"{source}: 'regions' must be a mapping, got {type(regions_raw).__name__}"
        )
    if not regions_raw:
        raise ConfigError(f"{source}: 'regions' must not be empty")

    regions: list[Region] = []
    for name, body in regions_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{source}: region name {name!r} must be a non-blank string")
        if not isinstance(body, dict):
            raise ConfigError(
                f"{source}: regions.{name} must be a mapping, got {type(body).__name__}"
            )
        if "match" not in body:
            raise ConfigError(f"{source}: regions.{name} missing required key 'match'")
        match_list = body["match"]
        if not isinstance(match_list, list) or not match_list:
            raise ConfigError(f"{source}: regions.{name}.match must be a non-empty list")
        match_terms: list[str] = []
        for i, term in enumerate(match_list):
            if not isinstance(term, str) or not term.strip():
                raise ConfigError(f"{source}: regions.{name}.match[{i}] must be a non-blank string")
            match_terms.append(term.lower())
        regions.append(Region(name=name, match=tuple(match_terms)))

    return tuple(regions)
