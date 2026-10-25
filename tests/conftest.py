import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parents[1]


@pytest.fixture(scope="session")
def plugin_module():
    """隔离加载 plugin.py（相对导入安全的完整姿势，见 references/isolated-import-testing.md）。

    裸 spec_from_file_location 加载带 `from .config import ...` 的多文件插件会直接
    ImportError，必须设 submodule_search_locations + __path__ 并先注册 sys.modules。
    """
    spec = spec_from_file_location(
        "feihua_ling",
        PLUGIN_DIR / "plugin.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = module_from_spec(spec)
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules["feihua_ling"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def game_mod(plugin_module):
    """game 纯逻辑模块（plugin.py 的相对导入已顺带加载其子模块）。"""
    return plugin_module.game


@pytest.fixture()
def handlers_mod(plugin_module):
    return plugin_module.handlers
