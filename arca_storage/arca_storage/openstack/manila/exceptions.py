"""ARCA Storage Manila Driver Exceptions."""


class ArcaManilaException(Exception):
    """Base exception for Manila driver errors."""

    message = "An unknown exception occurred."

    def __init__(self, message=None, **kwargs):
        """Initialize exception with optional custom message."""
        self.kwargs = kwargs
        if message:
            self.message = message
        super(ArcaManilaException, self).__init__(self.message % kwargs)


class ArcaManilaAPIError(ArcaManilaException):
    """API communication errors."""

    message = "API error occurred: %(details)s"


class ArcaResourceNotFound(ArcaManilaException):
    """Generic resource not found error.

    This is a base class for more specific "not found" exceptions.
    Use specific subclasses (ArcaShareNotFound, ArcaSVMNotFound, etc.)
    when the resource type is known.
    """

    message = "Resource %(resource_id)s not found"


class ArcaShareNotFound(ArcaResourceNotFound):
    """Share not found in backend."""

    message = "Share %(share_id)s not found"


class ArcaResourceAlreadyExists(ArcaManilaException):
    """Generic resource already exists error.

    This is a base class for more specific "already exists" exceptions.
    Use specific subclasses (ArcaShareAlreadyExists, ArcaSVMAlreadyExists)
    when the resource type is known.
    """

    message = "Resource %(resource_id)s already exists"


class ArcaShareAlreadyExists(ArcaResourceAlreadyExists):
    """Share already exists."""

    message = "Share %(share_id)s already exists"


class ArcaAccessRuleError(ArcaManilaException):
    """Access rule management error."""

    message = "Access rule error: %(details)s"


class ArcaSnapshotNotFound(ArcaManilaException):
    """Snapshot not found."""

    message = "Snapshot %(snapshot_id)s not found"


class ArcaSVMNotFound(ArcaResourceNotFound):
    """SVM not found."""

    message = "SVM %(svm_name)s not found"


class ArcaSVMAlreadyExists(ArcaResourceAlreadyExists):
    """SVM already exists."""

    message = "SVM %(svm_name)s already exists"


class ArcaAPIConnectionError(ArcaManilaException):
    """API connection error."""

    message = "Failed to connect to ARCA API: %(details)s"


class ArcaAPITimeout(ArcaManilaException):
    """API timeout error."""

    message = "ARCA API request timed out after %(timeout)s seconds"


class ArcaNetworkConflict(ArcaManilaException):
    """Network conflict error (IP address already in use).

    Note: VLAN reuse across pools is allowed, so this only indicates IP conflicts.
    """

    message = "Network conflict: %(details)s"
