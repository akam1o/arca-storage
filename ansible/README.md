# Ansible HA Storage Playbook

## Prerequisites
- Ansible 2.13+ (recommended)
- 2-node Linux cluster (RHEL 8/9, CentOS Stream 8/9, Rocky Linux 8/9, etc.)
- Root or sudo privileges on each node
- Network connectivity (inter-node communication, SSH)
- Required packages: `xfsprogs`, `parted`, `iproute2`
- `community.general` collection required
  - `ansible-galaxy collection install community.general`
- Node names (`node1`, `node2`) must be resolvable via DNS or `/etc/hosts` (for Pacemaker cluster setup)

## Directory Structure
- `ansible.cfg`: Ansible configuration
- `inventory.ini`: Sample 2-node inventory
- `group_vars/all.yml`: Common variables
- `host_vars/node1.yml`, `host_vars/node2.yml`: Node-specific variables
- `site.yml`: Main playbook

## Usage
1. Update IP addresses and user in `inventory.ini` according to your environment
2. Update device names and network settings in `group_vars/all.yml`
3. Execute

```bash
cd ansible
ansible-playbook -i inventory.ini site.yml
```

## Key Variables
- DRBD: `drbd_resource_name`, `drbd_device`, `drbd_disk`, `drbd_nodes`
- LVM: `lvm_vg_name`, `lvm_pv_devices`, `lvm_thinpool_name`
- Pacemaker: `pacemaker_cluster_name`, `pacemaker_nodes`
- NFS-Ganesha: `nfs_ganesha_export_dir`, `nfs_ganesha_export_clients`
- arca CLI: `arca_cli_install_method`, `arca_cli_download_url`

## Important Notes
- DRBD/LVM may destroy existing disk data - configure carefully.
- Change `pacemaker_hacluster_password` to an appropriate value.
- Set the same `drbd_shared_secret` value on all nodes and change it for production use (ansible-vault recommended).
- Adjust NFS-Ganesha exports according to your requirements.

## STONITH Configuration
By default, `pacemaker_enable_stonith: false` is set. For production environments, it is strongly recommended to enable STONITH:

1. Change `pacemaker_enable_stonith: true` in `group_vars/all.yml`
2. Manually create STONITH devices appropriate for your environment
   ```bash
   # Example: Using IPMI
   pcs stonith create fence_node1 fence_ipmilan \
     pcmk_host_list="node1" ipaddr="10.0.0.11" login="admin" passwd="password" \
     lanplus=1 cipher=1 op monitor interval=60s

   pcs stonith create fence_node2 fence_ipmilan \
     pcmk_host_list="node2" ipaddr="10.0.0.12" login="admin" passwd="password" \
     lanplus=1 cipher=1 op monitor interval=60s
   ```
3. Verify STONITH devices are working correctly
   ```bash
   pcs stonith show
   pcs stonith status
   ```

For detailed configuration procedures, refer to the documentation in `docs/` according to your fencing device environment.

## Testing

### Lint and Syntax Check

The playbooks include lint configurations for code quality:

```bash
# Install dependencies
pip install ansible-core>=2.13 ansible-lint yamllint

# Install Ansible collections
ansible-galaxy collection install -r requirements.yml

# Run yamllint
yamllint .

# Run ansible-lint
ansible-lint site.yml

# Run syntax check
ansible-playbook -i inventory.ini site.yml --syntax-check
```

### Molecule Integration Tests

Molecule tests are available for integration testing with Docker/Podman:

```bash
# Install Molecule
pip install molecule molecule-plugins[docker]

# Run all tests
molecule test

# Run specific test steps
molecule converge  # Apply playbook
molecule verify    # Run verification tests
molecule destroy   # Clean up
```

### Test Mode

For testing without making destructive changes, use `test_mode`:

```yaml
# In group_vars or inventory
test_mode: true
drbd_bootstrap_enabled: false
lvm_create_enabled: false
pacemaker_bootstrap_enabled: false
pacemaker_create_resources: false
```

### CI/CD

GitHub Actions automatically runs lint checks on every push and pull request. See `.github/workflows/ansible-lint.yml` for details.
