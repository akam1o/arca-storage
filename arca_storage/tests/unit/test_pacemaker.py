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
from arca_storage.errors import SubprocessError


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
        MagicMock(returncode=1),  # pcs resource config g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource config p_drbd_r0
        MagicMock(returncode=0),  # pcs resource config ms_drbd_r0
        MagicMock(returncode=1),  # pcs resource config fs_tenant_a
        MagicMock(returncode=0),  # pcs resource create fs_tenant_a
        MagicMock(returncode=1),  # pcs resource config ip_tenant_a
        MagicMock(returncode=0),  # pcs resource create ip_tenant_a
        MagicMock(returncode=1),  # pcs resource config ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource create ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource group add g_svm_tenant_a ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint config --full
        MagicMock(returncode=0),  # pcs constraint order ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint config --full
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
        MagicMock(returncode=1),  # pcs resource config g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource config p_drbd_r0
        MagicMock(returncode=0),  # pcs resource config ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource config fs_tenant_a
        MagicMock(
            returncode=0,
            stdout="Attributes: device=/dev/vg_pool_01/vol_tenant_a directory=/exports/tenant_a fstype=xfs",
            stderr="",
        ),  # pcs resource config fs_tenant_a
        MagicMock(returncode=0),  # pcs resource update fs_tenant_a ...
        MagicMock(returncode=1),  # pcs resource config ip_tenant_a
        MagicMock(returncode=0),  # pcs resource create ip_tenant_a
        MagicMock(returncode=1),  # pcs resource config ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource create ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource group add g_svm_tenant_a ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint config --full
        MagicMock(returncode=0),  # pcs constraint order ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint config --full
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
        MagicMock(returncode=1),  # pcs resource config g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource config p_drbd_r0
        MagicMock(returncode=0),  # pcs resource config ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource config fs_tenant_a
        MagicMock(returncode=1),  # pcs resource config netns_tenant_a
        MagicMock(returncode=0),  # pcs resource create netns_tenant_a
        MagicMock(returncode=1),  # pcs resource config ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource create ganesha_tenant_a
        MagicMock(returncode=0),  # pcs resource group add g_svm_tenant_a ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint config --full
        MagicMock(returncode=0),  # pcs constraint order ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint config --full
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
        "promote",
        "ms_drbd_r0",
        "then",
        "start",
        "fs_tenant_a",
    ] in calls


@pytest.mark.unit
def test_subprocess_adapter_repairs_constraints_when_group_exists(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # pcs resource config g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource config p_drbd_r0
        MagicMock(returncode=0),  # pcs resource config ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource config fs_tenant_a
        MagicMock(returncode=0),  # pcs resource config netns_tenant_a
        MagicMock(returncode=0),  # pcs resource config ganesha_tenant_a
        MagicMock(
            returncode=0,
            stdout="g_svm_tenant_a: fs_tenant_a netns_tenant_a ganesha_tenant_a\n",
            stderr="",
        ),
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint config --full
        MagicMock(returncode=0),  # pcs constraint order ...
        MagicMock(returncode=0, stdout="", stderr=""),  # pcs constraint config --full
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
        "promote",
        "ms_drbd_r0",
        "then",
        "start",
        "fs_tenant_a",
    ] in calls
    assert [
        "pcs",
        "constraint",
        "colocation",
        "add",
        "g_svm_tenant_a",
        "with",
        "Promoted",
        "ms_drbd_r0",
    ] in calls


@pytest.mark.unit
def test_subprocess_adapter_repairs_missing_members_when_group_exists(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # pcs resource config g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource config p_drbd_r0
        MagicMock(returncode=0),  # pcs resource config ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource config fs_tenant_a
        MagicMock(returncode=1),  # pcs resource config netns_tenant_a
        MagicMock(returncode=0),  # pcs resource create netns_tenant_a
        MagicMock(returncode=0),  # pcs resource config ganesha_tenant_a
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
            stdout="Order Constraints:\n  promote resource 'ms_drbd_r0' then start resource 'fs_tenant_a' (id: order-drbd-fs)\n",
            stderr="",
        ),
        MagicMock(
            returncode=0,
            stdout="Colocation Constraints:\n  resource 'g_svm_tenant_a' with Promoted resource 'ms_drbd_r0' (id: coloc-svm-drbd)\n    score=INFINITY\n",
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
        MagicMock(returncode=0),  # pcs resource config g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource config p_drbd_r0
        MagicMock(returncode=0),  # pcs resource config ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource config fs_tenant_a
        MagicMock(returncode=0),  # pcs resource config netns_tenant_a
        MagicMock(returncode=0),  # pcs resource config ganesha_tenant_a
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
            stdout="Order Constraints:\n  promote resource 'ms_drbd_r0' then start resource 'fs_tenant_a' (id: order-drbd-fs)\n",
            stderr="",
        ),
        MagicMock(
            returncode=0,
            stdout="Colocation Constraints:\n  resource 'g_svm_tenant_a' with Promoted resource 'ms_drbd_r0' (id: coloc-svm-drbd)\n    score=INFINITY\n",
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
        MagicMock(returncode=0),  # pcs resource config g_svm_tenant_a
        MagicMock(returncode=0),  # pcs resource config p_drbd_r0
        MagicMock(returncode=0),  # pcs resource config ms_drbd_r0
        MagicMock(returncode=0),  # pcs resource config fs_tenant_a
        MagicMock(returncode=1),  # pcs resource config netns_tenant_a
        MagicMock(returncode=0),  # pcs resource create netns_tenant_a
        MagicMock(returncode=0),  # pcs resource config ganesha_tenant_a
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
            stdout="Order Constraints:\n  promote resource 'ms_drbd_r0' then start resource 'fs_tenant_a' (id: order-drbd-fs)\n",
            stderr="",
        ),
        MagicMock(
            returncode=0,
            stdout="Colocation Constraints:\n  resource 'g_svm_tenant_a' with Promoted resource 'ms_drbd_r0' (id: coloc-svm-drbd)\n    score=INFINITY\n",
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


@pytest.mark.unit
def test_subprocess_adapter_creates_host_group_with_pcs_012_commands(mock_subprocess):
    def run(command, **kwargs):
        if command[:3] == ["pcs", "resource", "config"]:
            return MagicMock(returncode=1, stdout="", stderr="Resource not found")
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_subprocess.side_effect = run

    SubprocessPacemakerAdapter().create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=None,
        ip="192.0.2.16",
        prefix=24,
        gw=None,
        parent_if="ens18",
        create_filesystem=False,
    )

    calls = [call.args[0] for call in mock_subprocess.call_args_list]
    assert [
        "pcs",
        "resource",
        "create",
        "p_drbd_r0",
        "ocf:linbit:drbd",
        "drbd_resource=r0",
        "op",
        "monitor",
        "interval=15s",
        "role=Promoted",
    ] in calls
    assert [
        "pcs",
        "resource",
        "promotable",
        "p_drbd_r0",
        "ms_drbd_r0",
        "meta",
        "promoted-max=1",
        "promoted-node-max=1",
        "clone-max=2",
        "clone-node-max=1",
    ] in calls
    assert [
        "pcs",
        "constraint",
        "order",
        "promote",
        "ms_drbd_r0",
        "then",
        "start",
        "ip_tenant_a",
    ] in calls
    assert [
        "pcs",
        "constraint",
        "colocation",
        "add",
        "g_svm_tenant_a",
        "with",
        "Promoted",
        "ms_drbd_r0",
    ] in calls
    assert [
        "pcs",
        "resource",
        "create",
        "ganesha_tenant_a",
        "systemd:nfs-ganesha-host@tenant_a",
        "op",
        "monitor",
        "interval=10s",
    ] in calls
    assert not any("show" in command or "master" in command for command in calls)


@pytest.mark.unit
@pytest.mark.parametrize("target_action", ["start ", ""])
@pytest.mark.parametrize("group_role", ["Started ", ""])
def test_subprocess_adapter_existing_pcs_012_config_is_idempotent(
    mock_subprocess, target_action, group_role
):
    # pcs resource config describes resources even when maintenance mode keeps
    # them stopped. Its multiline attributes and constraint sentences differ
    # from the legacy output fixtures.
    group_config = """
Group: g_svm_tenant_a
  Resource: fs_tenant_a (class=ocf provider=heartbeat type=Filesystem)
    Attributes: fs_tenant_a-instance_attributes
      device=/dev/vg_pool_01/svmroot-tenant_a-abc123
      directory=/exports/tenant_a
      fstype=xfs
  Resource: ip_tenant_a (class=ocf provider=heartbeat type=IPaddr2)
  Resource: ganesha_tenant_a (class=systemd type=nfs-ganesha-host@tenant_a)
"""
    constraints = (
        "Order Constraints:\n"
        f"  promote resource 'ms_drbd_r0' then {target_action}resource 'fs_tenant_a' "
        "(id: order-drbd-fs)\n"
        "Colocation Constraints:\n"
        f"  {group_role}resource 'g_svm_tenant_a' with Promoted resource 'ms_drbd_r0' "
        "(id: coloc-svm-drbd)\n"
        "    score=INFINITY\n"
    )

    def run(command, **kwargs):
        if command[:3] == ["pcs", "resource", "config"]:
            return MagicMock(returncode=0, stdout=group_config, stderr="")
        if command == ["pcs", "constraint", "config", "--full"]:
            return MagicMock(returncode=0, stdout=constraints, stderr="")
        pytest.fail(f"Existing configuration should not be mutated: {command}")

    mock_subprocess.side_effect = run

    SubprocessPacemakerAdapter().create_group(
        "tenant_a",
        "/exports/tenant_a",
        vlan_id=None,
        ip="192.0.2.16",
        prefix=24,
        gw=None,
        parent_if="ens18",
        filesystem_lv_name="svmroot-tenant_a-abc123",
    )


@pytest.mark.unit
@pytest.mark.parametrize("target_action", ["start ", ""])
@pytest.mark.parametrize("role", ["Promoted", "Master"])
def test_subprocess_adapter_existing_legacy_constraints_are_idempotent(
    mock_subprocess, target_action, role
):
    mock_subprocess.return_value = MagicMock(
        returncode=0,
        stdout=(
            "Order Constraints:\n"
            f"  promote ms_drbd_r0 then {target_action}fs_tenant_a "
            "(kind:Mandatory) (id:order-drbd-fs)\n"
            "Colocation Constraints:\n"
            "  g_svm_tenant_a with ms_drbd_r0 (score:INFINITY) "
            f"(with-rsc-role:{role}) (id:coloc-svm-drbd)\n"
        ),
        stderr="",
    )

    adapter = SubprocessPacemakerAdapter()
    adapter._ensure_order("ms_drbd_r0", "fs_tenant_a")
    adapter._ensure_colocation("g_svm_tenant_a", "ms_drbd_r0")

    assert [call.args[0] for call in mock_subprocess.call_args_list] == [
        ["pcs", "constraint", "config", "--full"],
        ["pcs", "constraint", "config", "--full"],
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("order", "colocation"),
    [
        (
            "start resource 'ms_drbd_r0' then start resource 'fs_tenant_a'",
            "resource 'g_svm_tenant_a' with Unpromoted resource 'ms_drbd_r0'",
        ),
        (
            "start ms_drbd_r0 then start fs_tenant_a (kind:Mandatory)",
            "g_svm_tenant_a with ms_drbd_r0 (score:INFINITY) (with-rsc-role:Unpromoted)",
        ),
        (
            "promote ms_drbd_r0 then stop fs_tenant_a (kind:Mandatory)",
            "g_svm_tenant_a with ms_drbd_r0 (score:INFINITY) (with-rsc-role:Slave)",
        ),
        (
            "promote ms_drbd_r0 then start fs_tenant_a_other (kind:Mandatory)",
            "g_svm_tenant_a with ms_drbd_r0_other (score:INFINITY) (with-rsc-role:Promoted)",
        ),
        (
            "promote ms_drbd_r0_other then start fs_tenant_a (kind:Mandatory)",
            "g_svm_tenant_a_other with ms_drbd_r0 (score:INFINITY) (with-rsc-role:Promoted)",
        ),
        (
            (
                "promote ms_drbd_r0 then start fs_tenant_a_other\n"
                "  start ms_drbd_r0 then start fs_tenant_a"
            ),
            (
                "g_svm_tenant_a with ms_drbd_r0 (score:INFINITY) (rsc-role:Promoted)\n"
                "  g_svm_other with ms_drbd_r0 (score:INFINITY) (with-rsc-role:Promoted)"
            ),
        ),
    ],
)
def test_subprocess_adapter_does_not_accept_wrong_constraints(
    mock_subprocess, order, colocation
):
    mock_subprocess.return_value = MagicMock(
        returncode=0,
        stdout=(
            f"Order Constraints:\n  {order}\nColocation Constraints:\n  {colocation}\n"
        ),
        stderr="",
    )
    adapter = SubprocessPacemakerAdapter()
    adapter._ensure_order("ms_drbd_r0", "fs_tenant_a")
    adapter._ensure_colocation("g_svm_tenant_a", "ms_drbd_r0")

    calls = [call.args[0] for call in mock_subprocess.call_args_list]
    assert [
        "pcs",
        "constraint",
        "order",
        "promote",
        "ms_drbd_r0",
        "then",
        "start",
        "fs_tenant_a",
    ] in calls
    assert [
        "pcs",
        "constraint",
        "colocation",
        "add",
        "g_svm_tenant_a",
        "with",
        "Promoted",
        "ms_drbd_r0",
    ] in calls


@pytest.mark.unit
def test_subprocess_adapter_aborts_when_constraint_discovery_fails(mock_subprocess):
    mock_subprocess.return_value = MagicMock(
        returncode=1, stdout="", stderr="Unable to query cluster"
    )
    with pytest.raises(SubprocessError):
        SubprocessPacemakerAdapter()._ensure_order("ms_drbd_r0", "fs_tenant_a")
    assert mock_subprocess.call_count == 1
