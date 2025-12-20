#!/usr/bin/env python3
"""
Setup script for Arca Storage.
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

packages = find_packages(where=".")

setup(
    name="arca-storage",
    version="0.1.0",
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
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.8",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "arca=arca_storage.cli.cli:main",
        ],
    },
)
