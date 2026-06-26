import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_db as bt


class ParseDateTests(unittest.TestCase):
    def test_valid_date(self):
        self.assertEqual(bt._parse_date("2026-07-17").isoformat(), "2026-07-17")

    def test_bad_format_rejected(self):
        with self.assertRaises(bt.BacktestError):
            bt._parse_date("2026/07/17")

    def test_impossible_date_rejected(self):
        with self.assertRaises(bt.BacktestError):
            bt._parse_date("2026-02-30")


class NormalizeRecordTests(unittest.TestCase):
    def test_minimal_record(self):
        rec = bt.normalize_record({"test_date": "2026-06-26", "strategy": "RSI", "return_pct": 1.2})
        self.assertEqual(rec["strategy"], "RSI")
        self.assertEqual(rec["return_pct"], 1.2)

    def test_indicator_alias_accepted(self):
        rec = bt.normalize_record(
            {"test_date": "2026-06-26", "indicator": "MACD", "return_pct": 0.5}
        )
        self.assertEqual(rec["strategy"], "MACD")

    def test_missing_return_pct_rejected(self):
        with self.assertRaises(bt.BacktestError):
            bt.normalize_record({"test_date": "2026-06-26", "strategy": "RSI"})

    def test_missing_strategy_rejected(self):
        with self.assertRaises(bt.BacktestError):
            bt.normalize_record({"test_date": "2026-06-26", "return_pct": 1.0})

    def test_win_rate_derived_from_wins_losses(self):
        rec = bt.normalize_record(
            {
                "test_date": "2026-06-26",
                "strategy": "RSI",
                "return_pct": 1.0,
                "wins": 7,
                "losses": 3,
            }
        )
        self.assertEqual(rec["win_rate"], 70.0)

    def test_fractional_win_rate_rescaled_to_percent(self):
        rec = bt.normalize_record(
            {"test_date": "2026-06-26", "strategy": "RSI", "return_pct": 1.0, "win_rate": 0.42}
        )
        self.assertEqual(rec["win_rate"], 42.0)

    def test_percent_string_coerced(self):
        rec = bt.normalize_record(
            {"test_date": "2026-06-26", "strategy": "RSI", "return_pct": "1.5%"}
        )
        self.assertEqual(rec["return_pct"], 1.5)

    def test_extra_fields_preserved(self):
        rec = bt.normalize_record(
            {"test_date": "2026-06-26", "strategy": "RSI", "return_pct": 1.0, "custom_metric": 9}
        )
        self.assertEqual(json.loads(rec["extra"]), {"custom_metric": 9})

    def test_params_object_serialized_stably(self):
        rec = bt.normalize_record(
            {
                "test_date": "2026-06-26",
                "strategy": "RSI",
                "return_pct": 1.0,
                "params": {"exit": 70, "entry": 30},
            }
        )
        self.assertEqual(rec["params"], '{"entry": 30, "exit": 70}')


class WindowTests(unittest.TestCase):
    CONFIG = {"test_window": {"start": "2026-06-26", "end": "2026-07-17"}}

    def test_in_window_ok(self):
        rec = bt.normalize_record({"test_date": "2026-07-01", "strategy": "RSI", "return_pct": 1.0})
        bt.check_window(rec, self.CONFIG)  # should not raise

    def test_after_end_rejected(self):
        rec = bt.normalize_record({"test_date": "2026-07-18", "strategy": "RSI", "return_pct": 1.0})
        with self.assertRaises(bt.BacktestError):
            bt.check_window(rec, self.CONFIG)

    def test_before_start_rejected(self):
        rec = bt.normalize_record({"test_date": "2026-06-25", "strategy": "RSI", "return_pct": 1.0})
        with self.assertRaises(bt.BacktestError):
            bt.check_window(rec, self.CONFIG)


class AggregateAndScoreTests(unittest.TestCase):
    def _records(self):
        return [
            {
                "test_date": "2026-06-26",
                "strategy": "RSI",
                "params": {"period": 14},
                "stop_loss_pct": -3,
                "take_profit_pct": 5,
                "return_pct": 1.0,
                "wins": 6,
                "losses": 4,
                "max_drawdown_pct": -3.0,
            },
            {
                "test_date": "2026-06-27",
                "strategy": "RSI",
                "params": {"period": 14},
                "stop_loss_pct": -3,
                "take_profit_pct": 5,
                "return_pct": 2.0,
                "wins": 7,
                "losses": 3,
                "max_drawdown_pct": -2.0,
            },
            {
                "test_date": "2026-06-26",
                "strategy": "MACD",
                "stop_loss_pct": -3,
                "take_profit_pct": 6,
                "return_pct": -1.0,
                "wins": 3,
                "losses": 7,
                "max_drawdown_pct": -8.0,
            },
            {
                "test_date": "2026-06-27",
                "strategy": "MACD",
                "stop_loss_pct": -3,
                "take_profit_pct": 6,
                "return_pct": 3.0,
                "wins": 6,
                "losses": 4,
                "max_drawdown_pct": -1.0,
            },
        ]

    def test_aggregate_groups_by_config(self):
        aggs = bt.aggregate(self._records())
        self.assertEqual(len(aggs), 2)

    def test_cumulative_return_compounded(self):
        aggs = {a["strategy"]: a for a in bt.aggregate(self._records())}
        # (1+0.01)*(1+0.02)-1 = 0.0302 -> 3.02%
        self.assertAlmostEqual(aggs["RSI"]["cumulative_return_pct"], 3.02, places=2)

    def test_stable_config_outranks_volatile(self):
        aggs = bt.aggregate(self._records())
        scored = bt.score_configs(
            aggs, {"return": 0.35, "stability": 0.30, "win_rate": 0.20, "drawdown": 0.15}
        )
        # RSI is the steadier performer and should rank first.
        self.assertEqual(scored[0]["strategy"], "RSI")
        self.assertGreater(scored[0]["score"], scored[1]["score"])

    def test_indicator_summary_covers_each_strategy(self):
        summary = {s["strategy"] for s in bt.indicator_summary(self._records())}
        self.assertEqual(summary, {"RSI", "MACD"})


class InputLoadingTests(unittest.TestCase):
    def test_json_array(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump([{"test_date": "2026-06-26", "strategy": "RSI", "return_pct": 1.0}], fh)
            path = Path(fh.name)
        self.assertEqual(len(bt.load_input_records(path)), 1)
        path.unlink()

    def test_results_wrapper_object(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(
                {"results": [{"test_date": "2026-06-26", "strategy": "RSI", "return_pct": 1.0}]}, fh
            )
            path = Path(fh.name)
        self.assertEqual(len(bt.load_input_records(path)), 1)
        path.unlink()

    def test_csv(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as fh:
            fh.write("test_date,strategy,return_pct\n2026-06-26,RSI,1.0\n")
            path = Path(fh.name)
        records = bt.load_input_records(path)
        self.assertEqual(records[0]["strategy"], "RSI")
        path.unlink()


class BuildDbTests(unittest.TestCase):
    def test_build_db_inserts_rows(self):
        conn = bt.build_db(
            [
                {"test_date": "2026-06-26", "strategy": "RSI", "return_pct": 1.0},
                {"test_date": "2026-06-27", "strategy": "MACD", "return_pct": 0.5},
            ]
        )
        count = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        self.assertEqual(count, 2)
        conn.close()


if __name__ == "__main__":
    unittest.main()
