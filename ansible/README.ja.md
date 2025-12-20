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
- arca CLI: `arca_cli_install_method`, `arca_cli_download_url`

## 注意事項
- DRBD/LVMは既存ディスクを破壊する可能性があるため慎重に設定してください。
- `pacemaker_hacluster_password` は適切な値に変更してください。
- `drbd_shared_secret` は全ノードで同じ値を設定し、本番環境では必ず変更してください（ansible-vault推奨）。
- NFS-Ganeshaのエクスポートは要件に合わせて調整してください。

## STONITH設定
デフォルトでは `pacemaker_enable_stonith: false` に設定されています。本番環境では以下の手順でSTONITHを有効化することを強く推奨します：

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
