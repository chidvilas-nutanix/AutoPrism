"""On-disk cache layout for extracted Prism tarballs.

Per PRD section 5 the layout is::

    <cache_root>/
    ├── <version>/
    │   ├── package/       # extracted tarball root
    │   └── tarball.tgz    # raw bytes (kept for re-verify)
    └── latest -> <version> (symlink)

We treat ``<version>`` directories as opaque: the only invariant is that
``<version>/package/package.json`` exists when extraction completed
cleanly. Half-written directories are removed on next startup by
:meth:`Cache.list_versions`.
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

PACKAGE_DIRNAME = "package"
TARBALL_FILENAME = "tarball.tgz"
LATEST_SYMLINK_NAME = "latest"


class CacheError(RuntimeError):
    """Raised when the on-disk cache is in an unrecoverable state."""


class Cache:
    """Filesystem cache rooted at ``root``.

    Args:
        root (Path): cache root. Created on first use.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """Return the cache root directory."""
        return self._root

    def ensure_root(self) -> None:
        """Create the cache root if it doesn't exist."""
        self._root.mkdir(parents=True, exist_ok=True)

    def version_dir(self, version: str) -> Path:
        """Return the directory we'd use for ``version`` (may not exist).

        Args:
            version (str): semver string from the registry.

        Returns:
            Path: ``<root>/<version>``.
        """
        return self._root / version

    def package_dir(self, version: str) -> Path:
        """Return the extracted ``package/`` directory for ``version``."""
        return self.version_dir(version) / PACKAGE_DIRNAME

    def tarball_path(self, version: str) -> Path:
        """Return the raw tarball path for ``version``."""
        return self.version_dir(version) / TARBALL_FILENAME

    def latest_symlink(self) -> Path:
        """Return the ``latest`` convenience symlink path."""
        return self._root / LATEST_SYMLINK_NAME

    def is_version_cached(self, version: str) -> bool:
        """Return ``True`` iff ``version`` is fully extracted on disk.

        A version is considered cached only if both the package
        directory exists *and* it contains a ``package.json``.

        Args:
            version (str): version to probe.

        Returns:
            bool: ``True`` if usable, ``False`` otherwise.
        """
        pkg = self.package_dir(version)
        return pkg.is_dir() and (pkg / "package.json").is_file()

    def list_versions(self) -> list[str]:
        """Return cached versions, freshest layout first by mtime.

        Half-written directories (where ``package.json`` is missing) are
        ignored. Caller is responsible for any GC.

        Returns:
            list[str]: sorted version strings, newest mtime first.
        """
        if not self._root.is_dir():
            return []
        entries: list[tuple[float, str]] = []
        for child in self._root.iterdir():
            if (
                child.is_dir()
                and child.name != LATEST_SYMLINK_NAME
                and self.is_version_cached(child.name)
            ):
                entries.append((child.stat().st_mtime, child.name))
        entries.sort(reverse=True)
        return [name for _, name in entries]

    def latest_cached_version(self) -> str | None:
        """Return the most recently extracted version, or ``None``.

        The ``latest`` symlink is consulted first; if it's missing or
        broken we fall back to the most recent mtime in
        :meth:`list_versions`.

        Returns:
            str | None: version string or ``None`` when nothing is
            cached.
        """
        symlink = self.latest_symlink()
        if symlink.is_symlink():
            target_name = os.readlink(symlink)
            if self.is_version_cached(target_name):
                return target_name

        versions = self.list_versions()
        return versions[0] if versions else None

    def install_tarball(
        self,
        version: str,
        tarball_bytes: bytes,
    ) -> Path:
        """Extract ``tarball_bytes`` into ``<root>/<version>/`` atomically.

        We extract to a sibling temp directory first, then atomically
        rename it into place. That way a process crash mid-extract never
        leaves a half-written ``<version>/`` that fools
        :meth:`is_version_cached`.

        Args:
            version (str): version label for the destination directory.
            tarball_bytes (bytes): raw ``.tgz`` payload.

        Returns:
            Path: the final ``<root>/<version>/package`` directory.

        Raises:
            CacheError: if the tarball doesn't contain a ``package/``
                root (every npm tarball does; if ours doesn't, refuse).
        """
        self.ensure_root()

        if self.is_version_cached(version):
            logger.info("version already cached version=%s", version)
            return self.package_dir(version)

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".staging-{version}-",
                dir=self._root,
            )
        )
        try:
            with tarfile.open(
                fileobj=BytesIO(tarball_bytes), mode="r:gz"
            ) as tar:
                _safe_extract(tar, staging)
            extracted_package = staging / PACKAGE_DIRNAME
            if not extracted_package.is_dir():
                raise CacheError(
                    f"tarball for {version} did not contain a "
                    f"'package/' directory at the root"
                )

            tarball_dst = staging / TARBALL_FILENAME
            tarball_dst.write_bytes(tarball_bytes)

            final_dir = self.version_dir(version)
            if final_dir.exists():
                shutil.rmtree(final_dir)
            os.replace(staging, final_dir)
            self._update_latest_symlink(version)
            logger.info("installed version=%s path=%s", version, final_dir)
            return final_dir / PACKAGE_DIRNAME
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _update_latest_symlink(self, version: str) -> None:
        """Point the ``latest`` symlink at ``version`` atomically."""
        symlink = self.latest_symlink()
        tmp_link = symlink.with_suffix(".tmp")
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(version)
        os.replace(tmp_link, symlink)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract ``tar`` into ``dest``, refusing path-traversal entries.

    We perform two guards before delegating to ``tar.extractall``:

    1. Reject any member whose name is absolute or contains a ``..``
       segment. ``Path.resolve()`` collapses ``foo/..`` back to the
       parent before we can inspect it, so the string-level check is
       what actually keeps escapes out.
    2. Resolve and confirm the post-extraction path stays under
       ``dest``. This catches symlink-style trickery the filter would
       also reject but with a less helpful message.

    Python 3.12's ``filter='data'`` then runs as a final defense in
    depth: it strips setuid bits and re-validates paths.

    Args:
        tar (tarfile.TarFile): opened tarball.
        dest (Path): destination directory; must already exist.

    Raises:
        CacheError: if a member would escape ``dest``.
    """
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        parts = Path(member.name).parts
        if (
            any(part == ".." for part in parts)
            or Path(member.name).is_absolute()
        ):
            raise CacheError(
                f"tarball member {member.name!r} would escape cache dir"
            )
        target = (dest_resolved / member.name).resolve()
        if dest_resolved not in target.parents and target != dest_resolved:
            raise CacheError(
                f"tarball member {member.name!r} would escape cache dir"
            )
    tar.extractall(dest, filter="data")
