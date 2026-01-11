# クイックスタート

[English](quickstart.md) | 日本語

CSI ARCA Storage driver を Kubernetes に 10 分程度で導入するための手順です。

## 前提条件

- Kubernetes 1.20+
- ARCA storage backend（API 到達性と適切な権限を持つ token）
- `kubectl` が利用可能

## Step 0: 必要な CRD をインストール

まず、CSI ドライバ独自の CRD（ArcaVolume / ArcaSnapshot）を投入します。

```bash
kubectl apply -k deploy/crds/
```

`VolumeSnapshot` / `VolumeSnapshotClass` を使う場合、snapshot CRD がクラスタに入っていることを確認してください（デフォルトで入っていないクラスタもあります）。

```bash
kubectl get crd volumesnapshots.snapshot.storage.k8s.io
kubectl get crd volumesnapshotcontents.snapshot.storage.k8s.io
kubectl get crd volumesnapshotclasses.snapshot.storage.k8s.io
```

## Step 1: 認証 Secret を作成

```bash
kubectl create secret generic csi-arca-storage-secret \
  --namespace=kube-system \
  --from-literal=auth-token='your-arca-api-token'
```

## Step 2: network pool を設定

`deploy/controller.yaml` の ConfigMap（`csi-arca-storage-config`）を編集して、ARCA API と network pool を環境に合わせます。

```yaml
data:
  config.yaml: |
    arca:
      base_url: "https://your-arca-api.example.com"  # 更新
      timeout: "30s"
      auth_token: ""

    network:
      pools:
        - cidr: "10.0.0.0/24"          # 更新
          range: "10.0.0.100-10.0.0.200"
          vlan: 100
          gateway: "10.0.0.1"
      mtu: 1500
```

Kustomize を使う場合は、`deploy/kustomize/overlays/*/config.yaml` を編集してください。

## Step 3: ドライバをデプロイ

```bash
kubectl apply -f deploy/csidriver.yaml
kubectl apply -f deploy/rbac-controller.yaml
kubectl apply -f deploy/rbac-node.yaml
kubectl apply -f deploy/controller.yaml
kubectl apply -f deploy/node.yaml
```

## Step 4: StorageClass / VolumeSnapshotClass を作成

```bash
kubectl apply -f deploy/examples/storageclass.yaml
kubectl apply -f deploy/examples/volumesnapshotclass.yaml
```

## Step 5: インストール確認

```bash
kubectl get pods -n kube-system | grep csi-arca-storage
kubectl get csidriver csi.arca-storage.io
```

## Step 6: 最初のボリュームを作成

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

```bash
kubectl get pvc test-pvc
```

## Step 7: Pod でマウントして確認

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

```bash
kubectl get pod test-pod
kubectl exec test-pod -- df -h /data
```

## 次のステップ

- スナップショット: `deployment.ja.md`（「Snapshot 作成が失敗する」）
- クローン例: `../deploy/examples/snapshot.yaml`
- 拡張: `../README.ja.md`（「拡張」）
- HA 設定: `deployment.ja.md`（「高可用性（HA）」）

## トラブルシューティング

```bash
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver
kubectl logs -n kube-system -l app=csi-arca-storage-node -c csi-driver
kubectl describe pvc test-pvc
```

詳細は `deployment.ja.md` を参照してください。

## クリーンアップ

```bash
kubectl delete pod test-pod
kubectl delete pvc test-pvc
```

アンインストールは `deployment.ja.md`（「アンインストール」）を参照してください。
