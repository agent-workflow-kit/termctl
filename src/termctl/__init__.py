"""termctl."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The release job owns the version in pyproject.toml; read it from the installed metadata.
    __version__ = version("termctl")
except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled tree
    __version__ = "0.0.0"
