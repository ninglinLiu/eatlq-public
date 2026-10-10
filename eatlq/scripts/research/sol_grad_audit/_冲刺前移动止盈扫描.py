#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冲刺前移动止盈 / 回撤退出扫描（调用主审计脚本）."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent


def main():
    cmd = [
        sys.executable,
        str(DIR / "_毕业信号收益扫描.py"),
        "--trail",
        "--output",
        str(DIR / "_毕业信号收益扫描.json"),
    ] + sys.argv[1:]
    subprocess.run(cmd, check=True)
    print(f"Trailing output: {DIR / '_冲刺前移动止盈扫描.json'}")


if __name__ == "__main__":
    main()
