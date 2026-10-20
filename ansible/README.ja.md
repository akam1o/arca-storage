# Ansible HA Storage Playbook

## 前提条件
- Ansible 2.13+ (推奨)
- 管理対象は2ノードのLinux (RHEL 8/9, CentOS Stream 8/9, Rocky Linux 8/9など)
- 各ノードにrootまたはsudo権限が必要
- ネットワーク接続（ノード間通信、SSH）
- 必要なパッケージ: `xfsprogs`, `parted`, `iproute2`
- `community.general` コレクションが必要
  - `ansible-galaxy collection install community.general`
- ノード名（`node1`, `node2`）がDNSまたは`/etc/hosts`で解決できること（Pacemakerクラスター構築のため）

## ディレクトリ構成
- `ansible.cfg`: Ansible設定
- `inventory.ini`: 2ノードのサンプルインベントリ
- `group_vars/all.yml`: 共通変数
- `host_vars/node1.yml`, `host_vars/node2.yml`: ノード固有の変数
- `site.yml`: メインプレイブック

## 使い方
1. `inventory.ini` のIPとユーザーを環境に合わせて更新
2. `group_vars/all.yml` のデバイス名やネットワーク設定を更新
3. 実行

```bash
cd ansible
ansible-playbook -i inventory.ini site.yml
```

## 主要な変数
- DRBD: `drbd_resource_name`, `drbd_device`, `drbd_disk`, `drbd_nodes`
- LVM: `lvm_vg_name`, `lvm_pv_devices`, `lvm_thinpool_name`
- Pacemaker: `pacemaker_cluster_name`, `pacemaker_nodes`
- NFS-Ganesha: `nfs_ganesha_export_dir`, `nfs_ganesha_export_clients`
- arca CLI: `arca_cli_install_method`, `arca_cli_download_url`, `arca_cli_download_checksum`

## 注意事項
- DRBD/LVMは既存ディスクを破壊する可能性があるため慎重に設定してください。
- `pacemaker_hacluster_password` は適切な値に変更してください。
- `drbd_shared_secret` は全ノードで同じ値を設定し、本番環境では必ず変更してください（ansible-vault推奨）。
- URL 経由で arca CLI をインストールする場合、デフォルトで `arca_cli_download_checksum` が必須です。検証なしのダウンロードは、信頼済みのローカル開発用アーティファクトに限定してください。
- NFS-Ganeshaのエクスポートは要件に合わせて調整してください。
- このプレイブックは初期ブートストラップ用途を想定しています。SVM/Volume/Export の作成や Pacemaker リソース作成などのランタイム操作は、デフォルトでは `arca` 側で実施してください。

### systemd-networkd によるホスト VIP の削除を防ぐ

systemd-networkd が管理するインターフェイスでは、OS の自動更新などに伴う
再起動で、`IPaddr2` や `ip addr add` が追加した VIP が削除されることがあります。
この場合、ストレージや NFS プロセスが正常でも NFS クライアントが応答待ちになります。

**両ストレージノード**で `networkctl status <VIP のインターフェイス>` を実行し、
`Network File` に表示されるファイルの basename をホスト変数またはグループ変数に指定します。

```yaml
# /run/systemd/network/10-netplan-eth0.network の場合
common_networkd_vip_network_files:
  - 10-netplan-eth0.network
```

common ロールが `/etc/systemd/network/<basename>.d/90-arca-vip.conf` に
`[Network] KeepConfiguration=static` を設定し、変更時に `networkctl reload` を実行します。
Netplan の生成ファイルは編集しません。NetworkManager などが管理する場合は、
デフォルトの空リストのままにしてください。導入済みの systemd が
`KeepConfiguration=static` に対応していることも確認してください。

既存環境にこの設定だけを適用する場合：

```bash
ansible-playbook -i inventory.ini site.yml --tags networkd_vip
```

`networkctl status <VIP のインターフェイス>` に drop-in が表示されることと、
計画的な networkd 再起動後も稼働系の VIP が保持されることを確認します。
この設定は既存 VIP の保持のみを行い、消失した VIP の復元、OS 起動時の割り当て、
自動フェイルオーバーは行いません。消失時は重複所有がないことを確認し、
確認済みの稼働系にだけ VIP を復元してください。両台に VIP を固定設定しないでください。
VIP の所有権は引き続き Pacemaker または手動切り替え手順で管理します。

### ブートストラップ向けのデフォルト

ランタイムでのドリフトを避けるため、`group_vars/all.yml` ではデフォルトで以下を無効化しています：

- `pacemaker_create_resources: false`
- `nfs_ganesha_render_tenant_configs: false`
- `lvm_create_tenant_volume: false`
- `lvm_format_tenant_filesystem: false`

### オプション: `arca` 経由でのランタイム構築

Ansible でランタイム構築も行いたい場合は、`pcs` を直接叩いたりテンプレートを書き込むのではなく、`arca` を実行する方針を推奨します。

- 有効化: `arca_runtime_enabled: true`
- 入力: `arca_runtime_svms`, `arca_runtime_volumes`, `arca_runtime_exports`
- タグ: `arca_runtime`

例:

```bash
ansible-playbook -i inventory.ini site.yml --tags arca_cli,arca_runtime \
  -e arca_runtime_enabled=true
```

## STONITH設定
デフォルトでは `pacemaker_enable_stonith: false` に設定されていますが、`site.yml` は STONITH が有効か、隔離された検証環境向けに `pacemaker_allow_stonith_disabled_for_lab: true` が明示されていない限り fail-fast します。本番環境では以下の手順でSTONITHを有効化してください：

1. `group_vars/all.yml` で `pacemaker_enable_stonith: true` に変更
2. 環境に応じたSTONITHデバイスを手動で作成
   ```bash
   # 例: IPMI使用の場合
   pcs stonith create fence_node1 fence_ipmilan \
     pcmk_host_list="node1" ipaddr="10.0.0.11" login="admin" passwd="password" \
     lanplus=1 cipher=1 op monitor interval=60s

   pcs stonith create fence_node2 fence_ipmilan \
     pcmk_host_list="node2" ipaddr="10.0.0.12" login="admin" passwd="password" \
     lanplus=1 cipher=1 op monitor interval=60s
   ```
3. STONITHデバイスが正常に動作することを確認
   ```bash
   pcs stonith show
   pcs stonith status
   ```

詳細な設定手順は環境のフェンシングデバイスに応じて `docs/` 内のドキュメントを参照してください。

## テスト

### LintとSyntaxチェック

プレイブックにはコード品質チェックのためのLint設定が含まれています：

```bash
# 依存関係のインストール
pip install ansible-core>=2.13 ansible-lint yamllint

# Ansibleコレクションのインストール
ansible-galaxy collection install -r requirements.yml

# yamllintの実行
yamllint .

# ansible-lintの実行
ansible-lint site.yml

# Syntax checkの実行
ansible-playbook -i inventory.ini site.yml --syntax-check
```

### Molecule統合テスト

Docker/Podmanを使った統合テストにMoleculeが利用可能です：

```bash
# Moleculeのインストール
pip install molecule molecule-plugins[docker]

# 全テストの実行
molecule test

# 特定のテストステップの実行
molecule converge  # プレイブックの適用
molecule verify    # 検証テストの実行
molecule destroy   # クリーンアップ
```

### テストモード

破壊的な変更を行わずにテストするには、`test_mode`を使用します：

```yaml
# group_varsまたはインベントリ内
test_mode: true
drbd_bootstrap_enabled: false
lvm_create_enabled: false
pacemaker_bootstrap_enabled: false
pacemaker_create_resources: false
```

### CI/CD

GitHub Actionsがプッシュとプルリクエスト毎に自動的にLintチェックを実行します。詳細は `.github/workflows/ansible-lint.yml` を参照してください。
