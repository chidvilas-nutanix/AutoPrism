"""In-memory indexer over the extracted Prism tarball.

Builds the ``lookup`` dict that powers ``get_entity`` and a parallel
BM25 index for ``search_entities``, across components, hooks,
managers, utils, and tokens.

Construction is intentionally synchronous and idempotent: feed it a
package_root and a version and you get back an :class:`Index` you can
query. Re-running on a fresh tarball returns a brand-new instance so
the swap into the tool layer is just a pointer replacement.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from prism_mcp.entities import Entity, EntityType, entity_key
from prism_mcp.parsers.components import walk_components
from prism_mcp.parsers.hooks import walk_hooks
from prism_mcp.parsers.managers import walk_managers
from prism_mcp.parsers.tokens import walk_tokens
from prism_mcp.parsers.utils import walk_utils
from prism_mcp.search import Searcher

logger = logging.getLogger(__name__)


class Index:
    """Read-only lookup over an immutable collection of entities.

    Args:
        entities (Iterable[Entity]): entities to index. Duplicates by
            ``(type, name)`` are resolved by "last write wins" with a
            warning logged; in practice the walker never emits dupes.
        version (str): the tarball version these entities came from.
    """

    def __init__(self, entities: Iterable[Entity], version: str) -> None:
        self._version = version
        self._lookup: dict[tuple[str, str], Entity] = {}
        for entity in entities:
            key = entity_key(entity)
            if key in self._lookup:
                logger.warning("duplicate entity key %s; later wins", key)
            self._lookup[key] = entity
        self._searcher = Searcher(self._lookup.values())

    @property
    def version(self) -> str:
        """Return the tarball version this index was built from."""
        return self._version

    def __len__(self) -> int:
        return len(self._lookup)

    def __contains__(self, key: tuple[str, str]) -> bool:
        return key in self._lookup

    def all(self) -> list[Entity]:
        """Return all indexed entities in stable name order."""
        return sorted(
            self._lookup.values(),
            key=lambda e: (e.type, e.name.lower()),
        )

    def get(self, name: str, type: EntityType) -> Entity | None:
        """Return the entity for ``(type, name)`` or ``None``.

        Args:
            name (str): exact case-sensitive identifier.
            type (EntityType): one of the five entity-type literals.

        Returns:
            Entity | None: the matching entity, or ``None`` if absent.
        """
        return self._lookup.get((type, name))

    def search(
        self,
        query: str,
        top_k: int = 5,
        type: EntityType | None = None,
    ) -> list[dict]:
        """Run a BM25 search and return ranked match rows.

        Delegates to the embedded :class:`Searcher` so callers don't
        have to reach into the index's internals.

        Args:
            query (str): free-text query.
            top_k (int): max results.
            type (EntityType | None): optional type filter.

        Returns:
            list[dict]: see :meth:`Searcher.search`.
        """
        return self._searcher.search(query=query, top_k=top_k, type=type)

    def list(
        self,
        type: EntityType | None = None,
        include_deprecated: bool = False,
    ) -> list[Entity]:
        """Return entities filtered by ``type`` and deprecation status.

        Args:
            type (EntityType | None): if given, only return entities of
                that type.
            include_deprecated (bool): include entities whose
                ``deprecated`` flag is ``True``. Defaults to ``False``
                because LLMs almost never want them.

        Returns:
            list[Entity]: sorted entities matching the filter.
        """
        result = [
            entity
            for entity in self.all()
            if (type is None or entity.type == type)
            and (include_deprecated or not entity.deprecated)
        ]
        return result


def build_index(package_root: Path, version: str) -> Index:
    """Construct an :class:`Index` from an extracted tarball.

    For Slice 3 only component entities are emitted. Subsequent slices
    will append hooks/managers/utils/tokens to the same iterable
    before constructing the index.

    Args:
        package_root (Path): the extracted ``package/`` directory.
        version (str): tarball version label.

    Returns:
        Index: ready-to-query index.
    """
    logger.info(
        "building index package_root=%s version=%s",
        package_root,
        version,
    )
    entities: list[Entity] = []
    entities.extend(walk_components(package_root, version))
    entities.extend(walk_hooks(package_root, version))
    entities.extend(walk_managers(package_root, version))
    entities.extend(walk_utils(package_root, version))
    entities.extend(walk_tokens(package_root, version))
    logger.info("indexed %d entities for version=%s", len(entities), version)
    return Index(entities=entities, version=version)
