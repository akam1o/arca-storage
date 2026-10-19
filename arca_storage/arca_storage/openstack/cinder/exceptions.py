"""Custom exceptions for ARCA Storage Cinder Driver."""

from typing import Any


class ArcaStorageException(Exception):
    """Base exception for ARCA Storage driver errors."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class ArcaAPIConnectionError(ArcaStorageException):
    """Failed to connect to ARCA Storage API."""


class ArcaAPITimeout(ArcaStorageException):
    """API request timed out."""


class ArcaAPIError(ArcaStorageException):
    """API returned an error response."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_data: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class ArcaVolumeNotFound(ArcaStorageException):
    """Volume not found."""


class ArcaVolumeAlreadyExists(ArcaStorageException):
    """Volume already exists."""


class ArcaSVMNotFound(ArcaStorageException):
    """SVM not found."""


class ArcaExportError(ArcaStorageException):
    """Error managing NFS exports."""


class ArcaSnapshotNotFound(ArcaStorageException):
    """Snapshot not found."""


class ArcaSnapshotAlreadyExists(ArcaStorageException):
    """Snapshot already exists."""
