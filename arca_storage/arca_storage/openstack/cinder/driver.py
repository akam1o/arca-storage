"""ARCA Storage Cinder Volume Driver.

This driver provides OpenStack Cinder integration for ARCA Storage
using NFS as the transport protocol.
"""

import os
import posixpath
from typing import Any, Dict, Optional

from oslo_log import log as logging

from cinder import exception
from cinder.i18n import _
from cinder.volume.drivers import remotefs as remotefs_drv

from . import client as arca_client
from . import configuration as arca_config
from . import exceptions as arca_exceptions
from . import utils as arca_utils

LOG = logging.getLogger(__name__)

VERSION = "1.0.0"


class ArcaStorageNFSDriver(remotefs_drv.RemoteFSDriver):
    """ARCA Storage NFS volume driver.

    This driver integrates ARCA Storage as a Cinder backend using NFS protocol.
    It leverages ARCA Storage's REST API for volume management and NFS for
    data access.

    Version history:
        1.0.0 - Initial implementation
    """

    driver_volume_type = "nfs"
    driver_prefix = "arca_storage"
    VERSION = VERSION

    # Capabilities
    # These tell Cinder what features this driver supports
    _thin_provisioning_support = True
    _thick_provisioning_support = False
    _snapshot_support = True  # Enabled in Phase 2
    _clone_support = True  # Enabled in Phase 2
    _replication_support = False
    _multiattach_support = False
    _qos_extra_spec_keys = frozenset(
        (
            "arca_storage:read_iops_sec",
            "arca_storage:write_iops_sec",
            "arca_storage:total_iops_sec",
            "arca_storage:read_bytes_sec",
            "arca_storage:write_bytes_sec",
        )
    )

    def __init__(self, *args, **kwargs):
        """Initialize the ARCA Storage driver.

        Args:
            *args: Positional arguments
            **kwargs: Keyword arguments including 'configuration'
        """
        super(ArcaStorageNFSDriver, self).__init__(*args, **kwargs)

        # Register configuration options
        self.configuration.append_config_values(arca_config.get_arca_storage_opts())

        # ARCA Storage API client (optional; initialized in do_setup)
        self.arca_client: Optional[arca_client.ArcaStorageClient] = None

        # Cache for SVM information
        self._svm_cache: Dict[str, Dict[str, Any]] = {}

        # Best-effort context for snapshot operations (set in do_setup/retype)
        self._context = None

    def _get_api_auth_config(self):
        auth_type = (
            getattr(self.configuration, "arca_storage_api_auth_type", "token")
            or "token"
        )
        api_token = getattr(self.configuration, "arca_storage_api_token", None)

        if auth_type not in ("token", "none"):
            raise exception.VolumeBackendAPIException(
                data=_("arca_storage_api_auth_type must be 'token' or 'none'")
            )
        if auth_type == "token" and not api_token:
            raise exception.VolumeBackendAPIException(
                data=_(
                    "arca_storage_api_token must be set when "
                    "arca_storage_api_auth_type is 'token'"
                )
            )

        return auth_type, api_token

    def do_setup(self, context):
        """Perform driver setup and validation.

        This method is called once during driver initialization.

        Args:
            context: Cinder context

        Raises:
            exception.VolumeBackendAPIException: If setup fails
        """
        super(ArcaStorageNFSDriver, self).do_setup(context)
        self._context = context
        self._validate_supported_svm_strategy()

        try:
            # Initialize ARCA Storage API client if enabled
            if self.configuration.arca_storage_use_api:
                if not self.configuration.arca_storage_api_endpoint:
                    raise exception.VolumeBackendAPIException(
                        data=_("arca_storage_api_endpoint must be set when arca_storage_use_api is True")
                    )
                auth_type, api_token = self._get_api_auth_config()
                self.arca_client = arca_client.ArcaStorageClient(
                    api_endpoint=self.configuration.arca_storage_api_endpoint,
                    timeout=self.configuration.arca_storage_api_timeout,
                    retry_count=self.configuration.arca_storage_api_retry_count,
                    verify_ssl=self.configuration.arca_storage_verify_ssl,
                    auth_type=auth_type,
                    api_token=api_token,
                    ca_bundle=getattr(self.configuration, "arca_storage_driver_ssl_cert_path", None),
                )

            # Mount options alignment: Support standard RemoteFSDriver nfs_mount_options
            # as fallback if arca_storage_nfs_mount_options uses default value
            default_mount_opts = "rw,noatime,nodiratime,vers=4.1"
            if self.configuration.arca_storage_nfs_mount_options == default_mount_opts:
                # Check if standard nfs_mount_options is configured
                if hasattr(self.configuration, "nfs_mount_options"):
                    standard_opts = self.configuration.nfs_mount_options
                    if standard_opts and standard_opts != default_mount_opts:
                        LOG.info(
                            "Using standard nfs_mount_options: %s (overriding default arca_storage_nfs_mount_options)",
                            standard_opts,
                        )
                        # Override the arca-specific option with standard one
                        self.configuration.arca_storage_nfs_mount_options = standard_opts

            LOG.info(
                "ARCA Storage driver initialized (version=%s, use_api=%s, endpoint=%s, mount_options=%s)",
                VERSION,
                self.configuration.arca_storage_use_api,
                self.configuration.arca_storage_api_endpoint,
                self.configuration.arca_storage_nfs_mount_options,
            )

        except Exception as e:
            msg = _("Failed to initialize ARCA Storage driver: %s") % e
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def check_for_setup_error(self):
        """Validate driver configuration and connectivity.

        This method verifies that the driver is properly configured and can
        resolve NFS exports and (optionally) communicate with ARCA Storage API.

        Raises:
            exception.VolumeBackendAPIException: If validation fails
        """
        super(ArcaStorageNFSDriver, self).check_for_setup_error()
        self._validate_supported_svm_strategy()

        # Validate export path resolution
        try:
            if self.configuration.arca_storage_svm_strategy == "shared":
                default_svm = self.configuration.arca_storage_default_svm
                export_path = self._get_export_path(default_svm)
                LOG.info("Validated export path for default SVM: %s", export_path)
        except Exception as e:
            msg = _("Failed to validate ARCA Storage configuration: %s") % e
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def create_volume(self, volume):
        """Create a volume on ARCA Storage.

        Args:
            volume: Cinder volume object

        Returns:
            Provider location dict (optional)

        Raises:
            exception.VolumeBackendAPIException: If creation fails
        """
        volume_name = volume.name
        volume_size = volume.size
        volume_id = volume.id

        LOG.info("Creating volume: %s (size=%sGB)", volume_name, volume_size)

        self._apply_qos_to_volume(volume)

        # Track cleanup state
        cleanup_state = {
            "svm_name": None,
            "volume_file_created": False,
            "volume_file_path": None,  # Track full path for cleanup
        }

        try:
            # Determine SVM placement for this new volume.
            svm_name = self._get_svm_for_volume(volume)
            cleanup_state["svm_name"] = svm_name

            # Use per-SVM NFS export (no per-volume export needed)
            # The SVM exports /exports/{svm} which contains all volume files
            export_path = self._get_export_path(svm_name)

            # Mount SVM's NFS export (idempotent - won't remount if already mounted)
            mount_point = arca_utils.get_mount_point_for_svm(
                self.configuration.arca_storage_nfs_mount_point_base,
                svm_name,
            )

            arca_utils.mount_nfs(
                export_path=export_path,
                mount_point=mount_point,
                mount_options=self.configuration.arca_storage_nfs_mount_options,
            )

            LOG.info("Mounted SVM export at: %s", mount_point)

            # Create or adopt the raw sparse file using volume ID for unique naming.
            volume_file, volume_file_created = arca_utils.ensure_volume_file(
                mount_point=mount_point,
                volume_name=f"volume-{volume_id}",  # Use volume ID, not name
                size_gb=volume_size,
                adopt_existing=True,
            )
            cleanup_state["volume_file_created"] = volume_file_created
            cleanup_state["volume_file_path"] = volume_file

            LOG.info("Created volume file: %s", volume_file)

            return self._volume_model_update(svm_name, export_path)

        except arca_exceptions.ArcaStorageException as e:
            msg = _("Failed to create volume %s: %s") % (volume_name, e)
            LOG.error(msg)
            self._cleanup_failed_volume(volume_name, cleanup_state)
            raise exception.VolumeBackendAPIException(data=msg)
        except Exception as e:
            msg = _("Failed to create volume %s: %s") % (volume_name, e)
            LOG.error(msg)
            # Cleanup on failure with tracked state
            self._cleanup_failed_volume(volume_name, cleanup_state)
            raise exception.VolumeBackendAPIException(data=msg)

    def delete_volume(self, volume):
        """Delete a volume from ARCA Storage using per-SVM export architecture.

        Args:
            volume: Cinder volume object

        Raises:
            exception.VolumeBackendAPIException: If deletion fails
        """
        volume_name = volume.name
        volume_id = volume.id

        LOG.info("Deleting volume: %s (ID: %s)", volume_name, volume_id)

        try:
            # Use the SVM recorded when the volume was created.
            svm_name = self._get_existing_volume_svm(volume)

            # Get SVM-level mount point (per-SVM export architecture)
            mount_point = arca_utils.get_mount_point_for_svm(
                self.configuration.arca_storage_nfs_mount_point_base,
                svm_name,
            )

            # Mount SVM's NFS export if not already mounted (idempotent)
            # This ensures we can delete the file even after service restart
            if not arca_utils.is_mounted(mount_point):
                export_path = self._get_export_path(svm_name)

                arca_utils.mount_nfs(
                    export_path=export_path,
                    mount_point=mount_point,
                    mount_options=self.configuration.arca_storage_nfs_mount_options,
                )
                LOG.info("Mounted SVM export for deletion: %s", export_path)

            # Delete volume file from SVM's shared export
            # Volume file is named: volume-{volume_id}
            volume_file_name = f"volume-{volume_id}"
            arca_utils.delete_volume_file(mount_point, volume_file_name)
            LOG.info("Deleted volume file: %s from %s", volume_file_name, mount_point)

            # Note: We do NOT unmount the SVM export - it may be in use by other volumes
            # Note: We do NOT delete per-volume NFS export - we use per-SVM exports

        except arca_exceptions.ArcaStorageException as e:
            msg = _("Failed to delete volume %s: %s") % (volume_name, e)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        except Exception as e:
            msg = _("Failed to delete volume %s: %s") % (volume_name, e)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def extend_volume(self, volume, new_size):
        """Extend volume to new size using per-SVM export architecture.

        Args:
            volume: Cinder volume object
            new_size: New size in GB

        Raises:
            exception.VolumeBackendAPIException: If extend fails
        """
        volume_name = volume.name
        volume_id = volume.id
        current_size = volume.size

        LOG.info(
            "Extending volume: %s (ID: %s) (%dGB -> %dGB)",
            volume_name,
            volume_id,
            current_size,
            new_size,
        )

        try:
            # Use the SVM recorded when the volume was created.
            svm_name = self._get_existing_volume_svm(volume)

            # Get SVM-level mount point (per-SVM export architecture)
            mount_point = arca_utils.get_mount_point_for_svm(
                self.configuration.arca_storage_nfs_mount_point_base,
                svm_name,
            )

            # Mount SVM's NFS export if not already mounted (idempotent)
            if not arca_utils.is_mounted(mount_point):
                export_path = self._get_export_path(svm_name)

                arca_utils.mount_nfs(
                    export_path=export_path,
                    mount_point=mount_point,
                    mount_options=self.configuration.arca_storage_nfs_mount_options,
                )
                LOG.info("Mounted SVM export: %s", export_path)

            # Extend volume file (volume-{volume_id})
            volume_file_name = f"volume-{volume_id}"
            arca_utils.extend_volume_file(mount_point, volume_file_name, new_size)
            LOG.info("Extended volume file %s to %dGB", volume_file_name, new_size)

            # Note: We do NOT unmount the SVM export - it may be in use by other volumes

        except arca_exceptions.ArcaStorageException as e:
            msg = _("Failed to extend volume %s: %s") % (volume_name, e)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        except Exception as e:
            msg = _("Failed to extend volume %s: %s") % (volume_name, e)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def initialize_connection(self, volume, connector):
        """Initialize connection to volume for compute node using per-SVM export architecture.

        Args:
            volume: Cinder volume object
            connector: Connection info from compute node

        Returns:
            Connection info dictionary

        Raises:
            exception.VolumeBackendAPIException: If initialization fails
        """
        volume_name = volume.name
        volume_id = volume.id

        LOG.info("Initializing connection for volume: %s (ID: %s)", volume_name, volume_id)

        try:
            # Prioritize provider_location (persisted export path) over regenerating
            # This ensures consistency even if SVM VIP changes after volume creation
            if volume.provider_location:
                export_path = volume.provider_location
                LOG.debug(
                    "Using provider_location for volume %s: %s",
                    volume_name,
                    export_path,
                )
            else:
                # Fallback: regenerate per-SVM export path
                # (for volumes created before per-SVM export architecture)
                svm_name = self._get_existing_volume_svm(volume)
                # Use per-SVM export path, NOT per-volume export path
                export_path = self._get_export_path(svm_name)
                LOG.warning(
                    "Volume %s has no provider_location, regenerated per-SVM export: %s",
                    volume_name,
                    export_path,
                )

            # Return connection info for Nova compute node
            # Nova will mount the SVM's NFS export and find the volume file (volume-{volume_id})
            connection_info = {
                "driver_volume_type": "nfs",
                "data": {
                    "export": export_path,
                    "name": f"volume-{volume_id}",  # Volume filename in SVM export
                    "options": self.configuration.arca_storage_nfs_mount_options,
                },
            }

            LOG.debug("Connection info: %s", connection_info)

            return connection_info

        except Exception as e:
            msg = _("Failed to initialize connection for volume %s: %s") % (
                volume_name,
                e,
            )
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def terminate_connection(self, volume, connector, **kwargs):
        """Terminate connection to volume.

        Args:
            volume: Cinder volume object
            connector: Connection info from compute node
            **kwargs: Additional arguments
        """
        volume_name = volume.name

        LOG.info("Terminating connection for volume: %s", volume_name)

        # For NFS, termination is handled by compute node unmounting
        # No action needed on storage side

    def _update_volume_stats(self):
        """Update backend capabilities and statistics."""
        data = {
            "volume_backend_name": self.configuration.safe_get(
                "volume_backend_name"
            )
            or "arca_storage",
            "vendor_name": "ARCA Storage",
            "driver_version": VERSION,
            "storage_protocol": "nfs",
            # Capabilities
            "thin_provisioning_support": self._thin_provisioning_support,
            "thick_provisioning_support": self._thick_provisioning_support,
            "snapshot_support": self._snapshot_support,
            "clone_support": self._clone_support,
            "replication_enabled": self._replication_support,
            "multiattach": self._multiattach_support,
            # Capacity falls back to unknown when the ARCA API is unavailable.
            "total_capacity_gb": "unknown",
            "free_capacity_gb": "unknown",
            "reserved_percentage": self.configuration.reserved_percentage,
            "max_over_subscription_ratio": self.configuration.arca_storage_max_over_subscription_ratio,
        }

        capacity = self._get_backend_capacity()
        if capacity is not None:
            data.update(capacity)

        self._stats = data

    def _get_backend_capacity(self) -> Optional[Dict[str, float]]:
        """Return scheduler capacity stats when the backend SVM is unambiguous."""
        if not self.configuration.arca_storage_use_api or not self.arca_client:
            return None
        if self.configuration.arca_storage_svm_strategy != "shared":
            return None

        svm_name = self.configuration.arca_storage_default_svm
        try:
            capacity = self.arca_client.get_svm_capacity(svm_name)
            return {
                "total_capacity_gb": float(capacity["total_gb"]),
                "free_capacity_gb": float(capacity["free_gb"]),
                "provisioned_capacity_gb": float(capacity.get("provisioned_gb", 0)),
            }
        except Exception as e:
            LOG.warning("Failed to get capacity for SVM %s: %s", svm_name, e)
            return None

    def _get_svm_for_volume(self, volume) -> str:
        """Determine which SVM to use for a volume.

        Args:
            volume: Cinder volume object

        Returns:
            SVM name

        Raises:
            exception.VolumeBackendAPIException: If SVM cannot be determined
        """
        strategy = self.configuration.arca_storage_svm_strategy

        if strategy == "shared":
            # All volumes use default SVM
            return self.configuration.arca_storage_default_svm

        elif strategy == "manual":
            # Check volume type extra_specs
            if hasattr(volume, "volume_type") and volume.volume_type:
                svm_name = self._svm_name_from_volume_type(volume.volume_type)
                if svm_name:
                    return svm_name

            msg = _(
                "SVM strategy is 'manual' but volume type does not specify "
                "'arca_storage:svm_name' extra_spec"
            )
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        elif strategy == "per_project":
            msg = _(
                "SVM strategy 'per_project' requires auto-creation which is not "
                "implemented yet. Please use 'shared' or 'manual' strategy."
            )
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        else:
            msg = _("Invalid SVM strategy: %s") % strategy
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def _volume_provider_svm(self, volume) -> Optional[str]:
        """Return the driver-managed SVM persisted on a Cinder volume."""
        provider_id = getattr(volume, "provider_id", None)
        if isinstance(provider_id, str):
            provider_id = provider_id.strip()
            if provider_id:
                return provider_id
        return None

    def _svm_from_volume_provider_location(self, volume) -> Optional[str]:
        """Best-effort SVM inference for legacy volumes without provider_id."""
        provider_location = getattr(volume, "provider_location", None)
        if not isinstance(provider_location, str):
            return None

        provider_location = provider_location.strip()
        if not provider_location:
            return None

        _server, separator, export_root = provider_location.rpartition(":")
        if not separator or not export_root.startswith("/"):
            return None

        svm_name = posixpath.basename(export_root.rstrip("/"))
        if svm_name and svm_name not in (".", "/"):
            return svm_name
        return None

    def _get_existing_volume_svm(self, volume) -> str:
        """Resolve the SVM that owns an already-created volume."""
        svm_name = self._volume_provider_svm(volume)
        if svm_name:
            return svm_name

        svm_name = self._svm_from_volume_provider_location(volume)
        if svm_name:
            LOG.warning(
                "Volume %s has no provider_id; inferred SVM %s from provider_location",
                getattr(volume, "name", getattr(volume, "id", "<unknown>")),
                svm_name,
            )
            return svm_name

        return self._get_svm_for_volume(volume)

    def _get_svm_info(self, svm_name: str, refresh: bool = False) -> Dict[str, Any]:
        """Get SVM information, optionally using the last cached response.

        Args:
            svm_name: SVM name
            refresh: Fetch from the API even when a cached response exists.

        Returns:
            SVM information dictionary

        Raises:
            arca_exceptions.ArcaSVMNotFound: If SVM not found
        """
        if not refresh and svm_name in self._svm_cache:
            return self._svm_cache[svm_name]

        # Fetch from API
        if self.arca_client is None:
            raise exception.VolumeBackendAPIException(
                data=_("ARCA API client is not initialized (arca_storage_use_api is false)")
            )
        svm_info = self.arca_client.get_svm(svm_name)

        # Cache for future use
        self._svm_cache[svm_name] = svm_info

        return svm_info

    def _validate_supported_svm_strategy(self) -> None:
        """Reject configured strategies that this Cinder driver cannot serve."""
        if self.configuration.arca_storage_svm_strategy == "per_project":
            msg = _(
                "SVM strategy 'per_project' is not implemented for the Cinder "
                "driver. Please use 'shared' or 'manual' strategy."
            )
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def _configured_svm_export_root(self, svm_name: str) -> str:
        """Return the configured per-SVM export root for static NFS mode."""
        base = getattr(self.configuration, "arca_storage_nfs_export_root", None) or "/exports"
        base = str(base).rstrip("/")
        return f"{base}/{svm_name}" if base else f"/{svm_name}"

    def _get_export_path(self, svm_name: str) -> str:
        """Resolve NFS export path for an SVM.

        Preference order:
          1) Explicit `arca_storage_nfs_server` + configured export root.
          2) ARCA API (SVM vip + export_root) if `arca_storage_use_api=True`.
        """
        if getattr(self.configuration, "arca_storage_nfs_server", None):
            export_root = self._configured_svm_export_root(svm_name)
            return f"{self.configuration.arca_storage_nfs_server}:{export_root}"

        if self.configuration.arca_storage_use_api:
            svm_info = self._get_svm_info(svm_name, refresh=True)
            svm_vip = svm_info["vip"]
            export_root = svm_info.get("export_root") or self._configured_svm_export_root(svm_name)
            return f"{svm_vip}:{export_root}"

        raise exception.VolumeBackendAPIException(
            data=_(
                "Unable to determine NFS export path: set arca_storage_nfs_server "
                "or enable arca_storage_use_api"
            )
        )

    def _mount_svm_export(self, svm_name: str) -> tuple[str, str]:
        """Mount an SVM export and return (export_path, mount_point)."""
        export_path = self._get_export_path(svm_name)
        return self._mount_svm_export_path(svm_name, export_path)

    def _mount_svm_export_path(self, svm_name: str, export_path: str) -> tuple[str, str]:
        """Mount the provided export path for an SVM."""
        mount_point = arca_utils.get_mount_point_for_svm(
            self.configuration.arca_storage_nfs_mount_point_base,
            svm_name,
        )
        arca_utils.mount_nfs(
            export_path=export_path,
            mount_point=mount_point,
            mount_options=self.configuration.arca_storage_nfs_mount_options,
        )
        return export_path, mount_point

    def _snapshot_provider_location(self, snapshot) -> Optional[str]:
        """Return a persisted snapshot provider_location when present."""
        provider_location = getattr(snapshot, "provider_location", None)
        if isinstance(provider_location, str):
            provider_location = provider_location.strip()
            if provider_location:
                return provider_location
        return None

    def _snapshot_provider_svm(self, snapshot) -> Optional[str]:
        """Return a driver-managed snapshot SVM when present."""
        provider_id = getattr(snapshot, "provider_id", None)
        if isinstance(provider_id, str):
            provider_id = provider_id.strip()
            if provider_id:
                return provider_id
        return None

    def _get_snapshot_storage(self, snapshot) -> tuple[str, str]:
        """Resolve the SVM and export path where a snapshot file is stored."""
        svm_name = self._snapshot_provider_svm(snapshot)
        export_path = self._snapshot_provider_location(snapshot)
        if svm_name:
            return svm_name, export_path or self._get_export_path(svm_name)

        volume_id = snapshot.volume_id
        context = self._get_operation_context(snapshot=snapshot)
        volume = self.db.volume_get(context, volume_id)
        svm_name = self._get_existing_volume_svm(volume)
        return svm_name, export_path or self._get_export_path(svm_name)

    def _snapshot_model_update(self, svm_name: str, export_path: str) -> Dict[str, Any]:
        """Return driver-managed snapshot storage fields for future cleanup."""
        return {
            "provider_location": export_path,
            "provider_id": svm_name,
        }

    def _volume_model_update(self, svm_name: str, export_path: str) -> Dict[str, Any]:
        """Return driver-managed volume storage fields for future operations."""
        return {
            "provider_location": export_path,
            "provider_id": svm_name,
        }

    def _ensure_copy_stays_within_svm(
        self,
        operation: str,
        source_svm_name: str,
        target_svm_name: str,
    ) -> None:
        """Reject copy-style operations that would cross SVM boundaries."""
        if source_svm_name == target_svm_name:
            return

        msg = _(
            "Cross-SVM %(operation)s is not supported: "
            "source SVM %(source)s differs from target SVM %(target)s"
        ) % {
            "operation": operation,
            "source": source_svm_name,
            "target": target_svm_name,
        }
        LOG.error(msg)
        raise exception.VolumeBackendAPIException(data=msg)

    def _get_volume_type_extra_specs(self, volume_type) -> Dict[str, Any]:
        """Return extra_specs dict from either an object or a dict-like."""
        if volume_type is None:
            return {}

        extra_specs = getattr(volume_type, "extra_specs", None)
        if isinstance(extra_specs, dict):
            return extra_specs

        if isinstance(volume_type, dict):
            value = volume_type.get("extra_specs") or {}
            return value if isinstance(value, dict) else {}

        get_method = getattr(volume_type, "get", None)
        if callable(get_method):
            value = get_method("extra_specs", {}) or {}
            return value if isinstance(value, dict) else {}

        return {}

    def _svm_name_from_volume_type(self, volume_type) -> Optional[str]:
        """Return normalized ARCA SVM extra spec from a Cinder volume type."""
        extra_specs = self._get_volume_type_extra_specs(volume_type)
        svm_name = extra_specs.get("arca_storage:svm_name")
        if isinstance(svm_name, str):
            svm_name = svm_name.strip()
        return svm_name or None

    def _retype_preserves_svm_mapping(self, volume, new_type) -> bool:
        """Return whether a retype keeps the volume on its create-time SVM."""
        if self.configuration.arca_storage_svm_strategy != "manual":
            return True

        current_svm = self._svm_name_from_volume_type(
            getattr(volume, "volume_type", None)
        )
        requested_svm = self._svm_name_from_volume_type(new_type)
        if not current_svm or not requested_svm:
            LOG.error(
                "Retype requires ARCA SVM extra specs for volume %s: "
                "current=%s requested=%s",
                getattr(volume, "name", "<unknown>"),
                current_svm,
                requested_svm,
            )
            return False

        if current_svm == requested_svm:
            return True

        LOG.error(
            "Retype would change ARCA SVM for volume %s: current=%s requested=%s",
            getattr(volume, "name", "<unknown>"),
            current_svm,
            requested_svm,
        )
        return False

    def _assert_file_snapshot_source_available(self, volume):
        """Reject file-copy snapshots when Cinder reports active attachments."""
        if not self._volume_has_active_attachments(volume):
            return
        volume_id = getattr(volume, "id", "<unknown>")
        msg = _(
            "Cannot create a file-backed snapshot for attached volume %s; "
            "detach the volume before snapshotting"
        ) % volume_id
        LOG.error(msg)
        raise exception.VolumeBackendAPIException(data=msg)

    @staticmethod
    def _volume_has_active_attachments(volume) -> bool:
        status = getattr(volume, "status", None)
        if isinstance(status, str) and status.lower() == "in-use":
            return True

        attach_status = getattr(volume, "attach_status", None)
        if isinstance(attach_status, str):
            normalized = attach_status.strip().lower()
            if normalized and normalized != "detached":
                return True

        for attr_name in ("volume_attachment", "attachments"):
            attachments = getattr(volume, attr_name, None)
            if isinstance(attachments, dict) and attachments:
                return True
            if isinstance(attachments, (list, tuple, set)) and len(attachments) > 0:
                return True
        return False

    def _cleanup_failed_volume(self, volume_name: str, cleanup_state: dict):
        """Cleanup resources after failed volume creation.

        Args:
            volume_name: Volume name
            cleanup_state: Dictionary tracking what was created
                - svm_name: SVM name (if known)
                - volume_file_created: Whether volume file was created
                - volume_file_path: Full path to volume file (if created)
        """
        svm_name = cleanup_state.get("svm_name")
        if not svm_name:
            LOG.warning("Cannot cleanup volume %s: SVM name unknown", volume_name)
            return

        LOG.info("Cleaning up failed volume creation: %s", volume_name)

        # Note: We do NOT unmount the SVM export as it may be in use by other volumes

        # Delete volume file if it was created
        if cleanup_state.get("volume_file_created"):
            volume_file_path = cleanup_state.get("volume_file_path")
            if volume_file_path:
                try:
                    import os
                    if os.path.exists(volume_file_path):
                        os.remove(volume_file_path)
                        LOG.info("Deleted volume file during cleanup: %s", volume_file_path)
                except Exception as e:
                    LOG.warning("Failed to delete volume file during cleanup: %s", e)

    # Snapshot operations

    def create_snapshot(self, snapshot):
        """Create a snapshot using file-based copy.

        This implementation uses cp --sparse=always to copy the volume file,
        preserving sparseness for efficiency. No API calls to ARCA Storage
        are needed since the XFS filesystem is already mounted via NFS.

        Args:
            snapshot: Cinder snapshot object

        Returns:
            Dictionary with snapshot metadata

        Raises:
            exception.VolumeBackendAPIException: If snapshot creation fails
        """
        snapshot_name = snapshot.name
        snapshot_id = snapshot.id
        # Use volume_id instead of volume object (may not be hydrated)
        volume_id = snapshot.volume_id

        LOG.info("Creating snapshot: %s (id=%s) for volume: %s", snapshot_name, snapshot_id, volume_id)

        mount_point = None
        try:
            # Get source volume to determine SVM
            # Note: In Cinder, snapshot.volume may not be hydrated
            # We use get_volume to fetch it explicitly
            context = self._get_operation_context(snapshot=snapshot)
            volume = self.db.volume_get(context, volume_id)
            self._assert_file_snapshot_source_available(volume)

            # Use the SVM recorded when the source volume was created.
            svm_name = self._get_existing_volume_svm(volume)

            export_path, mount_point = self._mount_svm_export(svm_name)

            # Source volume file path
            source_file = os.path.join(mount_point, f"volume-{volume_id}")

            # Snapshot file path (using snapshot ID, not snapshot name)
            snapshot_file = os.path.join(mount_point, f"snapshot-{snapshot_id}")

            # Get timeout from configuration
            copy_timeout = self.configuration.arca_storage_snapshot_copy_timeout

            # Copy volume file to snapshot file (preserving sparseness)
            arca_utils.copy_sparse_file(source_file, snapshot_file, timeout=copy_timeout)

            LOG.info("Created snapshot file: %s", snapshot_file)

            # Note: We do NOT unmount to avoid concurrency issues
            # The SVM export remains mounted for subsequent operations

            return self._snapshot_model_update(svm_name, export_path)

        except exception.VolumeBackendAPIException:
            raise
        except Exception as e:
            msg = _("Failed to create snapshot %s: %s") % (snapshot_name, e)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def delete_snapshot(self, snapshot):
        """Delete a snapshot file.

        This implementation deletes the snapshot file from the NFS export.
        No API calls to ARCA Storage are needed.

        Args:
            snapshot: Cinder snapshot object

        Raises:
            exception.VolumeBackendAPIException: If snapshot deletion fails
        """
        snapshot_name = snapshot.name
        snapshot_id = snapshot.id
        # Use volume_id instead of volume object (may not be hydrated)
        volume_id = snapshot.volume_id

        LOG.info("Deleting snapshot: %s (id=%s) for volume: %s", snapshot_name, snapshot_id, volume_id)

        mount_point = None
        try:
            svm_name, export_path = self._get_snapshot_storage(snapshot)
            _, mount_point = self._mount_svm_export_path(svm_name, export_path)

            # Snapshot file path (using snapshot ID)
            snapshot_file = os.path.join(mount_point, f"snapshot-{snapshot_id}")

            # Delete snapshot file
            if os.path.exists(snapshot_file):
                os.remove(snapshot_file)
                LOG.info("Deleted snapshot file: %s", snapshot_file)
            else:
                LOG.warning("Snapshot file %s not found, already deleted?", snapshot_file)

            # Note: We do NOT unmount to avoid concurrency issues

        except Exception as e:
            msg = _("Failed to delete snapshot %s: %s") % (snapshot_name, e)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Create a volume from a snapshot using file-based copy.

        This implementation copies the snapshot file to create a new volume file.
        No API calls to ARCA Storage are needed since we work directly with files.

        Args:
            volume: Cinder volume object (new volume)
            snapshot: Cinder snapshot object (source)

        Returns:
            Dictionary with volume metadata

        Raises:
            exception.VolumeBackendAPIException: If volume creation fails
        """
        volume_name = volume.name
        volume_size = volume.size
        volume_id = volume.id

        snapshot_name = snapshot.name
        snapshot_id = snapshot.id

        LOG.info(
            "Creating volume: %s (id=%s, size=%sGB) from snapshot: %s (id=%s)",
            volume_name,
            volume_id,
            volume_size,
            snapshot_name,
            snapshot_id,
        )

        self._apply_qos_to_volume(volume)

        volume_file = None
        volume_file_created = False
        try:
            source_svm_name, source_export_path = self._get_snapshot_storage(snapshot)
            target_svm_name = self._get_svm_for_volume(volume)
            self._ensure_copy_stays_within_svm(
                "create volume from snapshot",
                source_svm_name,
                target_svm_name,
            )
            source_export_path, source_mount_point = self._mount_svm_export_path(
                source_svm_name,
                source_export_path,
            )
            target_export_path = source_export_path
            target_mount_point = source_mount_point

            # Snapshot file path (using snapshot ID)
            snapshot_file = os.path.join(source_mount_point, f"snapshot-{snapshot_id}")

            # New volume file path (using volume ID)
            volume_file = os.path.join(target_mount_point, f"volume-{volume_id}")

            # Get timeout from configuration
            copy_timeout = self.configuration.arca_storage_snapshot_copy_timeout

            # Copy snapshot file to volume file (preserving sparseness)
            arca_utils.copy_sparse_file(snapshot_file, volume_file, timeout=copy_timeout)
            volume_file_created = True

            LOG.info("Created volume file from snapshot: %s -> %s", snapshot_file, volume_file)

            # Get snapshot file size to determine if extension is needed
            snapshot_size_bytes = os.path.getsize(snapshot_file)
            gib = 1024 ** 3
            snapshot_size_gib = (snapshot_size_bytes + gib - 1) // gib

            # If new volume size is larger than snapshot, extend the file
            if volume_size > snapshot_size_gib:
                arca_utils.extend_volume_file(
                    mount_point=target_mount_point,
                    volume_name=f"volume-{volume_id}",
                    new_size_gb=volume_size,
                )
                LOG.info("Extended volume file to %sGB", volume_size)

            # Note: We do NOT unmount to avoid concurrency issues

            return self._volume_model_update(target_svm_name, target_export_path)

        except Exception as e:
            if volume_file_created and volume_file:
                try:
                    os.remove(volume_file)
                    LOG.info("Deleted incomplete volume file after create-from-snapshot failure: %s", volume_file)
                except FileNotFoundError:
                    pass
                except Exception as cleanup_error:
                    LOG.warning("Failed to delete incomplete volume file %s: %s", volume_file, cleanup_error)
            msg = _("Failed to create volume from snapshot %s: %s") % (snapshot_name, e)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def create_cloned_volume(self, volume, src_vref):
        """Create a clone of a volume using file-based copy.

        This implementation directly copies the source volume file to create
        the cloned volume, using a single atomic copy operation.

        Args:
            volume: Cinder volume object (new volume)
            src_vref: Source Cinder volume object

        Returns:
            Dictionary with volume metadata

        Raises:
            exception.VolumeBackendAPIException: If clone creation fails
        """
        volume_name = volume.name
        volume_id = volume.id
        volume_size = volume.size

        src_volume_name = src_vref.name
        src_volume_id = src_vref.id
        src_volume_size = src_vref.size

        LOG.info("Creating cloned volume: %s (id=%s) from source: %s (id=%s)",
                 volume_name, volume_id, src_volume_name, src_volume_id)

        self._apply_qos_to_volume(volume)

        volume_file = None
        volume_file_created = False

        try:
            source_svm_name = self._get_existing_volume_svm(src_vref)
            target_svm_name = self._get_svm_for_volume(volume)
            self._ensure_copy_stays_within_svm(
                "clone",
                source_svm_name,
                target_svm_name,
            )
            source_export_path, source_mount_point = self._mount_svm_export(source_svm_name)
            target_export_path = source_export_path
            target_mount_point = source_mount_point

            # Source volume file path
            source_file = os.path.join(source_mount_point, f"volume-{src_volume_id}")

            # New volume file path
            volume_file = os.path.join(target_mount_point, f"volume-{volume_id}")

            # Get timeout from configuration
            copy_timeout = self.configuration.arca_storage_snapshot_copy_timeout

            # Directly copy source to volume (single atomic operation)
            arca_utils.copy_sparse_file(source_file, volume_file, timeout=copy_timeout)
            volume_file_created = True
            LOG.info("Created cloned volume file: %s", volume_file)

            # If new volume size is larger than source, extend the file
            if volume_size > src_volume_size:
                arca_utils.extend_volume_file(
                    mount_point=target_mount_point,
                    volume_name=f"volume-{volume_id}",
                    new_size_gb=volume_size,
                )
                LOG.info("Extended cloned volume file to %sGB", volume_size)

            # Note: We do NOT unmount to avoid concurrency issues

            return self._volume_model_update(target_svm_name, target_export_path)

        except Exception as e:
            if volume_file_created and volume_file:
                try:
                    os.remove(volume_file)
                    LOG.info("Deleted incomplete cloned volume file after failure: %s", volume_file)
                except FileNotFoundError:
                    pass
                except Exception as cleanup_error:
                    LOG.warning("Failed to delete incomplete cloned volume file %s: %s", volume_file, cleanup_error)
            msg = _("Failed to create cloned volume %s: %s") % (volume_name, e)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    # QoS operations

    def _get_qos_extra_specs(self, volume) -> Dict[str, Any]:
        """Return ARCA QoS extra_specs configured on the volume type."""
        volume_type = getattr(volume, "volume_type", None)
        if not volume_type:
            return {}

        extra_specs = self._get_volume_type_extra_specs(volume_type)
        return {
            key: extra_specs[key]
            for key in self._qos_extra_spec_keys
            if key in extra_specs
        }

    @staticmethod
    def _qos_value_is_configured(value) -> bool:
        """Return whether a Cinder QoS value represents configured QoS."""
        if value is None or value == "" or value == {} or value == []:
            return False
        # Avoid treating dynamic mapping accessors as real QoS values.
        return not callable(value)

    def _get_cinder_qos_specs(self, volume_type):
        """Return Cinder QoS specs associated with a volume type, if any."""
        if not volume_type:
            return None

        qos_specs = getattr(volume_type, "qos_specs", None)
        if self._qos_value_is_configured(qos_specs):
            return qos_specs

        qos_specs_id = getattr(volume_type, "qos_specs_id", None)
        if self._qos_value_is_configured(qos_specs_id):
            return qos_specs_id

        if isinstance(volume_type, dict):
            for key in ("qos_specs", "qos_specs_id"):
                value = volume_type.get(key)
                if self._qos_value_is_configured(value):
                    return value
            return None

        get_method = getattr(volume_type, "get", None)
        if callable(get_method):
            for key in ("qos_specs", "qos_specs_id"):
                value = get_method(key)
                if self._qos_value_is_configured(value):
                    return value

        return None

    def _get_qos_specs(self, volume) -> Dict[str, Any]:
        """Extract QoS specifications from volume type extra_specs.

        Args:
            volume: Cinder volume object

        Returns:
            Dictionary with QoS parameters:
                - read_iops: Read IOPS limit
                - write_iops: Write IOPS limit
                - read_bps: Read bandwidth limit in bytes/sec
                - write_bps: Write bandwidth limit in bytes/sec

        Note:
            Cinder volume type extra_specs can contain:
            - arca_storage:read_iops_sec
            - arca_storage:write_iops_sec
            - arca_storage:read_bytes_sec
            - arca_storage:write_bytes_sec
            - arca_storage:total_iops_sec (applied to both read and write)
        """
        qos_specs = {}

        if not volume.volume_type:
            return qos_specs

        try:
            extra_specs = self._get_volume_type_extra_specs(volume.volume_type)

            # Read IOPS
            if "arca_storage:read_iops_sec" in extra_specs:
                try:
                    qos_specs["read_iops"] = int(extra_specs["arca_storage:read_iops_sec"])
                except ValueError:
                    LOG.warning("Invalid read_iops_sec value: %s", extra_specs["arca_storage:read_iops_sec"])

            # Write IOPS
            if "arca_storage:write_iops_sec" in extra_specs:
                try:
                    qos_specs["write_iops"] = int(extra_specs["arca_storage:write_iops_sec"])
                except ValueError:
                    LOG.warning("Invalid write_iops_sec value: %s", extra_specs["arca_storage:write_iops_sec"])

            # Total IOPS (applies to both read and write if not specified)
            if "arca_storage:total_iops_sec" in extra_specs:
                try:
                    total_iops = int(extra_specs["arca_storage:total_iops_sec"])
                    if "read_iops" not in qos_specs:
                        qos_specs["read_iops"] = total_iops
                    if "write_iops" not in qos_specs:
                        qos_specs["write_iops"] = total_iops
                except ValueError:
                    LOG.warning("Invalid total_iops_sec value: %s", extra_specs["arca_storage:total_iops_sec"])

            # Read bandwidth
            if "arca_storage:read_bytes_sec" in extra_specs:
                try:
                    qos_specs["read_bps"] = int(extra_specs["arca_storage:read_bytes_sec"])
                except ValueError:
                    LOG.warning("Invalid read_bytes_sec value: %s", extra_specs["arca_storage:read_bytes_sec"])

            # Write bandwidth
            if "arca_storage:write_bytes_sec" in extra_specs:
                try:
                    qos_specs["write_bps"] = int(extra_specs["arca_storage:write_bytes_sec"])
                except ValueError:
                    LOG.warning("Invalid write_bytes_sec value: %s", extra_specs["arca_storage:write_bytes_sec"])

        except Exception as e:
            LOG.warning("Failed to extract QoS specs from volume type: %s", e)

        return qos_specs

    def _apply_qos_to_volume(self, volume) -> None:
        """Reject QoS settings for file-backed Cinder volumes.

        Args:
            volume: Cinder volume object
        """
        volume_name = volume.name

        volume_type = getattr(volume, "volume_type", None)
        qos_extra_specs = self._get_qos_extra_specs(volume)
        cinder_qos_specs = self._get_cinder_qos_specs(volume_type)
        if not qos_extra_specs and not cinder_qos_specs:
            LOG.debug("No QoS specs found for volume: %s", volume_name)
            return

        details = []
        if qos_extra_specs:
            details.append("extra_specs: %s" % ", ".join(sorted(qos_extra_specs)))
        if cinder_qos_specs:
            details.append("qos_specs")

        msg = _("ARCA Cinder file-backed volumes do not support QoS: %(details)s") % {
            "details": "; ".join(details)
        }
        LOG.error("%s (volume=%s)", msg, volume_name)
        raise exception.VolumeBackendAPIException(data=msg)

    def retype(
        self,
        context,
        volume,
        new_type,
        diff,
        host,
    ):
        """Change volume type (including QoS changes).

        This method is called when a volume's type is changed, which may include
        changes to QoS specifications.

        Args:
            context: Cinder context
            volume: Cinder volume object
            new_type: New volume type dictionary
            diff: Dictionary of differences between old and new type
            host: Target host information

        Returns:
            Tuple (changed, updates_dict):
                - changed: Boolean indicating if retype was successful
                - updates_dict: Dictionary with volume updates

        Raises:
            exception.VolumeBackendAPIException: If retype fails
        """
        self._context = context
        volume_name = volume.name

        LOG.info("Retyping volume: %s (new_type=%s)", volume_name, new_type["name"])

        try:
            # For ARCA Storage NFS driver, we mainly care about QoS changes
            # Other attributes (thin provisioning, etc.) are set at volume creation
            if not self._retype_preserves_svm_mapping(volume, new_type):
                return False, {}

            # Check if QoS specs changed
            if "qos_specs" in diff or "extra_specs" in diff:
                LOG.info("QoS specs changed for volume %s, handling QoS update", volume_name)

                # Extract new QoS specs from new type
                # We need to temporarily set volume.volume_type to new_type to extract specs
                old_volume_type = volume.volume_type

                try:
                    # Create a mock volume type object
                    class MockVolumeType:
                        def __init__(self, extra_specs, qos_specs=None, qos_specs_id=None):
                            self.extra_specs = extra_specs
                            self.qos_specs = qos_specs
                            self.qos_specs_id = qos_specs_id

                    new_extra_specs = new_type.get("extra_specs", {})
                    volume.volume_type = MockVolumeType(
                        new_extra_specs,
                        qos_specs=new_type.get("qos_specs"),
                        qos_specs_id=new_type.get("qos_specs_id"),
                    )

                    # Apply new QoS settings
                    self._apply_qos_to_volume(volume)

                finally:
                    # Restore original volume type
                    volume.volume_type = old_volume_type

            LOG.info("Retype completed for volume: %s", volume_name)

            return True, {}

        except Exception as e:
            msg = _("Failed to retype volume %s: %s") % (volume_name, e)
            LOG.error(msg)
            # Return False to indicate retype failed
            # Cinder will keep the volume at the old type
            return False, {}

    def _get_operation_context(self, volume=None, snapshot=None):
        """Best-effort context resolver for DB operations."""
        for obj in (snapshot, volume):
            if obj is None:
                continue
            ctx = getattr(obj, "context", None) or getattr(obj, "_context", None)
            if ctx is not None:
                return ctx

        if self._context is not None:
            return self._context

        raise exception.VolumeBackendAPIException(
            data=_("Context is not available for DB operation")
        )
