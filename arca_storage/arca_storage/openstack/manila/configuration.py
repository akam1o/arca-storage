"""Configuration options for ARCA Storage Manila Driver."""

try:
    from oslo_config import cfg

    _HAS_OSLO = True
except ImportError:
    # oslo.config is an optional dependency for OpenStack integration
    _HAS_OSLO = False
    cfg = None


# Configuration group name
CONF_GROUP = "arca_storage"


def _get_arca_manila_opts():
    """Get ARCA Storage Manila configuration options.

    Returns:
        List of oslo_config options

    Raises:
        ImportError: If oslo.config is not installed
    """
    if not _HAS_OSLO or cfg is None:
        raise ImportError(
            "oslo.config library is required for ARCA Storage Manila driver. "
            "Install it with: pip install oslo.config"
        )

    return [
        # API Configuration (required for Manila)
        cfg.BoolOpt(
            "arca_storage_use_api",
            default=True,
            help=(
                "Enable ARCA Storage REST API usage. Required for Manila "
                "as all operations (shares, snapshots, access rules, capacity) "
                "are done via API."
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
        # API Authentication Configuration
        cfg.StrOpt(
            "arca_storage_api_auth_type",
            default=None,
            choices=["token", "basic", "none"],
            help=(
                "API authentication type. Options: "
                "'token' (Bearer token), 'basic' (HTTP Basic Auth), 'none' (no auth)"
            ),
        ),
        cfg.StrOpt(
            "arca_storage_api_token",
            default=None,
            secret=True,
            help="API authentication token (for auth_type=token)",
        ),
        cfg.StrOpt(
            "arca_storage_api_username",
            default=None,
            help="API username (for auth_type=basic)",
        ),
        cfg.StrOpt(
            "arca_storage_api_password",
            default=None,
            secret=True,
            help="API password (for auth_type=basic)",
        ),
        cfg.StrOpt(
            "arca_storage_api_ca_bundle",
            default=None,
            help="Path to CA bundle file for SSL verification (optional)",
        ),
        cfg.StrOpt(
            "arca_storage_api_client_cert",
            default=None,
            help="Path to client certificate file for mTLS (optional)",
        ),
        cfg.StrOpt(
            "arca_storage_api_client_key",
            default=None,
            secret=True,
            help="Path to client private key file for mTLS (optional)",
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
                "'manual': Admin pre-creates SVMs, share type extra_specs specify SVM."
            ),
        ),
        cfg.StrOpt(
            "arca_storage_default_svm",
            default="manila_default",
            help="Default SVM name (used when svm_strategy=shared)",
        ),
        cfg.StrOpt(
            "arca_storage_svm_prefix",
            default="manila_",
            help="Prefix for auto-created SVM names (used when svm_strategy=per_project)",
        ),
        # New multi-pool configuration (recommended)
        cfg.MultiStrOpt(
            "arca_storage_per_project_ip_pools",
            default=[],
            help=(
                "List of IP pool and VLAN ID pairs for per_project SVM allocation. "
                "Format: '<ip_cidr>|<start_ip>-<end_ip>:<vlan_id>'. "
                "Projects are assigned to pools using round-robin allocation to avoid "
                "hash collisions. Multiple pools provide collision-free allocation. "
                "Examples: "
                "  arca_storage_per_project_ip_pools = 10.0.0.0/24|10.0.0.10-10.0.0.200:100 "
                "  arca_storage_per_project_ip_pools = 192.168.100.0/24|192.168.100.50-192.168.100.250:100 "
                "  arca_storage_per_project_ip_pools = 172.16.0.0/24|172.16.0.100-172.16.0.200:200 "
                "The CIDR provides subnet context (prefix length for IP configuration), "
                "while the IP range specifies exact addresses to use for projects. "
                "This avoids conflicts with gateway addresses and static IPs. "
                "Each pool supports up to (end_ip - start_ip + 1) projects. "
                "Total capacity is sum of all pools."
            ),
        ),
        cfg.IntOpt(
            "arca_storage_per_project_mtu",
            default=1500,
            min=68,
            max=9000,
            help="MTU size for per_project SVM network interfaces",
        ),
        cfg.IntOpt(
            "arca_storage_per_project_root_volume_size_gib",
            default=None,
            min=1,
            help=(
                "Optional root volume size in GiB for per_project SVMs. "
                "Used for Pacemaker filesystem resource. If not set, no root volume is created."
            ),
        ),
        # Network Plugin Configuration
        cfg.StrOpt(
            "arca_storage_network_plugin_mode",
            default="standalone",
            choices=["standalone", "neutron"],
            help=(
                "Network plugin mode for IP/VLAN allocation in per_project strategy. "
                "'standalone': Use static IP pools from arca_storage_per_project_ip_pools. "
                "'neutron': Allocate IPs via Neutron ports (requires [neutron] auth config)."
            ),
        ),
        cfg.ListOpt(
            "arca_storage_neutron_net_ids",
            default=[],
            help=(
                "List of Neutron network IDs for SVM port creation (neutron mode only). "
                "Each network must be a VLAN provider network with provider:segmentation_id. "
                "VXLAN/Geneve networks are not supported. "
                "Ports are allocated in round-robin fashion across these networks. "
                "The first subnet in each network is automatically used. "
                "Example: net-uuid-1,net-uuid-2,net-uuid-3"
            ),
        ),
        cfg.BoolOpt(
            "arca_storage_neutron_port_security",
            default=False,
            help=(
                "Enable port security for Neutron ports (neutron mode only). "
                "Typically False for data plane ports to avoid security group overhead."
            ),
        ),
        cfg.StrOpt(
            "arca_storage_neutron_vnic_type",
            default="normal",
            help=(
                "VNIC type for Neutron ports (neutron mode only). "
                "Options: 'normal', 'direct', 'macvtap', 'baremetal', etc. "
                "Requires Neutron binding extension support."
            ),
        ),
        # Capacity Configuration
        cfg.FloatOpt(
            "arca_storage_max_over_subscription_ratio",
            default=20.0,
            min=1.0,
            help=(
                "Maximum oversubscription ratio for thin provisioning. "
                "Allows allocating more logical capacity than physical capacity. "
                "Default 20.0 means you can provision up to 20x the physical capacity."
            ),
        ),
        cfg.IntOpt(
            "arca_storage_reserved_percentage",
            default=0,
            min=0,
            max=100,
            help="Percentage of backend capacity reserved and unavailable for scheduling",
        ),
        cfg.IntOpt(
            "arca_storage_reserved_share_percentage",
            default=0,
            min=0,
            max=100,
            help="Percentage of backend capacity reserved for share extend operations",
        ),
        cfg.IntOpt(
            "arca_storage_reserved_share_from_snapshot_percentage",
            default=0,
            min=0,
            max=100,
            help="Percentage of backend capacity reserved for creating shares from snapshots",
        ),
        # Snapshot Configuration
        cfg.BoolOpt(
            "arca_storage_snapshot_support",
            default=True,
            help="Enable snapshot support (uses ARCA backend LVM thin snapshots)",
        ),
        cfg.BoolOpt(
            "arca_storage_revert_to_snapshot_support",
            default=False,
            help="Enable revert to snapshot support (not implemented)",
        ),
        cfg.BoolOpt(
            "arca_storage_create_share_from_snapshot_support",
            default=True,
            help="Enable creating shares from snapshots (uses ARCA clone API)",
        ),
        cfg.BoolOpt(
            "arca_storage_mount_snapshot_support",
            default=False,
            help="Enable mounting snapshots as read-only shares (not implemented)",
        ),
    ]


def register_opts(conf, group=None):
    """Register ARCA Storage Manila configuration options.

    Args:
        conf: oslo_config.cfg.ConfigOpts instance
        group: Configuration group name (default: CONF_GROUP)

    Raises:
        ImportError: If oslo.config is not installed
    """
    opts = _get_arca_manila_opts()
    if group is None:
        group = CONF_GROUP

    # Register options in the specified group
    conf.register_opts(opts, group=group)
    # Register with deprecated_group for migration support
    for opt in opts:
        opt.deprecated_group = "DEFAULT"


def list_opts():
    """Return a list of ARCA Storage Manila options for oslo-config-generator.

    This is used by oslo-config-generator to generate sample config files.

    Returns:
        List of (group_name, options) tuples

    Raises:
        ImportError: If oslo.config is not installed
    """
    opts = _get_arca_manila_opts()
    return [
        (CONF_GROUP, opts),
    ]


def get_arca_manila_opts():
    """Get ARCA Storage Manila configuration options (public API).

    This function can be called at any time to get the configuration options,
    allowing for lazy evaluation.

    Returns:
        List of oslo_config options

    Raises:
        ImportError: If oslo.config is not installed
    """
    return _get_arca_manila_opts()
