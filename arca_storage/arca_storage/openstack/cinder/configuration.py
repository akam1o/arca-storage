"""Configuration options for ARCA Storage Cinder Driver."""

try:
    from oslo_config import cfg

    _HAS_OSLO = True
except ImportError:
    # oslo.config is an optional dependency for OpenStack integration
    _HAS_OSLO = False
    cfg = None


# Configuration group name
CONF_GROUP = "arca_storage"


def _get_arca_storage_opts():
    """Get ARCA Storage configuration options.

    Returns:
        List of oslo_config options

    Raises:
        ImportError: If oslo.config is not installed
    """
    if not _HAS_OSLO or cfg is None:
        raise ImportError(
            "oslo.config library is required for ARCA Storage Cinder driver. "
            "Install it with: pip install oslo.config"
        )

    return [
        # API Configuration (optional)
        cfg.BoolOpt(
            "arca_storage_use_api",
            default=False,
            help=(
                "Enable ARCA Storage REST API usage for SVM discovery and optional "
                "features. When False, the driver operates as a pure NFS/file backend."
            ),
        ),
        cfg.StrOpt(
            "arca_storage_api_endpoint",
            default=None,
            help="ARCA Storage REST API endpoint URL (e.g., http://192.168.10.5:8080)",
        ),
        cfg.IntOpt(
            "arca_storage_api_timeout",
            default=30,
            min=1,
            max=300,
            help="API request timeout in seconds",
        ),
        cfg.IntOpt(
            "arca_storage_api_retry_count",
            default=3,
            min=0,
            max=10,
            help="Number of API request retries for transient failures",
        ),
        cfg.BoolOpt(
            "arca_storage_verify_ssl",
            default=True,
            help="Verify SSL certificates for API requests",
        ),
        # Multi-tenancy Configuration
        cfg.StrOpt(
            "arca_storage_svm_strategy",
            default="shared",
            choices=["shared", "per_project", "manual"],
            help=(
                "Strategy for mapping OpenStack projects to ARCA SVMs. "
                "'shared': All projects use default_svm. "
                "'per_project': Each project gets dedicated SVM (auto-created). "
                "'manual': Admin pre-creates SVMs, volume type extra_specs specify SVM."
            ),
        ),
        cfg.StrOpt(
            "arca_storage_default_svm",
            default="default_svm",
            help="Default SVM name (used when svm_strategy=shared)",
        ),
        cfg.StrOpt(
            "arca_storage_svm_prefix",
            default="cinder_",
            help="Prefix for auto-created SVM names (used when svm_strategy=per_project)",
        ),
        # NFS Configuration
        cfg.StrOpt(
            "arca_storage_nfs_server",
            default=None,
            help=(
                "NFS server (IP/hostname) that exports /exports/<svm>. "
                "Required when arca_storage_use_api is False."
            ),
        ),
        cfg.StrOpt(
            "arca_storage_nfs_mount_options",
            default="rw,noatime,nodiratime,vers=4.1",
            help="NFS mount options for volume mounts",
        ),
        cfg.StrOpt(
            "arca_storage_nfs_mount_point_base",
            default="/var/lib/cinder/mnt",
            help="Base directory for NFS volume mounts",
        ),
        # Storage Configuration
        cfg.BoolOpt(
            "arca_storage_thin_provisioning",
            default=True,
            help="Use thin provisioning for volumes (unused in pure NFS/file mode)",
        ),
        cfg.FloatOpt(
            "arca_storage_max_over_subscription_ratio",
            default=20.0,
            min=1.0,
            help=(
                "Maximum oversubscription ratio for thin provisioning. "
                "Allows allocating more logical capacity than physical capacity."
            ),
        ),
        # Compute Node Access Configuration
        cfg.StrOpt(
            "arca_storage_client_cidr",
            default=None,
            help=(
                "CIDR for OpenStack compute nodes that need NFS access "
                "(e.g., 10.0.0.0/16)."
            ),
        ),
        # Snapshot/Clone Configuration
        cfg.IntOpt(
            "arca_storage_snapshot_copy_timeout",
            default=600,
            min=60,
            max=7200,
            help=(
                "Timeout in seconds for snapshot/clone file copy operations. "
                "Increase this value for large volumes. Default: 600 (10 minutes)."
            ),
        ),
        # Driver Information
        cfg.StrOpt(
            "arca_storage_driver_ssl_cert_path",
            default=None,
            help="Path to SSL certificate file for API authentication (optional)",
        ),
        cfg.StrOpt(
            "arca_storage_volume_backend_name",
            default="arca_storage",
            help="Volume backend name for Cinder multi-backend support",
        ),
    ]


def register_opts(conf, group=None):
    """Register ARCA Storage configuration options.

    Args:
        conf: oslo_config.cfg.ConfigOpts instance
        group: Configuration group name (default: CONF_GROUP)

    Raises:
        ImportError: If oslo.config is not installed
    """
    opts = _get_arca_storage_opts()
    if group is None:
        group = CONF_GROUP

    # Register options in the specified group
    # Also register in DEFAULT for backward compatibility
    conf.register_opts(opts, group=group)
    # Register with deprecated_group for migration support
    for opt in opts:
        opt.deprecated_group = "DEFAULT"


def list_opts():
    """Return a list of ARCA Storage options for oslo-config-generator.

    This is used by oslo-config-generator to generate sample config files.

    Returns:
        List of (group_name, options) tuples

    Raises:
        ImportError: If oslo.config is not installed
    """
    opts = _get_arca_storage_opts()
    return [
        (CONF_GROUP, opts),
    ]


def get_arca_storage_opts():
    """Get ARCA Storage configuration options (public API).

    This function can be called at any time to get the configuration options,
    allowing for lazy evaluation.

    Returns:
        List of oslo_config options

    Raises:
        ImportError: If oslo.config is not installed
    """
    return _get_arca_storage_opts()
