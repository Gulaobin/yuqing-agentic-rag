"""命令行入口：python -m datagen [--seed N] [--out DIR]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config as C
from .generate import generate

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "generated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m datagen", description="生成舆情研判合成数据")
    parser.add_argument("--seed", type=int, default=C.DEFAULT_SEED, help=f"随机种子，默认 {C.DEFAULT_SEED}")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录，默认 data/generated")
    args = parser.parse_args(argv)

    result = generate(args.seed, args.out)
    manifest = result.manifest
    counts = manifest["counts"]
    print(f"seed {manifest['seed']} -> {result.db_path}")
    print("  " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    for plant in manifest["plants"]:
        print(f"  [{'OK' if plant['ok'] else 'FAIL'}] {plant['plant_id']} {plant['kind']} {','.join(plant['event_ids'])}")
    print(f"  sha256 {next(iter(manifest['sha256'].values()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
