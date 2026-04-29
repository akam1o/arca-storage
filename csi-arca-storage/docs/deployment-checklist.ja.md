# デプロイチェックリスト

[English](deployment-checklist.md) | 日本語

CSI ARCA Storage driver を安全にデプロイするためのチェックリストです。詳細手順は [deployment.ja.md](deployment.ja.md) を参照してください。

## デプロイ前

### インフラ要件

- [ ] Kubernetes 1.25+
- [ ] `kubectl` がクラスタ管理権限で利用可能
- [ ] ARCA storage backend が稼働している
- [ ] controller Pod から ARCA API へ到達できる
- [ ] 各 node から ARCA storage network / SVM VIP へ到達できる
- [ ] pool の VLAN / CIDR に node から到達できる
- [ ] Node plugin の privileged pod、`SYS_ADMIN`、host mount、`hostNetwork` が許可されている
- [ ] kubelet path が `/var/lib/kubelet/...` である、または manifest を環境に合わせて修正済み

### ARCA backend

- [ ] ARCA API endpoint URL が確定している
- [ ] API token が発行済み
- [ ] token に必要な権限がある:
  ```text
  SVM create/delete
  directory create/delete
  quota set/expand/get
  snapshot create/delete
  ```
- [ ] TLS を使う場合の CA 証明書を準備済み
- [ ] mTLS を使う場合の client 証明書 / 秘密鍵を準備済み
- [ ] backend 側で NFS-Ganesha export が SVM VIP 経由で利用可能

### Network

- [ ] SVM 割り当て用 CIDR / IP range を決定済み
- [ ] VLAN ID を決定済み、または VLAN なし構成として確認済み
- [ ] gateway を決定済み
- [ ] MTU を決定済み（通常は `1500`、jumbo frame は end-to-end 対応時のみ）
- [ ] Firewall / NetworkPolicy を確認済み:
  ```text
  controller/node -> ARCA API
  node -> SVM VIP: NFSv4.2
  ```

### CRD / Snapshot

- [ ] Driver 独自 CRD を適用予定:
  ```bash
  kubectl apply -k deploy/crds/
  ```
- [ ] `ArcaVolume` / `ArcaSnapshot` CRD が Kubernetes 1.25+ の CEL validation 前提であることを確認済み
- [ ] `VolumeSnapshot` を使う場合、snapshot CRD が導入済み:
  ```bash
  kubectl get crd volumesnapshots.snapshot.storage.k8s.io
  kubectl get crd volumesnapshotcontents.snapshot.storage.k8s.io
  kubectl get crd volumesnapshotclasses.snapshot.storage.k8s.io
  ```
- [ ] `VolumeSnapshot` を使う場合、snapshot controller が稼働済み

## 設定

### Config

- [ ] `arca.base_url` を環境に合わせて設定済み
- [ ] `network.pools` に少なくとも 1 つの `cidr` を設定済み
- [ ] `range` / `vlan` / `gateway` を運用設計どおりに設定済み
- [ ] `network.mtu` を設定済み
- [ ] TLS path と `insecure_skip_verify` を確認済み
- [ ] `driver.endpoint` が `unix:///csi/csi.sock` であることを確認済み
- [ ] Node state path と mount base path を確認済み:
  ```text
  /var/lib/csi-arca-storage/node-volumes.json
  /var/lib/kubelet/plugins/csi.arca-storage.io/mounts
  ```

### Secret

- [ ] API token Secret を作成済み:
  ```bash
  kubectl create secret generic csi-arca-storage-secret \
    --namespace=kube-system \
    --from-literal=auth-token='<your-token>'
  ```
- [ ] `ARCA_AUTH_TOKEN` が manifest から Secret key `auth-token` を参照している
- [ ] Kustomize production overlay を使う場合、`secrets.env` を作成済み
- [ ] `secrets.env` が version control 対象外である

### Image

- [ ] 利用する image tag を決定済み
- [ ] 独自 image の場合、build / push 済み:
  ```bash
  docker build -t <registry>/csi-arca-storage:<tag> .
  docker push <registry>/csi-arca-storage:<tag>
  ```
- [ ] 直接適用の場合、`deploy/controller.yaml` と `deploy/node.yaml` の image を更新済み
- [ ] Kustomize の場合、overlay の `images` block を更新済み

## デプロイ

### Option A: `kubectl apply`

- [ ] Driver CRD:
  ```bash
  kubectl apply -k deploy/crds/
  ```
- [ ] CSIDriver:
  ```bash
  kubectl apply -f deploy/csidriver.yaml
  ```
- [ ] RBAC:
  ```bash
  kubectl apply -f deploy/rbac-controller.yaml
  kubectl apply -f deploy/rbac-node.yaml
  ```
- [ ] Controller / node:
  ```bash
  kubectl apply -f deploy/controller.yaml
  kubectl apply -f deploy/node.yaml
  ```

### Option B: Kustomize

- [ ] Development:
  ```bash
  kubectl kustomize --load-restrictor LoadRestrictionsNone \
    deploy/kustomize/overlays/development | kubectl apply -f -
  ```
- [ ] Production:
  ```bash
  kubectl kustomize --load-restrictor LoadRestrictionsNone \
    deploy/kustomize/overlays/production | kubectl apply -f -
  ```
- [ ] Production overlay の controller replica が `2` 以上であることを確認済み

### StorageClass / VolumeSnapshotClass

- [ ] StorageClass:
  ```bash
  kubectl apply -f deploy/examples/storageclass.yaml
  ```
- [ ] VolumeSnapshotClass:
  ```bash
  kubectl apply -f deploy/examples/volumesnapshotclass.yaml
  ```
- [ ] default StorageClass にする場合、annotation を明示的に設定済み:
  ```bash
  kubectl patch storageclass arca-storage \
    -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
  ```

## 確認

### Pod / 登録状態

- [ ] Controller pod:
  ```bash
  kubectl get pods -n kube-system -l app=csi-arca-storage-controller
  ```
  期待値: `5/5` containers Running

- [ ] Node pod:
  ```bash
  kubectl get pods -n kube-system -l app=csi-arca-storage-node
  ```
  期待値: node ごとに `3/3` containers Running

- [ ] CSIDriver:
  ```bash
  kubectl get csidriver csi.arca-storage.io
  ```

- [ ] Driver CRD:
  ```bash
  kubectl get crd arcavolumes.storage.arca.io arcasnapshots.storage.arca.io
  ```

- [ ] StorageClass / VolumeSnapshotClass:
  ```bash
  kubectl get storageclass arca-storage
  kubectl get volumesnapshotclass arca-snapshots
  ```

### Log

- [ ] Controller log に起動成功が出ている:
  ```bash
  kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver --tail=100
  ```
- [ ] Node log に mount manager 初期化が出ている:
  ```bash
  kubectl logs -n kube-system -l app=csi-arca-storage-node -c csi-driver --tail=100
  ```
- [ ] Sidecar log に leader election / socket 接続の異常がない:
  ```bash
  kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-provisioner --tail=100
  kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-snapshotter --tail=100
  ```

## Functional test

### PVC

- [ ] Test PVC を作成:
  ```bash
  kubectl apply -f deploy/examples/pvc.yaml
  ```
- [ ] PVC が Bound:
  ```bash
  kubectl get pvc example-pvc
  ```
- [ ] `ArcaVolume` が作成されている:
  ```bash
  kubectl get arcavolumes
  ```

### Pod mount

- [ ] Test pod を作成:
  ```bash
  kubectl apply -f deploy/examples/pod.yaml
  ```
- [ ] Pod が Running:
  ```bash
  kubectl get pod example-pod
  ```
- [ ] Mount / write が成功:
  ```bash
  kubectl exec example-pod -- df -h /data
  kubectl exec example-pod -- sh -c "echo test > /data/test.txt"
  kubectl exec example-pod -- cat /data/test.txt
  ```

### Snapshot

- [ ] Snapshot を作成:
  ```bash
  kubectl apply -f deploy/examples/snapshot.yaml
  ```
- [ ] VolumeSnapshot が ready:
  ```bash
  kubectl get volumesnapshot example-snapshot
  ```
- [ ] `ArcaSnapshot` が作成されている:
  ```bash
  kubectl get arcasnapshots
  ```

### Expansion

- [ ] PVC を拡張:
  ```bash
  kubectl patch pvc example-pvc \
    -p '{"spec":{"resources":{"requests":{"storage":"20Gi"}}}}'
  ```
- [ ] 容量が更新されている:
  ```bash
  kubectl get pvc example-pvc
  ```

## 本番 readiness

### HA / rollout

- [ ] Controller replicas が 2 以上
- [ ] Leader election が動作している:
  ```bash
  kubectl logs -n kube-system csi-arca-storage-controller-0 -c csi-provisioner | grep leader
  ```
- [ ] Rollout 確認手順を用意済み:
  ```bash
  kubectl rollout status statefulset/csi-arca-storage-controller -n kube-system
  kubectl rollout status daemonset/csi-arca-storage-node -n kube-system
  ```

### Monitoring

- [ ] CSI driver logs を収集している
- [ ] Pod restart を監視している
- [ ] PVC provisioning failure を監視している
- [ ] Mount failure を監視している
- [ ] ARCA API connectivity failure を監視している
- [ ] PVC provisioning / mount latency を確認している

### Security

- [ ] Secret が Git に含まれていない
- [ ] 本番で `insecure_skip_verify: false`
- [ ] 必要に応じて mTLS を有効化
- [ ] ARCA API への通信を CSI pod に限定
- [ ] RBAC をレビュー済み
- [ ] Node plugin の privileged 権限を運用ポリシーとして承認済み
- [ ] Token rotation 手順を用意済み

### Backup / DR

- [ ] SVM 命名規則（namespace ベース）を文書化済み
- [ ] Volume path / ID の規則を文書化済み
- [ ] Snapshot retention policy を決定済み
- [ ] Snapshot restore を検証済み
- [ ] ConfigMap / Secret / overlay の復旧手順を用意済み

## Cleanup

- [ ] Test pod:
  ```bash
  kubectl delete pod example-pod --ignore-not-found
  ```
- [ ] Test PVC:
  ```bash
  kubectl delete pvc example-pvc --ignore-not-found
  ```
- [ ] Test snapshot:
  ```bash
  kubectl delete volumesnapshot example-snapshot --ignore-not-found
  ```

## Rollback plan

- [ ] 現在の driver image tag を記録済み
- [ ] 直前の manifest / overlay を保持済み
- [ ] Rollback command を確認済み:
  ```bash
  kubectl rollout undo statefulset/csi-arca-storage-controller -n kube-system
  kubectl rollout undo daemonset/csi-arca-storage-node -n kube-system
  ```
- [ ] Rollback 後も既存 volume が mount できることを検証済み
- [ ] Rollback 時に `ArcaVolume` / `ArcaSnapshot` CRD を削除しないことを確認済み

---

**Deployment Date**: _______________
**Deployed By**: _______________
**Driver Version**: _______________
**Environment**: [ ] Development [ ] Staging [ ] Production
