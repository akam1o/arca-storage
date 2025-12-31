# MVP Setup Guide

This guide provides step-by-step instructions for setting up the Arca Storage MVP (Minimum Viable Product) configuration.

## Prerequisites

### Hardware Requirements (per node)

- CPU: AMD EPYC 32Core (64Thread) or higher
- RAM: Standard amount (ZFS ARC not used)
- Boot Disk: 2x disks in RAID1 (OS)
- Storage: 12-24x NVMe SSD (data)
- NIC (Service): 4x 100Gbps → Bonding (200Gbps)
- NIC (Cluster): 4x 100Gbps → Bonding (200Gbps)

### Software Requirements

- OS: RHEL/Alma/Rocky Linux 8 or 9
- Software stack:
  - Pacemaker + Corosync
  - NFS-Ganesha
  - LVM2
  - DRBD
  - XFS utilities

### Network Configuration

- Service Network: `bond0` (200Gbps)
- Interconnect: `bond1` (200Gbps, for DRBD)

## MVP Configuration

The MVP uses a simplified 2-node configuration with:
- DRBD resource `r0` only (single resource)
- 1 SVM (Storage Virtual Machine)
- 1 Volume
- NFS mount verification
- Failover testing

## Step 1: Install Required Software

### On both nodes:

```bash
# Install base packages
sudo dnf install -y pacemaker corosync pcs resource-agents
sudo dnf install -y nfs-ganesha nfs-ganesha-utils
sudo dnf install -y lvm2 xfsprogs
sudo dnf install -y drbd-utils drbd-kmod

# Enable services
sudo systemctl enable --now pcsd
sudo systemctl enable --now corosync
sudo systemctl enable --now pacemaker
```

## Step 2: Configure DRBD

### On both nodes:

1. **Prepare storage device:**

   ```bash
   # Identify NVMe device (example: /dev/nvme0n1)
   # Partition the device for DRBD metadata
   sudo parted /dev/nvme0n1 mklabel gpt
   sudo parted /dev/nvme0n1 mkpart primary 1MiB 100%
   sudo partprobe /dev/nvme0n1
   ```

2. **Create DRBD resource configuration:**

   On node1, create `/etc/drbd.d/r0.res`:

   ```conf
   resource r0 {
     protocol C;
     meta-disk internal;

     on node1 {
       device /dev/drbd0;
       disk /dev/nvme0n1p1;
       address 10.1.0.1:7788;
     }
     on node2 {
       device /dev/drbd0;
       disk /dev/nvme0n1p1;
       address 10.1.0.2:7788;
     }
   }
   ```

   Copy to node2:

   ```bash
   sudo scp /etc/drbd.d/r0.res node2:/etc/drbd.d/r0.res
   ```

3. **Initialize and start DRBD:**

   On both nodes:

   ```bash
   sudo drbdadm create-md r0
   sudo drbdadm up r0
   ```

   On node1 (primary):

   ```bash
   sudo drbdadm primary r0 --force
   ```

   Verify synchronization:

   ```bash
   sudo drbdadm status r0
   ```

## Step 3: Configure LVM Thin Pool

### On node1 (where DRBD is primary):

1. **Create Physical Volume:**

   ```bash
   sudo pvcreate /dev/drbd0
   ```

2. **Create Volume Group:**

   ```bash
   sudo vgcreate vg_pool_01 /dev/drbd0
   ```

3. **Create Thin Pool:**

   ```bash
   sudo lvcreate -L 20T -T vg_pool_01/pool \
     -c 256K \
     --poolmetadatasize 15.8G \
     -Z y
   ```

   Configure auto-extend:

   ```bash
   # Edit /etc/lvm/lvm.conf
   # Set: activation/thin_pool_autoextend_threshold = 80
   # Set: activation/thin_pool_autoextend_percent = 5
   
   sudo systemctl enable --now lvm2-monitor
   ```

## Step 4: Create SVM Volume

### On node1:

This section uses `export_dir` (default: `/exports`) and `ganesha_config_dir` (default: `/etc/ganesha`).
You can change them in `/etc/arca-storage/storage-runtime.conf`.
After editing configs, re-generate `/etc/arca-storage/arca-storage.env`:

```bash
sudo arca bootstrap render-env
```

1. **Create Logical Volume for SVM:**

   ```bash
   sudo lvcreate -V 2T -T vg_pool_01/pool -n vol_tenant_a
   ```

2. **Format with XFS:**

   ```bash
   sudo mkfs.xfs -b size=4096 \
     -m crc=1,finobt=1 \
     -i size=512,maxpct=25 \
     -d agcount=32,su=256k,sw=1 \
     /dev/vg_pool_01/vol_tenant_a
   ```

3. **Create mount point:**

   ```bash
   sudo mkdir -p <export_dir>/tenant_a
   ```

4. **Mount filesystem:**

   ```bash
   sudo mount -o rw,noatime,nodiratime,logbsize=256k,inode64 \
     /dev/vg_pool_01/vol_tenant_a \
     <export_dir>/tenant_a
   ```

5. **Add to /etc/fstab (optional):**

   ```
   /dev/vg_pool_01/vol_tenant_a <export_dir>/tenant_a xfs rw,noatime,nodiratime,logbsize=256k,inode64 0 0
   ```

## Step 5: Install Pacemaker Resource Agent

### On both nodes:

```bash
# Copy NetnsVlan RA
sudo cp resources/pacemaker/NetnsVlan /usr/lib/ocf/resource.d/local/NetnsVlan
sudo chmod +x /usr/lib/ocf/resource.d/local/NetnsVlan

# Verify installation
sudo /usr/lib/ocf/resource.d/local/NetnsVlan meta-data
```

## Step 6: Configure Pacemaker Cluster

### On node1:

1. **Authenticate nodes:**

   ```bash
   sudo pcs host auth node1 node2 -u hacluster -p <password>
   ```

2. **Create cluster:**

   ```bash
   sudo pcs cluster setup arca-cluster node1 node2
   sudo pcs cluster start --all
   sudo pcs cluster enable --all
   ```

3. **Configure STONITH (recommended for production):**

   **Note:** For MVP/testing environments, you can proceed without STONITH. For production environments, STONITH is strongly recommended.

   To enable STONITH:
   - Set `pacemaker_enable_stonith: true` in `ansible/group_vars/all.yml`
   - Manually configure STONITH devices after running the playbook:

   ```bash
   # Example: IPMI STONITH (adjust for your hardware)
   sudo pcs stonith create fence_node1 fence_ipmilan \
     ipaddr=<node1-ipmi-ip> \
     login=<user> \
     passwd=<password> \
     lanplus=1 cipher=1 \
     pcmk_host_list=node1 \
     op monitor interval=60s

   sudo pcs stonith create fence_node2 fence_ipmilan \
     ipaddr=<node2-ipmi-ip> \
     login=<user> \
     passwd=<password> \
     lanplus=1 cipher=1 \
     pcmk_host_list=node2 \
     op monitor interval=60s

   # Verify STONITH devices
   sudo pcs stonith show
   sudo pcs stonith status
   ```

   For alternative fencing methods (AWS, Azure, VMware, etc.), refer to the [Ansible README](../ansible/README.md#stonith設定).

4. **Create DRBD resource:**

   ```bash
   sudo pcs resource create p_drbd_r0 ocf:linbit:drbd \
     drbd_resource=r0 \
     op monitor interval=15s role=Master
   
   sudo pcs resource master ms_drbd_r0 p_drbd_r0 \
     master-max=1 \
     master-node-max=1 \
     clone-max=2 \
     clone-node-max=1
   ```

5. **Create Filesystem resource:**

   ```bash
   sudo pcs resource create fs_tenant_a ocf:heartbeat:Filesystem \
     device=/dev/vg_pool_01/vol_tenant_a \
     directory=<export_dir>/tenant_a \
     fstype=xfs \
     op monitor interval=10s
   ```

6. **Create NetnsVlan resource:**

   ```bash
   sudo pcs resource create netns_tenant_a ocf:local:NetnsVlan \
     ns=tenant_a \
     vlan_id=100 \
     parent_if=bond0 \
     ip=192.168.10.5 \
     prefix=24 \
     gw=192.168.10.1 \
     mtu=9000 \
     op monitor interval=10s
   ```

7. **Create NFS-Ganesha resource:**

   First, ensure systemd unit template `/etc/systemd/system/nfs-ganesha@.service` is installed.
   You can install it via:

   ```bash
   sudo arca bootstrap install
   ```

   If you install it manually, use `${ARCA_GANESHA_CONFIG_DIR}` (from `/etc/arca-storage/arca-storage.env`) or the default `/etc/ganesha`:

   ```ini
   [Unit]
   Description=NFS-Ganesha Service for SVM %i
   After=network.target

   [Service]
   Type=forking
   EnvironmentFile=-/etc/arca-storage/arca-storage.env
   NetworkNamespacePath=/var/run/netns/%i
   ExecStart=/usr/bin/ganesha.nfsd -f ${ARCA_GANESHA_CONFIG_DIR}/ganesha.%i.conf
   ExecReload=/bin/kill -HUP $MAINPID
   PIDFile=/var/run/ganesha.%i.pid

   [Install]
   WantedBy=multi-user.target
   ```

   Then create resource:

   ```bash
   sudo pcs resource create ganesha_tenant_a systemd:nfs-ganesha@tenant_a \
     op monitor interval=10s
   ```

8. **Create resource group:**

   ```bash
   sudo pcs resource group add g_svm_tenant_a \
     fs_tenant_a \
     netns_tenant_a \
     ganesha_tenant_a
   ```

9. **Set resource constraints:**

   ```bash
   # Ensure DRBD is primary before filesystem
   sudo pcs constraint order ms_drbd_r0:promote fs_tenant_a:start
   
   # Colocate SVM resources with DRBD
   sudo pcs constraint colocation add g_svm_tenant_a with ms_drbd_r0:Master
   ```

## Step 7: Configure NFS-Ganesha

### On node1:

1. **(Recommended) Create ganesha configuration via `arca export`:**

   ```bash
   sudo arca export add --svm tenant_a --volume vol1 --client 10.0.0.0/24 --access rw
   ```

   This generates `<ganesha_config_dir>/ganesha.tenant_a.conf` (default: `/etc/ganesha/ganesha.tenant_a.conf`).

2. **(Manual) Create ganesha configuration:**

   Create `<ganesha_config_dir>/ganesha.tenant_a.conf`:

   ```conf
   NFS_CORE_PARAM {
       Protocols = 4;
       NFS_Port = 2049;
   }

   EXPORT_DEFAULTS {
       Access_Type = RW;
       Squash = Root_Squash;
   }

   EXPORT {
       Export_Id = 101;
       Path = "<export_dir>/tenant_a";
       Pseudo = "<export_dir>/tenant_a";
       Protocols = 4;
       Access_Type = RW;
       Squash = Root_Squash;
       SecType = sys;
       CLIENT {
           Clients = 0.0.0.0/0;
       }
       FSAL {
           Name = VFS;
       }
   }
   ```

3. **Start NFS-Ganesha (if not managed by Pacemaker):**

   ```bash
   sudo systemctl start nfs-ganesha@tenant_a
   sudo systemctl enable nfs-ganesha@tenant_a
   ```

## Step 8: Verify Configuration

### On node1:

1. **Check Pacemaker status:**

   ```bash
   sudo pcs status
   sudo pcs status resources
   ```

2. **Verify namespace:**

   ```bash
   sudo ip netns list
   sudo ip netns exec tenant_a ip addr show
   ```

3. **Verify NFS export:**

   ```bash
   # From a client machine
   showmount -e 192.168.10.5
   ```

4. **Test NFS mount:**

   ```bash
   # From a client machine
   sudo mkdir -p /mnt/nfs-test
   sudo mount -t nfs4 192.168.10.5:<export_dir>/tenant_a /mnt/nfs-test
   df -h /mnt/nfs-test
   sudo touch /mnt/nfs-test/testfile
   sudo umount /mnt/nfs-test
   ```

## Step 9: Test Failover

### On node1:

1. **Simulate node failure:**

   ```bash
   # Option 1: Stop Pacemaker
   sudo pcs cluster stop node1
   
   # Option 2: Fence node (if STONITH configured)
   sudo pcs stonith fence node1
   ```

2. **On node2, verify resources moved:**

   ```bash
   sudo pcs status
   sudo drbdadm status r0
   # Should show node2 as primary
   ```

3. **Verify NFS still accessible:**

   ```bash
   # From client
   showmount -e 192.168.10.5
   # Should still work
   ```

4. **Recover node1:**

   ```bash
   sudo pcs cluster start node1
   sudo pcs cluster enable node1
   ```

## Troubleshooting

### DRBD not synchronizing

```bash
# Check DRBD status
sudo drbdadm status r0

# Check network connectivity
ping -c 3 <peer-ip>

# Check firewall rules
sudo firewall-cmd --list-all
```

### Pacemaker resources not starting

```bash
# Check resource logs
sudo pcs status resources <resource-name>

# Check system logs
sudo journalctl -u pacemaker -f

# Validate resource agent
sudo /usr/lib/ocf/resource.d/local/NetnsVlan validate-all \
  OCF_RESKEY_ns=tenant_a \
  OCF_RESKEY_vlan_id=100 \
  OCF_RESKEY_parent_if=bond0 \
  OCF_RESKEY_ip=192.168.10.5 \
  OCF_RESKEY_prefix=24
```

### NFS not accessible

```bash
# Check ganesha process
sudo ip netns exec tenant_a ps aux | grep ganesha

# Check ganesha logs
sudo journalctl -u nfs-ganesha@tenant_a -f

# Check export configuration
sudo cat <ganesha_config_dir>/ganesha.tenant_a.conf
```

### Namespace issues

```bash
# List namespaces
sudo ip netns list

# Check namespace configuration
sudo ip netns exec tenant_a ip addr show
sudo ip netns exec tenant_a ip route show

# Delete and recreate if needed
sudo ip netns del tenant_a
# Then restart Pacemaker resource
```

## Next Steps

After MVP verification:

1. Add second DRBD resource (`r1`) for Active/Active
2. Create additional SVMs
3. Implement full CLI/API management
4. Set up monitoring (Prometheus/Zabbix)
5. Configure backup and recovery procedures

## References

- [Pacemaker RA README](../arca_storage/arca_storage/resources/pacemaker/) - Resource agent documentation
