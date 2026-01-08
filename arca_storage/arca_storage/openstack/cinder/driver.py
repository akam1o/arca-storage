"""ARCA Storage Cinder Volume Driver.

This driver provides OpenStack Cinder integration for ARCA Storage
using NFS as the transport protocol.
"""

import os
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

        try:
            # Initialize ARCA Storage API client if enabled
            if self.configuration.arca_storage_use_api:
                if not self.configuration.arca_storage_api_endpoint:
                    raise exception.VolumeBackendAPIException(
                        data=_("arca_storage_api_endpoint must be set when arca_storage_use_api is True")
                    )
                self.arca_client = arca_client.ArcaStorageClient(
                    api_endpoint=self.configuration.arca_storage_api_endpoint,
                    timeout=self.configuration.arca_storage_api_timeout,
                    retry_count=self.configuration.arca_storage_api_retry_count,
                    verify_ssl=self.configuration.arca_storage_verify_ssl,
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

        # Track cleanup state
        cleanup_state = {
            "svm_name": None,
            "volume_file_created": False,
            "volume_file_path": None,  # Track full path for cleanup
        }

        try:
            # Determine SVM for this volume
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

            # Create volume file (raw sparse file) using volume ID for unique naming
            volume_file = arca_utils.create_volume_file(
                mount_point=mount_point,
                volume_name=f"volume-{volume_id}",  # Use volume ID, not name
                size_gb=volume_size,
            )
            cleanup_state["volume_file_created"] = True
            cleanup_state["volume_file_path"] = volume_file

            LOG.info("Created volume file: %s", volume_file)

            # Apply QoS if specified in volume type
            self._apply_qos_to_volume(volume)

            # Store provider location (per-SVM export path)
            # Note: All volumes in same SVM share this export
            provider_location = export_path

            return {"provider_location": provider_location}

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
            # Determine SVM for this volume
            svm_name = self._get_svm_for_volume(volume)

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
            # Determine SVM for this volume
            svm_name = self._get_svm_for_volume(volume)

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
                svm_name = self._get_svm_for_volume(volume)
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
            # Capacity (these would need real values from ARCA Storage)
            "total_capacity_gb": "unknown",
            "free_capacity_gb": "unknown",
            "reserved_percentage": self.configuration.reserved_percentage,
            "max_over_subscription_ratio": self.configuration.arca_storage_max_over_subscription_ratio,
        }

        self._stats = data

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
                extra_specs = self._get_volume_type_extra_specs(volume.volume_type)
                svm_name = extra_specs.get("arca_storage:svm_name")
                if svm_name:
                    return svm_name

            msg = _(
                "SVM strategy is 'manual' but volume type does not specify "
                "'arca_storage:svm_name' extra_spec"
            )
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        elif strategy == "per_project":
            # Each project gets dedicated SVM
            # Note: This requires SVM auto-creation which is not implemented yet
            project_id = volume.project_id
            svm_name = f"{self.configuration.arca_storage_svm_prefix}{project_id}"

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

    def _get_svm_info(self, svm_name: str) -> Dict[str, Any]:
        """Get SVM information with caching.

        Args:
            svm_name: SVM name

        Returns:
            SVM information dictionary

        Raises:
            arca_exceptions.ArcaSVMNotFound: If SVM not found
        """
        # Check cache first
        if svm_name in self._svm_cache:
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

    def _get_export_path(self, svm_name: str) -> str:
        """Resolve NFS export path for an SVM.

        Preference order:
          1) Explicit `arca_storage_nfs_server` + default export layout.
          2) ARCA API (SVM vip) if `arca_storage_use_api=True`.
        """
        if getattr(self.configuration, "arca_storage_nfs_server", None):
            return f"{self.configuration.arca_storage_nfs_server}:/exports/{svm_name}"

        if self.configuration.arca_storage_use_api:
            svm_info = self._get_svm_info(svm_name)
            svm_vip = svm_info["vip"]
            return f"{svm_vip}:/exports/{svm_name}"

        raise exception.VolumeBackendAPIException(
            data=_(
                "Unable to determine NFS export path: set arca_storage_nfs_server "
                "or enable arca_storage_use_api"
            )
        )

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

            # Determine SVM for this volume
            svm_name = self._get_svm_for_volume(volume)

            # Get NFS export path (per-SVM, not per-volume)
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

            return {}  # Cinder expects empty dict for snapshot creation

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
            # Get source volume to determine SVM
            context = self._get_operation_context(snapshot=snapshot)
            volume = self.db.volume_get(context, volume_id)

            # Determine SVM for this volume
            svm_name = self._get_svm_for_volume(volume)

            # Get NFS export path (per-SVM, not per-volume)
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
        source_volume_id = snapshot.volume_id

        LOG.info(
            "Creating volume: %s (id=%s, size=%sGB) from snapshot: %s (id=%s)",
            volume_name,
            volume_id,
            volume_size,
            snapshot_name,
            snapshot_id,
        )

        mount_point = None
        try:
            # Get source volume to determine SVM
            # IMPORTANT: Use snapshot's volume, not the new volume
            context = self._get_operation_context(volume=volume, snapshot=snapshot)
            source_volume = self.db.volume_get(context, source_volume_id)

            # Determine SVM from SOURCE volume (where snapshot resides)
            svm_name = self._get_svm_for_volume(source_volume)

            # Get NFS export path (per-SVM, not per-volume)
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

            # Snapshot file path (using snapshot ID)
            snapshot_file = os.path.join(mount_point, f"snapshot-{snapshot_id}")

            # New volume file path (using volume ID)
            volume_file = os.path.join(mount_point, f"volume-{volume_id}")

            # Get timeout from configuration
            copy_timeout = self.configuration.arca_storage_snapshot_copy_timeout

            # Copy snapshot file to volume file (preserving sparseness)
            arca_utils.copy_sparse_file(snapshot_file, volume_file, timeout=copy_timeout)

            LOG.info("Created volume file from snapshot: %s -> %s", snapshot_file, volume_file)

            # Get snapshot file size to determine if extension is needed
            snapshot_size_bytes = os.path.getsize(snapshot_file)
            gib = 1024 ** 3
            snapshot_size_gib = (snapshot_size_bytes + gib - 1) // gib

            # If new volume size is larger than snapshot, extend the file
            if volume_size > snapshot_size_gib:
                arca_utils.extend_volume_file(
                    mount_point=mount_point,
                    volume_name=f"volume-{volume_id}",
                    new_size_gb=volume_size,
                )
                LOG.info("Extended volume file to %sGB", volume_size)

            # Store provider location (export path)
            provider_location = export_path

            # Note: We do NOT unmount to avoid concurrency issues

            return {"provider_location": provider_location}

        except Exception as e:
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

        mount_point = None

        try:
            # Determine SVM for source volume
            svm_name = self._get_svm_for_volume(src_vref)

            # Get NFS export path (per-SVM, not per-volume)
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

            # Source volume file path
            source_file = os.path.join(mount_point, f"volume-{src_volume_id}")

            # New volume file path
            volume_file = os.path.join(mount_point, f"volume-{volume_id}")

            # Get timeout from configuration
            copy_timeout = self.configuration.arca_storage_snapshot_copy_timeout

            # Directly copy source to volume (single atomic operation)
            arca_utils.copy_sparse_file(source_file, volume_file, timeout=copy_timeout)
            LOG.info("Created cloned volume file: %s", volume_file)

            # If new volume size is larger than source, extend the file
            if volume_size > src_volume_size:
                arca_utils.extend_volume_file(
                    mount_point=mount_point,
                    volume_name=f"volume-{volume_id}",
                    new_size_gb=volume_size,
                )
                LOG.info("Extended cloned volume file to %sGB", volume_size)

            # Store provider location (export path)
            provider_location = export_path

            # Note: We do NOT unmount to avoid concurrency issues

            return {"provider_location": provider_location}

        except Exception as e:
            msg = _("Failed to create cloned volume %s: %s") % (volume_name, e)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    # QoS operations

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
        """Apply QoS settings from volume type to a volume.

        Args:
            volume: Cinder volume object

        Raises:
            exception.VolumeBackendAPIException: If QoS application fails
        """
        volume_name = volume.name

        try:
            if self.arca_client is None or not hasattr(self.arca_client, "apply_qos"):
                LOG.debug("QoS API is not available; skipping QoS apply for %s", volume_name)
                return

            # Extract QoS specs from volume type
            qos_specs = self._get_qos_specs(volume)

            if not qos_specs:
                LOG.debug("No QoS specs found for volume: %s", volume_name)
                return

            # Determine SVM for this volume
            svm_name = self._get_svm_for_volume(volume)

            # Apply QoS via ARCA Storage API
            LOG.info("Applying QoS to volume %s: %s", volume_name, qos_specs)

            self.arca_client.apply_qos(
                volume=volume_name,
                svm=svm_name,
                read_iops=qos_specs.get("read_iops"),
                write_iops=qos_specs.get("write_iops"),
                read_bps=qos_specs.get("read_bps"),
                write_bps=qos_specs.get("write_bps"),
            )

            LOG.info("Applied QoS to volume: %s", volume_name)

        except Exception as e:
            # QoS application is not critical, log warning and continue
            LOG.warning("Failed to apply QoS to volume %s: %s", volume_name, e)

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

            # Check if QoS specs changed
            if "qos_specs" in diff or "extra_specs" in diff:
                LOG.info("QoS specs changed for volume %s, reapplying QoS", volume_name)

                # Extract new QoS specs from new type
                # We need to temporarily set volume.volume_type to new_type to extract specs
                old_volume_type = volume.volume_type

                try:
                    # Create a mock volume type object
                    class MockVolumeType:
                        def __init__(self, extra_specs):
                            self.extra_specs = extra_specs

                    new_extra_specs = new_type.get("extra_specs", {})
                    volume.volume_type = MockVolumeType(new_extra_specs)

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
