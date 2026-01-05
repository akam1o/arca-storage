#!/usr/bin/env python3
"""
Setup script for Arca Storage.
"""

from __future__ import annotations

import os
import subprocess

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

packages = find_packages(where=".")

def _resolve_version() -> str:
    """
    Resolve a version string for legacy `setup.py` usage.

    Primary source is Git tags like `v0.2.7` (GitHub Releases format).
    """
    candidates = [
        os.environ.get("ARCA_VERSION", ""),
        os.environ.get("GITHUB_REF_NAME", ""),
    ]
    github_ref = os.environ.get("GITHUB_REF", "")
    if github_ref:
        candidates.append(github_ref.rsplit("/", 1)[-1])

    for raw in candidates:
        raw = (raw or "").strip()
        if raw:
            return raw.lstrip("v")

    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--match", "v*", "--abbrev=0"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if tag:
            return tag.lstrip("v")
    except Exception:
        pass

    return "0.0.0"


_setup_kwargs: dict[str, object] = {}
try:
    import setuptools_scm  # noqa: F401

    # Keep versioning consistent with pyproject.toml (setuptools-scm).
    _setup_kwargs["use_scm_version"] = True
except Exception:
    _setup_kwargs["version"] = _resolve_version()

setup(
    name="arca-storage",
    **_setup_kwargs,
    author="Arca Storage Project",
    description="Software-Defined Storage system with SVM functionality",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/akam1o/arca-storage",
    license="Apache-2.0",
    packages=packages,
    package_dir={"": "."},
    package_data={
        "arca_storage": ["templates/**/*", "resources/**/*"],
    },
    include_package_data=True,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.9",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "arca=arca_storage.cli.cli:main",
            "arca-storage-api=arca_storage.api.server:main",
        ],
    },
)
