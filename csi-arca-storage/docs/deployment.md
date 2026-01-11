# CSI ARCA Storage Deployment Guide

English | [日本語](deployment.ja.md)

This guide provides detailed instructions for deploying the CSI ARCA Storage driver in a Kubernetes cluster.

## Prerequisites

### Kubernetes Cluster
- Kubernetes 1.20 or later
- CSI volume snapshots feature enabled (for snapshot support)
- Network connectivity from all nodes to ARCA storage network

### ARCA Storage Backend
- ARCA storage backend API accessible
- Authentication token with appropriate permissions
- Network pools configured for SVM allocation

### Tools
- `kubectl` configured to access your cluster
- `kustomize` (optional, for kustomize-based deployment)
- Docker (for building custom images)

## Deployment Methods

### Method 1: Direct kubectl Apply (Quickstart)

1. **Configure ARCA API Access**

Create a secret with your ARCA API authentication token:

```bash
kubectl create secret generic csi-arca-storage-secret \
  --namespace=kube-system \
  --from-literal=auth-token='your-auth-token-here'
```

2. **Configure Network Pools**

Edit `deploy/controller.yaml` to configure your network pools:

```yaml
network:
  pools:
    - cidr: "10.0.0.0/24"
      range: "10.0.0.100-10.0.0.200"
      vlan: 100
      gateway: "10.0.0.1"
  mtu: 1500
```

3. **Deploy the Driver**

```bash
# Apply all manifests
kubectl apply -f deploy/csidriver.yaml
kubectl apply -f deploy/rbac-controller.yaml
kubectl apply -f deploy/rbac-node.yaml
kubectl apply -f deploy/controller.yaml
kubectl apply -f deploy/node.yaml

# Apply storage class and snapshot class
kubectl apply -f deploy/examples/storageclass.yaml
kubectl apply -f deploy/examples/volumesnapshotclass.yaml
```

4. **Verify Installation**

```bash
# Check controller pod
kubectl get pods -n kube-system -l app=csi-arca-storage-controller

# Check node pods
kubectl get pods -n kube-system -l app=csi-arca-storage-node

# Check CSIDriver
kubectl get csidriver csi.arca-storage.io
```

### Method 2: Kustomize-Based Deployment (Recommended)

#### Development Environment

1. **Configure Development Settings**

Edit `deploy/kustomize/overlays/development/config.yaml`:

```yaml
arca:
  base_url: "https://arca-api.dev.example.com"
  tls:
    insecure_skip_verify: true  # OK for dev
```

2. **Deploy**

```bash
kubectl apply -k deploy/kustomize/overlays/development
```

#### Production Environment

1. **Configure Production Settings**

Edit `deploy/kustomize/overlays/production/config.yaml`:

```yaml
arca:
  base_url: "https://arca-api.prod.example.com"
  timeout: "60s"
network:
  pools:
    - cidr: "10.100.0.0/22"
      range: "10.100.0.100-10.100.3.200"
      vlan: 1000
      gateway: "10.100.0.1"
  mtu: 9000  # Jumbo frames
```

2. **Create Secrets**

```bash
cd deploy/kustomize/overlays/production
cp secrets.env.example secrets.env
# Edit secrets.env and add your auth token
echo "auth-token=your-production-token" > secrets.env
```

**Important**: Add `secrets.env` to `.gitignore` to prevent committing secrets!

3. **Deploy**

```bash
kubectl apply -k deploy/kustomize/overlays/production
```

4. **Verify HA Setup**

```bash
# Should show 2 controller replicas
kubectl get statefulset -n kube-system csi-arca-storage-controller
```

## Configuration Reference

### ARCA API Configuration

```yaml
arca:
  base_url: "https://arca-api.example.com"  # ARCA API endpoint
  timeout: "30s"                             # Request timeout
  auth_token: ""                             # Set via Secret
  tls:
    ca_cert_path: "/etc/csi-arca-storage/ca.crt"  # Path to CA cert
    client_cert_path: ""                     # Optional: mTLS client cert
    client_key_path: ""                      # Optional: mTLS client key
    insecure_skip_verify: false              # Skip TLS verification (NOT for production)
```

### Network Configuration

```yaml
network:
  pools:
    - cidr: "10.0.0.0/24"          # Network CIDR
      range: "10.0.0.100-10.0.0.200"  # IP range for SVMs (optional)
      vlan: 100                     # VLAN ID
      gateway: "10.0.0.1"           # Gateway IP
  mtu: 1500                         # MTU for network interfaces
```

**Notes**:
- Multiple pools can be defined for round-robin allocation
- If `range` is omitted, entire CIDR is used
- MTU should match your network infrastructure (use 9000 for jumbo frames)

### Driver Configuration

```yaml
driver:
  node_id: ""                       # Auto-detected from hostname if empty
  endpoint: "unix:///csi/csi.sock"  # CSI gRPC endpoint
  state_file_path: "/var/lib/csi-arca-storage/node-volumes.json"  # Node state file
  base_mount_path: "/var/lib/kubelet/plugins/csi.arca-storage.io/mounts"  # Mount base path
```

## TLS Configuration

### Using CA Certificate

1. Create a ConfigMap with your CA certificate:

```bash
kubectl create configmap csi-arca-storage-ca \
  --namespace=kube-system \
  --from-file=ca.crt=/path/to/ca.crt
```

2. Mount the ConfigMap in controller and node pods:

```yaml
volumeMounts:
  - name: ca-cert
    mountPath: /etc/csi-arca-storage
    readOnly: true
volumes:
  - name: ca-cert
    configMap:
      name: csi-arca-storage-ca
```

### Using Mutual TLS (mTLS)

1. Create a Secret with client certificates:

```bash
kubectl create secret generic csi-arca-storage-client-certs \
  --namespace=kube-system \
  --from-file=client.crt=/path/to/client.crt \
  --from-file=client.key=/path/to/client.key
```

2. Update configuration to reference the certificates:

```yaml
arca:
  tls:
    ca_cert_path: "/etc/csi-arca-storage/ca.crt"
    client_cert_path: "/etc/csi-arca-storage/client.crt"
    client_key_path: "/etc/csi-arca-storage/client.key"
```

## Resource Requirements

### Controller Pod

**Default Resources**:
- Requests: 100m CPU, 128Mi memory
- Limits: 500m CPU, 512Mi memory

**Production Recommendations**:
- Requests: 200m CPU, 256Mi memory
- Limits: 1000m CPU, 1Gi memory

### Node Pod (per node)

**Default Resources**:
- Requests: 50m CPU, 64Mi memory
- Limits: 200m CPU, 256Mi memory

**Adjust based on**:
- Number of volumes per node
- Mount/unmount frequency
- Network I/O load

## High Availability

### Controller HA

For production, run multiple controller replicas:

```yaml
replicas: 2  # Or more
```

**Behavior**:
- Leader election ensures only one active controller
- Automatic failover on controller failure
- No downtime during controller updates

### Node Plugin HA

Node plugins run as a DaemonSet (one per node). HA is inherent:
- Each node has its own plugin instance
- Node plugin failure only affects that node
- Automatic restart on failure

## Upgrade Strategy

### Rolling Update (Recommended)

```bash
# Update image tag in your kustomization or manifest
kubectl set image statefulset/csi-arca-storage-controller \
  -n kube-system \
  csi-driver=csi-arca-storage:v1.1.0

# Node plugin will automatically roll out to all nodes
kubectl set image daemonset/csi-arca-storage-node \
  -n kube-system \
  csi-driver=csi-arca-storage:v1.1.0
```

### Monitoring Rollout

```bash
# Watch controller update
kubectl rollout status statefulset/csi-arca-storage-controller -n kube-system

# Watch node plugin update
kubectl rollout status daemonset/csi-arca-storage-node -n kube-system
```

## Troubleshooting

### Check Driver Status

```bash
# Controller logs
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver

# Node plugin logs (specific node)
kubectl logs -n kube-system -l app=csi-arca-storage-node -c csi-driver \
  --field-selector spec.nodeName=<node-name>

# Provisioner logs
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-provisioner
```

### Common Issues

#### 1. Volume Creation Fails

**Symptoms**: PVC stuck in `Pending` state

**Check**:
```bash
kubectl describe pvc <pvc-name>
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver
```

**Common Causes**:
- ARCA API not reachable
- Invalid auth token
- Network pool exhausted
- Insufficient permissions

#### 2. Volume Mount Fails

**Symptoms**: Pod stuck in `ContainerCreating`

**Check**:
```bash
kubectl describe pod <pod-name>
kubectl logs -n kube-system -l app=csi-arca-storage-node -c csi-driver \
  --field-selector spec.nodeName=<node-name>
```

**Common Causes**:
- Network connectivity to SVM VIP
- NFS mount failure
- Permission issues

#### 3. Snapshot Creation Fails

**Check**:
```bash
kubectl describe volumesnapshot <snapshot-name>
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-snapshotter
```

**Common Causes**:
- XFS reflink not supported on backend
- Source volume not found
- API error

### Enable Debug Logging

Edit controller/node manifests to increase log verbosity:

```yaml
args:
  - --config=/etc/csi-arca-storage/config.yaml
  - -v=8  # Maximum verbosity (default: 5)
```

## Uninstallation

### Delete in Reverse Order

```bash
# Delete storage resources first
kubectl delete -f deploy/examples/pod.yaml
kubectl delete -f deploy/examples/pvc.yaml

# Delete storage classes
kubectl delete -f deploy/examples/storageclass.yaml
kubectl delete -f deploy/examples/volumesnapshotclass.yaml

# Delete driver components
kubectl delete -f deploy/node.yaml
kubectl delete -f deploy/controller.yaml
kubectl delete -f deploy/rbac-node.yaml
kubectl delete -f deploy/rbac-controller.yaml
kubectl delete -f deploy/csidriver.yaml

# Delete secrets and configmaps
kubectl delete secret csi-arca-storage-secret -n kube-system
kubectl delete configmap csi-arca-storage-config -n kube-system
```

### Clean Up Node State

On each node (if needed):

```bash
sudo rm -rf /var/lib/csi-arca-storage
sudo rm -rf /var/lib/kubelet/plugins/csi.arca-storage.io
```

## Security Considerations

1. **Secrets Management**
   - Never commit secrets to git
   - Use Kubernetes Secrets or external secret managers
   - Rotate auth tokens regularly

2. **Network Policies**
   - Restrict ARCA API access to driver pods only
   - Use NetworkPolicies to limit traffic

3. **RBAC**
   - Review and customize RBAC permissions
   - Follow principle of least privilege

4. **TLS**
   - Always use TLS in production
   - Don't skip certificate verification
   - Use mTLS for enhanced security

## Performance Tuning

### NFS Mount Options

Adjust mount options in StorageClass:

```yaml
mountOptions:
  - nfsvers=4.2       # Use NFSv4.2
  - rsize=1048576     # 1MB read size
  - wsize=1048576     # 1MB write size
  - hard              # Hard mount (vs soft)
  - timeo=600         # 60s timeout
  - retrans=2         # Retransmit attempts
  - noresvport        # Use non-reserved port
```

### Network MTU

For high-throughput workloads, enable jumbo frames:

```yaml
network:
  mtu: 9000  # Requires jumbo frames support in network
```

### Resource Limits

Adjust based on workload:

```yaml
resources:
  requests:
    cpu: 200m
    memory: 256Mi
  limits:
    cpu: 1000m
    memory: 1Gi
```
