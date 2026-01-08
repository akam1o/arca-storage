# OpenStack Cinder (NFS) 連携

このリポジトリには、ARCA の SVM エクスポートを **共有 NFS** として利用し、ボリュームを **ファイル** として管理する Cinder NFS ドライバが含まれます。

## 概要

- SVM 1つ = NFS エクスポート 1つ: `server:/exports/<svm>`
- Cinder ボリューム = ファイル: `volume-<volume_id>`
- スナップショット/クローン = ファイルコピー: `snapshot-<snapshot_id>`
- 本ドライバは **基本的に file-only backend** として動作します（ARCA REST API は任意）。

## インストール / 配置

Cinder volume サービスが動作するホスト上で、本リポジトリの Python パッケージを import できるようにします。

- パッケージ（rpm/deb）をインストール、または `pip install .`
- ドライバの import パス: `arca_storage.openstack.cinder.driver`

## Cinder 設定例

`cinder.conf`（multi-backend 例）:

```ini
[DEFAULT]
enabled_backends = arca

[arca]
volume_driver = arca_storage.openstack.cinder.driver.ArcaStorageNFSDriver
volume_backend_name = arca_storage
driver_volume_type = nfs

# NFS/file-only mode（推奨）
arca_storage_use_api = false
arca_storage_nfs_server = 192.168.10.5
arca_storage_nfs_mount_point_base = /var/lib/cinder/mnt
arca_storage_nfs_mount_options = rw,noatime,nodiratime,vers=4.1

# SVM の割り当て戦略
arca_storage_svm_strategy = shared
arca_storage_default_svm = tenant_a

# snapshot/clone のコピータイムアウト（秒）
arca_storage_snapshot_copy_timeout = 600
```

ARCA REST API で SVM の NFS サーバ（VIP）を解決したい場合（任意）:

```ini
arca_storage_use_api = true
arca_storage_api_endpoint = http://127.0.0.1:8080
arca_storage_api_timeout = 30
arca_storage_api_retry_count = 3
arca_storage_verify_ssl = true
```

## SVM 割り当て戦略

### `shared`

全ボリュームが `arca_storage_default_svm` を使用します。

### `manual`

volume type の extra_specs で SVM を明示指定します:

```bash
openstack volume type create arca_tenant_a
openstack volume type set --property arca_storage:svm_name=tenant_a arca_tenant_a
```

この type を指定してボリュームを作成します。

### `per_project`

現状このリポジトリでは未実装です（将来的に `project_id` から SVM 名を導出する想定）。

## Snapshot / Clone の挙動

- snapshot は `volume-<volume_id>` を `snapshot-<snapshot_id>` に sparse copy（`cp --sparse=always`）します
- snapshot からの新規ボリューム、clone はコピー元ファイルを `volume-<new_volume_id>` にコピーします
- 競合回避のため、SVM の NFS マウントは維持し（都度 unmount しません）

## QoS

QoS は best-effort です:

- volume type の extra_specs（例: `arca_storage:read_iops_sec`）を読み取ります
- ARCA REST client が有効で QoS API が利用可能な場合のみ適用を試み、利用できない場合はスキップします

## 運用メモ

- Cinder volume ホストと Nova compute ホストで NFS クライアントが利用可能であること
- `/exports/<svm>` が到達可能で、必要なクライアント CIDR に RW 許可されていること
- sparse file でも書き込みに応じて実容量が消費されるため、エクスポート側の空き容量を監視すること

## トラブルシュート

- mount 失敗: `arca_storage_nfs_server` / export / firewall / `arca_storage_nfs_mount_options` を確認
- PermissionError: エクスポート配下に Cinder がファイル作成/削除できる権限を確認
- “Unable to determine NFS export path”: `arca_storage_nfs_server` を設定するか `arca_storage_use_api` を有効化
- snapshot/clone 失敗: `cp --sparse=always`（GNU coreutils）が利用できること、巨大ボリュームでは `arca_storage_snapshot_copy_timeout` の増加を検討
