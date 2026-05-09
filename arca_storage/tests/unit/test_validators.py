"""
Unit tests for validators.
"""

import pytest

from arca_storage.cli.lib.validators import (
    legacy_svm_root_lv_name,
    snapshot_lv_name,
    svm_root_lv_name,
    validate_gateway_for_ip_cidr,
    validate_ip_cidr,
    validate_name,
    validate_svm_ip_cidr,
    validate_vlan,
    volume_lv_name,
)


class TestValidateName:
    """Tests for validate_name function."""

    @pytest.mark.unit
    def test_valid_name(self):
        """Test valid names."""
        validate_name("tenant_a")
        validate_name("tenant-1")
        validate_name("tenant_1")
        validate_name("tenant.1")
        validate_name("a")
        validate_name("a" * 64)

    @pytest.mark.unit
    def test_empty_name(self):
        """Test empty name raises error."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_name("")

    @pytest.mark.unit
    def test_name_too_long(self):
        """Test name too long raises error."""
        with pytest.raises(ValueError, match="between 1 and 64"):
            validate_name("a" * 65)

    @pytest.mark.unit
    def test_name_invalid_chars(self):
        """Test name with invalid characters raises error."""
        with pytest.raises(ValueError):
            validate_name("tenant a")  # space
        with pytest.raises(ValueError):
            validate_name("tenant@a")  # @
        with pytest.raises(ValueError):
            validate_name("-tenant")  # starts with hyphen
        with pytest.raises(ValueError):
            validate_name("_tenant")  # starts with underscore
        with pytest.raises(ValueError):
            validate_name("tenant\n")  # trailing newline
        with pytest.raises(ValueError):
            validate_name("tenant\r")  # trailing carriage return


class TestValidateVlan:
    """Tests for validate_vlan function."""

    @pytest.mark.unit
    def test_valid_vlan(self):
        """Test valid VLAN IDs."""
        validate_vlan(1)
        validate_vlan(100)
        validate_vlan(4094)

    @pytest.mark.unit
    def test_vlan_too_small(self):
        """Test VLAN ID too small raises error."""
        with pytest.raises(ValueError, match="between 1 and 4094"):
            validate_vlan(0)

    @pytest.mark.unit
    def test_vlan_too_large(self):
        """Test VLAN ID too large raises error."""
        with pytest.raises(ValueError, match="between 1 and 4094"):
            validate_vlan(4095)


class TestValidateIpCidr:
    """Tests for validate_ip_cidr function."""

    @pytest.mark.unit
    def test_valid_cidr(self):
        """Test valid CIDR notations."""
        ip, prefix = validate_ip_cidr("192.168.10.5/24")
        assert ip == "192.168.10.5"
        assert prefix == 24

        ip, prefix = validate_ip_cidr("10.0.0.0/8")
        assert ip == "10.0.0.0"
        assert prefix == 8

        ip, prefix = validate_ip_cidr("172.16.0.0/12")
        assert ip == "172.16.0.0"
        assert prefix == 12

    @pytest.mark.unit
    def test_invalid_format(self):
        """Test invalid CIDR format raises error."""
        with pytest.raises(ValueError, match="CIDR must be in format"):
            validate_ip_cidr("192.168.10.5")  # missing prefix

        with pytest.raises(ValueError, match="CIDR must be in format"):
            validate_ip_cidr("192.168.10.5/24/32")  # too many parts

    @pytest.mark.unit
    def test_invalid_ip(self):
        """Test invalid IP address raises error."""
        with pytest.raises(ValueError):
            validate_ip_cidr("256.256.256.256/24")

        with pytest.raises(ValueError):
            validate_ip_cidr("not.an.ip/24")

    @pytest.mark.unit
    def test_invalid_prefix(self):
        """Test invalid prefix length raises error."""
        with pytest.raises(ValueError, match="Prefix length must be between"):
            validate_ip_cidr("192.168.10.5/33")

        with pytest.raises(ValueError, match="Prefix length must be between"):
            validate_ip_cidr("192.168.10.5/-1")


class TestValidateSvmIpCidr:
    """Tests for validate_svm_ip_cidr function."""

    @pytest.mark.unit
    def test_valid_host_cidr(self):
        """Test valid SVM VIP CIDRs."""
        ip, prefix = validate_svm_ip_cidr("192.168.10.5/24")
        assert ip == "192.168.10.5"
        assert prefix == 24

        ip, prefix = validate_svm_ip_cidr("192.168.10.5/32")
        assert ip == "192.168.10.5"
        assert prefix == 32

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "cidr",
        [
            "0.0.0.0/0",
            "10.0.0.1/0",
            "192.168.10.0/24",
            "192.168.10.255/24",
            "224.0.0.1/24",
            "127.0.0.1/8",
            "240.0.0.1/24",
        ],
    )
    def test_rejects_non_unicast_host_addresses(self, cidr):
        """Test SVM VIP rejects addresses that cannot be bound as service hosts."""
        with pytest.raises(ValueError):
            validate_svm_ip_cidr(cidr)


class TestValidateGatewayForIpCidr:
    """Tests for validate_gateway_for_ip_cidr function."""

    @pytest.mark.unit
    def test_valid_gateway(self):
        validate_gateway_for_ip_cidr("192.168.10.5/24", "192.168.10.1")

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "gateway, match",
        [
            ("10.0.0.1", "inside SVM network"),
            ("192.168.10.5", "SVM IP address"),
            ("192.168.10.0", "network or broadcast"),
            ("192.168.10.255", "network or broadcast"),
        ],
    )
    def test_rejects_unusable_gateway(self, gateway, match):
        with pytest.raises(ValueError, match=match):
            validate_gateway_for_ip_cidr("192.168.10.5/24", gateway)


class TestLVMNameBuilders:
    """Tests for generated LVM object name length validation."""

    @pytest.mark.unit
    def test_volume_lv_name_stays_within_lvm_limit(self):
        name = volume_lv_name("s" * 64, "v" * 64)

        assert name.startswith("vol-")
        assert len(name) <= 127

    @pytest.mark.unit
    def test_volume_lv_name_disambiguates_underscore_components(self):
        assert volume_lv_name("a_b", "c") != volume_lv_name("a", "b_c")
        assert volume_lv_name("a", "b") != svm_root_lv_name("a_b")

    @pytest.mark.unit
    def test_snapshot_lv_name_stays_within_lvm_limit(self):
        name = snapshot_lv_name("s" * 64, "v" * 64, "p" * 64)

        assert name.startswith("snap-")
        assert len(name) <= 127

    @pytest.mark.unit
    def test_svm_root_lv_name_uses_hash_suffix(self):
        name = svm_root_lv_name("tenant")

        assert name.startswith("svmroot-tenant-")
        assert name != legacy_svm_root_lv_name("tenant")
