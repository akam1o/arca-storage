# Arca Storage

[![CI](https://github.com/akam1o/arca-storage/actions/workflows/ci.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/ci.yml)
[![Python Tests](https://github.com/akam1o/arca-storage/actions/workflows/python-tests.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/python-tests.yml)
[![Ansible Lint](https://github.com/akam1o/arca-storage/actions/workflows/ansible-lint.yml/badge.svg)](https://github.com/akam1o/arca-storage/actions/workflows/ansible-lint.yml)

Linux標準技術を使用して構築された、Storage Virtual Machine (SVM) 機能を備えたソフトウェア・デファインド・ストレージシステム。

## 概要

Arca Storageは、Linux標準技術を使用してNetApp ONTAPのようなSVM機能を提供するソフトウェア・デファインド・ストレージシステムです：

- **マルチプロトコル**: NFS v4.1 / v4.2
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

- RHEL/Alma/Rocky Linux 8 または 9
- Pacemaker, Corosync, NFS-Ganesha, LVM2, DRBD がインストール済み
- 2ノードクラスター構成

### インストール

1. **Pythonパッケージのインストール:**

   ```bash
   cd arca_storage
   pip install -e ".[dev]"
   ```

2. **Pacemaker Resource Agentのインストール:**

   ```bash
   sudo cp arca_storage/arca_storage/resources/pacemaker/NetnsVlan /usr/lib/ocf/resource.d/local/NetnsVlan
   sudo chmod +x /usr/lib/ocf/resource.d/local/NetnsVlan
   ```

3. **MVPセットアップガイドに従う:**

   詳細なセットアップ手順については [docs/mvp-setup.md](docs/mvp-setup.md) を参照してください。

## 使い方

### CLIツール (arca)

```bash
# SVMの作成
arca svm create tenant_a --vlan 100 --ip 192.168.10.5/24 --gateway 192.168.10.1

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
uvicorn arca_storage.api.main:app --host 0.0.0.0 --port 8080
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
- [arca_storage/arca_storage/templates/](arca_storage/arca_storage/templates/) - テンプレートドキュメント

## ライセンス

Apache License 2.0

## コントリビューション

コントリビューションを歓迎します！コーディング規約に従ってください。

## ステータス

このプロジェクトは活発に開発中です。MVP実装は完了していますが、追加機能と最適化が計画されています。
