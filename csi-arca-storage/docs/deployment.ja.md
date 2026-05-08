# CSI ARCA Storage デプロイガイド

[English](deployment.md) | 日本語

このドキュメントでは、`deploy/` 配下のマニフェストを使って CSI ARCA Storage driver を Kubernetes へデプロイする手順をまとめます。現在の実装では、controller は `ArcaVolume` / `ArcaSnapshot` CRD にメタデータを保存し、node plugin は SVM ごとに共有 NFS マウントを作成して個別ボリュームへ bind mount します。ARCA API token は `ARCA_AUTH_TOKEN` 環境変数で注入します。

このドキュメントのコマンドは `csi-arca-storage/` ディレクトリで実行する前提です。

## デプロイ構成

デプロイされる主なリソースは以下です。

- `CSIDriver`: `deploy/csidriver.yaml`
- Controller `StatefulSet`: 直接適用では `deploy/controller.yaml`、Kustomize では `deploy/controller-statefulset.yaml`
- Node `DaemonSet`: `deploy/node.yaml`
- RBAC: `deploy/rbac-controller.yaml`, `deploy/rbac-node.yaml`
- Driver メタデータ用 CRD: `deploy/crds/`
- StorageClass / VolumeSnapshotClass 例: `deploy/examples/`

Controller は `ArcaVolume` と `ArcaSnapshot` を永続メタデータストアとして使うため、controller 起動前に CRD を適用してください。

## 前提条件

### Kubernetes クラスタ

- Kubernetes 1.25 以降。
  - 同梱の `ArcaVolume` / `ArcaSnapshot` CRD は `x-kubernetes-validations` による CEL validation を使います。この機能は Kubernetes 1.25 で beta になっています。詳細は Kubernetes の [CRD validation rules](https://kubernetes.io/blog/2022/09/23/crd-validation-rules-beta/) を参照してください。
- CRD、cluster-scoped RBAC、`StatefulSet`、`DaemonSet`、`Secret`、`ConfigMap` を作成できる `kubectl` 権限。
- `VolumeSnapshot` / `VolumeSnapshotClass` を使う場合は、snapshot CRD と snapshot controller が導入済み。
- kubelet plugin path がマニフェストの想定どおり `/var/lib/kubelet/...`。
- privileged CSI node pod、`SYS_ADMIN`、host mount、`hostNetwork` が許可されている。

### ARCA Storage backend

- Controller / node pod から ARCA API endpoint へ到達可能。
- SVM 作成/削除、directory 作成/削除、snapshot 作成/削除、quota 設定/拡張/取得が可能な token。
- Backend 側で以下が利用可能:
  - SVM lifecycle 操作
  - directory 作成/削除
  - quota 操作
  - server-side snapshot / clone 操作
- SVM VIP で NFS-Ganesha export が提供されている。

### Network

- Controller pod から ARCA API へ到達できる。
- すべての Kubernetes node から、設定した pool から割り当てられる SVM VIP へ到達できる。
- Node から storage network への NFSv4.2 通信が許可されている。
- `mtu` が storage network と一致している。Jumbo frame を end-to-end で構成していない場合は `1500` を使います。

### ローカルツール

- `kubectl`
- `kubectl apply -k` または standalone `kustomize`
- 独自イメージをビルドする場合は Docker 互換のビルダー

## デプロイ前の準備

### 1. Image を決める

素のマニフェストと production Kustomize overlay は `ghcr.io/akam1o/csi-arca-storage:v1.0.0` を使います。

独自 image を使う場合:

```bash
docker build -t <registry>/csi-arca-storage:<tag> .
docker push <registry>/csi-arca-storage:<tag>
```

以下のどちらかを更新します。

- 直接適用: `deploy/controller.yaml` と `deploy/node.yaml`
- Kustomize: `deploy/kustomize/overlays/*/kustomization.yaml`

### 2. ARCA API 設定を用意する

Driver は Pod 内の `/etc/csi-arca-storage/config.yaml` を読みます。Kubernetes では `csi-arca-storage-config` ConfigMap から配布します。

現在の設定 schema:

```yaml
arca:
  base_url: "https://arca-api.example.com"
  timeout: "30s"
  auth_token: ""  # Secret 由来の ARCA_AUTH_TOKEN を推奨
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

補足:

- `arca.base_url`、少なくとも 1 つの `network.pools[].cidr`、`driver.endpoint` は必須です。
- `arca.timeout` は省略時 `30s` です。
- `network.mtu` は省略時 `1500` です。
- `ARCA_AUTH_TOKEN` が設定されている場合、`arca.auth_token` を上書きします。
- `CSI_ENDPOINT` が設定されている場合、`driver.endpoint` を上書きします。
- `network.pools[].range`、`vlan`、`gateway` は任意ですが、本番では割り当て範囲を明示することを推奨します。

### 3. Secret を用意する

API token を Secret に保存します。

```bash
kubectl create secret generic csi-arca-storage-secret \
  --namespace=kube-system \
  --from-literal=auth-token='<your-arca-api-token>'
```

Controller / node manifest はこの値を `ARCA_AUTH_TOKEN` として渡します。

### 4. Snapshot サポートを確認する

ARCA driver 独自 CRD と Kubernetes snapshot CRD は別物です。

Driver CRD を適用します。

```bash
kubectl apply -k deploy/crds/
```

Kubernetes `VolumeSnapshot` を使う場合は、以下の CRD が存在することも確認します。

```bash
kubectl get crd volumesnapshots.snapshot.storage.k8s.io
kubectl get crd volumesnapshotcontents.snapshot.storage.k8s.io
kubectl get crd volumesnapshotclasses.snapshot.storage.k8s.io
```

未導入の場合は、利用中の Kubernetes distribution または標準化している external-snapshotter release の手順に従って、snapshot CRD と snapshot controller を導入してください。

## 方法1: `kubectl apply` で直接デプロイ

raw manifest を直接編集して試す場合に使います。

### 1. ConfigMap を編集する

`deploy/controller.yaml` 内の `csi-arca-storage-config` ConfigMap を環境に合わせて編集します。

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

### 2. Manifest を順番に適用する

```bash
kubectl apply -k deploy/crds/
kubectl apply -f deploy/csidriver.yaml
kubectl apply -f deploy/rbac-controller.yaml
kubectl apply -f deploy/rbac-node.yaml
kubectl apply -f deploy/controller.yaml
kubectl apply -f deploy/node.yaml
```

### 3. StorageClass を適用する

```bash
kubectl apply -f deploy/examples/storageclass.yaml
kubectl apply -f deploy/examples/volumesnapshotclass.yaml
```

`deploy/examples/storageclass.yaml` は以下を作成します。

- `arca-storage`: delete policy
- `arca-storage-retain`: retain policy
- `arca-storage-wait`: `WaitForFirstConsumer`

`deploy/examples/volumesnapshotclass.yaml` は以下を作成します。

- `arca-snapshots`: delete policy
- `arca-snapshots-retain`: retain policy

## 方法2: Kustomize

本番や環境別の繰り返し可能なデプロイでは Kustomize を推奨します。Kustomize base は `controller-statefulset.yaml` を使い、ConfigMap / Secret は generator で作成します。

現在の Kustomize base は `deploy/` 配下の共有 manifest を base ディレクトリ外から再利用しています。そのため plain `kubectl apply -k` では load restrictor により失敗します。`kubectl kustomize --load-restrictor LoadRestrictionsNone ... | kubectl apply -f -` を使ってください。

### Development overlay

`deploy/kustomize/overlays/development/config.yaml` を編集してから適用します。

```bash
kubectl kustomize --load-restrictor LoadRestrictionsNone \
  deploy/kustomize/overlays/development | kubectl apply -f -
```

Development overlay の特徴:

- `csi-arca-storage:dev` を使用
- `config.yaml` から `csi-arca-storage-config` を生成
- `auth-token=dev-token` の `csi-arca-storage-secret` を生成
- example config では `tls.insecure_skip_verify: true`

### Production overlay

`deploy/kustomize/overlays/production/config.yaml` を編集し、`secrets.env` を用意して適用します。

```bash
cd deploy/kustomize/overlays/production
cp secrets.env.example secrets.env
printf 'auth-token=%s\n' '<your-production-token>' > secrets.env
cd ../../../..

kubectl kustomize --load-restrictor LoadRestrictionsNone \
  deploy/kustomize/overlays/production | kubectl apply -f -
```

Production overlay の特徴:

- `ghcr.io/akam1o/csi-arca-storage:v1.0.0` を使用
- controller replica を `2` に設定
- controller / sidecar の resource request / limit を引き上げ
- ConfigMap / Secret を stable name で生成

`secrets.env` は version control に含めないでください。

## TLS / mTLS

### Custom CA

ConfigMap を作成します。

```bash
kubectl create configmap csi-arca-storage-ca \
  --namespace=kube-system \
  --from-file=ca.crt=/path/to/ca.crt
```

Pod に mount して、設定で参照します。

```yaml
arca:
  tls:
    ca_cert_path: "/etc/csi-arca-storage/ca.crt"
```

`/etc/csi-arca-storage` に複数の volume を mount する場合、`config.yaml` を隠さないように注意してください。`/etc/csi-arca-storage/tls` のように別ディレクトリへ mount する方が安全です。

### mTLS

Client certificate 用の Secret を作成します。

```bash
kubectl create secret generic csi-arca-storage-client-certs \
  --namespace=kube-system \
  --from-file=client.crt=/path/to/client.crt \
  --from-file=client.key=/path/to/client.key
```

Read-only で mount し、設定で参照します。

```yaml
arca:
  tls:
    ca_cert_path: "/etc/csi-arca-storage/tls/ca.crt"
    client_cert_path: "/etc/csi-arca-storage/tls/client.crt"
    client_key_path: "/etc/csi-arca-storage/tls/client.key"
    insecure_skip_verify: false
```

本番で `insecure_skip_verify: true` は使わないでください。

## 確認

### Controller

```bash
kubectl get statefulset -n kube-system csi-arca-storage-controller
kubectl get pods -n kube-system -l app=csi-arca-storage-controller
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver --tail=100
```

直接適用時の controller pod は `5/5` containers Running が目安です。

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

Node pod は `3/3` containers Running が目安です。

Node containers:

- `csi-driver`
- `node-driver-registrar`
- `liveness-probe`

### CRD / 登録状態

```bash
kubectl get crd arcavolumes.storage.arca.io arcasnapshots.storage.arca.io
kubectl get csidriver csi.arca-storage.io
kubectl get storageclass arca-storage
kubectl get volumesnapshotclass arca-snapshots
```

### Functional smoke test

PVC を作成します。

```bash
kubectl apply -f deploy/examples/pvc.yaml
kubectl get pvc example-pvc
```

Pod で mount して確認します。

```bash
kubectl apply -f deploy/examples/pod.yaml
kubectl get pod example-pod
kubectl exec example-pod -- df -h /data
kubectl exec example-pod -- sh -c "echo test > /data/test.txt"
kubectl exec example-pod -- cat /data/test.txt
```

Driver metadata を確認します。

```bash
kubectl get arcavolumes
```

Snapshot CRD / controller を導入している場合:

```bash
kubectl apply -f deploy/examples/snapshot.yaml
kubectl get volumesnapshot example-snapshot
kubectl get arcasnapshots
```

## 運用

### HA

Controller は `StatefulSet` で動作します。本番では 2 replicas 以上を推奨します。

```bash
kubectl scale statefulset csi-arca-storage-controller \
  -n kube-system \
  --replicas=2
```

CSI sidecar は leader election を使うため、controller 操作は active leader のみが実行します。Node plugin は `DaemonSet` で動作し、再起動の影響は対象 node に限定されます。

### Resource sizing

直接適用時の default:

- Controller driver: request `100m` CPU / `128Mi` memory、limit `500m` / `512Mi`
- Node driver: request `50m` CPU / `64Mi` memory、limit `200m` / `256Mi`
- Sidecar は manifest 内でより小さい request / limit を設定

Production overlay は `controller-patch.yaml` で controller 側 resource を増やします。

監視例:

```bash
kubectl top pods -n kube-system -l app=csi-arca-storage-controller
kubectl top pods -n kube-system -l app=csi-arca-storage-node
```

### Driver image の更新

直接適用:

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

Kustomize の場合は overlay の `images` block を更新して再適用します。

### 設定変更

直接適用:

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

Network pool、TLS path、endpoint、auth 設定を変更した場合は controller と node の両方を restart してください。

### Rollback

```bash
kubectl rollout undo statefulset/csi-arca-storage-controller -n kube-system
kubectl rollout undo daemonset/csi-arca-storage-node -n kube-system
```

Rollback 時に `ArcaVolume` / `ArcaSnapshot` CRD は削除しないでください。削除すると driver metadata も失われます。

## トラブルシューティング

### PVC が Pending のまま

確認:

```bash
kubectl describe pvc <pvc-name>
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver --tail=200
kubectl get arcavolumes
```

主な原因:

- Controller から ARCA API に到達できない。
- `ARCA_AUTH_TOKEN` が未設定、期限切れ、または権限不足。
- `network.pools` が空、無効、または枯渇している。
- Controller 起動前に driver CRD が適用されていない。
- Snapshot restore で source snapshot が ready ではない。

### Pod が ContainerCreating のまま

確認:

```bash
kubectl describe pod <pod-name>
kubectl logs -n kube-system -l app=csi-arca-storage-node -c csi-driver \
  --field-selector spec.nodeName=<node-name> \
  --tail=200
```

主な原因:

- Node から SVM VIP に到達できない。
- NFSv4.2 が firewall / network policy で遮断されている。
- Backend export が存在しない、または reload されていない。
- Node plugin が privileged mount 操作を実行できない。
- `/var/lib/kubelet/plugins_registry` などの host path が manifest の想定と異なる。

### Snapshot 作成が失敗する

確認:

```bash
kubectl describe volumesnapshot <snapshot-name>
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-snapshotter --tail=200
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver --tail=200
kubectl get arcasnapshots
```

主な原因:

- Kubernetes snapshot CRD または snapshot controller が未導入。
- Backend volume path が存在しない。
- Backend 側の XFS / reflink snapshot 操作が失敗した。
- ARCA API が authorization / conflict error を返した。

### Controller が起動しない

確認:

```bash
kubectl logs -n kube-system -l app=csi-arca-storage-controller -c csi-driver --previous
kubectl get crd arcavolumes.storage.arca.io arcasnapshots.storage.arca.io
kubectl get secret csi-arca-storage-secret -n kube-system
kubectl get configmap csi-arca-storage-config -n kube-system -o yaml
```

主な原因:

- `arca.base_url` が空。
- network pool が未設定。
- `driver.endpoint` が空。
- CRD が未適用、または API server に rejected された。

### Debug log を有効化する

Controller / node driver args の verbosity を上げます。

```yaml
args:
  - --mode=controller
  - --config=/etc/csi-arca-storage/config.yaml
  - -v=8
```

再適用して対象 workload を restart します。

## アンインストール

まず test workload を削除します。

```bash
kubectl delete -f deploy/examples/pod.yaml --ignore-not-found
kubectl delete -f deploy/examples/pvc.yaml --ignore-not-found
kubectl delete -f deploy/examples/snapshot.yaml --ignore-not-found
```

Class を削除します。

```bash
kubectl delete -f deploy/examples/volumesnapshotclass.yaml --ignore-not-found
kubectl delete -f deploy/examples/storageclass.yaml --ignore-not-found
```

Driver workload を削除します。

```bash
kubectl delete -f deploy/node.yaml --ignore-not-found
kubectl delete -f deploy/controller.yaml --ignore-not-found
kubectl delete -f deploy/rbac-node.yaml --ignore-not-found
kubectl delete -f deploy/rbac-controller.yaml --ignore-not-found
kubectl delete -f deploy/csidriver.yaml --ignore-not-found
```

Config / Secret を削除します。

```bash
kubectl delete secret csi-arca-storage-secret -n kube-system --ignore-not-found
kubectl delete configmap csi-arca-storage-config -n kube-system --ignore-not-found
```

Driver が管理する volume / snapshot が不要になった後でのみ metadata CRD を削除します。

```bash
kubectl delete -k deploy/crds/
```

必要に応じて各 node の状態を削除します。

```bash
sudo rm -rf /var/lib/csi-arca-storage
sudo rm -rf /var/lib/kubelet/plugins/csi.arca-storage.io
```

## Security checklist

- ARCA token は Kubernetes Secret または external secret manager に保存する。
- `secrets.env`、token、秘密鍵を commit しない。
- 本番では TLS certificate verification を有効にする。
- ARCA API が client identity を要求する場合は mTLS を使う。
- NetworkPolicy や firewall で ARCA API へのアクセスを CSI pod に限定する。
- 追加権限を付与する前に RBAC を確認する。
- Node plugin の privileged 権限は CSI node workload に限定する。

## Performance notes

Node plugin は node ごとに SVM export を 1 回 mount し、その共有 SVM mount から個別 volume path を bind mount します。

Driver の default NFS options:

```text
vers=4.2,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport
```

高スループット workload では以下を確認してください。

- Jumbo frame は経路全体で対応している場合のみ使う。
- Worker node から SVM VIP への routing / firewall を単純に保つ。
- PVC / snapshot の burst 時に provisioning latency が伸びる場合は controller resource を増やす。
