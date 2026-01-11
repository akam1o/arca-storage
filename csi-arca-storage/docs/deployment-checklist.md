# Deployment Checklist

English | [日本語](deployment-checklist.ja.md)

Use this checklist to ensure a successful deployment of the CSI ARCA Storage driver.

## Pre-Deployment

### Infrastructure Requirements

- [ ] Kubernetes cluster version 1.20 or later
- [ ] kubectl configured with cluster admin access
- [ ] ARCA storage backend is operational
- [ ] Network connectivity verified between:
  - [ ] Controller pod network → ARCA API
  - [ ] Node network → ARCA storage network (data plane)
  - [ ] All nodes can reach configured VLAN/network pools

### ARCA Backend

- [ ] ARCA API endpoint URL confirmed
- [ ] Authentication token generated with required permissions:
  - [ ] Create/delete SVMs
  - [ ] Create/delete directories
  - [ ] Create/delete snapshots
  - [ ] Set/update quotas
- [ ] TLS certificates prepared (if using TLS):
  - [ ] CA certificate
  - [ ] Client certificate (if using mTLS)
  - [ ] Client private key (if using mTLS)

### Network Configuration

- [ ] Network CIDR ranges identified
- [ ] VLAN IDs allocated
- [ ] IP address ranges for SVM allocation confirmed
- [ ] Gateway addresses noted
- [ ] MTU size determined (1500 for standard, 9000 for jumbo frames)
- [ ] Firewall rules configured (if applicable):
  - [ ] HTTPS to ARCA API
  - [ ] NFS ports (2049, 111) from nodes to storage network

### CSI Snapshot Support

- [ ] VolumeSnapshot CRDs installed (if using snapshots):
  ```bash
  kubectl get crd volumesnapshots.snapshot.storage.k8s.io
  kubectl get crd volumesnapshotcontents.snapshot.storage.k8s.io
  kubectl get crd volumesnapshotclasses.snapshot.storage.k8s.io
  ```
- [ ] If not installed, install snapshot CRDs:
  ```bash
  kubectl apply -f https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/master/client/config/crd/snapshot.storage.k8s.io_volumesnapshotclasses.yaml
  kubectl apply -f https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/master/client/config/crd/snapshot.storage.k8s.io_volumesnapshotcontents.yaml
  kubectl apply -f https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/master/client/config/crd/snapshot.storage.k8s.io_volumesnapshots.yaml
  ```

## Configuration

### Configuration Files

- [ ] Copy config.example.yaml to your configuration management system
- [ ] Update ARCA API base_url
- [ ] Configure network pools with correct CIDR, ranges, VLANs, gateways
- [ ] Set MTU size
- [ ] Configure TLS settings
- [ ] Review and adjust timeout values

### Secrets

- [ ] Create authentication secret:
  ```bash
  kubectl create secret generic csi-arca-storage-secret \
    --namespace=kube-system \
    --from-literal=auth-token='your-token-here'
  ```
- [ ] If using TLS with CA cert:
  ```bash
  kubectl create configmap csi-arca-storage-ca \
    --namespace=kube-system \
    --from-file=ca.crt=/path/to/ca.crt
  ```
- [ ] If using mTLS:
  ```bash
  kubectl create secret generic csi-arca-storage-client-certs \
    --namespace=kube-system \
    --from-file=client.crt=/path/to/client.crt \
    --from-file=client.key=/path/to/client.key
  ```

### Container Image

- [ ] Build container image:
  ```bash
  cd csi-arca-storage
  docker build -t <your-registry>/csi-arca-storage:v1.0.0 .
  ```
- [ ] Push to registry:
  ```bash
  docker push <your-registry>/csi-arca-storage:v1.0.0
  ```
- [ ] Update image references in manifests or kustomization.yaml

## Deployment

### Deploy CSI Driver

Choose your deployment method:

#### Option A: Direct kubectl

- [ ] Deploy driver CRDs (required before controller starts):
  ```bash
  kubectl apply -k deploy/crds/
  ```
- [ ] Deploy CSIDriver:
  ```bash
  kubectl apply -f deploy/csidriver.yaml
  ```
- [ ] Deploy RBAC:
  ```bash
  kubectl apply -f deploy/rbac-controller.yaml
  kubectl apply -f deploy/rbac-node.yaml
  ```
- [ ] Deploy controller:
  ```bash
  kubectl apply -f deploy/controller.yaml
  ```
- [ ] Deploy node plugin:
  ```bash
  kubectl apply -f deploy/node.yaml
  ```

#### Option B: Kustomize

- [ ] For development:
  ```bash
  kubectl apply -k deploy/kustomize/overlays/development
  ```
- [ ] For production:
  ```bash
  kubectl apply -k deploy/kustomize/overlays/production
  ```

### Deploy Storage Classes

- [ ] Deploy StorageClass:
  ```bash
  kubectl apply -f deploy/examples/storageclass.yaml
  ```
- [ ] Deploy VolumeSnapshotClass (if using snapshots):
  ```bash
  kubectl apply -f deploy/examples/volumesnapshotclass.yaml
  ```
- [ ] Set default StorageClass (if desired):
  ```bash
  kubectl patch storageclass arca-storage \
    -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
  ```

## Verification

### Pod Status

- [ ] Check controller pod is running:
  ```bash
  kubectl get pods -n kube-system -l app=csi-arca-storage-controller
  ```
  Expected: 1 pod with 5/5 containers Running

- [ ] Check node pods are running:
  ```bash
  kubectl get pods -n kube-system -l app=csi-arca-storage-node
  ```
  Expected: 1 pod per node with 3/3 containers Running

### CSI Driver Registration

- [ ] Verify CSIDriver is registered:
  ```bash
  kubectl get csidriver csi.arca-storage.io
  ```
- [ ] Check driver capabilities:
  ```bash
  kubectl describe csidriver csi.arca-storage.io
  ```

### Storage Classes

- [ ] Verify StorageClass exists:
  ```bash
  kubectl get storageclass arca-storage
  ```
- [ ] Verify VolumeSnapshotClass exists (if using snapshots):
  ```bash
  kubectl get volumesnapshotclass arca-snapshots
  ```

### Log Validation

- [ ] Check controller logs for errors:
  ```bash
  kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver --tail=50
  ```
  Look for:
  - "Starting CSI ARCA Storage Driver"
  - "Configuration loaded successfully"
  - No error messages

- [ ] Check node plugin logs on a sample node:
  ```bash
  NODE=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')
  kubectl logs -n kube-system -l app=csi-arca-storage-node \
    -c csi-driver --field-selector spec.nodeName=$NODE --tail=50
  ```

## Functional Testing

### Create Test PVC

- [ ] Create test PVC:
  ```bash
  kubectl apply -f deploy/examples/pvc.yaml
  ```
- [ ] Verify PVC is Bound:
  ```bash
  kubectl get pvc example-pvc
  ```
  Expected: STATUS = Bound

### Mount Volume in Pod

- [ ] Create test pod:
  ```bash
  kubectl apply -f deploy/examples/pod.yaml
  ```
- [ ] Verify pod is Running:
  ```bash
  kubectl get pod example-pod
  ```
- [ ] Verify volume is mounted:
  ```bash
  kubectl exec example-pod -- df -h /data
  ```
- [ ] Test write operation:
  ```bash
  kubectl exec example-pod -- sh -c "echo 'test' > /data/test.txt"
  kubectl exec example-pod -- cat /data/test.txt
  ```
  Expected: "test"

### Test Snapshot (Optional)

- [ ] Create snapshot:
  ```bash
  kubectl apply -f deploy/examples/snapshot.yaml
  ```
- [ ] Verify snapshot is Ready:
  ```bash
  kubectl get volumesnapshot example-snapshot
  ```
  Expected: READYTOUSE = true

- [ ] Create PVC from snapshot:
  ```yaml
  apiVersion: v1
  kind: PersistentVolumeClaim
  metadata:
    name: restored-pvc
  spec:
    accessModes: [ReadWriteMany]
    storageClassName: arca-storage
    dataSource:
      name: example-snapshot
      kind: VolumeSnapshot
      apiGroup: snapshot.storage.k8s.io
    resources:
      requests:
        storage: 10Gi
  ```
- [ ] Verify data is restored:
  ```bash
  kubectl exec restored-pod -- cat /data/test.txt
  ```

### Test Volume Expansion

- [ ] Edit PVC to increase size:
  ```bash
  kubectl patch pvc example-pvc -p '{"spec":{"resources":{"requests":{"storage":"20Gi"}}}}'
  ```
- [ ] Verify expansion succeeded:
  ```bash
  kubectl get pvc example-pvc
  ```
  Expected: CAPACITY = 20Gi

## Production Readiness

### High Availability

- [ ] Set controller replicas to 2+ for HA:
  ```bash
  kubectl scale statefulset csi-arca-storage-controller -n kube-system --replicas=2
  ```
- [ ] Verify leader election is working:
  ```bash
  kubectl logs -n kube-system csi-arca-storage-controller-0 -c csi-provisioner | grep leader
  ```

### Resource Limits

- [ ] Review and adjust resource requests/limits based on cluster size
- [ ] Monitor resource usage:
  ```bash
  kubectl top pods -n kube-system -l app=csi-arca-storage-controller
  kubectl top pods -n kube-system -l app=csi-arca-storage-node
  ```

### Monitoring

- [ ] Set up log aggregation for CSI driver logs
- [ ] Configure alerts for:
  - [ ] Pod restarts
  - [ ] Volume provisioning failures
  - [ ] Mount failures
  - [ ] API connectivity issues
- [ ] Monitor PVC provisioning time
- [ ] Monitor volume mount/unmount latency

### Security

- [ ] Review RBAC permissions (principle of least privilege)
- [ ] Ensure secrets are not committed to git
- [ ] Enable Pod Security Standards/Policies if required
- [ ] Configure NetworkPolicies to restrict driver pod network access
- [ ] Rotate authentication tokens regularly

### Backup and Disaster Recovery

- [ ] Document SVM naming convention (namespace-based)
- [ ] Document volume path convention
- [ ] Establish snapshot retention policy
- [ ] Test volume restoration from snapshots
- [ ] Document driver configuration for disaster recovery

## Post-Deployment

### Documentation

- [ ] Document custom configurations
- [ ] Document network topology
- [ ] Create runbooks for common issues
- [ ] Train operations team on troubleshooting

### Cleanup Test Resources

- [ ] Delete test pod:
  ```bash
  kubectl delete pod example-pod
  ```
- [ ] Delete test PVC:
  ```bash
  kubectl delete pvc example-pvc
  ```
- [ ] Delete test snapshot:
  ```bash
  kubectl delete volumesnapshot example-snapshot
  ```

## Rollback Plan

In case of issues:

- [ ] Document current driver version
- [ ] Keep previous manifests for rollback:
  ```bash
  kubectl rollout undo statefulset/csi-arca-storage-controller -n kube-system
  kubectl rollout undo daemonset/csi-arca-storage-node -n kube-system
  ```
- [ ] Test that existing volumes remain accessible after rollback
- [ ] Document any known issues or workarounds

## Support Contacts

- [ ] ARCA storage backend support contact: _______________
- [ ] Kubernetes cluster administrator: _______________
- [ ] Network team contact: _______________
- [ ] On-call escalation: _______________

---

**Deployment Date**: _______________  
**Deployed By**: _______________  
**Driver Version**: _______________  
**Environment**: [ ] Development [ ] Staging [ ] Production
