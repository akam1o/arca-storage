"""
Unit tests for pacemaker module.
"""

from unittest.mock import MagicMock

import pytest

from arca_storage.adapters.pacemaker import (
    FakePacemakerAdapter,
    SubprocessPacemakerAdapter,
    _parse_group_members as parse_adapter_group_members,
)
from arca_storage.cli.lib.pacemaker import (
    create_group,
    _parse_group_members as parse_cli_group_members,
)


def _assert_redacted(error: BaseException, *values: str) -> None:
    rendered = str(error)
    for value in values:
        assert value not in rendered


@pytest.mark.unit
@pytest.mark.parametrize(
    "parse_members", [parse_adapter_group_members, parse_cli_group_members]
)
def test_parse_group_members_handles_detailed_pcs_output(parse_members):
    text = """
Group: g_svm_tenant_a
  Resource: fs_tenant_a (class=ocf provider=heartbeat type=Filesystem)
    Attributes: device=/dev/vg_pool_01/vol_tenant_a directory=/exports/tenant_a fstype=xfs
    Operations: monitor interval=10s
  Resource: netns_tenant_a (class=ocf provider=local type=NetnsVlan)
  Resource: ganesha_tenant_a (class=systemd type=nfs-ganesha@tenant_a)
"""

    assert parse_members("g_svm_tenant_a", text) == [
        "fs_tenant_a",
        "netns_tenant_a",
        "ganesha_tenant_a",
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "parse_members", [parse_adapter_group_members, parse_cli_group_members]
)
def test_parse_group_members_handles_resource_group_status_output(parse_members):
    text = """
  * Resource Group: g_svm_tenant_a:
    * fs_tenant_a      (ocf:heartbeat:Filesystem):   Started node1
    * netns_tenant_a   (ocf:local:NetnsVlan):        Started node1
    * ganesha_tenant_a (systemd:nfs-ganesha@tenant_a): Started node1
"""

    assert parse_members("g_svm_tenant_a", text) == [
        "fs_tenant_a",
        "netns_tenant_a",
        "ganesha_tenant_a",
    ]


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
        filesystem_lv_name="svmroot-tenant_a-abc123",
    )

    # Ensure we attempted to create NetnsVlan with expected args.
    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    assert any(
        "device=/dev/vg_pool_01/svmroot-tenant_a-abc123" in cmd
        for cmd in calls
        if isinstance(cmd, list)
    )
    assert any(
        cmd[:5]
        == ["pcs", "resource", "create", "netns_tenant_a", "ocf:local:NetnsVlan"]
        for cmd in calls
    )
    assert any("vlan_id=100" in cmd for cmd in calls if isinstance(cmd, list))
    assert any(
        "ifname=v100-tenantxxxx" in cmd for cmd in calls if isinstance(cmd, list)
    )


@pytest.mark.unit
def test_create_group_failure_redacts_pcs_stderr_and_arguments(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=1),  # pcs resource show g_svm_tenant_secret
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=1),  # pcs resource show fs_tenant_secret
        MagicMock(
            returncode=1,
            stderr="secret-token /dev/vg_pool_01/secret-lv /exports/tenant_secret",
        ),
    ]

    with pytest.raises(
        RuntimeError, match="Failed to create Filesystem resource"
    ) as exc_info:
        create_group(
            "tenant_secret",
            "/exports/tenant_secret",
            vlan_id=None,
            ip="192.168.10.5",
            prefix=32,
            gw=None,
            parent_if="bond0",
            vg_name="vg_pool_01",
            filesystem_lv_name="secret-lv",
        )

    _assert_redacted(
        exc_info.value,
        "secret-token",
        "/dev/vg_pool_01/secret-lv",
        "/exports/tenant_secret",
        "tenant_secret",
    )


@pytest.mark.unit
def test_create_group_updates_existing_filesystem_device(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=1),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
        MagicMock(
            returncode=0,
            stdout="Attributes: device=/dev/vg_pool_01/vol_tenant_a directory=/exports/tenant_a fstype=xfs",
            stderr="",
        ),  # pcs resource config fs_tenant_a
        MagicMock(returncode=0),  # pcs resource update fs_tenant_a ...
        MagicMock(returncode=1),  # pcs resource show ip_tenant_a
        MagicMock(returncode=0),  # pcs resource create ip_tenant_a
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
        vlan_id=None,
        ip="192.168.10.5",
        prefix=32,
        gw=None,
        parent_if="bond0",
        vg_name="vg_pool_01",
        filesystem_lv_name="svmroot-tenant_a-abc123",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    assert [
        "pcs",
        "resource",
        "update",
        "fs_tenant_a",
        "device=/dev/vg_pool_01/svmroot-tenant_a-abc123",
    ] in calls
    assert not any(
        cmd[:5]
        == ["pcs", "resource", "create", "fs_tenant_a", "ocf:heartbeat:Filesystem"]
        for cmd in calls
    )


@pytest.mark.unit
def test_create_group_without_vlan_creates_ipaddr2_and_host_ganesha(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=1),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=1),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource create p_drbd_r0
        MagicMock(returncode=1),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource master ms_drbd_r0 p_drbd_r0 ...
        MagicMock(returncode=1),  # pcs resource show ip_tenant_a
        MagicMock(returncode=0),  # pcs resource create ip_tenant_a
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
        vlan_id=None,
        ip="192.168.10.5",
        prefix=32,
        gw=None,
        parent_if="bond0",
        vg_name="vg_pool_01",
        create_filesystem=False,
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    assert any(
        cmd[:5] == ["pcs", "resource", "create", "ip_tenant_a", "ocf:heartbeat:IPaddr2"]
        for cmd in calls
    )
    assert any("cidr_netmask=32" in cmd for cmd in calls if isinstance(cmd, list))
    assert any(
        "systemd:nfs-ganesha-host@tenant_a" in cmd
        for cmd in calls
        if isinstance(cmd, list)
    )


@pytest.mark.unit
def test_create_group_includes_existing_filesystem_on_retry(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=1),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
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
        parent_if="bond0",
        vg_name="vg_pool_01",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    group_add = next(
        cmd for cmd in calls if cmd[:4] == ["pcs", "resource", "group", "add"]
    )
    assert group_add == [
        "pcs",
        "resource",
        "group",
        "add",
        "g_svm_tenant_a",
        "fs_tenant_a",
        "netns_tenant_a",
        "ganesha_tenant_a",
    ]
    assert [
        "pcs",
        "constraint",
        "order",
        "ms_drbd_r0:promote",
        "fs_tenant_a:start",
    ] in calls


@pytest.mark.unit
def test_create_group_repairs_constraints_when_group_exists(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
        MagicMock(returncode=0),  # pcs resource show netns_tenant_a
        MagicMock(returncode=0),  # pcs resource show ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="g_svm_tenant_a: fs_tenant_a netns_tenant_a ganesha_tenant_a\n",
            stderr="",
        ),
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
        parent_if="bond0",
        vg_name="vg_pool_01",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    assert not any(cmd[:4] == ["pcs", "resource", "group", "add"] for cmd in calls)
    assert [
        "pcs",
        "constraint",
        "order",
        "ms_drbd_r0:promote",
        "fs_tenant_a:start",
    ] in calls
    assert [
        "pcs",
        "constraint",
        "colocation",
        "add",
        "g_svm_tenant_a",
        "with",
        "ms_drbd_r0:Master",
    ] in calls


@pytest.mark.unit
def test_create_group_repairs_missing_members_when_group_exists(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
        MagicMock(returncode=1),  # pcs resource show netns_tenant_a
        MagicMock(returncode=0),  # pcs resource create netns_tenant_a
        MagicMock(returncode=0),  # pcs resource show ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="g_svm_tenant_a: fs_tenant_a ganesha_tenant_a\n",
            stderr="",
        ),
        MagicMock(
            returncode=0
        ),  # pcs resource group add g_svm_tenant_a netns_tenant_a --before ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="order ms_drbd_r0:promote fs_tenant_a:start\n",
            stderr="",
        ),
        MagicMock(
            returncode=0,
            stdout="colocation g_svm_tenant_a with ms_drbd_r0:Master\n",
            stderr="",
        ),
    ]

    create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=100,
        ifname="v100-tenantxxxx",
        ip="192.168.10.5",
        prefix=24,
        gw="192.168.10.1",
        parent_if="bond0",
        vg_name="vg_pool_01",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    assert any(
        cmd[:5]
        == ["pcs", "resource", "create", "netns_tenant_a", "ocf:local:NetnsVlan"]
        for cmd in calls
    )
    assert [
        "pcs",
        "resource",
        "group",
        "add",
        "g_svm_tenant_a",
        "netns_tenant_a",
        "--before",
        "ganesha_tenant_a",
    ] in calls


@pytest.mark.unit
def test_create_group_reorders_existing_members_when_group_exists(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
        MagicMock(returncode=0),  # pcs resource show netns_tenant_a
        MagicMock(returncode=0),  # pcs resource show ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="g_svm_tenant_a: ganesha_tenant_a fs_tenant_a netns_tenant_a\n",
            stderr="",
        ),
        MagicMock(
            returncode=0
        ),  # pcs resource group add g_svm_tenant_a ganesha_tenant_a --after netns_tenant_a
        MagicMock(
            returncode=0,
            stdout="order ms_drbd_r0:promote fs_tenant_a:start\n",
            stderr="",
        ),
        MagicMock(
            returncode=0,
            stdout="colocation g_svm_tenant_a with ms_drbd_r0:Master\n",
            stderr="",
        ),
    ]

    create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=100,
        ifname="v100-tenantxxxx",
        ip="192.168.10.5",
        prefix=24,
        gw="192.168.10.1",
        parent_if="bond0",
        vg_name="vg_pool_01",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    assert [
        "pcs",
        "resource",
        "group",
        "add",
        "g_svm_tenant_a",
        "ganesha_tenant_a",
        "--after",
        "netns_tenant_a",
    ] in calls


@pytest.mark.unit
def test_create_group_reorders_first_member_when_group_exists(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
        MagicMock(returncode=1),  # pcs resource show netns_tenant_a
        MagicMock(returncode=0),  # pcs resource create netns_tenant_a
        MagicMock(returncode=0),  # pcs resource show ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="g_svm_tenant_a: ganesha_tenant_a fs_tenant_a\n",
            stderr="",
        ),
        MagicMock(
            returncode=0
        ),  # pcs resource group add g_svm_tenant_a fs_tenant_a --before ganesha_tenant_a
        MagicMock(
            returncode=0
        ),  # pcs resource group add g_svm_tenant_a netns_tenant_a --before ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="order ms_drbd_r0:promote fs_tenant_a:start\n",
            stderr="",
        ),
        MagicMock(
            returncode=0,
            stdout="colocation g_svm_tenant_a with ms_drbd_r0:Master\n",
            stderr="",
        ),
    ]

    create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=100,
        ifname="v100-tenantxxxx",
        ip="192.168.10.5",
        prefix=24,
        gw="192.168.10.1",
        parent_if="bond0",
        vg_name="vg_pool_01",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    fs_repair = [
        "pcs",
        "resource",
        "group",
        "add",
        "g_svm_tenant_a",
        "fs_tenant_a",
        "--before",
        "ganesha_tenant_a",
    ]
    netns_repair = [
        "pcs",
        "resource",
        "group",
        "add",
        "g_svm_tenant_a",
        "netns_tenant_a",
        "--before",
        "ganesha_tenant_a",
    ]
    assert fs_repair in calls
    assert netns_repair in calls
    assert calls.index(fs_repair) < calls.index(netns_repair)


@pytest.mark.unit
def test_subprocess_adapter_uses_configured_filesystem_lv_name(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=1),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=1),  # pcs resource show fs_tenant_a
        MagicMock(returncode=0),  # pcs resource create fs_tenant_a
        MagicMock(returncode=1),  # pcs resource show ip_tenant_a
        MagicMock(returncode=0),  # pcs resource create ip_tenant_a
        MagicMock(returncode=1),  # pcs resource show ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource create ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource group add g_svm_tenant_a ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint show --full
        MagicMock(returncode=0),  # pcs constraint order ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint show --full
        MagicMock(returncode=0),  # pcs constraint colocation add ...
    ]

    SubprocessPacemakerAdapter().create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=None,
        ip="192.168.10.5",
        prefix=32,
        gw=None,
        parent_if="bond0",
        vg_name="vg_pool_01",
        filesystem_lv_name="svmroot-tenant_a-abc123",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    fs_create = next(
        cmd
        for cmd in calls
        if cmd[:5]
        == ["pcs", "resource", "create", "fs_tenant_a", "ocf:heartbeat:Filesystem"]
    )
    assert "device=/dev/vg_pool_01/svmroot-tenant_a-abc123" in fs_create


@pytest.mark.unit
def test_subprocess_adapter_updates_existing_filesystem_device(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=1),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
        MagicMock(
            returncode=0,
            stdout="Attributes: device=/dev/vg_pool_01/vol_tenant_a directory=/exports/tenant_a fstype=xfs",
            stderr="",
        ),  # pcs resource config fs_tenant_a
        MagicMock(returncode=0),  # pcs resource update fs_tenant_a ...
        MagicMock(returncode=1),  # pcs resource show ip_tenant_a
        MagicMock(returncode=0),  # pcs resource create ip_tenant_a
        MagicMock(returncode=1),  # pcs resource show ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource create ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource group add g_svm_tenant_a ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint show --full
        MagicMock(returncode=0),  # pcs constraint order ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint show --full
        MagicMock(returncode=0),  # pcs constraint colocation add ...
    ]

    SubprocessPacemakerAdapter().create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=None,
        ip="192.168.10.5",
        prefix=32,
        gw=None,
        parent_if="bond0",
        vg_name="vg_pool_01",
        filesystem_lv_name="svmroot-tenant_a-abc123",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    assert [
        "pcs",
        "resource",
        "update",
        "fs_tenant_a",
        "device=/dev/vg_pool_01/svmroot-tenant_a-abc123",
    ] in calls
    assert not any(
        cmd[:5]
        == ["pcs", "resource", "create", "fs_tenant_a", "ocf:heartbeat:Filesystem"]
        for cmd in calls
    )


@pytest.mark.unit
def test_fake_adapter_updates_existing_filesystem_device():
    adapter = FakePacemakerAdapter()
    adapter.resources["fs_tenant_a"] = {
        "type": "Filesystem",
        "device": "/dev/vg_pool_01/vol_tenant_a",
    }

    adapter.create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=None,
        ip="192.168.10.5",
        prefix=32,
        gw=None,
        parent_if="bond0",
        vg_name="vg_pool_01",
        filesystem_lv_name="svmroot-tenant_a-abc123",
    )

    assert (
        adapter.resources["fs_tenant_a"]["device"]
        == "/dev/vg_pool_01/svmroot-tenant_a-abc123"
    )


@pytest.mark.unit
def test_subprocess_adapter_includes_existing_filesystem_on_retry(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=1),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
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

    SubprocessPacemakerAdapter().create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=100,
        ifname="v100-tenantxxxx",
        ip="192.168.10.5",
        prefix=24,
        gw="192.168.10.1",
        parent_if="bond0",
        vg_name="vg_pool_01",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    group_add = next(
        cmd for cmd in calls if cmd[:4] == ["pcs", "resource", "group", "add"]
    )
    assert group_add == [
        "pcs",
        "resource",
        "group",
        "add",
        "g_svm_tenant_a",
        "fs_tenant_a",
        "netns_tenant_a",
        "ganesha_tenant_a",
    ]
    assert [
        "pcs",
        "constraint",
        "order",
        "ms_drbd_r0:promote",
        "fs_tenant_a:start",
    ] in calls


@pytest.mark.unit
def test_subprocess_adapter_repairs_constraints_when_group_exists(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
        MagicMock(returncode=0),  # pcs resource show netns_tenant_a
        MagicMock(returncode=0),  # pcs resource show ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="g_svm_tenant_a: fs_tenant_a netns_tenant_a ganesha_tenant_a\n",
            stderr="",
        ),
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint show --full
        MagicMock(returncode=0),  # pcs constraint order ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint show --full
        MagicMock(returncode=0),  # pcs constraint colocation add ...
    ]

    SubprocessPacemakerAdapter().create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=100,
        ifname="v100-tenantxxxx",
        ip="192.168.10.5",
        prefix=24,
        gw="192.168.10.1",
        parent_if="bond0",
        vg_name="vg_pool_01",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    assert not any(cmd[:4] == ["pcs", "resource", "group", "add"] for cmd in calls)
    assert [
        "pcs",
        "constraint",
        "order",
        "ms_drbd_r0:promote",
        "fs_tenant_a:start",
    ] in calls
    assert [
        "pcs",
        "constraint",
        "colocation",
        "add",
        "g_svm_tenant_a",
        "with",
        "ms_drbd_r0:Master",
    ] in calls


@pytest.mark.unit
def test_subprocess_adapter_repairs_missing_members_when_group_exists(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
        MagicMock(returncode=1),  # pcs resource show netns_tenant_a
        MagicMock(returncode=0),  # pcs resource create netns_tenant_a
        MagicMock(returncode=0),  # pcs resource show ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="g_svm_tenant_a: fs_tenant_a ganesha_tenant_a\n",
            stderr="",
        ),
        MagicMock(
            returncode=0
        ),  # pcs resource group add g_svm_tenant_a netns_tenant_a --before ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="order ms_drbd_r0:promote fs_tenant_a:start\n",
            stderr="",
        ),
        MagicMock(
            returncode=0,
            stdout="colocation g_svm_tenant_a with ms_drbd_r0:Master\n",
            stderr="",
        ),
    ]

    SubprocessPacemakerAdapter().create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=100,
        ifname="v100-tenantxxxx",
        ip="192.168.10.5",
        prefix=24,
        gw="192.168.10.1",
        parent_if="bond0",
        vg_name="vg_pool_01",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    assert any(
        cmd[:5]
        == ["pcs", "resource", "create", "netns_tenant_a", "ocf:local:NetnsVlan"]
        for cmd in calls
    )
    assert [
        "pcs",
        "resource",
        "group",
        "add",
        "g_svm_tenant_a",
        "netns_tenant_a",
        "--before",
        "ganesha_tenant_a",
    ] in calls


@pytest.mark.unit
def test_subprocess_adapter_reorders_existing_members_when_group_exists(
    mock_subprocess,
):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
        MagicMock(returncode=0),  # pcs resource show netns_tenant_a
        MagicMock(returncode=0),  # pcs resource show ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="g_svm_tenant_a: ganesha_tenant_a fs_tenant_a netns_tenant_a\n",
            stderr="",
        ),
        MagicMock(
            returncode=0
        ),  # pcs resource group add g_svm_tenant_a ganesha_tenant_a --after netns_tenant_a
        MagicMock(
            returncode=0,
            stdout="order ms_drbd_r0:promote fs_tenant_a:start\n",
            stderr="",
        ),
        MagicMock(
            returncode=0,
            stdout="colocation g_svm_tenant_a with ms_drbd_r0:Master\n",
            stderr="",
        ),
    ]

    SubprocessPacemakerAdapter().create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=100,
        ifname="v100-tenantxxxx",
        ip="192.168.10.5",
        prefix=24,
        gw="192.168.10.1",
        parent_if="bond0",
        vg_name="vg_pool_01",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    assert [
        "pcs",
        "resource",
        "group",
        "add",
        "g_svm_tenant_a",
        "ganesha_tenant_a",
        "--after",
        "netns_tenant_a",
    ] in calls


@pytest.mark.unit
def test_subprocess_adapter_reorders_first_member_when_group_exists(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # pcs resource show g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource show p_drbd_r0
        MagicMock(returncode=0),  # pcs resource show ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource show fs_tenant_a
        MagicMock(returncode=1),  # pcs resource show netns_tenant_a
        MagicMock(returncode=0),  # pcs resource create netns_tenant_a
        MagicMock(returncode=0),  # pcs resource show ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="g_svm_tenant_a: ganesha_tenant_a fs_tenant_a\n",
            stderr="",
        ),
        MagicMock(
            returncode=0
        ),  # pcs resource group add g_svm_tenant_a fs_tenant_a --before ganesha_tenant_a
        MagicMock(
            returncode=0
        ),  # pcs resource group add g_svm_tenant_a netns_tenant_a --before ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="order ms_drbd_r0:promote fs_tenant_a:start\n",
            stderr="",
        ),
        MagicMock(
            returncode=0,
            stdout="colocation g_svm_tenant_a with ms_drbd_r0:Master\n",
            stderr="",
        ),
    ]

    SubprocessPacemakerAdapter().create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=100,
        ifname="v100-tenantxxxx",
        ip="192.168.10.5",
        prefix=24,
        gw="192.168.10.1",
        parent_if="bond0",
        vg_name="vg_pool_01",
    )

    calls = [c.args[0] for c in mock_subprocess.call_args_list]
    fs_repair = [
        "pcs",
        "resource",
        "group",
        "add",
        "g_svm_tenant_a",
        "fs_tenant_a",
        "--before",
        "ganesha_tenant_a",
    ]
    netns_repair = [
        "pcs",
        "resource",
        "group",
        "add",
        "g_svm_tenant_a",
        "netns_tenant_a",
        "--before",
        "ganesha_tenant_a",
    ]
    assert fs_repair in calls
    assert netns_repair in calls
    assert calls.index(fs_repair) < calls.index(netns_repair)
