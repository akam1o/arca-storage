# Contributing to Arca Storage

English | [日本語](CONTRIBUTING.ja.md)

Thanks for your interest in contributing to Arca Storage!

## How to contribute

- **Questions / proposals**: open an Issue with context (goal, environment, logs).
- **Bug reports**: include OS/distro, versions, repro steps, expected/actual behavior, and logs.
- **Pull requests**: keep changes focused, add/update tests where practical, and ensure CI passes.

## Development setup

This repository contains multiple components (Python package, Ansible, packaging scripts). Pick what you need.

### Python (CLI / API)

```bash
cd arca_storage
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### Tests

```bash
cd arca_storage
pytest
# or
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

## Packaging (rpm/deb)

Packaging helpers live under `packaging/`.

- Debian/Ubuntu: `packaging/vendor-wheels.sh`, `packaging/build-deb.sh`
- EL9: `packaging/vendor-wheels.sh`, `packaging/build-rpm.sh`

If you are preparing a release, note that CI uses the Git tag (e.g. `v0.2.7`) as the version via `setuptools-scm`.

## Pull request guidelines

- Prefer small, reviewable PRs.
- Update docs when behavior changes.
- Avoid mixing refactors with functional changes unless necessary.

