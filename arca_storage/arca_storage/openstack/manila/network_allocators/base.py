"""Base class for network allocators."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class NetworkAllocation:
    """Network allocation result.

    Attributes:
        vlan_id: VLAN ID for the SVM network interface
        ip_cidr: IP address with prefix length (e.g., "192.168.100.10/24")
        gateway: Gateway IP address
        allocation_id: Optional allocation ID for tracking (e.g., Neutron Port ID)
    """

    vlan_id: int
    ip_cidr: str
    gateway: str
    allocation_id: Optional[str] = None


class NetworkAllocator(ABC):
    """Abstract base class for network allocators.

    Network allocators provide IP/VLAN allocation strategies for per_project SVM creation.
    """

    @abstractmethod
    def allocate(
        self, project_id: str, svm_name: str, retry_attempt: int = 0
    ) -> NetworkAllocation:
        """Allocate network resources for SVM.

        Args:
            project_id: OpenStack project ID
            svm_name: SVM name (for tracking/labeling)
            retry_attempt: Retry attempt number (0 for first attempt)

        Returns:
            NetworkAllocation with VLAN/IP/gateway information

        Raises:
            ArcaNetworkConflict: Network allocation failed (retryable)
            ArcaManilaException: Other allocation errors
        """
        pass

    @abstractmethod
    def deallocate(self, allocation_id: str) -> None:
        """Deallocate network resources.

        Args:
            allocation_id: Allocation ID returned by allocate()

        Note:
            This method should be idempotent and not raise exceptions
            for missing resources (best-effort cleanup).
        """
        pass

    @abstractmethod
    def validate_config(self) -> None:
        """Validate allocator configuration.

        Called during driver setup (do_setup) to verify configuration
        before the driver becomes active.

        Raises:
            ValueError: Invalid configuration
            Exception: Validation failure (e.g., network not found)
        """
        pass
