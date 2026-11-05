"""数据生成器测试。只用标准库：python -m unittest（pytest 也能直接收集）。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from datagen import checks
from datagen import config as C
from datagen.generate import generate

ROOT = Path(__file__).resolve().parent.parent


def sha(manifest: dict) -> str:
    return next(iter(manifest["sha256"].values()))


class GeneratedDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.result = generate(C.DEFAULT_SEED, Path(cls.tmp.name) / "a")
        cls.conn = sqlite3.connect(cls.result.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()
        cls.tmp.cleanup()

    def test_counts(self) -> None:
        counts = self.result.manifest["counts"]
        self.assertEqual(counts["events"], 30)
        self.assertEqual(counts["metrics_hourly"], 30 * C.WINDOW_HOURS)
        self.assertGreaterEqual(counts["accounts"], C.N_BASE_ACCOUNTS)
        self.assertTrue(5000 <= counts["posts"] <= 8000, counts["posts"])

    def test_integrity(self) -> None:
        self.assertEqual(checks.integrity(self.conn), [])

    def test_all_plants_verified(self) -> None:
        failed = [p["plant_id"] for p in self.result.manifest["plants"] if not p["ok"]]
        self.assertEqual(failed, [])
        self.assertEqual(len(self.result.manifest["plants"]), 8)

    def test_coordinated_repost_by_plain_sql(self) -> None:
        """不经过 plants.py，直接用 SQL 数「注册不足 7 天的账号转发」，结果应恰好只在两个事件里出现。"""
        rows = self.conn.execute(
            """
            SELECT p.event_id, COUNT(DISTINCT p.account_id),
                   (MAX(julianday(p.created_at)) - MIN(julianday(p.created_at))) * 86400
            FROM posts p JOIN accounts a ON a.account_id = p.account_id
            WHERE p.parent_post_id IS NOT NULL
              AND julianday(p.created_at) - julianday(a.created_at) < 7
            GROUP BY p.event_id ORDER BY p.event_id
            """
        ).fetchall()
        by_event = {r[0]: (r[1], r[2]) for r in rows}
        self.assertEqual(sorted(by_event), ["EVT-0007", "EVT-0018"])
        self.assertEqual(by_event["EVT-0007"][0], 40)
        self.assertLess(by_event["EVT-0007"][1], 300)  # 5 分钟内
        self.assertEqual(by_event["EVT-0018"][0], 12)
        self.assertGreater(by_event["EVT-0018"][1], 3600)  # 分散在数小时内

    def test_same_seed_same_output(self) -> None:
        again = generate(C.DEFAULT_SEED, Path(self.tmp.name) / "b")
        self.assertEqual(sha(again.manifest), sha(self.result.manifest))
        self.assertEqual(
            again.manifest_path.read_text(encoding="utf-8"),
            self.result.manifest_path.read_text(encoding="utf-8"),
        )

    def test_different_seed_different_output(self) -> None:
        other = generate(C.DEFAULT_SEED + 1, Path(self.tmp.name) / "c")
        self.assertNotEqual(sha(other.manifest), sha(self.result.manifest))

    def test_independent_of_pythonhashseed(self) -> None:
        """字符串哈希随 PYTHONHASHSEED 变化；生成结果不能依赖 set/dict 的遍历顺序。"""
        hashes = []
        for hash_seed in ("0", "12345"):
            out = Path(self.tmp.name) / f"hashseed-{hash_seed}"
            env = {**os.environ, "PYTHONHASHSEED": hash_seed}
            subprocess.run(
                [sys.executable, "-m", "datagen", "--out", str(out)],
                cwd=ROOT, env=env, check=True, capture_output=True,
            )
            hashes.append(sha(json.loads((out / "manifest.json").read_text(encoding="utf-8"))))
        self.assertEqual(hashes[0], hashes[1])
        self.assertEqual(hashes[0], sha(self.result.manifest))


if __name__ == "__main__":
    unittest.main()
