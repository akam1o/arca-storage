"""
Unit tests for pacemaker module.
"""

from unittest.mock import MagicMock

import pytest

from arca_storage.cli.lib.pacemaker import create_group


@pytest.mark.unit
def test_create_group_creates_missing_resources(mock_subprocess):
    # Simulate: group/fs/netns/ganesha don't exist initially, all creates succeed.
    mock_subprocess.side_effect = [
        MagicMock(returncode=1),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=1),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource create p_drbd_r0
        MagicMock(returncode=1),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource master ms_drbd_r0 p_drbd_r0 ...
        MagicMock(returncode=1),  # pcs resource show fs_tenant_a
        MagicMock(returncode=0),  # pcs resource create fs_tenant_a
        MagicMock(returncode=1),  # pcs resource show netns_tenant_a
        MagicMock(returncode=0),  # pcs resource create netns_tenant_a
        MagicMock(returncode=1),  # pcs resource show ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource create ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource group add g_svm_tenant_a ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint show --full
        MagicMock(returncode=0),  # pcs constraint order ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint show --full
        MagicMock(returncode=0),  # pcs constraint colocation add ...
    ]

    create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=100,
        ifname="v100-tenantxxxx",
        ip="192.168.10.5",
        prefix=24,
        gw="192.168.10.1",
        mtu=9000,
        parent_if="bond0",
        vg_name="vg_pool_01",
    )

    # Ensure we attempted to create NetnsVlan with expected args.
    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    assert any(cmd[:5] == ["pcs", "resource", "create", "netns_tenant_a", "ocf:local:NetnsVlan"] for cmd in calls)
    assert any("vlan_id=100" in cmd for cmd in calls if isinstance(cmd, list))
    assert any("ifname=v100-tenantxxxx" in cmd for cmd in calls if isinstance(cmd, list))
