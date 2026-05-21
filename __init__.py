"""Hermes plugin entry shim.

Hermes Agent's directory-based plugin loader looks for ``__init__.py`` at the
root of ``~/.hermes/plugins/<name>/`` and calls a top-level ``register(ctx)``
function on it. Our actual code lives in ``src/hermes_minebean/`` for the
standard Python ``src/`` layout (so the pip-installable package and the
plugin-loader path stay clean).

This shim does the minimum work needed to bridge the two paths:

1. Ensures ``src/`` is on ``sys.path`` so ``hermes_minebean`` is importable
   when Hermes loads this file via its directory loader (no pip install of
   the plugin required for the directory-load path).
2. Re-exports ``register`` from ``hermes_minebean.plugin_entry`` so Hermes
   can call it.

Users who pip-install ``hermes-mine-bean`` get the same code via the
``hermes_minebean`` package; the shim only matters for the
``hermes plugins install`` install path.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).parent / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Absolute import: works whether loaded as a top-level Hermes plugin module
# (Hermes loader assigns its own module name) or via the pip package.
from hermes_minebean.plugin_entry import register  # noqa: E402

__all__ = ["register"]
