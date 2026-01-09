# OpenStack Manila (NFS) 連携

このリポジトリには、ARCA Storage を Manila のバックエンドとして利用する **Manila share driver** が含まれます。share の作成/削除、スナップショット、アクセス制御などの操作は **ARCA REST API** 経由で行います。

## 概要

- Manila share = ARCA volume（LVM thin 上の XFS ファイルシステム）
- Export パス形式: `{svm_vip}:/exports/{svm}/{share-volume}`
  - ARCA 側の volume 名は `share-<share_id>` を使用します
- Snapshot / clone は ARCA の snapshot/clone API を利用します
- アクセスルールは ARCA の export ACL として反映します
  - 対応 access_type: `ip` のみ
  - 対応 access_level: `rw`, `ro`
  - セキュリティのため `root_squash` は常に有効です

## インストール / 配置

`manila-share` が動作するホスト上で、本リポジトリの Python パッケージを import できるようにします。

- パッケージ（rpm/deb）をインストール、または `pip install .`
- OpenStack 連携向けの optional 依存関係: `pip install ".[openstack]"`
- ドライバの import パス: `arca_storage.openstack.manila.driver`

## Manila 設定例

`manila.conf`（multi-backend 例）:

```ini
[DEFAULT]
enabled_share_backends = arca

[arca]
share_driver = arca_storage.openstack.manila.driver.ArcaStorageManilaDriver
share_backend_name = arca_storage
driver_handles_share_servers = False

# ARCA REST API（必須）
arca_storage_use_api = true
arca_storage_api_endpoint = http://127.0.0.1:8080
arca_storage_api_timeout = 30
arca_storage_api_retry_count = 3
arca_storage_verify_ssl = true

# 認証（任意）
# arca_storage_api_auth_type = token
# arca_storage_api_token = <token>
#
# arca_storage_api_auth_type = basic
# arca_storage_api_username = <username>
# arca_storage_api_password = <password>
#
# （任意）TLS 設定
# arca_storage_api_ca_bundle = /etc/ssl/certs/ca-bundle.crt
# arca_storage_api_client_cert = /path/to/client.crt
# arca_storage_api_client_key = /path/to/client.key

# SVM 割り当て戦略
arca_storage_svm_strategy = shared
arca_storage_default_svm = manila_default

# （per_project のみ）ネットワーク割り当てプラグイン
# arca_storage_network_plugin_mode = standalone  # or: neutron

# （per_project + standalone）IP/VLAN プール（network_plugin_mode=standalone の場合は必須）
# 形式: '<ip_cidr>|<start_ip>-<end_ip>:<vlan_id>'
# arca_storage_per_project_ip_pools = 192.168.100.0/24|192.168.100.10-192.168.100.200:100
# arca_storage_per_project_ip_pools = 172.16.0.0/24|172.16.0.100-172.16.0.200:200

# （per_project + neutron）Neutron ネットワーク（network_plugin_mode=neutron の場合は必須）
# arca_storage_neutron_net_ids = <net-uuid-1>,<net-uuid-2>
# arca_storage_neutron_port_security = false
# arca_storage_neutron_vnic_type = normal

# per_project の SVM 設定
# arca_storage_per_project_mtu = 1500
# arca_storage_per_project_root_volume_size_gib = 100
```

## SVM 割り当て戦略

### `shared`

全 share が `arca_storage_default_svm` を使用します。

### `manual`

share type の extra_specs で SVM を指定します:

- `arca_manila:svm_name=<svm_name>`

### `per_project`

OpenStack の project ごとに SVM を自動作成します。

- SVM 名: `{arca_storage_svm_prefix}{project_id}`（prefix のデフォルトは `manila_`）
- VLAN/IP の割り当ては `arca_storage_network_plugin_mode` に依存します:
  - `standalone`: 静的プール（`arca_storage_per_project_ip_pools`）
  - `neutron`: provider VLAN network 上の Neutron Port（`arca_storage_neutron_net_ids` + `[neutron]` 認証）
- SVM の自動 GC は未実装です（削除/回収は手動）

#### `per_project` + `standalone`（静的プール）

- `arca_storage_per_project_ip_pools` で 1つ以上のプールを設定します
- project は round-robin でプールに割り当てます（衝突回避のため）
- IPv4 のみ

#### `per_project` + `neutron`（Neutron Port）

SVM ごとに Neutron Port を 1つ作成し、port の fixed IP と subnet の gateway/prefix を使用します。

- 対象 network は provider VLAN network である必要があります（`provider:network_type=vlan`、`provider:segmentation_id` 必須）
- allocator は `gateway_ip` を持つ最初の IPv4 subnet を自動選択します（見つからない場合は first subnet）
- 作成される Port の属性:
  - `device_owner=compute:arca-storage-svm`
  - `device_id=arca-svm-<svm_name>`
  - `tags`（Neutron の tag extension があれば best-effort）

`manila.conf` 追加例:

```ini
[arca]
arca_storage_svm_strategy = per_project
arca_storage_network_plugin_mode = neutron
arca_storage_neutron_net_ids = <net-uuid-1>,<net-uuid-2>

[neutron]
auth_type = password
auth_url = https://keystone.example/v3
username = manila
password = <password>
project_name = service
user_domain_name = Default
project_domain_name = Default
region_name = RegionOne
interface = internal
```

## Share type（scheduler / placement）

DHSS=False の share type を作成し、バックエンドに紐付けます:

```bash
openstack share type create arca --snapshot-support True
openstack share type set arca --extra-specs share_backend_name=arca_storage
```

`arca_storage_svm_strategy=manual` の場合、SVM を指定します:

```bash
openstack share type set arca --extra-specs arca_manila:svm_name=tenant_a
```

## QoS（best-effort）

以下の share type extra_specs を読み取ります（任意）:

- `arca_manila:read_iops_sec`
- `arca_manila:write_iops_sec`
- `arca_manila:read_bytes_sec`
- `arca_manila:write_bytes_sec`

ARCA API が QoS 適用に対応している場合のみ、制限の適用を試みます（非対応の場合はスキップ）。

## アクセスルール

IP ベースのアクセスルールのみ対応します:

```bash
openstack share access create <share> ip 10.0.0.0/24 --access-level rw
openstack share access delete <share> <access-id>
```

Manila 側から差分（add_rules/delete_rules）が渡されないケースに備え、最終的な desired list に合わせて backend export を整合させる処理も行います（best-effort）。

## Snapshot / Snapshot からの share 作成

- Snapshot: デフォルトで有効（`arca_storage_snapshot_support=true`）
- Snapshot からの share 作成: デフォルトで有効（`arca_storage_create_share_from_snapshot_support=true`）
- Snapshot への revert / snapshot の mount: 未実装

`per_project` 戦略では、テナント分離のため **同一 project 内** でのみ snapshot からの share 作成を許可します。

## 運用メモ

- `manila-share` から ARCA REST API に到達できる必要があります。
- NFS クライアント（ユーザー/計算ノード）が SVM VIP と export パスに到達できる必要があります。
- `per_project` のアドレス設計: 各プールが扱える project 数は `(end_ip - start_ip + 1)`、総容量はプールの合計です。

## 動作確認（簡易）

例（share type / サイズ / クライアント CIDR は環境に合わせて調整してください）:

```bash
# share 作成
openstack share create NFS 1 --name arca-test --share-type arca

# export 確認とアクセス許可
openstack share export location list arca-test
openstack share access create arca-test ip 10.0.0.0/24 --access-level rw

# snapshot と snapshot からの share 作成
openstack share snapshot create arca-test --name arca-test-snap
openstack share create NFS 1 --name arca-test-from-snap --share-type arca --snapshot arca-test-snap
```

## 制限事項

- access rule: `ip` のみ（`user`/`cert` は非対応）
- `per_project` のプール割り当ては IPv4 のみ
- snapshot: revert / mount は未実装
- `per_project` の SVM は自動 GC 未実装（削除は手動）
- `per_project` + `neutron`: 作成失敗時の Port cleanup は best-effort ですが、SVM/Port の自動 GC は未実装です
- QoS は best-effort（ARCA API の対応状況に依存）

## トラブルシュート

- 初期化失敗: `arca_storage_api_endpoint` と認証設定を確認
- scheduler が置けても作成が失敗: `shared`/`manual` では対象 SVM の存在、API 到達性を確認
- access rule エラー: `ip` のみ対応、`access_to` が正しい IP/CIDR か確認
- `per_project` の衝突/枯渇: network/broadcast を範囲に含めない、十分な未使用 IP を確保する
