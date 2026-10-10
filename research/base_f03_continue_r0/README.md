# F03 继研 R0

只读。最高状态 `SHADOW_CANDIDATE`。`NO_LIVE.lock` 缺失则所有入口退出。

不修改 `/root/base_wildcat_alpha/`。

```bash
python3 src/analyze_panel.py
python3 src/mature_shadow.py
python3 src/event_flips.py
python3 src/quote_shadow_readonly.py
```

报告：`reports/R0_F03_CONTINUE.md`
