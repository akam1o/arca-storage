"""Custom exceptions for ARCA Storage Cinder Driver."""


class ArcaStorageException(Exception):
    """Base exception for ARCA Storage driver errors."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class ArcaAPIConnectionError(ArcaStorageException):
    """Failed to connect to ARCA Storage API."""

    pass


class ArcaAPITimeout(ArcaStorageException):
    """API request timed out."""

    pass


class ArcaAPIError(ArcaStorageException):
    """API returned an error response."""

    def __init__(self, message: str, status_code: int = None, response_data: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class ArcaVolumeNotFound(ArcaStorageException):
    """Volume not found."""

    pass


class ArcaVolumeAlreadyExists(ArcaStorageException):
    """Volume already exists."""

    pass


class ArcaSVMNotFound(ArcaStorageException):
    """SVM not found."""

    pass


class ArcaExportError(ArcaStorageException):
    """Error managing NFS exports."""

    pass


class ArcaSnapshotNotFound(ArcaStorageException):
    """Snapshot not found."""

    pass


class ArcaSnapshotAlreadyExists(ArcaStorageException):
    """Snapshot already exists."""

    pass
