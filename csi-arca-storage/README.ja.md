# CSI Driver for ARCA Storage

[English](README.md) | 日本語

ARCA Storage 向けの Kubernetes CSI (Container Storage Interface) ドライバです。XFS の project quota による容量制御と、reflink ベースの高速スナップショットを使って、永続ボリュームを動的にプロビジョニングします。

## 特長

- **動的プロビジョニング**: PVC 作成に応じてボリューム（ディレクトリ）を自動作成
- **スナップショット**: サーバー側 reflink による高速・省スペースなスナップショット
- **クローン**: 既存ボリューム / スナップショットからのクローン
- **拡張**: クォータ更新によるオンライン拡張
- **Namespace ごとの SVM 分離**: Kubernetes namespace 単位で Storage Virtual Machine を分離
- **複数 AccessMode**: ReadWriteOnce / ReadOnlyMany / ReadWriteMany
- **冪等性**: リトライされても安全に再実行できるよう設計

## アーキテクチャ

この CSI ドライバは大きく 2 つのコンポーネントで構成されます。

1. **Controller Plugin**: Volume/Snapshot の作成・削除、拡張などの制御系
2. **Node Plugin**: 各ノード上でのマウント/アンマウント（NFS）

主な内部コンポーネントは以下です。

- **ARCA API Client**: ARCA Storage backend の REST API クライアント
- **SVM Manager**: SVM のライフサイクル管理（Kubernetes Lease による分散ロック）
- **Network Allocator**: 設定した pool からのラウンドロビン IP 割り当て
- **Mount Manager**: SVM 単位で共有 NFS マウントを維持（参照カウント）
- **Node State**: ノード側の永続状態（クラッシュリカバリ）

## ビルド

```bash
go mod download
go build -o bin/csi-driver ./cmd/csi-driver
```

## 設定

設定ファイルは `/etc/csi-arca-storage/config.yaml` を想定しています（Kubernetes では ConfigMap で配布し、Pod 内へマウントします）。

```yaml
arca:
  base_url: "https://arca-api.example.com"
  timeout: "30s"
  auth_token: ""
  tls:
    ca_cert_path: "/etc/csi-arca-storage/ca.crt"
    insecure_skip_verify: false

network:
  pools:
    - cidr: "10.0.0.0/24"
      range: "10.0.0.100-10.0.0.200"
      vlan: 100
      gateway: "10.0.0.1"
  mtu: 1500

driver:
  node_id: ""  # 未指定なら hostname から自動判別
  endpoint: "unix:///csi/csi.sock"
  state_file_path: "/var/lib/csi-arca-storage/node-volumes.json"
  base_mount_path: "/var/lib/kubelet/plugins/csi.arca-storage.io/mounts"
```

認証トークンは Secret から環境変数 `ARCA_AUTH_TOKEN` として渡し、設定ファイル側の `auth_token` を上書きする想定です（マニフェスト例もその構成です）。

## デプロイ

- クイックスタート: [docs/quickstart.ja.md](docs/quickstart.ja.md)
- 詳細ガイド: [docs/deployment.ja.md](docs/deployment.ja.md)

### 前提条件

- Kubernetes 1.20+
- ARCA Storage backend（API 到達性と適切な権限を持つ token）
- ノードから ARCA storage network（データプレーン）への疎通

### インストール方法

#### 方法1: `kubectl apply`（手早く試す）

```bash
# (1) Driver 独自 CRD（ArcaVolume/ArcaSnapshot）を先に投入
kubectl apply -k deploy/crds/

# (2) ARCA API token を Secret に登録
kubectl create secret generic csi-arca-storage-secret \
  --namespace=kube-system \
  --from-literal=auth-token='your-token'

# (3) ドライバをデプロイ
kubectl apply -f deploy/csidriver.yaml
kubectl apply -f deploy/rbac-controller.yaml
kubectl apply -f deploy/rbac-node.yaml
kubectl apply -f deploy/controller.yaml
kubectl apply -f deploy/node.yaml

# (4) StorageClass / VolumeSnapshotClass
kubectl apply -f deploy/examples/storageclass.yaml
kubectl apply -f deploy/examples/volumesnapshotclass.yaml
```

#### 方法2: Kustomize（本番向けに推奨）

```bash
# 開発用
kubectl apply -k deploy/kustomize/overlays/development

# 本番用
kubectl apply -k deploy/kustomize/overlays/production
```

設定の詳細は `docs/deployment.ja.md` を参照してください。

## 使い方

### PersistentVolumeClaim（PVC）

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-pvc
  namespace: default
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: arca-storage
  resources:
    requests:
      storage: 10Gi
```

### スナップショット

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: my-snapshot
  namespace: default
spec:
  volumeSnapshotClassName: arca-snapshots
  source:
    persistentVolumeClaimName: my-pvc
```

### スナップショットからクローン

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-clone
  namespace: default
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: arca-storage
  dataSource:
    name: my-snapshot
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
  resources:
    requests:
      storage: 10Gi
```

### 拡張（Volume Expansion）

```bash
kubectl edit pvc my-pvc
```

`spec.resources.requests.storage` を増やすと、controller が backend 側のクォータを更新します（オンライン拡張）。

## トラブルシューティング

```bash
# Controller
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver

# Node（対象ノードに絞りたい場合）
kubectl logs -n kube-system -l app=csi-arca-storage-node -c csi-driver \
  --field-selector spec.nodeName=<node-name>
```

## License

[LICENSE](../LICENSE)

## Contributing

- [CONTRIBUTING.md](../CONTRIBUTING.md)
- [CONTRIBUTING.ja.md](../CONTRIBUTING.ja.md)
