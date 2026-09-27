# -*- coding: utf-8 -*-
"""兼容旧工具的构建入口：真正的元数据在 `pyproject.toml`。

保留本文件的作用：
  * `python setup.py sdist` 在没有 `build` 模块的环境里也能出源码包
    （`pip wheel .` 只能出 wheel）；
  * 某些老版本 pip/setuptools 只认 setup.py。

不要在这里写配置——统一维护 `pyproject.toml`，避免两处不一致。
"""
from setuptools import setup

setup()
