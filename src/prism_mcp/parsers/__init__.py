"""Parsers for extracting Entity data from the Prism tarball.

Each submodule is intentionally narrow and pure: input goes in, an
``Entity`` (or a list of them) comes out, no I/O. The :mod:`indexer`
glues them to filesystem walking and to the registry-acquired tarball.
"""

from __future__ import annotations
