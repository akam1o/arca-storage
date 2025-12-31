# Pacemaker Resource Agent: NetnsVlan

Network Namespace with VLAN interface and VIP management resource agent for Pacemaker.

## Overview

This resource agent manages a network namespace with a VLAN interface and VIP (Virtual IP) configuration. It is designed to support multi-tenancy in the Arca Storage system by isolating network traffic per tenant using Linux Network Namespaces.

## Installation

### 1. Copy the resource agent

```bash
sudo cp resources/pacemaker/NetnsVlan /usr/lib/ocf/resource.d/local/NetnsVlan
sudo chmod +x /usr/lib/ocf/resource.d/local/NetnsVlan
```

### 2. Verify installation

```bash
sudo /usr/lib/ocf/resource.d/local/NetnsVlan meta-data
```

This should output the XML metadata definition.

## Resource Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `ns` | Yes | Network namespace name |
| `vlan_id` | Yes | VLAN ID (1-4094) |
| `parent_if` | Yes | Parent interface name (e.g., bond0) |
| `ip` | Yes | VIP IP address |
| `prefix` | Yes | IP prefix length (e.g., 24) |
| `gw` | Yes | Gateway IP address |
| `mtu` | No | MTU size (default: 1500) |

## Usage Example

### Create a resource using pcs

```bash
pcs resource create netns_tenant_a ocf:local:NetnsVlan \
  ns=tenant_a \
  vlan_id=100 \
  parent_if=bond0 \
  ip=192.168.10.5 \
  prefix=24 \
  gw=192.168.10.1 \
  mtu=9000
```

### Add to a resource group

```bash
pcs resource group add g_svm_tenant_a netns_tenant_a
```

### Monitor resource status

```bash
pcs status resources netns_tenant_a
```

## Testing

### Manual Testing

1. **Test start action:**
   ```bash
   sudo /usr/lib/ocf/resource.d/local/NetnsVlan start \
     OCF_RESKEY_ns=test_ns \
     OCF_RESKEY_vlan_id=100 \
     OCF_RESKEY_parent_if=bond0 \
     OCF_RESKEY_ip=192.168.10.5 \
     OCF_RESKEY_prefix=24 \
     OCF_RESKEY_gw=192.168.10.1
   ```

2. **Verify namespace and interface:**
   ```bash
   sudo ip netns list | grep test_ns
   sudo ip netns exec test_ns ip addr show
   sudo ip netns exec test_ns ip link show
   ```

3. **Test monitor action:**
   ```bash
   sudo /usr/lib/ocf/resource.d/local/NetnsVlan monitor \
     OCF_RESKEY_ns=test_ns \
     OCF_RESKEY_vlan_id=100 \
     OCF_RESKEY_parent_if=bond0 \
     OCF_RESKEY_ip=192.168.10.5 \
     OCF_RESKEY_prefix=24 \
     OCF_RESKEY_gw=192.168.10.1
   ```

4. **Test stop action:**
   ```bash
   sudo /usr/lib/ocf/resource.d/local/NetnsVlan stop \
     OCF_RESKEY_ns=test_ns \
     OCF_RESKEY_vlan_id=100 \
     OCF_RESKEY_parent_if=bond0 \
     OCF_RESKEY_ip=192.168.10.5 \
     OCF_RESKEY_prefix=24 \
     OCF_RESKEY_gw=192.168.10.1
   ```

5. **Verify cleanup:**
   ```bash
   sudo ip netns list | grep test_ns
   # Should return nothing
   ```

### Integration with Pacemaker

1. **Create the resource:**
   ```bash
   pcs resource create test_netns ocf:local:NetnsVlan \
     ns=test_ns \
     vlan_id=100 \
     parent_if=bond0 \
     ip=192.168.10.5 \
     prefix=24 \
     gw=192.168.10.1
   ```

2. **Start the resource:**
   ```bash
   pcs resource enable test_netns
   pcs resource start test_netns
   ```

3. **Check status:**
   ```bash
   pcs status resources test_netns
   ```

4. **Test failover (if in cluster):**
   ```bash
   # Move resource to another node
   pcs resource move test_netns node2
   ```

5. **Cleanup:**
   ```bash
   pcs resource disable test_netns
   pcs resource delete test_netns
   ```

## Troubleshooting

### Resource fails to start

- Check if parent interface exists: `ip link show bond0`
- Check if VLAN ID is already in use: `ip link show | grep bond0.100`
- Check if namespace already exists: `ip netns list`
- Check Pacemaker logs: `journalctl -u pacemaker -f`

### Resource shows as failed in monitor

- Verify namespace exists: `ip netns list`
- Verify interface is up: `ip netns exec <ns> ip link show`
- Verify IP is configured: `ip netns exec <ns> ip addr show`
- Check resource logs: `pcs status resources <resource_name>`

### Namespace cleanup issues

If a namespace is not properly cleaned up:

```bash
# List all namespaces
ip netns list

# Delete namespace manually (use with caution)
sudo ip netns del <namespace_name>
```

## Notes

- This resource agent requires root privileges to manage network namespaces and interfaces.
- The VLAN ID must be unique within the cluster.
- The namespace name should match the SVM name for consistency.
- The resource agent is idempotent - it can be safely started multiple times.
