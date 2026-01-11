# デプロイチェックリスト

[English](deployment-checklist.md) | 日本語

CSI ARCA Storage driver を安全にデプロイするためのチェックリストです。

## デプロイ前

### インフラ要件

- [ ] Kubernetes 1.20+
- [ ] `kubectl` がクラスタ管理権限で利用可能
- [ ] ARCA storage backend が稼働している
- [ ] ネットワーク疎通を確認済み:
  - [ ] controller Pod → ARCA API（HTTPS）
  - [ ] 各 node → ARCA storage network（NFS / データプレーン）
  - [ ] 各 node から pool の VLAN/CIDR に到達できる

### ARCA backend

- [ ] ARCA API endpoint URL が確定
- [ ] token の権限が十分（例）:
  - [ ] SVM 作成/削除
  - [ ] ディレクトリ作成/削除
  - [ ] snapshot 作成/削除
  - [ ] quota 設定/更新
- [ ] TLS を使う場合の証明書を準備済み:
  - [ ] CA 証明書
  - [ ]（任意）mTLS の client 証明書/秘密鍵

### network 設定

- [ ] pool の CIDR / IP range / VLAN / gateway を決定
- [ ] MTU を決定（1500 / 9000 など）
- [ ] 必要な FW 設定を反映（該当する場合）:
  - [ ] ARCA API（HTTPS）
  - [ ] NFS（例: 2049, 111）

### Snapshot 機能（利用する場合）

- [ ] snapshot CRD が導入済み:
  ```bash
  kubectl get crd volumesnapshots.snapshot.storage.k8s.io
  kubectl get crd volumesnapshotcontents.snapshot.storage.k8s.io
  kubectl get crd volumesnapshotclasses.snapshot.storage.k8s.io
  ```
- [ ] 未導入の場合は導入手順を用意（external-snapshotter の CRD 適用など）

## 設定

### 設定ファイル

- [ ] `config.example.yaml` をベースに設定を作成済み
- [ ] `arca.base_url` を設定済み
- [ ] network pool を設定済み
- [ ] TLS 設定をレビュー済み

### Secret

- [ ] 認証 Secret を作成済み:
  ```bash
  kubectl create secret generic csi-arca-storage-secret \
    --namespace=kube-system \
    --from-literal=auth-token='your-token-here'
  ```

## デプロイ

### CSI Driver のデプロイ

#### Option A: `kubectl apply`

- [ ] CRD を先に適用:
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
- [ ] controller / node:
  ```bash
  kubectl apply -f deploy/controller.yaml
  kubectl apply -f deploy/node.yaml
  ```

#### Option B: Kustomize

- [ ] 開発:
  ```bash
  kubectl apply -k deploy/kustomize/overlays/development
  ```
- [ ] 本番:
  ```bash
  kubectl apply -k deploy/kustomize/overlays/production
  ```

### StorageClass

- [ ] StorageClass:
  ```bash
  kubectl apply -f deploy/examples/storageclass.yaml
  ```
- [ ] VolumeSnapshotClass（snapshot を使う場合）:
  ```bash
  kubectl apply -f deploy/examples/volumesnapshotclass.yaml
  ```

## 確認

- [ ] controller Pod:
  ```bash
  kubectl get pods -n kube-system -l app=csi-arca-storage-controller
  ```
- [ ] node Pod:
  ```bash
  kubectl get pods -n kube-system -l app=csi-arca-storage-node
  ```
- [ ] CSIDriver:
  ```bash
  kubectl get csidriver csi.arca-storage.io
  ```
