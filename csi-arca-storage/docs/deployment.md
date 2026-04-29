# CSI ARCA Storage Deployment Guide

English | [日本語](deployment.ja.md)

This guide describes how to deploy the CSI ARCA Storage driver to Kubernetes using the manifests in `deploy/`. It reflects the current driver layout: the controller stores volume and snapshot metadata in ARCA-specific CRDs, the node plugin mounts one shared NFS export per SVM, and authentication is injected through `ARCA_AUTH_TOKEN`.

Run the commands in this document from the `csi-arca-storage/` directory.

## Deployment Model

The deployed driver consists of:

- `CSIDriver` registration: `deploy/csidriver.yaml`
- Controller `StatefulSet`: `deploy/controller.yaml` for direct apply, or `deploy/controller-statefulset.yaml` through Kustomize
- Node `DaemonSet`: `deploy/node.yaml`
- Controller and node RBAC: `deploy/rbac-controller.yaml`, `deploy/rbac-node.yaml`
- Driver metadata CRDs: `deploy/crds/`
- Example `StorageClass` and `VolumeSnapshotClass`: `deploy/examples/`

The controller uses `ArcaVolume` and `ArcaSnapshot` cluster-scoped CRDs as its persistent metadata store. Apply these CRDs before starting the controller.

## Prerequisites

### Kubernetes Cluster

- Kubernetes 1.25 or later.
  - The bundled `ArcaVolume` and `ArcaSnapshot` CRDs use CEL validation through `x-kubernetes-validations`, which graduated to beta in Kubernetes 1.25. See the Kubernetes note on [CRD validation rules](https://kubernetes.io/blog/2022/09/23/crd-validation-rules-beta/).
- `kubectl` access with permission to create CRDs, cluster-scoped RBAC, `StatefulSet`, `DaemonSet`, `Secret`, and `ConfigMap` resources.
- Snapshot CRDs and the snapshot controller installed if you plan to use `VolumeSnapshot` and `VolumeSnapshotClass`.
- Kubelet plugin paths follow the standard `/var/lib/kubelet/...` layout used by the manifests.
- Nodes allow privileged CSI node pods with `SYS_ADMIN`, host mounts, and `hostNetwork`.

### ARCA Storage Backend

- ARCA API endpoint reachable from the controller and node pods.
- Authentication token with permission to create/delete SVMs, directories, snapshots, and quotas.
- Backend support for:
  - SVM lifecycle operations
  - Directory creation/deletion
  - Quota set/expand/get operations
  - Server-side snapshot/clone operations
- NFS-Ganesha exports available on the SVM VIPs.

### Network

- Controller pods can reach the ARCA API endpoint.
- Every Kubernetes node can reach every SVM VIP selected from the configured pools.
- NFSv4.2 traffic from nodes to the storage network is allowed.
- The configured MTU matches the storage network. Use `1500` unless jumbo frames are configured end to end.

### Local Tools

- `kubectl`
- `kustomize` support through `kubectl apply -k` or a standalone `kustomize` binary
- Docker or a compatible builder if you build your own image

## Before You Deploy

### 1. Choose an Image

The raw manifests use `csi-arca-storage:latest`. The production Kustomize overlay uses `ghcr.io/akam1o/csi-arca-storage:v1.0.0`.

For a custom image:

```bash
docker build -t <registry>/csi-arca-storage:<tag> .
docker push <registry>/csi-arca-storage:<tag>
```

Then update either:

- `deploy/controller.yaml` and `deploy/node.yaml` for direct apply
- `deploy/kustomize/overlays/*/kustomization.yaml` for Kustomize

### 2. Prepare ARCA API Configuration

The driver reads `/etc/csi-arca-storage/config.yaml` inside the pod. In Kubernetes this file is supplied by the `csi-arca-storage-config` ConfigMap.

Current configuration schema:

```yaml
arca:
  base_url: "https://arca-api.example.com"
  timeout: "30s"
  auth_token: ""  # Prefer ARCA_AUTH_TOKEN from Secret
  tls:
    ca_cert_path: ""
    client_cert_path: ""
    client_key_path: ""
    insecure_skip_verify: false

network:
  pools:
    - cidr: "10.0.0.0/24"
      range: "10.0.0.100-10.0.0.200"
      vlan: 100
      gateway: "10.0.0.1"
  mtu: 1500

driver:
  endpoint: "unix:///csi/csi.sock"
  state_file_path: "/var/lib/csi-arca-storage/node-volumes.json"
  base_mount_path: "/var/lib/kubelet/plugins/csi.arca-storage.io/mounts"
```

Notes:

- `arca.base_url`, at least one `network.pools[].cidr`, and `driver.endpoint` are required.
- `arca.timeout` defaults to `30s` if omitted.
- `network.mtu` defaults to `1500` if omitted.
- `ARCA_AUTH_TOKEN` overrides `arca.auth_token` when set.
- `CSI_ENDPOINT` overrides `driver.endpoint` when set by the manifests.
- `network.pools[].range`, `vlan`, and `gateway` are optional, but production deployments should make the intended allocation range explicit.

### 3. Prepare Secrets

Create the API token Secret:

```bash
kubectl create secret generic csi-arca-storage-secret \
  --namespace=kube-system \
  --from-literal=auth-token='<your-arca-api-token>'
```

The controller and node manifests read this value as `ARCA_AUTH_TOKEN`.

### 4. Prepare Snapshot Support

The ARCA driver CRDs are not the same as Kubernetes snapshot CRDs.

Apply the driver CRDs:

```bash
kubectl apply -k deploy/crds/
```

If you use Kubernetes `VolumeSnapshot`, confirm these CRDs also exist:

```bash
kubectl get crd volumesnapshots.snapshot.storage.k8s.io
kubectl get crd volumesnapshotcontents.snapshot.storage.k8s.io
kubectl get crd volumesnapshotclasses.snapshot.storage.k8s.io
```

If they are missing, install the snapshot CRDs and snapshot controller according to your cluster distribution or the external-snapshotter release you standardize on.

## Deployment Method 1: Direct `kubectl apply`

Use direct apply for quick tests or small environments where editing the raw manifests is acceptable.

### 1. Edit the ConfigMap

Edit `deploy/controller.yaml` and update the embedded `csi-arca-storage-config` ConfigMap:

```yaml
data:
  config.yaml: |
    arca:
      base_url: "https://arca-api.example.com"
      timeout: "30s"
      auth_token: ""
      tls:
        ca_cert_path: ""
        insecure_skip_verify: false

    network:
      pools:
        - cidr: "10.0.0.0/24"
          range: "10.0.0.100-10.0.0.200"
          vlan: 100
          gateway: "10.0.0.1"
      mtu: 1500

    driver:
      endpoint: "unix:///csi/csi.sock"
      state_file_path: "/var/lib/csi-arca-storage/node-volumes.json"
      base_mount_path: "/var/lib/kubelet/plugins/csi.arca-storage.io/mounts"
```

### 2. Apply Manifests in Order

```bash
kubectl apply -k deploy/crds/
kubectl apply -f deploy/csidriver.yaml
kubectl apply -f deploy/rbac-controller.yaml
kubectl apply -f deploy/rbac-node.yaml
kubectl apply -f deploy/controller.yaml
kubectl apply -f deploy/node.yaml
```

### 3. Apply Storage Classes

```bash
kubectl apply -f deploy/examples/storageclass.yaml
kubectl apply -f deploy/examples/volumesnapshotclass.yaml
```

`deploy/examples/storageclass.yaml` creates:

- `arca-storage`: default delete policy
- `arca-storage-retain`: retain policy
- `arca-storage-wait`: `WaitForFirstConsumer`

`deploy/examples/volumesnapshotclass.yaml` creates:

- `arca-snapshots`: delete policy
- `arca-snapshots-retain`: retain policy

## Deployment Method 2: Kustomize

Use Kustomize for production or repeatable environment-specific deployment. The Kustomize base uses `controller-statefulset.yaml`, which does not embed a ConfigMap or Secret. Those are generated from overlay files.

The current Kustomize base reuses shared manifests from `deploy/`, outside the base directory. Use `kubectl kustomize --load-restrictor LoadRestrictionsNone ... | kubectl apply -f -`; plain `kubectl apply -k` rejects those out-of-root references.

### Development Overlay

Edit `deploy/kustomize/overlays/development/config.yaml`, then deploy:

```bash
kubectl kustomize --load-restrictor LoadRestrictionsNone \
  deploy/kustomize/overlays/development | kubectl apply -f -
```

The development overlay:

- Uses `csi-arca-storage:dev`
- Generates `csi-arca-storage-config` from `config.yaml`
- Generates `csi-arca-storage-secret` with `auth-token=dev-token`
- Enables `tls.insecure_skip_verify: true` in the example config

### Production Overlay

Edit `deploy/kustomize/overlays/production/config.yaml`, prepare `secrets.env`, and deploy:

```bash
cd deploy/kustomize/overlays/production
cp secrets.env.example secrets.env
printf 'auth-token=%s\n' '<your-production-token>' > secrets.env
cd ../../../..

kubectl kustomize --load-restrictor LoadRestrictionsNone \
  deploy/kustomize/overlays/production | kubectl apply -f -
```

The production overlay:

- Uses `ghcr.io/akam1o/csi-arca-storage:v1.0.0`
- Sets controller replicas to `2`
- Applies higher controller sidecar resource requests and limits
- Generates ConfigMap and Secret with stable names

Keep `secrets.env` out of version control.

## TLS and mTLS

### Custom CA

Create a ConfigMap:

```bash
kubectl create configmap csi-arca-storage-ca \
  --namespace=kube-system \
  --from-file=ca.crt=/path/to/ca.crt
```

Mount it into `/etc/csi-arca-storage` or another path, then set:

```yaml
arca:
  tls:
    ca_cert_path: "/etc/csi-arca-storage/ca.crt"
```

If you mount more than one file into `/etc/csi-arca-storage`, make sure the mount does not hide `config.yaml`. A separate mount path such as `/etc/csi-arca-storage/tls` is often cleaner.

### mTLS

Create a Secret:

```bash
kubectl create secret generic csi-arca-storage-client-certs \
  --namespace=kube-system \
  --from-file=client.crt=/path/to/client.crt \
  --from-file=client.key=/path/to/client.key
```

Mount it read-only and set:

```yaml
arca:
  tls:
    ca_cert_path: "/etc/csi-arca-storage/tls/ca.crt"
    client_cert_path: "/etc/csi-arca-storage/tls/client.crt"
    client_key_path: "/etc/csi-arca-storage/tls/client.key"
    insecure_skip_verify: false
```

Never set `insecure_skip_verify: true` in production.

## Verification

### Controller

```bash
kubectl get statefulset -n kube-system csi-arca-storage-controller
kubectl get pods -n kube-system -l app=csi-arca-storage-controller
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver --tail=100
```

Expected direct-apply controller pod container count: `5/5`.

Controller containers:

- `csi-driver`
- `csi-provisioner`
- `csi-snapshotter`
- `csi-resizer`
- `liveness-probe`

### Node Plugin

```bash
kubectl get daemonset -n kube-system csi-arca-storage-node
kubectl get pods -n kube-system -l app=csi-arca-storage-node
kubectl logs -n kube-system -l app=csi-arca-storage-node -c csi-driver --tail=100
```

Expected node pod container count: `3/3`.

Node containers:

- `csi-driver`
- `node-driver-registrar`
- `liveness-probe`

### CRDs and Registration

```bash
kubectl get crd arcavolumes.storage.arca.io arcasnapshots.storage.arca.io
kubectl get csidriver csi.arca-storage.io
kubectl get storageclass arca-storage
kubectl get volumesnapshotclass arca-snapshots
```

### Functional Smoke Test

Create a PVC:

```bash
kubectl apply -f deploy/examples/pvc.yaml
kubectl get pvc example-pvc
```

Create a pod that mounts it:

```bash
kubectl apply -f deploy/examples/pod.yaml
kubectl get pod example-pod
kubectl exec example-pod -- df -h /data
kubectl exec example-pod -- sh -c "echo test > /data/test.txt"
kubectl exec example-pod -- cat /data/test.txt
```

Check driver metadata:

```bash
kubectl get arcavolumes
```

Snapshot test, if snapshot CRDs and controller are installed:

```bash
kubectl apply -f deploy/examples/snapshot.yaml
kubectl get volumesnapshot example-snapshot
kubectl get arcasnapshots
```

## Operations

### High Availability

The controller runs as a `StatefulSet`. Production uses at least two replicas:

```bash
kubectl scale statefulset csi-arca-storage-controller \
  -n kube-system \
  --replicas=2
```

The CSI sidecars use leader election, so only the active leader performs controller work. Node pods run as a `DaemonSet`; a node plugin restart affects only that node.

### Resource Sizing

Direct-apply defaults:

- Controller driver: `100m` CPU / `128Mi` memory requests, `500m` / `512Mi` limits
- Node driver: `50m` CPU / `64Mi` memory requests, `200m` / `256Mi` limits
- Sidecars have smaller per-container requests and limits in the manifests

Production overlay increases controller-side resources through `controller-patch.yaml`.

Monitor:

```bash
kubectl top pods -n kube-system -l app=csi-arca-storage-controller
kubectl top pods -n kube-system -l app=csi-arca-storage-node
```

### Updating the Driver Image

For direct apply:

```bash
kubectl set image statefulset/csi-arca-storage-controller \
  -n kube-system \
  csi-driver=<registry>/csi-arca-storage:<tag>

kubectl set image daemonset/csi-arca-storage-node \
  -n kube-system \
  csi-driver=<registry>/csi-arca-storage:<tag>

kubectl rollout status statefulset/csi-arca-storage-controller -n kube-system
kubectl rollout status daemonset/csi-arca-storage-node -n kube-system
```

For Kustomize, update the `images` block in the overlay and re-apply.

### Updating Configuration

Direct apply:

```bash
kubectl apply -f deploy/controller.yaml
kubectl rollout restart statefulset/csi-arca-storage-controller -n kube-system
kubectl rollout restart daemonset/csi-arca-storage-node -n kube-system
```

Kustomize:

```bash
kubectl kustomize --load-restrictor LoadRestrictionsNone \
  deploy/kustomize/overlays/production | kubectl apply -f -
kubectl rollout restart statefulset/csi-arca-storage-controller -n kube-system
kubectl rollout restart daemonset/csi-arca-storage-node -n kube-system
```

Restart both controller and node pods when network pools, TLS paths, endpoint, or auth configuration changes.

### Rollback

```bash
kubectl rollout undo statefulset/csi-arca-storage-controller -n kube-system
kubectl rollout undo daemonset/csi-arca-storage-node -n kube-system
```

Do not delete `ArcaVolume` or `ArcaSnapshot` CRDs during rollback unless you intentionally want to remove driver metadata.

## Troubleshooting

### PVC Stays Pending

Check:

```bash
kubectl describe pvc <pvc-name>
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver --tail=200
kubectl get arcavolumes
```

Common causes:

- ARCA API endpoint is unreachable from the controller.
- `ARCA_AUTH_TOKEN` is missing, expired, or lacks required permissions.
- `network.pools` is empty, invalid, or exhausted.
- Driver CRDs were not applied before the controller started.
- Snapshot restore was requested but the source snapshot is not ready.

### Pod Stays ContainerCreating

Check:

```bash
kubectl describe pod <pod-name>
kubectl logs -n kube-system -l app=csi-arca-storage-node -c csi-driver \
  --field-selector spec.nodeName=<node-name> \
  --tail=200
```

Common causes:

- The node cannot reach the SVM VIP.
- NFSv4.2 is blocked by firewall or network policy.
- The backend export is missing or not yet reloaded.
- The node plugin cannot perform privileged mount operations.
- Host paths such as `/var/lib/kubelet/plugins_registry` differ from the manifest assumptions.

### Snapshot Creation Fails

Check:

```bash
kubectl describe volumesnapshot <snapshot-name>
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-snapshotter --tail=200
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver --tail=200
kubectl get arcasnapshots
```

Common causes:

- Kubernetes snapshot CRDs or snapshot controller are missing.
- The backend volume path does not exist.
- Backend XFS/reflink snapshot operation failed.
- ARCA API returned an authorization or conflict error.

### Controller Cannot Start

Check:

```bash
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver --previous
kubectl get crd arcavolumes.storage.arca.io arcasnapshots.storage.arca.io
kubectl get secret csi-arca-storage-secret -n kube-system
kubectl get configmap csi-arca-storage-config -n kube-system -o yaml
```

Common causes:

- `arca.base_url` is empty.
- No network pools are configured.
- `driver.endpoint` is empty.
- CRDs are missing or rejected by the API server.

### Enable Debug Logging

Increase verbosity in controller and node driver args:

```yaml
args:
  - --mode=controller
  - --config=/etc/csi-arca-storage/config.yaml
  - -v=8
```

Re-apply and restart the affected workload.

## Uninstallation

Delete test workloads first:

```bash
kubectl delete -f deploy/examples/pod.yaml --ignore-not-found
kubectl delete -f deploy/examples/pvc.yaml --ignore-not-found
kubectl delete -f deploy/examples/snapshot.yaml --ignore-not-found
```

Delete classes:

```bash
kubectl delete -f deploy/examples/volumesnapshotclass.yaml --ignore-not-found
kubectl delete -f deploy/examples/storageclass.yaml --ignore-not-found
```

Delete driver workloads:

```bash
kubectl delete -f deploy/node.yaml --ignore-not-found
kubectl delete -f deploy/controller.yaml --ignore-not-found
kubectl delete -f deploy/rbac-node.yaml --ignore-not-found
kubectl delete -f deploy/rbac-controller.yaml --ignore-not-found
kubectl delete -f deploy/csidriver.yaml --ignore-not-found
```

Delete generated configuration:

```bash
kubectl delete secret csi-arca-storage-secret -n kube-system --ignore-not-found
kubectl delete configmap csi-arca-storage-config -n kube-system --ignore-not-found
```

Delete driver metadata CRDs only after all volumes and snapshots managed by the driver are no longer needed:

```bash
kubectl delete -k deploy/crds/
```

Optional node cleanup:

```bash
sudo rm -rf /var/lib/csi-arca-storage
sudo rm -rf /var/lib/kubelet/plugins/csi.arca-storage.io
```

## Security Checklist

- Store the ARCA token in a Kubernetes Secret or external secret manager.
- Do not commit `secrets.env`, tokens, or private keys.
- Use TLS with certificate verification in production.
- Use mTLS when the ARCA API requires client identity.
- Restrict ARCA API access to CSI pods through network policy or infrastructure firewall rules.
- Review RBAC before granting additional permissions.
- Keep node plugin privilege limited to the CSI node workload.

## Performance Notes

The node plugin mounts each SVM export once per node and bind-mounts individual volume paths from that shared SVM mount.

Default NFS options used by the driver:

```text
vers=4.2,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport
```

For high-throughput workloads:

- Use jumbo frames only when the whole path supports the configured MTU.
- Keep SVM VIPs close to the worker nodes from a routing and firewall perspective.
- Scale controller resources if provisioning latency rises during PVC or snapshot bursts.
