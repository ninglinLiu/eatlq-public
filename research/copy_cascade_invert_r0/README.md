# copy_cascade_invert_r0

Isolated research. Does not modify `/opt/EATLQ`. Does not use our RPC.

```bash
cd /root/copy_cascade_invert_r0
python3 -m src.run_all
python3 -m unittest discover -s tests -v
```

Outputs land in `out/`. Raw vendor snapshots land in `data/raw/`.
