"""配置层测试：项目根目录的定位。

这个测试守着一个真实踩过的坑：容器里 `pip install .` 之后，包在 site-packages 下，
`Path(__file__).resolve().parents[2]` 指向的是 site-packages 而不是项目根，
默认的 `data/` / `config/` 路径随之落到了不可写的系统目录，容器一启动就崩。
`_resolve_project_root` 就是为此存在的：源码形态锚定仓库，安装形态退回工作目录。
"""

from __future__ import annotations

from pathlib import Path

from staguard.config.app import PROJECT_ROOT, _resolve_project_root

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_source_checkout_resolves_to_repo_root():
    """源码形态：`parents[2]` 下有 pyproject.toml，就要用它。"""
    assert _resolve_project_root(REPO_ROOT) == REPO_ROOT
    assert PROJECT_ROOT == REPO_ROOT, "本仓库以 editable 方式安装，PROJECT_ROOT 应指向仓库根"


def test_installed_layout_falls_back_to_working_directory(tmp_path: Path, monkeypatch):
    """安装形态：源码上级没有 pyproject.toml，必须退回当前工作目录。

    用 tmp_path 模拟 site-packages 那种「上一级不是项目」的情形。
    """
    fake_site_packages = tmp_path / "site-packages"
    fake_site_packages.mkdir()
    monkeypatch.chdir(tmp_path)

    assert _resolve_project_root(fake_site_packages) == tmp_path
