# Quick Start Guide

English | [日本語](quickstart.ja.md)

Get the CSI ARCA Storage driver running in your Kubernetes cluster in under 10 minutes.

## Prerequisites

- Kubernetes 1.20+
- ARCA storage backend with API access
- `kubectl` configured

## Step 0: Install Required CRDs

Install the CSI driver's own CRDs first:

```bash
kubectl apply -k deploy/crds/
```

If you plan to use `VolumeSnapshot`/`VolumeSnapshotClass`, ensure snapshot CRDs are installed (many clusters do not have them by default):

```bash
kubectl get crd volumesnapshots.snapshot.storage.k8s.io
```

If they are missing, install snapshot CRDs first (see `docs/deployment-checklist.md`).

## Step 1: Create Authentication Secret

```bash
kubectl create secret generic csi-arca-storage-secret \
  --namespace=kube-system \
  --from-literal=auth-token='your-arca-api-token'
```

## Step 2: Configure Network Pools

Edit `deploy/controller.yaml` and update the network configuration:

```yaml
data:
  config.yaml: |
    arca:
      base_url: "https://your-arca-api.example.com"  # Update this
      timeout: "30s"
      auth_token: ""
    
    network:
      pools:
        - cidr: "10.0.0.0/24"          # Update to your network
          range: "10.0.0.100-10.0.0.200"
          vlan: 100
          gateway: "10.0.0.1"
      mtu: 1500
```

## Step 3: Deploy the Driver

```bash
# From the project root
kubectl apply -f deploy/csidriver.yaml
kubectl apply -f deploy/rbac-controller.yaml
kubectl apply -f deploy/rbac-node.yaml
kubectl apply -f deploy/controller.yaml
kubectl apply -f deploy/node.yaml
```

## Step 4: Create Storage Class

```bash
kubectl apply -f deploy/examples/storageclass.yaml
kubectl apply -f deploy/examples/volumesnapshotclass.yaml
```

## Step 5: Verify Installation

```bash
# Check that all pods are running
kubectl get pods -n kube-system | grep csi-arca-storage

# You should see:
# csi-arca-storage-controller-0   5/5   Running
# csi-arca-storage-node-xxxxx     3/3   Running (one per node)

# Verify CSIDriver is registered
kubectl get csidriver csi.arca-storage.io
```

## Step 6: Create Your First Volume

Create a test PVC:

```bash
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-pvc
  namespace: default
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: arca-storage
  resources:
    requests:
      storage: 1Gi
EOF
```

Verify the PVC is bound:

```bash
kubectl get pvc test-pvc

# Should show:
# NAME       STATUS   VOLUME                                     CAPACITY
# test-pvc   Bound    pvc-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx   1Gi
```

## Step 7: Use the Volume in a Pod

```bash
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: test-pod
  namespace: default
spec:
  containers:
    - name: app
      image: nginx:latest
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: test-pvc
EOF
```

Verify the pod is running and volume is mounted:

```bash
kubectl get pod test-pod
kubectl exec test-pod -- df -h /data
```

## Next Steps

- [Create snapshots](deployment.md#3-snapshot-creation-fails)
- [Clone volumes](../deploy/examples/snapshot.yaml)
- [Expand volumes](../README.md#volume-expansion)
- [Configure HA](deployment.md#high-availability)

## Troubleshooting

If something doesn't work:

1. **Check controller logs**:
   ```bash
   kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver
   ```

2. **Check node plugin logs**:
   ```bash
   kubectl logs -n kube-system -l app=csi-arca-storage-node -c csi-driver
   ```

3. **Check PVC events**:
   ```bash
   kubectl describe pvc test-pvc
   ```

See [deployment.md](deployment.md) for detailed troubleshooting.

## Clean Up

To remove the test resources:

```bash
kubectl delete pod test-pod
kubectl delete pvc test-pvc
```

To uninstall the driver, see the [uninstallation guide](deployment.md#uninstallation).
