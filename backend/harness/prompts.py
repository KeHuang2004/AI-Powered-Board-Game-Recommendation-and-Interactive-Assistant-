"""Versioned runtime prompt loading and strict template rendering."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..config import ROOT

PROMPT_DIR = ROOT / "prompts" / "runtime"
_ENV = Environment(loader=FileSystemLoader(PROMPT_DIR), undefined=StrictUndefined,
                   trim_blocks=True, lstrip_blocks=True, autoescape=False)


def _path(name: str, suffix: str = ".j2") -> Path:
    path = Path(name)
    if path.name != str(path):
        raise ValueError("Prompt name must not contain a path")
    return PROMPT_DIR / (name if path.suffix else name + suffix)


def metadata(name: str) -> dict[str, str]:
    path = _path(name)
    data = path.read_bytes()
    first_line = data.decode("utf-8").splitlines()[0]
    match = re.search(r"version:\s*(v\d+)", first_line)
    if not match:
        raise ValueError(f"Missing prompt version: {name}")
    return {"name": path.stem, "version": match.group(1), "sha256": hashlib.sha256(data).hexdigest()}


def render(name: str, **variables: object) -> str:
    path = _path(name)
    metadata(path.name)
    return _ENV.get_template(path.name).render(**variables).strip()


def load_yaml(name: str) -> dict:
    path = _path(name, ".yaml")
    first = path.read_text(encoding="utf-8").splitlines()[0]
    if not re.search(r"version:\s*v\d+", first):
        raise ValueError(f"Missing prompt version: {name}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"Prompt YAML must be a mapping: {name}")
    return data
