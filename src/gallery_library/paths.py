"""Library mount configuration, host→backend path mapping and confinement.

``ODYSSEUS_GALLERY_LIBRARY_MOUNTS`` lists the folders a library source may live
in. It is the allowlist *and* the translation table for Docker, where the user
thinks in host paths but the backend sees the container mount::

    ODYSSEUS_GALLERY_LIBRARY_MOUNTS=H:\\Pictures\\Privat=/library/privat;/srv/photos

Entries are separated by ``;`` or newlines. ``host=backend`` maps a host path
(Windows or POSIX) to the backend path; a bare path is used as-is (native
installs). Host paths match case-insensitively for Windows-style paths.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from src.path_confinement import confine

ENV_VAR = "ODYSSEUS_GALLERY_LIBRARY_MOUNTS"

_WINDOWS_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


@dataclass(frozen=True)
class LibraryMount:
    host: str      # user-facing root as configured (may equal backend)
    backend: str   # canonical root the backend reads


def _is_windows_path(value: str) -> bool:
    return bool(_WINDOWS_ABS.match(value or ""))


def _windows_key(value: str) -> str:
    """Comparable form of a Windows path: backslashes, no trailing slash, casefolded."""
    text = (value or "").strip().replace("/", "\\")
    while len(text) > 3 and text.endswith("\\"):
        text = text[:-1]
    return text.lower()  # lower() keeps the length (casefold maps ß→ss), needed for slicing


def library_mounts() -> list[LibraryMount]:
    """Configured mounts, longest host path first. Invalid entries are skipped."""
    mounts: list[LibraryMount] = []
    raw = os.environ.get("ODYSSEUS_GALLERY_LIBRARY_MOUNTS", "")  # literal: the env reference scans for it
    for item in re.split(r"[;\n]", raw):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            host, backend = (part.strip() for part in item.split("=", 1))
        else:
            host = backend = item
        if not backend or not os.path.isabs(backend):
            continue
        mounts.append(LibraryMount(host=host or backend, backend=os.path.realpath(backend)))
    mounts.sort(key=lambda m: len(m.host), reverse=True)
    return mounts


def _host_to_backend(user_path: str, mount: LibraryMount) -> str | None:
    """Translate ``user_path`` through ``mount`` if it lies under the mount's host root."""
    if _is_windows_path(mount.host) or _is_windows_path(user_path):
        root, cand = _windows_key(mount.host), _windows_key(user_path)
        if cand == root:
            rel = ""
        elif cand.startswith(root + "\\"):
            # slice the original string to keep the folder names' real case
            rel = user_path.strip().replace("/", "\\")[len(root) + 1:]
        else:
            return None
        parts = [p for p in rel.split("\\") if p]
        return str(PurePosixPath(mount.backend, *parts)) if parts else mount.backend
    try:
        rel = os.path.relpath(os.path.abspath(user_path), os.path.abspath(mount.host))
    except ValueError:
        return None
    if rel == ".":
        return mount.backend
    if rel.startswith(".."):
        return None
    return os.path.join(mount.backend, rel)


def resolve_source_path(user_path: str) -> str:
    """Return the canonical backend path for a user-entered library folder.

    Accepts a host path (translated through the mounts) or a backend path. The
    result must be an existing directory inside a configured mount.

    :raises ValueError: empty input, no mounts configured, outside every mount,
        or not an existing directory.
    """
    text = str(user_path or "").strip()
    if not text or "\n" in text or "\r" in text or "\x00" in text:
        raise ValueError("Enter the folder path of your photo library")
    mounts = library_mounts()
    if not mounts:
        raise ValueError(
            f"No library folders are configured. Set {ENV_VAR} (and mount the folder "
            "read-only in Docker) — see docker/gallery-library.yml."
        )
    candidates = [c for c in (_host_to_backend(text, m) for m in mounts) if c]
    if not _is_windows_path(text) and os.path.isabs(text):
        candidates.append(text)
    for candidate in candidates:
        for mount in mounts:
            try:
                resolved = confine(mount.backend, candidate)
            except ValueError:
                continue
            if not os.path.isdir(resolved):
                raise ValueError(f"Folder not found: {text}")
            return resolved
    allowed = ", ".join(m.host for m in mounts)
    raise ValueError(f"'{text}' is not inside a configured library folder ({allowed})")


def display_path(backend_path: str) -> str:
    """Best user-facing form of a backend path (host path when a mount maps it)."""
    for mount in library_mounts():
        try:
            rel = os.path.relpath(backend_path, mount.backend)
        except ValueError:
            continue
        if rel == ".":
            return mount.host
        if rel.startswith(".."):
            continue
        if _is_windows_path(mount.host):
            return mount.host.rstrip("\\/") + "\\" + rel.replace("/", "\\")
        return os.path.join(mount.host, rel)
    return backend_path


def confine_to_source(root: str, relpath: str = "") -> str:
    """Resolve ``relpath`` inside a source ``root`` (``..`` / symlink escapes raise ValueError)."""
    return confine(root, relpath or ".")


def is_inside_library(path: str) -> bool:
    """True if ``path`` resolves inside any configured library mount."""
    for mount in library_mounts():
        try:
            confine(mount.backend, path)
            return True
        except ValueError:
            continue
    return False
