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


class ArcaNeutronError(ArcaManilaException):
    """Neutron API error.

    Base exception for Neutron-related errors when using neutron mode.
    """

    message = "Neutron error: %(details)s"


class ArcaNeutronPortCreationFailed(ArcaNeutronError):
    """Failed to create Neutron port."""

    message = "Failed to create Neutron port: %(details)s"


class ArcaNeutronAuthenticationError(ArcaNeutronError):
    """Neutron authentication error.

    Raised when [neutron] section is not configured or authentication fails.
    """

    message = "Neutron authentication error: %(details)s"


class ArcaNeutronNetworkNotFound(ArcaNeutronError):
    """Neutron network not found."""

    message = "Neutron network %(network_id)s not found"


class ArcaNeutronInvalidNetworkType(ArcaNeutronError):
    """Neutron network has invalid type.

    Raised when network is not a VLAN provider network (e.g., VXLAN, Geneve).
    """

    message = "Invalid network type %(network_type)s for network %(network_id)s. Only VLAN provider networks are supported."


class ArcaNetworkPoolExhausted(ArcaManilaException):
    """All network pools are exhausted.

    This is a non-retryable error indicating that all configured IP pools
    have been fully allocated. Retry will not help - operator intervention
    is required to either:
    1. Add more IP pools to configuration
    2. Delete unused SVMs to free IPs
    3. Increase pool size ranges
    """

    message = "All network pools exhausted: %(details)s"


class ArcaNetworkConfigurationError(ArcaManilaException):
    """Network configuration error.

    This is a non-retryable error indicating invalid network configuration
    (e.g., invalid CIDR, invalid VLAN ID, gateway in allocatable range).
    Retry will not help - operator must fix the configuration.
    """

    message = "Network configuration error: %(details)s"
