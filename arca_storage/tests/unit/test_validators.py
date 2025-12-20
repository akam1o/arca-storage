"""
Unit tests for validators.
"""

import pytest

from arca_storage.cli.lib.validators import (validate_ip_cidr, validate_name,
                                   validate_vlan)


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
