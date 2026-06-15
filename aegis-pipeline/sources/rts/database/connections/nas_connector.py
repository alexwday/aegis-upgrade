"""NAS input source helpers."""

from __future__ import annotations

import io
import logging
from typing import Any

from ..utils.config_setup import get_nas_config, load_config

logger = logging.getLogger(__name__)

CLIENT_MACHINE_NAME = "AEGIS"

try:
    from smb.SMBConnection import SMBConnection
except ImportError:  # pragma: no cover - exercised via runtime environment
    SMBConnection = Any  # type: ignore[misc,assignment]


def _require_smb_dependency() -> None:
    """Raise a clear configuration error when pysmb is unavailable."""
    if SMBConnection is Any:
        raise RuntimeError(
            "pysmb is required for NAS access. Install it with "
            "`pip install pysmb` or use INPUT_SOURCE=local."
        )


def _nas_share() -> str:
    """Return the configured SMB share name."""
    return str(get_nas_config()["share_name"])


def _nas_full(relative: str) -> str:
    """Join the configured NAS base path with a share-relative path."""
    base = str(get_nas_config()["base_path"]).strip("/")
    relative_path = str(relative).strip("/")
    if base and relative_path:
        return f"{base}/{relative_path}"
    return base or relative_path


def get_nas_connection() -> SMBConnection:
    """Create a live SMB connection using database/.env NAS settings.

    Raises RuntimeError when required NAS configuration is missing and
    ConnectionError when the SMB connection attempt fails.
    """
    load_config()
    _require_smb_dependency()

    config = get_nas_config()
    required = ("username", "password", "server_ip", "server_name", "share_name")
    missing = [name for name in required if not config.get(name)]
    if missing:
        raise RuntimeError(f"Missing NAS configuration: {', '.join(missing)}")

    conn = SMBConnection(
        config["username"],
        config["password"],
        CLIENT_MACHINE_NAME,
        config["server_name"],
        use_ntlm_v2=True,
        is_direct_tcp=True,
    )
    if not conn.connect(config["server_ip"], int(config["port"])):
        raise ConnectionError("NAS connection failed")
    return conn


def nas_list_files(conn: SMBConnection, path: str) -> list[Any]:
    """List entries in a NAS directory, returning an empty list on failure."""
    try:
        return conn.listPath(_nas_share(), _nas_full(path))
    except Exception as exc:
        logger.warning("Failed to list NAS directory path=%s error=%s", path, exc)
        return []


def nas_download_file(conn: SMBConnection, path: str) -> bytes | None:
    """Download a NAS file into memory, returning None on failure."""
    try:
        buf = io.BytesIO()
        conn.retrieveFile(_nas_share(), _nas_full(path), buf)
        return buf.getvalue()
    except Exception as exc:
        logger.warning("Failed to download NAS file path=%s error=%s", path, exc)
        return None
