# Arca Storage

[English](README.md) | 日本語

[![CI](https://github.com/akam1o/arca-storage/actions/workflows/ci.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/ci.yml)
[![Python Tests](https://github.com/akam1o/arca-storage/actions/workflows/python-tests.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/python-tests.yml)
[![Ansible Lint](https://github.com/akam1o/arca-storage/actions/workflows/ansible-lint.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/ansible-lint.yml)

Linux標準技術を使用して構築された、Storage Virtual Machine (SVM) 機能を備えたソフトウェア・デファインド・ストレージシステム。

## 概要

Arca Storageは、Linux標準技術を使用してNetApp ONTAPのようなSVM機能を提供するソフトウェア・デファインド・ストレージシステムです：

- **マルチプロトコル**: NFS v4.1 / v4.2 (デフォルト)、オプションでNFSv3サポート
- **マルチテナンシー**: Network Namespaceベースのネットワーク分離
- **高可用性**: Pacemakerベースのアクティブ/アクティブ・フェイルオーバー
- **データ効率**: LVM Thin Provisioningによるオーバーコミット
- **クライアント統合**: Kubernetes (CSI) およびOpenStack (Cinder NFS Driver) サポート

## アーキテクチャ

システムは以下のコンポーネントを組み合わせています：
- **Pacemaker + Corosync**: HAクラスタリングとリソース管理
- **NFS-Ganesha**: ユーザースペースNFSサーバー (SVM毎に1プロセス)
- **Network Namespace**: テナントネットワーク分離
- **XFS**: NVMe最適化ファイルシステム
- **LVM Thin Provisioning**: 仮想ボリューム管理とスナップショット
- **DRBD**: ノード間同期データミラーリング

## クイックスタート

### 前提条件

- RHEL/Alma/Rocky Linux 8/9, Debian, または Ubuntu
- Pacemaker/Corosync/pcs, NFS-Ganesha, LVM2, DRBD がインストール済み
- 2ノードクラスター構成

### インストール

1. **OS 依存パッケージのインストール（例）:**

   ```bash
   # EL9 (RHEL/Alma/Rocky 9)
   sudo dnf install -y pacemaker corosync pcs resource-agents \
     nfs-ganesha nfs-ganesha-utils \
     lvm2 xfsprogs \
     drbd-utils drbd-kmod

   # Debian/Ubuntu（パッケージ名はディストリ/リポジトリにより異なる場合があります）
   sudo apt-get update
   sudo apt-get install -y pacemaker corosync pcs resource-agents \
     nfs-ganesha \
     lvm2 xfsprogs \
     drbd-utils
   ```

2. **`arca-storage` パッケージのインストール（rpm/deb）:**

   GitHub Releases から最新のパッケージを取得してインストールします。

   ```bash
   # EL9 (rpm)
   sudo dnf install -y ./arca-storage-*.rpm

   # Debian/Ubuntu (deb)
   sudo apt-get install -y ./arca-storage_*.deb
   ```

3. **MVPセットアップガイドに従う:**

   詳細なセットアップ手順については [docs/mvp-setup.md](docs/mvp-setup.md) を参照してください。

## 設定

### NFSv3サポート (オプション)

デフォルトでは、Arca StorageはNFSv4のみを使用します。NFSv3サポートを有効にするには：

1. **runtime 設定の編集:**

   `/etc/arca-storage/storage-runtime.conf` に設定します：

   ```ini
   [storage]
   # NFSv3 を有効化（v3 + v4 の両方を利用）
   ganesha_protocols = 3,4

   # 固定ポート（NFSv3 利用時に推奨）
   ganesha_mountd_port = 20048
   ganesha_nlm_port = 32768
   ```

2. **設定の再生成と reload:**

   ```bash
   # 設定変更後に env を同期（任意だが推奨）
   sudo arca bootstrap render-env

   # SVM ごとの ganesha.conf を再生成して reload
   sudo arca export sync --all
   ```

3. **NFSv3有効時に必要なファイアウォールポート:**

   ```
   111/tcp,udp   (rpcbind/portmapper)
   2049/tcp,udp  (NFS)
   20048/tcp,udp (mountd)
   32768/tcp,udp (NLM)
   ```

4. **クライアントマウント例:**

   ```bash
   # NFSv4 (デフォルト)
   mount -t nfs4 server:/101 /mnt

   # NFSv3 (有効時)
   mount -t nfs -o vers=3 server:/exports /mnt
   ```

**注意**: NFSv3 を利用する場合、`rpcbind` がインストールされて起動していることを確認してください。NFSv3 と NFSv4 の両プロトコルが同時に利用可能になります。

## 使い方

### CLIツール (arca)

```bash
# Ansibleなしでのブートストラップ
arca bootstrap install

# (任意) 設定の編集
sudo vi /etc/arca-storage/storage-bootstrap.conf
sudo vi /etc/arca-storage/storage-runtime.conf

# 設定変更後に /etc/arca-storage/arca-storage.env を再生成
arca bootstrap render-env

# SVMの作成
# --gateway は省略可です（未指定の場合は --ip から推定。/31,/32 は指定してください）
arca svm create tenant_a --vlan 100 --ip 192.168.10.5/24

# ボリュームの作成
arca volume create vol1 --svm tenant_a --size 100

# エクスポートの追加
arca export add --volume vol1 --svm tenant_a --client 10.0.0.0/24 --rw

# SVMの一覧表示
arca svm list
```

### REST API

APIサーバーの起動:

```bash
arca-storage-api --host 127.0.0.1 --port 8080
```

または systemd サービスとして起動します（パッケージインストール時）:

```bash
sudo systemctl enable --now arca-storage-api
```

APIエンドポイント:

- `POST /v1/svms` - SVM作成
- `GET /v1/svms` - SVM一覧
- `DELETE /v1/svms/{name}` - SVM削除
- `POST /v1/volumes` - ボリューム作成
- `PATCH /v1/volumes/{name}` - ボリュームリサイズ
- `DELETE /v1/volumes/{name}` - ボリューム削除
- `POST /v1/exports` - エクスポート追加
- `GET /v1/exports` - エクスポート一覧
- `DELETE /v1/exports` - エクスポート削除

サーバー起動時に `http://localhost:8080/docs` でAPIドキュメントを参照できます。

## プロジェクト構成

```
arca-storage/
├── arca_storage/               # Pythonパッケージ
│   ├── arca_storage/           # パッケージソースコード
│   │   ├── api/                # FastAPI REST API
│   │   │   ├── main.py         # APIアプリケーション
│   │   │   ├── models.py       # Pydanticモデル
│   │   │   └── services/       # サービス層
│   │   ├── cli/                # CLIツール
│   │   │   ├── cli.py          # メインCLIエントリ
│   │   │   ├── commands/       # コマンド実装
│   │   │   └── lib/            # ライブラリ関数
│   │   ├── resources/          # システムリソース
│   │   │   └── pacemaker/      # Pacemakerリソースエージェント
│   │   └── templates/          # 設定テンプレート
│   ├── tests/                  # テストスイート
│   ├── pyproject.toml          # パッケージ設定
│   ├── setup.py                # レガシー設定
│   ├── pytest.ini              # テスト設定
│   └── README.md               # パッケージドキュメント
├── ansible/                    # Ansibleプレイブック
│   ├── roles/                  # Ansibleロール
│   └── site.yml                # メインプレイブック
├── docs/                       # プロジェクトドキュメント
│   └── mvp-setup.md            # MVPセットアップガイド
└── README.md                   # このファイル
```

## 開発

### 開発環境のセットアップ

```bash
# リポジトリのクローン
git clone https://github.com/akam1o/arca-storage.git
cd arca-storage/arca_storage

# 仮想環境の作成
python3 -m venv venv
source venv/bin/activate

# 開発モードで開発用依存関係と共にインストール
pip install -e ".[dev]"
```

### テストの実行

```bash
cd arca_storage

# 全テストの実行
pytest

# カバレッジ付きで実行
pytest --cov=arca_storage --cov-report=html

# 特定のテストの実行
pytest tests/unit/
pytest tests/integration/
```

### コーディングスタイル

Pythonコードは PEP 8 に従ってください。

## ドキュメント

- [docs/mvp-setup.md](docs/mvp-setup.md) - MVPセットアップガイド
- [arca_storage/arca_storage/resources/pacemaker/](arca_storage/arca_storage/resources/pacemaker/) - Pacemaker RAドキュメント
- [arca_storage/arca_storage/resources/systemd/](arca_storage/arca_storage/resources/systemd/) - systemd ユニットファイル
- [arca_storage/arca_storage/templates/](arca_storage/arca_storage/templates/) - テンプレートドキュメント

## ライセンス

Apache License 2.0

## コントリビューション

コントリビューションを歓迎します！[CONTRIBUTING.ja.md](CONTRIBUTING.ja.md) を参照してください。

## ステータス

このプロジェクトは活発に開発中です。MVP実装は完了していますが、追加機能と最適化が計画されています。
