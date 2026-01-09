"""Network allocators for ARCA Storage Manila driver.

This package provides network allocation strategies for per_project SVM mode:
- StandaloneAllocator: Static IP pool allocation (existing behavior)
- NeutronAllocator: Neutron port-based allocation (new)
"""

from .base import NetworkAllocator, NetworkAllocation

__all__ = ["NetworkAllocator", "NetworkAllocation"]
