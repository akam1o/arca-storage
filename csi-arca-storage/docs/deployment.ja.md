# CSI ARCA Storage デプロイガイド

[English](deployment.md) | 日本語

このドキュメントでは、CSI ARCA Storage driver を Kubernetes クラスタへデプロイするための手順と、設定項目、運用時の注意点をまとめます。

## 前提条件

### Kubernetes クラスタ

- Kubernetes 1.20+
- snapshot 機能を使う場合は CSI snapshot CRD が導入済み
- 全ノードから ARCA storage network（データプレーン）への疎通

### ARCA Storage backend

- ARCA API エンドポイントへ到達可能
- 必要権限を持つ認証トークン（SVM 作成/削除、quota 更新、snapshot 操作など）
- SVM 割り当て用の network pool（CIDR/VLAN/GW など）を決定済み

### ツール

- `kubectl`
- `kustomize`（任意。運用では推奨）
- Docker（独自イメージをビルドする場合）

## デプロイ方法

### 方法1: `kubectl apply`（クイックに導入）

1. **CRD の適用（必須）**

```bash
kubectl apply -k deploy/crds/
```

2. **ARCA API token を Secret に登録**

```bash
kubectl create secret generic csi-arca-storage-secret \
  --namespace=kube-system \
  --from-literal=auth-token='your-auth-token-here'
```

3. **network pool の設定**

`deploy/controller.yaml` の ConfigMap（`csi-arca-storage-config`）を環境に合わせて編集します。

```yaml
network:
  pools:
    - cidr: "10.0.0.0/24"
      range: "10.0.0.100-10.0.0.200"
      vlan: 100
      gateway: "10.0.0.1"
  mtu: 1500
```

4. **ドライバのデプロイ**

```bash
kubectl apply -f deploy/csidriver.yaml
kubectl apply -f deploy/rbac-controller.yaml
kubectl apply -f deploy/rbac-node.yaml
kubectl apply -f deploy/controller.yaml
kubectl apply -f deploy/node.yaml

kubectl apply -f deploy/examples/storageclass.yaml
kubectl apply -f deploy/examples/volumesnapshotclass.yaml
```

5. **確認**

```bash
kubectl get pods -n kube-system -l app=csi-arca-storage-controller
kubectl get pods -n kube-system -l app=csi-arca-storage-node
kubectl get csidriver csi.arca-storage.io
```

### 方法2: Kustomize（本番向けに推奨）

Kustomize の base では `controller-statefulset.yaml` を利用し、ConfigMap/Secret は generator で生成します。

#### 開発環境

1. `deploy/kustomize/overlays/development/config.yaml` を編集
2. デプロイ

```bash
kubectl apply -k deploy/kustomize/overlays/development
```

#### 本番環境

1. `deploy/kustomize/overlays/production/config.yaml` を編集
2. `deploy/kustomize/overlays/production/secrets.env` を用意（例をコピーして token を設定）
3. デプロイ

```bash
kubectl apply -k deploy/kustomize/overlays/production
```

## 設定リファレンス

### ARCA API

```yaml
arca:
  base_url: "https://arca-api.example.com"
  timeout: "30s"
  auth_token: ""  # Secret/環境変数で注入する想定
  tls:
    ca_cert_path: "/etc/csi-arca-storage/ca.crt"
    client_cert_path: ""
    client_key_path: ""
    insecure_skip_verify: false
```

### network

```yaml
network:
  pools:
    - cidr: "10.0.0.0/24"
      range: "10.0.0.100-10.0.0.200"
      vlan: 100
      gateway: "10.0.0.1"
  mtu: 1500
```

### driver（ノード側）

```yaml
driver:
  node_id: ""  # Node プラグイン起動時に --node-id で上書き可能
  endpoint: "unix:///csi/csi.sock"
  state_file_path: "/var/lib/csi-arca-storage/node-volumes.json"
  base_mount_path: "/var/lib/kubelet/plugins/csi.arca-storage.io/mounts"
```

## 高可用性（HA）

### Controller

controller は StatefulSet で動作し、サイドカーが leader election を行います。Kustomize の production overlay は `replicas: 2` の例を含みます。

### Node Plugin

node plugin は DaemonSet（ノードごとに 1 Pod）で、ノード単位で自動復旧します。

## トラブルシューティング

```bash
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver
kubectl logs -n kube-system -l app=csi-arca-storage-node -c csi-driver \
  --field-selector spec.nodeName=<node-name>
```

### 1. Volume 作成が失敗する

PVC が `Pending` の場合は以下を確認します。

```bash
kubectl describe pvc <pvc-name>
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver
```

主な原因:

- ARCA API への到達性
- token の権限/設定ミス
- network pool 枯渇

### 2. マウントが失敗する

Pod が `ContainerCreating` のままの場合は node 側ログを確認します。

```bash
kubectl describe pod <pod-name>
kubectl logs -n kube-system -l app=csi-arca-storage-node -c csi-driver \
  --field-selector spec.nodeName=<node-name>
```

### 3. Snapshot 作成が失敗する

```bash
kubectl describe volumesnapshot <snapshot-name>
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-snapshotter
```

主な原因:

- backend の XFS で reflink が無効/非対応
- 元ボリューム未作成/削除済み
- API エラー

## アンインストール

```bash
kubectl delete -f deploy/examples/pod.yaml
kubectl delete -f deploy/examples/pvc.yaml

kubectl delete -f deploy/examples/storageclass.yaml
kubectl delete -f deploy/examples/volumesnapshotclass.yaml

kubectl delete -f deploy/node.yaml
kubectl delete -f deploy/controller.yaml
kubectl delete -f deploy/rbac-node.yaml
kubectl delete -f deploy/rbac-controller.yaml
kubectl delete -f deploy/csidriver.yaml

kubectl delete secret csi-arca-storage-secret -n kube-system
kubectl delete configmap csi-arca-storage-config -n kube-system
```

必要に応じて各ノード上の状態をクリーンアップします。

```bash
sudo rm -rf /var/lib/csi-arca-storage
sudo rm -rf /var/lib/kubelet/plugins/csi.arca-storage.io
```
