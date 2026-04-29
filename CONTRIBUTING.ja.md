# Arca Storage へのコントリビューション

[English](CONTRIBUTING.md) | 日本語

Arca Storage へのコントリビューションに興味を持っていただきありがとうございます！

## コントリビューション方法

- **質問 / 提案**: 目的・環境・ログなどの情報を添えて Issue を作成してください。
- **バグ報告**: OS/ディストリ、バージョン、再現手順、期待結果/実際の結果、ログを含めてください。
- **Pull Request**: 変更は小さく焦点を絞り、可能な範囲でテストを追加/更新し、CI が通ることを確認してください。

## 開発環境セットアップ

このリポジトリには複数コンポーネント（Python パッケージ、Ansible、パッケージング）が含まれます。用途に応じて選んでください。

### Python（CLI / API）

```bash
cd arca_storage
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### テスト

```bash
cd arca_storage
pytest
# または
pytest tests/unit
pytest tests/integration
```

### Ansible lint

```bash
cd ansible
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install "ansible-core>=2.13" ansible-lint yamllint
ansible-galaxy collection install -r requirements.yml
yamllint .
ansible-lint site.yml
ansible-playbook -i inventory.ini site.yml --syntax-check
```

## パッケージング（rpm/deb）

パッケージング用のスクリプトは `packaging/` 配下にあります。

- Debian/Ubuntu: `packaging/vendor-wheels.sh`, `packaging/build-deb.sh`
- EL9/EL10: `packaging/vendor-wheels.sh`, `packaging/build-rpm.sh`

リリース準備では、CI は Git tag（例: `v0.2.7`）を `setuptools-scm` 経由でバージョンとして利用します。

## アーキテクチャについて

Python パッケージは **宣言的リコンシリエーション** アーキテクチャを採用しています：

- **リソースモデル** (`models/`): 各リソースの Spec（期待状態）と Status（実際の状態）を定義。
- **リコンサイラー** (`reconcilers/`): リソースをステップごとに冪等に進めるループ。新しい操作はステップチェーンの拡張で対応。
- **アダプター** (`adapters/`): システムコマンドの Protocol ベース抽象化。新規追加時は Subprocess（本番）と Fake（テスト）の両実装を提供。
- **状態ストア** (`db/`): SQLite WAL。すべての変更は DB を経由。
- **テスト**: ユニットテストは models, errors, DB, reconcilers をカバー。統合テストは `AppContext` 経由で Fake アダプターを注入。

## Pull Request のガイドライン

- 小さくレビューしやすい単位を優先してください。
- 挙動が変わる場合はドキュメントも更新してください。
- 必要がない限り、リファクタと機能変更を混ぜないでください。
