# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Location denylist for ``/object/ingest-path``.

The endpoint copies any readable file into the downloadable object store, so the
source must never be a system/app-data directory, a hidden directory, or this
product's own install tree (config.yaml, .env, .proxy-config).
"""

import os
import pathlib

import pytest

from content_search.utils import path_ingest_policy as pip
from content_search.utils.path_ingest_policy import PathIngestPolicy, default_policy

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows path forms")


@pytest.fixture
def tree(tmp_path):
    denied = tmp_path / "AppData"
    allowed = denied / "Local" / "Temp"
    user = tmp_path / "Documents"
    for d in (allowed, user, tmp_path / ".ssh"):
        d.mkdir(parents=True)
    policy = PathIngestPolicy([str(denied)], [str(allowed)])
    return tmp_path, policy


def test_regular_user_folder_is_allowed(tree):
    root, policy = tree
    assert policy.check((root / "Documents" / "lesson.pdf").resolve()) is None


def test_denied_root_and_descendants_rejected(tree):
    root, policy = tree
    assert policy.check((root / "AppData").resolve())
    assert policy.check((root / "AppData" / "Roaming" / "x" / "notes.txt").resolve())


def test_allowed_exception_inside_denied_root(tree):
    root, policy = tree
    assert policy.check((root / "AppData" / "Local" / "Temp" / "Temp1_a.zip" / "a.mp4").resolve()) is None


def test_sibling_with_common_prefix_not_denied(tree):
    root, policy = tree
    assert policy.check((root / "AppDataBackup" / "a.pdf").resolve()) is None


def test_hidden_component_rejected(tree):
    root, policy = tree
    assert policy.check((root / ".ssh" / "notes.txt").resolve())
    assert policy.check((root / "Documents" / ".secret.txt").resolve())


def test_hidden_rule_not_overridden_by_allowed_root(tree):
    root, policy = tree
    assert policy.check((root / "AppData" / "Local" / "Temp" / ".cache" / "a.txt").resolve())


def test_default_policy_denies_install_tree():
    assert default_policy().check((pip._INSTALL_ROOT / "README.md").resolve())
    assert default_policy().check((pip._INSTALL_ROOT / "config.yaml").resolve())


def test_default_policy_allows_session_storage():
    assert default_policy().check((pip._INSTALL_ROOT / "storage" / "s1" / "transcript.txt").resolve()) is None


@windows_only
def test_default_policy_denies_windows_dirs():
    system_root = os.environ["SystemRoot"]
    assert default_policy().check(pathlib.Path(system_root, "win.ini").resolve())
    assert default_policy().check(pathlib.Path(os.environ["APPDATA"], "app", "a.txt").resolve())


@windows_only
@pytest.mark.parametrize("path", [
    "\\\\?\\C:\\Windows\\win.ini",
    "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume3\\Windows\\win.ini",
    "\\\\localhost\\C$\\Users\\Public\\a.txt",
    "\\\\127.0.0.1\\share\\a.txt",
    "\\\\nas\\D$\\a.txt",
])
def test_windows_prefix_and_share_forms_rejected(path):
    assert default_policy().check(pathlib.Path(path))


@windows_only
def test_regular_network_share_allowed():
    assert default_policy().check(pathlib.Path("\\\\nas\\courses\\week1.mp4")) is None
