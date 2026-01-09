"""ARCA Storage Manila Share Driver.

This driver provides OpenStack Manila integration for ARCA Storage
using NFS as the protocol for shared filesystem access.
"""

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from oslo_log import log as logging
    _HAS_OSLO_LOG = True
except ImportError:
    import logging
    _HAS_OSLO_LOG = False

try:
    from oslo_concurrency import lockutils
    _HAS_OSLO_CONCURRENCY = True
except ImportError:
    # oslo_concurrency is optional - degrades to thread-local locking
    _HAS_OSLO_CONCURRENCY = False
    lockutils = None

try:
    from manila import exception as manila_exception
    from manila.share import driver as manila_driver
    _HAS_MANILA = True
except ImportError:
    # Manila is optional for standalone development
    _HAS_MANILA = False

    class _ManilaBaseException(Exception):
        pass

    class _ManilaExceptionModule:
        class ManilaException(_ManilaBaseException):
            pass

        class ShareBackendException(_ManilaBaseException):
            pass

        class ShareExists(_ManilaBaseException):
            def __init__(self, *args, **kwargs):
                super().__init__(*args)

        class ShareResourceNotFound(_ManilaBaseException):
            def __init__(self, *args, **kwargs):
                super().__init__(*args)

        class InvalidShareAccess(_ManilaBaseException):
            def __init__(self, reason=None, *args, **kwargs):
                super().__init__(reason or "Invalid share access")

    manila_exception = _ManilaExceptionModule()

    # Create a dummy base class for development without Manila
    class manila_driver:  # noqa: F811
        class ShareDriver:
            """Dummy ShareDriver for development without Manila."""
            def __init__(self, *args, **kwargs):
                pass

from . import client as arca_client
from . import configuration as arca_config
from . import exceptions as arca_exceptions

LOG = logging.getLogger(__name__)

VERSION = "1.0.0"


class ArcaStorageManilaDriver(manila_driver.ShareDriver):
    """ARCA Storage Manila share driver.

    This driver integrates ARCA Storage as a Manila backend using NFS protocol.
    It leverages ARCA Storage's REST API for share, snapshot, export, and
    SVM management operations.

    Architecture:
        - Each Manila share = ARCA Volume (XFS filesystem on LVM thin volume)
        - Export path format: {vip}:/exports/{svm}/{volume_name}
        - Snapshots use ARCA backend LVM thin snapshots
        - Access rules managed via NFS Ganesha export ACLs

    Version history:
        1.0.0 - Initial implementation
    """

    # Driver identification
    VERSION = VERSION

    def __init__(self, *args, **kwargs):
        """Initialize the ARCA Storage Manila driver.

        Args:
            *args: Positional arguments
            **kwargs: Keyword arguments including 'configuration'
        """
        # Call parent ShareDriver constructor
        super(ArcaStorageManilaDriver, self).__init__(*args, **kwargs)

        # Manila's real ShareDriver sets self.configuration; our dummy base class does not.
        # Allow tests/standalone usage to inject configuration after initialization.
        if not hasattr(self, "configuration"):
            self.configuration = kwargs.get("configuration")

        # Register ARCA Storage configuration options
        if self.configuration:
            self.configuration.append_config_values(arca_config.get_arca_manila_opts())

        # ARCA Storage API client (initialized in do_setup)
        self.arca_client: Optional[arca_client.ArcaManilaClient] = None

        # Cache for SVM information
        self._svm_cache: Dict[str, Dict[str, Any]] = {}

        # Driver stats cache
        self._stats: Dict[str, Any] = {}

        # Effective strategy after validating/normalizing configuration in do_setup
        self._svm_strategy_effective: Optional[str] = None

        # Per-project SVM allocation tracking
        # Format: {project_id: {"svm_name": str, "vlan_id": int, "ip": str}}
        self._per_project_svm_cache: Dict[str, Dict[str, Any]] = {}

        # Parsed IP/VLAN pools for per_project strategy
        # Format: [{"ip_network": ipaddress.IPv4Network, "vlan_start": int, "gateway": str}, ...]
        self._ip_vlan_pools: List[Dict[str, Any]] = []

        # Round-robin counter for pool selection
        self._pool_allocation_counter: int = 0

        # Thread lock for per-project SVM allocation (protects counter and cache)
        # NOTE: This only protects against thread-level races within a single process
        # Multi-process races (multiple manila-share workers) require backend-level conflict detection
        self._allocation_lock = threading.Lock()

        # Pool stats cache for per_project/manual strategies
        # Format: (stats_dict, timestamp)
        self._pool_stats_cache: Optional[Tuple[Dict[str, Any], float]] = None
        self._pool_stats_cache_ttl: int = 300  # 5 minutes TTL
        self._pool_stats_lock = threading.Lock()

    @property
    def driver_handles_share_servers(self):
        """Driver does not manage share servers.

        Returns:
            False: This driver uses existing ARCA SVMs
        """
        return False

    def do_setup(self, context):
        """Perform driver setup and validation.

        This method is called once during driver initialization.

        Args:
            context: Manila context

        Raises:
            manila.exception.ManilaException: If setup fails
        """
        LOG.info("Initializing ARCA Storage Manila driver version %s", self.VERSION)

        try:
            # Validate API configuration
            if not self.configuration.arca_storage_use_api:
                raise manila_exception.ManilaException(
                    "arca_storage_use_api must be True for Manila driver"
                )

            if not self.configuration.arca_storage_api_endpoint:
                raise manila_exception.ManilaException(
                    "arca_storage_api_endpoint must be set"
                )

            # Initialize ARCA Storage API client
            self.arca_client = arca_client.ArcaManilaClient(
                api_endpoint=self.configuration.arca_storage_api_endpoint,
                timeout=self.configuration.arca_storage_api_timeout,
                retry_count=self.configuration.arca_storage_api_retry_count,
                verify_ssl=self.configuration.arca_storage_verify_ssl,
                auth_type=self.configuration.arca_storage_api_auth_type,
                api_token=self.configuration.arca_storage_api_token,
                username=self.configuration.arca_storage_api_username,
                password=self.configuration.arca_storage_api_password,
                ca_bundle=self.configuration.arca_storage_api_ca_bundle,
                client_cert=self.configuration.arca_storage_api_client_cert,
                client_key=self.configuration.arca_storage_api_client_key,
            )

            # Validate SVM configuration
            configured_strategy = self.configuration.arca_storage_svm_strategy
            svm_strategy = configured_strategy

            if configured_strategy == "per_project":
                # Initialize network allocator based on mode
                network_mode = self.configuration.arca_storage_network_plugin_mode

                if network_mode == "standalone":
                    # Validate standalone mode requires pool configuration
                    ip_pools_config = self.configuration.arca_storage_per_project_ip_pools

                    if not ip_pools_config:
                        raise manila_exception.ManilaException(
                            "per_project strategy with standalone mode requires "
                            "arca_storage_per_project_ip_pools configuration. Example: "
                            "arca_storage_per_project_ip_pools = 192.168.100.0/24|192.168.100.10-192.168.100.200:100"
                        )

                    from .network_allocators.standalone import StandaloneAllocator
                    self._network_allocator = StandaloneAllocator(
                        self.configuration,
                        self.arca_client,
                        self._allocation_lock,
                        self._pool_allocation_counter,
                    )

                elif network_mode == "neutron":
                    from .network_allocators.neutron import NeutronAllocator
                    self._network_allocator = NeutronAllocator(self.configuration)

                else:
                    raise manila_exception.ManilaException(
                        f"Invalid network_plugin_mode: {network_mode}. "
                        f"Valid options: standalone, neutron"
                    )

                # Validate allocator configuration
                self._network_allocator.validate_config()

                LOG.info(
                    "Using per_project SVM strategy with %s network allocator",
                    network_mode
                )

                svm_strategy = "per_project"

            self._svm_strategy_effective = svm_strategy

            if svm_strategy == "shared":
                # Validate default SVM exists
                default_svm = self.configuration.arca_storage_default_svm
                try:
                    self.arca_client.get_svm(default_svm)
                    LOG.info("Using shared SVM strategy with SVM: %s", default_svm)
                except arca_exceptions.ArcaSVMNotFound:
                    raise manila_exception.ManilaException(
                        f"Default SVM '{default_svm}' not found"
                    )
            elif svm_strategy == "manual":
                LOG.info("Using manual SVM strategy (SVMs specified via share types)")
            elif svm_strategy == "per_project":
                LOG.info("Using per_project SVM strategy (auto-create SVMs per project)")
            else:
                raise manila_exception.ManilaException(
                    f"Invalid SVM strategy: {svm_strategy}"
                )

            LOG.info("ARCA Storage Manila driver initialized successfully")

        except Exception as e:
            LOG.exception("Failed to initialize ARCA Storage Manila driver: %s", e)
            raise manila_exception.ManilaException(
                f"Driver initialization failed: {str(e)}"
            )

    def check_for_setup_error(self):
        """Check for setup errors.

        Raises:
            manila.exception.ManilaException: If configuration is invalid
        """
        # Validate configuration
        if not self.arca_client:
            raise manila_exception.ManilaException(
                "ARCA Storage API client not initialized"
            )

        # Test API connectivity
        try:
            self.arca_client.list_svms()
            LOG.debug("API connectivity test passed")
        except Exception as e:
            raise manila_exception.ManilaException(
                f"Failed to connect to ARCA Storage API: {str(e)}"
            )

    def _update_share_stats(self, data=None):
        """Update share statistics for Manila scheduler.

        Args:
            data: Optional dict to update (if None, creates new dict)

        Returns:
            Updated statistics dict
        """
        if data is None:
            data = {}

        # Basic driver information
        data["share_backend_name"] = self.configuration.share_backend_name
        data["vendor_name"] = "ARCA Storage"
        data["driver_version"] = self.VERSION
        data["storage_protocol"] = "NFS"

        # Driver capabilities (Manila 2025.1 Epoxy)
        data["snapshot_support"] = self.configuration.arca_storage_snapshot_support
        data["create_share_from_snapshot_support"] = (
            self.configuration.arca_storage_create_share_from_snapshot_support
        )
        data["revert_to_snapshot_support"] = (
            self.configuration.arca_storage_revert_to_snapshot_support
        )
        data["mount_snapshot_support"] = (
            self.configuration.arca_storage_mount_snapshot_support
        )

        # Pool reporting (required for production Manila scheduler)
        pools = []

        strategy = self._svm_strategy_effective or self.configuration.arca_storage_svm_strategy

        try:
            if strategy == "shared":
                # Single pool for shared SVM
                svm_name = self.configuration.arca_storage_default_svm
                pool = self._get_pool_stats(svm_name)
                pool.update(self._get_pool_capabilities())
                pools.append(pool)
            elif strategy == "per_project":
                # For per_project strategy, report a single aggregated pool
                # to avoid O(N projects) overhead in scheduler path
                # Individual SVM capacities are created on-demand
                pool = self._get_per_project_aggregate_pool_stats()
                pool.update(self._get_pool_capabilities())
                pools.append(pool)
            elif strategy == "manual":
                # For manual strategy, report a single "manual" pool
                # Actual SVM selection is done via share_type extra_specs
                # This avoids scheduler/placement mismatch where Manila schedules
                # to backend@poolX but driver uses extra_specs to select SVM
                pool = self._get_manual_aggregate_pool_stats()
                pool.update(self._get_pool_capabilities())
                pools.append(pool)
            else:
                # Fallback for unknown strategy
                LOG.warning("Unknown SVM strategy: %s", strategy)
                pool = self._get_unknown_pool()
                pool.update(self._get_pool_capabilities())
                pools.append(pool)

        except Exception as e:
            LOG.warning("Failed to get pool stats: %s", e)
            # Fallback: single pool with unknown capacity
            pool = self._get_unknown_pool()
            pool.update(self._get_pool_capabilities())
            pools.append(pool)

        data["pools"] = pools
        self._stats = data

        return data

    def _get_pool_capabilities(self) -> Dict[str, Any]:
        """Return per-pool capability flags for Manila scheduler.

        Manila scheduler primarily consults pool-level capabilities, so keep these
        flags on each pool dict in addition to top-level stats.
        """
        return {
            "snapshot_support": self.configuration.arca_storage_snapshot_support,
            "create_share_from_snapshot_support": (
                self.configuration.arca_storage_create_share_from_snapshot_support
            ),
            "revert_to_snapshot_support": (
                self.configuration.arca_storage_revert_to_snapshot_support
            ),
            "mount_snapshot_support": (
                self.configuration.arca_storage_mount_snapshot_support
            ),
        }

    def _get_metadata_value(self, obj: Any, key: str) -> Optional[str]:
        """Best-effort metadata lookup for Manila dict-like objects.

        Manila may pass share/snapshot objects with different metadata keys
        depending on the call path (e.g., 'metadata', 'share_metadata',
        'snapshot_metadata').
        """
        if not obj or not hasattr(obj, "get"):
            return None

        for container_key in ("metadata", "share_metadata", "snapshot_metadata"):
            container = obj.get(container_key)
            if isinstance(container, dict):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _set_metadata_value(self, obj: Any, key: str, value: str) -> None:
        """Best-effort set metadata on dict-like Manila objects (in-memory)."""
        if not obj or not hasattr(obj, "get"):
            return

        for container_key in ("metadata", "share_metadata", "snapshot_metadata"):
            container = obj.get(container_key)
            if isinstance(container, dict):
                container[key] = value
                return

        # Default to 'metadata' if no known container exists.
        obj.setdefault("metadata", {})[key] = value

    def _persist_share_metadata(self, context, share: Any, metadata: Dict[str, str]) -> None:
        """Persist share metadata via Manila if available; otherwise best-effort.

        This method always updates the passed object in-memory (useful for
        immediate follow-up operations), and attempts to call Manila helper
        methods when present.
        """
        try:
            for k, v in metadata.items():
                self._set_metadata_value(share, k, v)

            if hasattr(self, "_update_share_metadata"):
                try:
                    self._update_share_metadata(context, share, metadata)  # type: ignore[misc]
                    return
                except TypeError:
                    # Alternate signature
                    self._update_share_metadata(context, share.get("id"), metadata)  # type: ignore[misc]
                    return
        except Exception as e:
            LOG.debug("Failed to persist share metadata (best-effort): %s", e)

    def _persist_snapshot_metadata(self, context, snapshot: Any, metadata: Dict[str, str]) -> None:
        """Persist snapshot metadata via Manila if available; otherwise best-effort."""
        try:
            for k, v in metadata.items():
                self._set_metadata_value(snapshot, k, v)

            if hasattr(self, "_update_snapshot_metadata"):
                try:
                    self._update_snapshot_metadata(context, snapshot, metadata)  # type: ignore[misc]
                    return
                except TypeError:
                    self._update_snapshot_metadata(context, snapshot.get("id"), metadata)  # type: ignore[misc]
                    return
        except Exception as e:
            LOG.debug("Failed to persist snapshot metadata (best-effort): %s", e)

    def _get_pool_stats(self, svm_name):
        """Get capacity statistics for a pool (SVM).

        Args:
            svm_name: SVM name

        Returns:
            Pool statistics dict
        """
        pool = {
            "pool_name": svm_name,
            "qos": True,
            "reserved_percentage": self.configuration.arca_storage_reserved_percentage,
            "reserved_snapshot_percentage": (
                self.configuration.arca_storage_reserved_share_from_snapshot_percentage
            ),
            "reserved_share_extend_percentage": (
                self.configuration.arca_storage_reserved_share_percentage
            ),
            "dedupe": False,
            "compression": False,
            "thin_provisioning": True,
            "max_over_subscription_ratio": (
                self.configuration.arca_storage_max_over_subscription_ratio
            ),
        }

        # Query capacity from ARCA API
        try:
            capacity = self.arca_client.get_svm_capacity(svm_name)
            pool["total_capacity_gb"] = float(capacity["total_gb"])
            pool["free_capacity_gb"] = float(capacity["free_gb"])
            pool["provisioned_capacity_gb"] = float(capacity.get("provisioned_gb", 0))
        except Exception as e:
            LOG.warning("Failed to get capacity for SVM %s: %s", svm_name, e)
            # Fallback to unknown (Manila 2025.1 accepts 'unknown' string)
            pool["total_capacity_gb"] = "unknown"
            pool["free_capacity_gb"] = "unknown"

        return pool

    def _get_unknown_pool(self):
        """Get fallback pool with unknown capacity.

        Returns:
            Pool statistics dict with unknown capacity
        """
        return {
            "pool_name": "unknown",
            "qos": False,
            "reserved_percentage": 0,
            "reserved_snapshot_percentage": 0,
            "reserved_share_extend_percentage": 0,
            "dedupe": False,
            "compression": False,
            "thin_provisioning": False,
            "max_over_subscription_ratio": 1.0,
            "total_capacity_gb": "unknown",
            "free_capacity_gb": "unknown",
        }

    def _get_per_project_aggregate_pool_stats(self):
        """Get aggregated pool statistics for per_project strategy.

        This reports a single logical pool representing the aggregate capacity
        of all per-project SVMs, with caching to avoid O(N projects) overhead
        on every scheduler cycle.

        Returns:
            Pool statistics dict
        """
        # Check cache first
        with self._pool_stats_lock:
            now = time.time()
            if self._pool_stats_cache is not None:
                cached_stats, cache_time = self._pool_stats_cache
                if now - cache_time < self._pool_stats_cache_ttl:
                    LOG.debug("Using cached per_project pool stats (age: %.1fs)", now - cache_time)
                    return cached_stats.copy()

        # Cache miss or expired - recompute stats
        pool = {
            "pool_name": "per_project_aggregate",
            "qos": True,
            "reserved_percentage": self.configuration.arca_storage_reserved_percentage,
            "reserved_snapshot_percentage": (
                self.configuration.arca_storage_reserved_share_from_snapshot_percentage
            ),
            "reserved_share_extend_percentage": (
                self.configuration.arca_storage_reserved_share_percentage
            ),
            "dedupe": False,
            "compression": False,
            "thin_provisioning": True,
            "max_over_subscription_ratio": (
                self.configuration.arca_storage_max_over_subscription_ratio
            ),
        }

        # Try to aggregate capacity from existing per-project SVMs
        # This is best-effort; if it fails, report unknown capacity
        try:
            prefix = self.configuration.arca_storage_svm_prefix
            svms = self.arca_client.list_svms()
            per_project_svms = [svm for svm in svms if svm["name"].startswith(prefix)]

            if per_project_svms:
                # Aggregate capacity from existing SVMs
                total_capacity = 0.0
                free_capacity = 0.0
                provisioned_capacity = 0.0

                for svm in per_project_svms:
                    try:
                        capacity = self.arca_client.get_svm_capacity(svm["name"])
                        total_capacity += float(capacity.get("total_gb", 0))
                        free_capacity += float(capacity.get("free_gb", 0))
                        provisioned_capacity += float(capacity.get("provisioned_gb", 0))
                    except Exception as e:
                        LOG.warning(
                            "Failed to get capacity for SVM %s: %s", svm["name"], e
                        )
                        continue

                pool["total_capacity_gb"] = total_capacity
                pool["free_capacity_gb"] = free_capacity
                pool["provisioned_capacity_gb"] = provisioned_capacity
            else:
                # No existing SVMs, report unknown capacity
                # Manila scheduler will still allow placement (infinite capacity)
                pool["total_capacity_gb"] = "unknown"
                pool["free_capacity_gb"] = "unknown"

        except Exception as e:
            LOG.warning("Failed to aggregate per-project pool stats: %s", e)
            pool["total_capacity_gb"] = "unknown"
            pool["free_capacity_gb"] = "unknown"

        # Update cache
        with self._pool_stats_lock:
            self._pool_stats_cache = (pool.copy(), time.time())
            LOG.debug("Updated per_project pool stats cache")

        return pool

    def _get_manual_aggregate_pool_stats(self):
        """Get aggregated pool statistics for manual strategy.

        For manual strategy, we report a single logical pool since actual
        SVM selection is done via share_type extra_specs, not via pool selection.
        This prevents scheduler/placement mismatch. Uses caching to avoid
        O(N SVMs) overhead on every scheduler cycle.

        Returns:
            Pool statistics dict
        """
        # Check cache first (shares same cache with per_project for simplicity)
        with self._pool_stats_lock:
            now = time.time()
            if self._pool_stats_cache is not None:
                cached_stats, cache_time = self._pool_stats_cache
                if now - cache_time < self._pool_stats_cache_ttl:
                    LOG.debug("Using cached manual pool stats (age: %.1fs)", now - cache_time)
                    # Return cached stats but with manual pool name
                    manual_stats = cached_stats.copy()
                    manual_stats["pool_name"] = "manual"
                    return manual_stats

        # Cache miss or expired - recompute stats
        pool = {
            "pool_name": "manual",
            "qos": True,
            "reserved_percentage": self.configuration.arca_storage_reserved_percentage,
            "reserved_snapshot_percentage": (
                self.configuration.arca_storage_reserved_share_from_snapshot_percentage
            ),
            "reserved_share_extend_percentage": (
                self.configuration.arca_storage_reserved_share_percentage
            ),
            "dedupe": False,
            "compression": False,
            "thin_provisioning": False,
            "max_over_subscription_ratio": 1.0,
        }

        # Try to aggregate capacity from all SVMs
        # This is best-effort; if it fails, report unknown capacity
        try:
            svms = self.arca_client.list_svms()

            if svms:
                # Aggregate capacity from all SVMs
                total_capacity = 0.0
                free_capacity = 0.0
                provisioned_capacity = 0.0

                for svm in svms:
                    try:
                        capacity = self.arca_client.get_svm_capacity(svm["name"])
                        total_capacity += float(capacity.get("total_gb", 0))
                        free_capacity += float(capacity.get("free_gb", 0))
                        provisioned_capacity += float(capacity.get("provisioned_gb", 0))
                    except Exception as e:
                        LOG.warning(
                            "Failed to get capacity for SVM %s: %s", svm["name"], e
                        )
                        continue

                pool["total_capacity_gb"] = total_capacity
                pool["free_capacity_gb"] = free_capacity
                pool["provisioned_capacity_gb"] = provisioned_capacity
            else:
                # No existing SVMs, report unknown capacity
                pool["total_capacity_gb"] = "unknown"
                pool["free_capacity_gb"] = "unknown"

        except Exception as e:
            LOG.warning("Failed to aggregate manual pool stats: %s", e)
            pool["total_capacity_gb"] = "unknown"
            pool["free_capacity_gb"] = "unknown"

        # Update cache
        with self._pool_stats_lock:
            self._pool_stats_cache = (pool.copy(), time.time())
            LOG.debug("Updated manual pool stats cache")

        return pool

    def _get_svm_for_share(self, share, ensure_exists=False):
        """Determine SVM for a share based on strategy.

        Args:
            share: Manila share object
            ensure_exists: If True, create SVM if it doesn't exist (per_project only).
                          If False, only return SVM name without creation.

        Returns:
            SVM name string

        Raises:
            manila.exception.ManilaException: If SVM cannot be determined
        """
        strategy = self._svm_strategy_effective or self.configuration.arca_storage_svm_strategy

        # Prefer stored metadata if present (enables snapshot ops without embedded share object).
        svm_from_metadata = self._get_metadata_value(share, "arca_svm_name")
        if svm_from_metadata:
            return svm_from_metadata

        if strategy == "shared":
            # Use default SVM for all shares
            return self.configuration.arca_storage_default_svm

        elif strategy == "manual":
            # Extract SVM from share type extra_specs
            share_type = share.get("share_type")
            if not share_type:
                raise manila_exception.ManilaException(
                    "Share type required for manual SVM strategy"
                )

            extra_specs = share_type.get("extra_specs", {})
            svm_name = extra_specs.get("arca_manila:svm_name")
            if isinstance(svm_name, str):
                svm_name = svm_name.strip()

            if not svm_name:
                raise manila_exception.ManilaException(
                    "Share type must specify 'arca_manila:svm_name' for manual SVM strategy"
                )

            return svm_name

        elif strategy == "per_project":
            # Each project gets dedicated SVM
            project_id = share.get("project_id")
            if not project_id:
                raise manila_exception.ManilaException(
                    "Cannot determine project_id for per_project SVM strategy. "
                    "This may occur when snapshot operations receive incomplete share info "
                    "from Manila API. Ensure Manila is configured to include parent share "
                    "details in snapshot objects."
                )

            if ensure_exists:
                # Create SVM if it doesn't exist (for create operations)
                return self._allocate_per_project_svm(project_id)
            else:
                # Just return SVM name without creating (for other operations)
                svm_name = f"{self.configuration.arca_storage_svm_prefix}{project_id}"
                return svm_name

        else:
            raise manila_exception.ManilaException(
                f"Invalid SVM strategy: {strategy}"
            )

    def _get_svm_info(self, svm_name):
        """Get SVM information with caching.

        Args:
            svm_name: SVM name

        Returns:
            SVM info dict

        Raises:
            arca_exceptions.ArcaSVMNotFound: If SVM not found
        """
        # Check cache first
        if svm_name in self._svm_cache:
            return self._svm_cache[svm_name]

        # Query from API
        svm_info = self.arca_client.get_svm(svm_name)
        self._svm_cache[svm_name] = svm_info

        return svm_info

    def _allocate_per_project_svm(self, project_id: str) -> str:
        """Allocate or get existing SVM for a project.

        This method implements automatic SVM creation for per_project strategy.
        It uses sequential VLAN/IP allocation from configured pools.

        Thread-safe for concurrent requests within a single process.
        When oslo.concurrency is available, uses distributed locking for
        multi-process coordination. Otherwise, falls back to thread-local locking
        with backend conflict detection and retries.

        Args:
            project_id: OpenStack project ID

        Returns:
            SVM name string

        Raises:
            manila.exception.ShareBackendException: If SVM allocation fails
        """
        # Check cache first (outside lock for fast path)
        if project_id in self._per_project_svm_cache:
            return self._per_project_svm_cache[project_id]["svm_name"]

        # Generate lock name for distributed locking
        lock_name = f"arca-manila-svm-alloc-{project_id}"

        # Use distributed lock if available, otherwise fall back to thread lock
        if _HAS_OSLO_CONCURRENCY and lockutils:
            @lockutils.synchronized(lock_name, external=True)
            def _allocate_with_distributed_lock():
                return self._allocate_per_project_svm_impl(project_id)
            return _allocate_with_distributed_lock()
        else:
            # Fall back to thread-local lock
            with self._allocation_lock:
                return self._allocate_per_project_svm_impl(project_id)

    def _allocate_per_project_svm_impl(self, project_id: str) -> str:
        """Implementation of per-project SVM allocation (lock-protected).

        This is the actual implementation called by _allocate_per_project_svm
        after acquiring appropriate locks.

        Args:
            project_id: OpenStack project ID

        Returns:
            SVM name string

        Raises:
            manila.exception.ShareBackendException: If SVM allocation fails
        """
        # Double-check cache after acquiring lock (another thread may have allocated)
        if project_id in self._per_project_svm_cache:
            return self._per_project_svm_cache[project_id]["svm_name"]

        # Generate SVM name
        svm_name = f"{self.configuration.arca_storage_svm_prefix}{project_id}"

        # Check if SVM already exists
        try:
            svm_info = self.arca_client.get_svm(svm_name)
            LOG.info("Found existing SVM %s for project %s", svm_name, project_id)

            # Cache the existing SVM
            self._per_project_svm_cache[project_id] = {
                "svm_name": svm_name,
                "vlan_id": svm_info.get("vlan_id"),
                "ip": svm_info.get("vip"),
            }
            return svm_name

        except arca_exceptions.ArcaSVMNotFound:
            # SVM doesn't exist, create it
            LOG.info("Creating new SVM %s for project %s", svm_name, project_id)

            # Retry loop for network conflicts (multi-process race conditions)
            max_retries = 3
            allocation = None

            for attempt in range(max_retries):
                try:
                    # Allocate network using NetworkAllocator plugin
                    allocation = self._network_allocator.allocate(
                        project_id=project_id,
                        svm_name=svm_name,
                        retry_attempt=attempt,
                    )

                    # Create SVM via API
                    svm_info = self.arca_client.create_svm(
                        name=svm_name,
                        vlan_id=allocation.vlan_id,
                        ip_cidr=allocation.ip_cidr,
                        gateway=allocation.gateway,
                        mtu=self.configuration.arca_storage_per_project_mtu,
                        root_volume_size_gib=self.configuration.arca_storage_per_project_root_volume_size_gib,
                    )

                    LOG.info(
                        "Created SVM %s for project %s (VLAN: %d, IP: %s, allocation_id: %s)",
                        svm_name,
                        project_id,
                        allocation.vlan_id,
                        allocation.ip_cidr,
                        allocation.allocation_id,
                    )

                    # Cache the new SVM
                    self._per_project_svm_cache[project_id] = {
                        "svm_name": svm_name,
                        "vlan_id": allocation.vlan_id,
                        "ip": svm_info.get("vip"),
                        "allocation_id": allocation.allocation_id,
                    }
                    return svm_name

                except arca_exceptions.ArcaSVMAlreadyExists:
                    # Race condition: another process created SVM with same name
                    LOG.info("SVM %s was created by another process", svm_name)

                    # Cleanup allocated network resource if it exists
                    # (the SVM created by the other process has its own network allocation)
                    if allocation and allocation.allocation_id:
                        try:
                            self._network_allocator.deallocate(allocation.allocation_id)
                            LOG.info(
                                "Cleaned up allocation %s after concurrent SVM creation",
                                allocation.allocation_id,
                            )
                        except Exception as cleanup_error:
                            LOG.error(
                                "Failed to cleanup allocation %s: %s",
                                allocation.allocation_id,
                                cleanup_error,
                            )

                    svm_info = self.arca_client.get_svm(svm_name)
                    self._per_project_svm_cache[project_id] = {
                        "svm_name": svm_name,
                        "vlan_id": svm_info.get("vlan_id"),
                        "ip": svm_info.get("vip"),
                        "allocation_id": None,  # We don't own this allocation
                    }
                    return svm_name

                except (arca_exceptions.ArcaNetworkPoolExhausted, arca_exceptions.ArcaNetworkConfigurationError) as e:
                    # Non-retryable network error (pool exhausted or config error)
                    LOG.error(
                        "Non-retryable network error for SVM %s: %s",
                        svm_name,
                        e,
                    )

                    # Cleanup allocated network resource if it exists (though unlikely for these errors)
                    if allocation and allocation.allocation_id:
                        try:
                            self._network_allocator.deallocate(allocation.allocation_id)
                            LOG.info(
                                "Cleaned up allocation %s after non-retryable error",
                                allocation.allocation_id,
                            )
                        except Exception as cleanup_error:
                            LOG.error(
                                "Failed to cleanup allocation %s: %s",
                                allocation.allocation_id,
                                cleanup_error,
                            )

                    # Don't retry - raise immediately
                    raise manila_exception.ShareBackendException(
                        f"Failed to allocate network for SVM {svm_name}: {str(e)}"
                    )

                except arca_exceptions.ArcaNetworkConflict as e:
                    # Network conflict - cleanup allocated port if it exists and retry
                    LOG.warning(
                        "Network conflict on attempt %d/%d for SVM %s: %s",
                        attempt + 1,
                        max_retries,
                        svm_name,
                        e,
                    )

                    # Cleanup allocated network resource if it exists
                    if allocation and allocation.allocation_id:
                        try:
                            self._network_allocator.deallocate(allocation.allocation_id)
                            LOG.info(
                                "Cleaned up allocation %s after network conflict",
                                allocation.allocation_id,
                            )
                        except Exception as cleanup_error:
                            LOG.error(
                                "Failed to cleanup allocation %s: %s",
                                allocation.allocation_id,
                                cleanup_error,
                            )

                    if attempt < max_retries - 1:
                        continue  # Retry with new allocation
                    else:
                        # All retries exhausted
                        raise manila_exception.ShareBackendException(
                            f"Failed to allocate network for SVM {svm_name} "
                            f"after {max_retries} attempts: {str(e)}"
                        )

                except Exception as e:
                    # Unexpected error - cleanup and raise
                    LOG.exception("Failed to create SVM %s for project %s", svm_name, project_id)

                    # Cleanup allocated network resource if it exists
                    if allocation and allocation.allocation_id:
                        try:
                            self._network_allocator.deallocate(allocation.allocation_id)
                            LOG.info(
                                "Cleaned up allocation %s after SVM creation failure",
                                allocation.allocation_id,
                            )
                        except Exception as cleanup_error:
                            LOG.error(
                                "Failed to cleanup allocation %s: %s",
                                allocation.allocation_id,
                                cleanup_error,
                            )

                    raise manila_exception.ShareBackendException(
                        f"Failed to create SVM for project: {str(e)}"
                    )

    # Share Lifecycle Methods

    def create_share(self, context, share, share_server=None):
        """Create a share.

        Args:
            context: Manila context
            share: Manila share object
            share_server: Share server (not used)

        Returns:
            List[Dict]: Export locations in Manila 2025.1 format
                [{'path': export_path, 'is_admin_only': False, 'metadata': {}}]

        Raises:
            manila.exception.ManilaException: If creation fails
        """
        share_id = share["id"]
        size_gib = share["size"]
        volume_name = f"share-{share_id}"

        LOG.info("Creating share %s (size: %d GiB)", share_id, size_gib)

        try:
            # Determine SVM for this share (create if needed for per_project)
            svm_name = self._get_svm_for_share(share, ensure_exists=True)
            LOG.debug("Using SVM %s for share %s", svm_name, share_id)

            # Create volume via ARCA API
            volume_info = self.arca_client.create_volume(
                name=volume_name,
                svm=svm_name,
                size_gib=size_gib,
                thin=True,
                fs_type="xfs",
            )

            # Extract export path from API response (authoritative)
            export_path = volume_info.get("export_path")
            if not export_path:
                raise manila_exception.ShareBackendException(
                    f"No export_path in volume creation response for {volume_name}"
                )

            LOG.info("Created share %s with export path: %s", share_id, export_path)

            # Persist SVM mapping for later operations (best-effort).
            self._persist_share_metadata(context, share, {"arca_svm_name": svm_name})

            # Apply QoS if share type has specs (best-effort)
            self._apply_qos_to_share(share, volume_name, svm_name)

            # Return export locations in Manila 2025.1 format
            return [
                {
                    "path": export_path,
                    "is_admin_only": False,
                    "metadata": {},
                }
            ]

        except arca_exceptions.ArcaShareAlreadyExists:
            # Share already exists - fetch and return existing export location
            # This makes the operation idempotent for Manila retries
            LOG.info("Share %s already exists, fetching existing export location", share_id)
            try:
                existing_volume = self.arca_client.get_volume(volume_name, svm_name)
                export_path = existing_volume.get("export_path")
                if export_path:
                    return [
                        {
                            "path": export_path,
                            "is_admin_only": False,
                            "metadata": {},
                        }
                    ]
                else:
                    # Share exists but has no export path - this indicates an incomplete
                    # provisioning state. Raise backend exception to trigger Manila retry.
                    LOG.error(
                        "Share %s exists but is not exported (no export_path). "
                        "This may indicate incomplete provisioning or backend error.",
                        share_id
                    )
                    raise manila_exception.ShareBackendException(
                        f"Share {share_id} exists but is not exported. "
                        "This may indicate incomplete provisioning."
                    )
            except manila_exception.ShareBackendException:
                # Re-raise ShareBackendException as-is
                raise
            except Exception as fetch_error:
                LOG.error("Failed to fetch existing share %s: %s", share_id, fetch_error)
                # If fetch fails, raise backend exception to allow retry
                raise manila_exception.ShareBackendException(
                    f"Failed to verify existing share {share_id}: {str(fetch_error)}"
                )
        except arca_exceptions.ArcaSVMNotFound as e:
            LOG.exception("SVM not found for share %s", share_id)
            raise manila_exception.ShareBackendException(f"SVM not found: {str(e)}")
        except Exception as e:
            LOG.exception("Failed to create share %s", share_id)
            raise manila_exception.ShareBackendException(
                f"Failed to create share: {str(e)}"
            )

    def delete_share(self, context, share, share_server=None):
        """Delete a share.

        Args:
            context: Manila context
            share: Manila share object
            share_server: Share server (not used)

        Raises:
            manila.exception.ManilaException: If deletion fails
        """
        share_id = share["id"]
        volume_name = f"share-{share_id}"

        LOG.info("Deleting share %s", share_id)

        try:
            # Determine SVM for this share
            svm_name = self._get_svm_for_share(share)

            # Delete volume via ARCA API
            self.arca_client.delete_volume(
                name=volume_name,
                svm=svm_name,
                force=False,
            )

            LOG.info("Deleted share %s", share_id)

        except arca_exceptions.ArcaShareNotFound:
            # Share already deleted, consider success
            LOG.warning("Share %s not found, already deleted", share_id)
        except Exception as e:
            LOG.exception("Failed to delete share %s", share_id)
            raise manila_exception.ShareBackendException(
                f"Failed to delete share: {str(e)}"
            )

    def extend_share(self, share, new_size, share_server=None):
        """Extend share capacity.

        Args:
            share: Manila share object
            new_size: New size in GiB
            share_server: Share server (not used)

        Raises:
            manila.exception.ManilaException: If extension fails
        """
        share_id = share["id"]
        volume_name = f"share-{share_id}"
        old_size = share["size"]

        LOG.info("Extending share %s from %d GiB to %d GiB", share_id, old_size, new_size)

        try:
            # Determine SVM for this share
            svm_name = self._get_svm_for_share(share)

            # Resize volume via ARCA API
            # ARCA backend handles both LV resize and XFS filesystem grow
            self.arca_client.resize_volume(
                name=volume_name,
                svm=svm_name,
                new_size_gib=new_size,
            )

            LOG.info("Extended share %s to %d GiB", share_id, new_size)

        except arca_exceptions.ArcaShareNotFound:
            raise manila_exception.ShareResourceNotFound(share_id=share_id)
        except Exception as e:
            LOG.exception("Failed to extend share %s", share_id)
            raise manila_exception.ShareBackendException(
                f"Failed to extend share: {str(e)}"
            )

    def shrink_share(self, share, new_size, share_server=None):
        """Shrink share capacity.

        Args:
            share: Manila share object
            new_size: New size in GiB
            share_server: Share server (not used)

        Raises:
            NotImplementedError: XFS doesn't support shrinking
        """
        raise NotImplementedError(
            "Share shrinking is not supported (XFS filesystem limitation)"
        )

    # Snapshot Operations

    def create_snapshot(self, context, snapshot, share_server=None):
        """Create snapshot using ARCA backend LVM thin snapshot.

        Args:
            context: Manila context
            snapshot: Manila snapshot object
            share_server: Share server (not used)

        Returns:
            Dict with 'provider_location' key (string value) if mountable,
            or None if snapshot is not mountable

        Raises:
            manila.exception.ManilaException: If creation fails
        """
        snapshot_id = snapshot["id"]
        share_id = snapshot["share_id"]
        snapshot_name = f"snapshot-{snapshot_id}"
        volume_name = f"share-{share_id}"

        LOG.info("Creating snapshot %s for share %s", snapshot_id, share_id)

        try:
            # Prefer metadata-stored SVM name if available.
            svm_name = self._get_metadata_value(snapshot, "arca_svm_name")
            share = None

            # Get parent share to determine SVM
            # Note: snapshot["share"] may not be available in all Manila API versions
            if not svm_name:
                share = snapshot.get("share")
            if not svm_name and not share:
                # Check strategy before attempting fallback
                strategy = self._svm_strategy_effective or self.configuration.arca_storage_svm_strategy

                if strategy == "shared":
                    # Shared strategy: can proceed with minimal share info
                    LOG.debug(
                        "snapshot['share'] not available for snapshot %s, "
                        "using shared strategy SVM", snapshot_id
                    )
                    share = {"id": share_id}
                elif strategy in ("manual", "per_project"):
                    # Fail closed: these strategies require full share info
                    raise manila_exception.ManilaException(
                        f"snapshot['share'] not available for snapshot {snapshot_id}. "
                        f"Strategy '{strategy}' requires complete parent share information. "
                        f"Alternatively, ensure 'arca_svm_name' is persisted in snapshot metadata. "
                        f"Ensure Manila is configured to include parent share details in snapshot objects."
                    )

            if not svm_name:
                svm_name = self._get_svm_for_share(share)

            # Create snapshot via ARCA API (LVM thin snapshot)
            snapshot_info = self.arca_client.create_snapshot(
                name=snapshot_name,
                svm=svm_name,
                volume=volume_name,
            )

            LOG.info("Created snapshot %s", snapshot_id)

            # Persist SVM mapping on snapshot for later operations (best-effort).
            self._persist_snapshot_metadata(context, snapshot, {"arca_svm_name": svm_name})

            # Return provider_location as string if snapshot has export_path
            # Manila expects a string provider_location, not a dict
            if "export_path" in snapshot_info:
                return {"provider_location": snapshot_info["export_path"]}

            return None

        except Exception as e:
            LOG.exception("Failed to create snapshot %s", snapshot_id)
            raise manila_exception.ShareBackendException(
                f"Failed to create snapshot: {str(e)}"
            )

    def delete_snapshot(self, context, snapshot, share_server=None):
        """Delete snapshot.

        Args:
            context: Manila context
            snapshot: Manila snapshot object
            share_server: Share server (not used)

        Raises:
            manila.exception.ManilaException: If deletion fails
        """
        snapshot_id = snapshot["id"]
        share_id = snapshot["share_id"]
        snapshot_name = f"snapshot-{snapshot_id}"
        volume_name = f"share-{share_id}"

        LOG.info("Deleting snapshot %s", snapshot_id)

        try:
            # Prefer metadata-stored SVM name if available.
            svm_name = self._get_metadata_value(snapshot, "arca_svm_name")
            share = None

            # Get parent share to determine SVM
            # Note: snapshot["share"] may not be available in all Manila API versions
            if not svm_name:
                share = snapshot.get("share")
            if not svm_name and not share:
                # Check strategy before attempting fallback
                strategy = self._svm_strategy_effective or self.configuration.arca_storage_svm_strategy

                if strategy == "shared":
                    # Shared strategy: can proceed with minimal share info
                    LOG.debug(
                        "snapshot['share'] not available for snapshot %s, "
                        "using shared strategy SVM", snapshot_id
                    )
                    share = {"id": share_id}
                elif strategy in ("manual", "per_project"):
                    # Fail closed: these strategies require full share info
                    raise manila_exception.ManilaException(
                        f"snapshot['share'] not available for snapshot {snapshot_id}. "
                        f"Strategy '{strategy}' requires complete parent share information. "
                        f"Alternatively, ensure 'arca_svm_name' is persisted in snapshot metadata. "
                        f"Ensure Manila is configured to include parent share details in snapshot objects."
                    )

            if not svm_name:
                svm_name = self._get_svm_for_share(share)

            # Delete snapshot via ARCA API
            self.arca_client.delete_snapshot(
                name=snapshot_name,
                svm=svm_name,
                volume=volume_name,
            )

            LOG.info("Deleted snapshot %s", snapshot_id)

        except arca_exceptions.ArcaSnapshotNotFound:
            # Snapshot already deleted, consider success
            LOG.warning("Snapshot %s not found, already deleted", snapshot_id)
        except Exception as e:
            LOG.exception("Failed to delete snapshot %s", snapshot_id)
            raise manila_exception.ShareBackendException(
                f"Failed to delete snapshot: {str(e)}"
            )

    def create_share_from_snapshot(
        self, context, share, snapshot, share_server=None, parent_share=None
    ):
        """Create share from snapshot using ARCA clone API (Manila 2025.1).

        Args:
            context: Manila context
            share: Manila share object (new share)
            snapshot: Manila snapshot object (source)
            share_server: Share server (not used)
            parent_share: Parent share object (optional)

        Returns:
            List[Dict]: Export locations in Manila 2025.1 format
                [{'path': export_path, 'is_admin_only': False, 'metadata': {}}]

        Raises:
            manila.exception.ManilaException: If creation fails

        Note:
            ARCA API must handle:
            - Filesystem resize automatically if size_gib > snapshot size
            - Writable clone creation (not read-only snapshot mount)
        """
        share_id = share["id"]
        share_size = share["size"]
        snapshot_id = snapshot["id"]
        parent_share_id = snapshot["share_id"]
        volume_name = f"share-{share_id}"
        snapshot_name = f"snapshot-{snapshot_id}"
        parent_volume_name = f"share-{parent_share_id}"

        LOG.info(
            "Creating share %s from snapshot %s (size: %d GiB)",
            share_id,
            snapshot_id,
            share_size,
        )

        try:
            # Get parent share to determine SVM
            if not parent_share:
                parent_share = snapshot.get("share")
            if not parent_share:
                raise manila_exception.ShareBackendException(
                    f"Parent share not found for snapshot {snapshot_id}"
                )

            # For per_project strategy, verify new share is in same project as parent
            strategy = self._svm_strategy_effective or self.configuration.arca_storage_svm_strategy
            if strategy == "per_project":
                parent_project_id = parent_share.get("project_id")
                new_project_id = share.get("project_id")

                # Fail closed: require project IDs to be present
                if not parent_project_id or not new_project_id:
                    raise manila_exception.ManilaException(
                        f"Cannot verify project isolation for per_project strategy: "
                        f"parent_project_id={parent_project_id}, new_project_id={new_project_id}. "
                        f"Both project IDs must be present to ensure tenant isolation."
                    )

                if parent_project_id != new_project_id:
                    raise manila_exception.ManilaException(
                        f"Cross-project snapshot cloning not supported for per_project strategy. "
                        f"Parent share is in project {parent_project_id}, "
                        f"new share is in project {new_project_id}. "
                        f"Create share from snapshot only within the same project."
                    )

            svm_name = self._get_svm_for_share(parent_share)
            LOG.debug("Using SVM %s for share %s from snapshot", svm_name, share_id)

            # Clone volume from snapshot via ARCA API
            volume_info = self.arca_client.clone_volume_from_snapshot(
                name=volume_name,
                svm=svm_name,
                source_volume=parent_volume_name,
                snapshot_name=snapshot_name,
                size_gib=share_size,  # May be larger than snapshot
            )

            # Extract export path from API response (authoritative)
            export_path = volume_info.get("export_path")
            if not export_path:
                raise manila_exception.ShareBackendException(
                    f"No export_path in clone response for {volume_name}"
                )

            LOG.info(
                "Created share %s from snapshot %s with export path: %s",
                share_id,
                snapshot_id,
                export_path,
            )

            # Persist SVM mapping for later operations (best-effort).
            self._persist_share_metadata(context, share, {"arca_svm_name": svm_name})

            # Apply QoS if share type has specs (best-effort)
            self._apply_qos_to_share(share, volume_name, svm_name)

            # Return export locations in Manila 2025.1 format
            return [
                {
                    "path": export_path,
                    "is_admin_only": False,
                    "metadata": {},
                }
            ]

        except Exception as e:
            LOG.exception(
                "Failed to create share %s from snapshot %s",
                share_id,
                snapshot_id,
            )
            raise manila_exception.ShareBackendException(
                f"Failed to create share from snapshot: {str(e)}"
            )

    # Access Rule Management

    def update_access(
        self, context, share, access_rules, add_rules, delete_rules, share_server=None
    ):
        """Update share access rules (Manila 2025.1 Epoxy interface).

        This method is called to reconcile access rules to desired state.
        Implementation must be idempotent (adding existing or removing missing is OK).

        Args:
            context: Manila context
            share: Manila share object
            access_rules: All desired access rules (complete list)
            add_rules: Rules to add (incremental)
            delete_rules: Rules to delete (incremental)
            share_server: Share server (not used)

        Returns:
            None (Manila 2025.1)

        Note:
            On errors:
            - Log warnings for individual rule failures
            - Raise ShareBackendException only for systemic failures
            - Manila will retry failed rules automatically
        """
        share_id = share["id"]
        volume_name = f"share-{share_id}"

        LOG.info(
            "Updating access rules for share %s (add: %d, delete: %d)",
            share_id,
            len(add_rules) if add_rules else 0,
            len(delete_rules) if delete_rules else 0,
        )

        try:
            # Determine SVM for this share
            svm_name = self._get_svm_for_share(share)

            # If Manila didn't provide incremental changes, reconcile from full desired list.
            # This prevents drift when add_rules/delete_rules are omitted or empty.
            if access_rules and not add_rules and not delete_rules:
                self._reconcile_access_rules(svm_name, volume_name, access_rules)
                LOG.info("Access rules reconciled for share %s", share_id)
                return None

            # Delete rules first (cleanup before adding)
            if delete_rules:
                for rule in delete_rules:
                    try:
                        self._delete_access_rule(svm_name, volume_name, rule)
                    except Exception as e:
                        # Log warning but continue with other rules
                        LOG.warning(
                            "Failed to delete access rule %s for share %s: %s",
                            rule.get("id"),
                            share_id,
                            e,
                        )

            # Add new rules
            if add_rules:
                for rule in add_rules:
                    try:
                        self._add_access_rule(svm_name, volume_name, rule)
                    except Exception as e:
                        # Log warning but continue with other rules
                        LOG.warning(
                            "Failed to add access rule %s for share %s: %s",
                            rule.get("id"),
                            share_id,
                            e,
                        )

            LOG.info("Access rules updated for share %s", share_id)

        except Exception as e:
            # Keep Manila's semantic exception when rule type is invalid.
            if isinstance(e, manila_exception.InvalidShareAccess):
                raise
            # Systemic failure (e.g., can't determine SVM)
            LOG.exception("Failed to update access rules for share %s", share_id)
            raise manila_exception.ShareBackendException(
                f"Failed to update access rules: {str(e)}"
            )
        return None

    def _normalize_access_to(self, access_to: str) -> str:
        """Normalize an IP/CIDR string for stable comparisons."""
        import ipaddress

        # ip_network() accepts bare IPs with strict=False (becomes /32)
        return str(ipaddress.ip_network(str(access_to).strip(), strict=False))

    def _reconcile_access_rules(self, svm_name: str, volume_name: str, access_rules):
        """Reconcile backend exports against desired Manila access rules.

        This is a safety-net for cases where Manila doesn't provide incremental
        diffs (add_rules/delete_rules). Best-effort: individual failures are logged.

        Uses backend-returned raw client strings for delete operations to avoid
        format mismatches, while normalizing for comparison.
        """
        desired: Dict[str, str] = {}

        for rule in access_rules or []:
            access_type = rule.get("access_type")
            if access_type != "ip":
                raise manila_exception.InvalidShareAccess(
                    reason=(
                        f"Access type '{access_type}' not supported "
                        "(only 'ip' allowed)"
                    )
                )

            access_to_raw = rule.get("access_to")
            access_level = rule.get("access_level", "rw")
            if access_level not in ["rw", "ro"]:
                raise manila_exception.InvalidShareAccess(
                    reason=(
                        f"Invalid access level: {access_level} "
                        "(must be 'rw' or 'ro')"
                    )
                )

            access_to = self._normalize_access_to(access_to_raw)
            desired[access_to] = access_level

        # Query current exports from backend
        # Map: normalized_client -> (raw_client, access_level)
        current: Dict[str, tuple] = {}
        try:
            exports = self.arca_client.list_exports(svm=svm_name, volume=volume_name)
        except Exception as e:
            raise manila_exception.ShareBackendException(
                f"Failed to list current exports: {str(e)}"
            )

        for export in exports or []:
            client_raw = export.get("client")
            if not client_raw:
                continue
            try:
                client_norm = self._normalize_access_to(client_raw)
            except Exception:
                # Keep raw if backend returned an unexpected format
                client_norm = str(client_raw).strip()
            access_level = export.get("access", "rw")
            current[client_norm] = (client_raw, access_level)

        # Compute changes (using normalized keys for comparison)
        to_delete = [client_norm for client_norm in current.keys() if client_norm not in desired]
        to_add = [client_norm for client_norm in desired.keys() if client_norm not in current]
        to_update = [
            client_norm
            for client_norm in desired.keys()
            if client_norm in current and current[client_norm][1] != desired[client_norm]
        ]

        # Apply deletions first to allow access-level changes via delete+add
        # Use raw backend strings for delete operations
        for client_norm in to_delete + to_update:
            client_raw = current[client_norm][0]
            try:
                self.arca_client.delete_export(svm=svm_name, volume=volume_name, client=client_raw)
            except Exception as e:
                LOG.warning(
                    "Failed to delete export for %s (raw: %s) on %s/%s: %s",
                    client_norm,
                    client_raw,
                    svm_name,
                    volume_name,
                    e,
                )

        # Apply additions (including updates)
        # Use normalized strings for create operations (backend will normalize)
        for client_norm in to_add + to_update:
            try:
                self.arca_client.create_export(
                    svm=svm_name,
                    volume=volume_name,
                    client=client_norm,
                    access=desired[client_norm],
                    root_squash=True,
                )
            except Exception as e:
                LOG.warning(
                    "Failed to create export for %s on %s/%s: %s",
                    client_norm,
                    svm_name,
                    volume_name,
                    e,
                )

    def _add_access_rule(self, svm_name, volume_name, rule):
        """Add access rule to share.

        Args:
            svm_name: SVM name
            volume_name: Volume name
            rule: Access rule dict with access_type, access_to, access_level

        Raises:
            manila.exception.InvalidShareAccess: If rule type not supported
            arca_exceptions.ArcaAccessRuleError: If API call fails
        """
        access_type = rule.get("access_type")
        access_to = rule.get("access_to")
        access_level = rule.get("access_level", "rw")

        # Validate access type (only IP-based rules supported)
        if access_type != "ip":
            raise manila_exception.InvalidShareAccess(
                reason=f"Access type '{access_type}' not supported (only 'ip' allowed)"
            )

        # Validate CIDR format
        try:
            import ipaddress

            ipaddress.ip_network(access_to, strict=False)
        except ValueError as e:
            raise manila_exception.InvalidShareAccess(
                reason=f"Invalid IP address or CIDR: {access_to}"
            )

        # Normalize access level
        if access_level not in ["rw", "ro"]:
            raise manila_exception.InvalidShareAccess(
                reason=f"Invalid access level: {access_level} (must be 'rw' or 'ro')"
            )

        LOG.debug(
            "Adding access rule: %s %s (%s) to volume %s on SVM %s",
            access_type,
            access_to,
            access_level,
            volume_name,
            svm_name,
        )

        try:
            # Create NFS export ACL entry
            self.arca_client.create_export(
                svm=svm_name,
                volume=volume_name,
                client=access_to,
                access=access_level,
                root_squash=True,  # Default to root_squash for security
            )
            LOG.info("Added access rule %s to share", access_to)
        except arca_exceptions.ArcaShareAlreadyExists:
            # Rule already exists, idempotent success
            LOG.debug("Access rule %s already exists", access_to)
        except Exception as e:
            # Re-raise as access rule error
            raise arca_exceptions.ArcaAccessRuleError(
                details=f"Failed to add access rule: {str(e)}"
            )

    def _delete_access_rule(self, svm_name, volume_name, rule):
        """Delete access rule from share.

        Args:
            svm_name: SVM name
            volume_name: Volume name
            rule: Access rule dict with access_type, access_to

        Raises:
            arca_exceptions.ArcaAccessRuleError: If API call fails
        """
        access_type = rule.get("access_type")
        access_to = rule.get("access_to")

        if access_type != "ip":
            LOG.warning("Skipping non-IP access rule deletion: %s", access_type)
            return

        LOG.debug(
            "Deleting access rule: %s %s from volume %s on SVM %s",
            access_type,
            access_to,
            volume_name,
            svm_name,
        )

        try:
            # Delete NFS export ACL entry
            self.arca_client.delete_export(
                svm=svm_name,
                volume=volume_name,
                client=access_to,
            )
            LOG.info("Deleted access rule %s from share", access_to)
        except arca_exceptions.ArcaShareNotFound:
            # Rule doesn't exist, idempotent success
            LOG.debug("Access rule %s not found (already deleted)", access_to)
        except Exception as e:
            # Re-raise as access rule error
            raise arca_exceptions.ArcaAccessRuleError(
                details=f"Failed to delete access rule: {str(e)}"
            )

    # QoS Helper Method

    def _apply_qos_to_share(self, share, volume_name, svm_name):
        """Apply QoS limits to share (best-effort).

        Args:
            share: Manila share object
            volume_name: Volume name
            svm_name: SVM name
        """
        try:
            share_type = share.get("share_type")
            if not share_type:
                return

            extra_specs = share_type.get("extra_specs", {})

            # Extract QoS specs (arca_manila:* prefix)
            read_iops = extra_specs.get("arca_manila:read_iops_sec")
            write_iops = extra_specs.get("arca_manila:write_iops_sec")
            read_bps = extra_specs.get("arca_manila:read_bytes_sec")
            write_bps = extra_specs.get("arca_manila:write_bytes_sec")

            if not any([read_iops, write_iops, read_bps, write_bps]):
                return

            # Apply QoS via ARCA API
            self.arca_client.apply_qos(
                volume=volume_name,
                svm=svm_name,
                read_iops=int(read_iops) if read_iops else None,
                write_iops=int(write_iops) if write_iops else None,
                read_bps=int(read_bps) if read_bps else None,
                write_bps=int(write_bps) if write_bps else None,
            )

            LOG.info("Applied QoS to share %s", share["id"])

        except Exception as e:
            # QoS is best-effort, don't fail share creation
            LOG.warning("Failed to apply QoS to share %s: %s", share["id"], e)

    # SVM Lifecycle Management
    # NOTE: SVM garbage collection for per_project strategy is planned but not yet implemented.
    # Future implementation will require:
    # 1. Adding list_volumes(svm) method to ArcaManilaClient
    # 2. Adding delete_svm(name, force) method to ArcaManilaClient
    # 3. Proper timezone-aware datetime handling
    # For now, SVM cleanup must be performed manually via ARCA API or CLI.
