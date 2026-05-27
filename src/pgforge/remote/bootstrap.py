"""Render the bash scripts pgforge runs on remote servers.

Templates live in ``pgforge/remote/scripts/*.sh.j2`` and are loaded via the
package resource API so they work after ``pip install`` (forced-included by
hatchling — see pyproject.toml).
"""

from __future__ import annotations

from importlib.resources import files
from typing import Any

from jinja2 import Environment, StrictUndefined


def _env() -> Environment:
    return Environment(
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def render_script(template_name: str, **context: Any) -> str:
    """Render ``pgforge/remote/scripts/<template_name>`` with ``context``.

    ``template_name`` should include the ``.sh.j2`` suffix. Missing variables
    raise (StrictUndefined) so we catch template bugs at render time, not at
    the remote shell.
    """
    resource = files("pgforge.remote.scripts").joinpath(template_name)
    template = _env().from_string(resource.read_text(encoding="utf-8"))
    return template.render(**context)
