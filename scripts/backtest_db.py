#!/usr/bin/env python3
"""Store and analyze TossInvest trading-bot backtest results.

This is a local, offline bookkeeping tool. It does NOT call any TossInvest
endpoint and is unrelated to live trading. It only organizes backtest numbers
the user provides, persists them, and ranks indicator / stop-loss / take-profit
configurations by stability and return.

Durable store of record is an append-only JSONL file (git-friendly). A SQLite
database is materialized from that JSONL on demand for querying/analysis.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "backtests"
JSONL_PATH = DATA_DIR / "results.jsonl"
DB_PATH = DATA_DIR / "results.db"
CONFIG_PATH = DATA_DIR / "config.json"
REPORT_PATH = DATA_DIR / "REPORT.md"

_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

# Core record fields persisted as dedicated SQLite columns. Anything else the
# bot reports is preserved under "extra" so no information is lost.
CORE_FIELDS = (
    "test_date",
    "strategy",
    "params",
    "stop_loss_pct",
    "take_profit_pct",
    "symbol",
    "trades",
    "wins",
    "losses",
    "win_rate",
    "return_pct",
    "cumulative_return_pct",
    "max_drawdown_pct",
    "sharpe",
    "profit_factor",
    "notes",
)
NUMERIC_FIELDS = (
    "stop_loss_pct",
    "take_profit_pct",
    "trades",
    "wins",
    "losses",
    "win_rate",
    "return_pct",
    "cumulative_return_pct",
    "max_drawdown_pct",
    "sharpe",
    "profit_factor",
)


class BacktestError(Exception):
    """Raised for invalid input or out-of-window data."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "test_window": {"start": None, "end": None},
        "scoring_weights": {
            "return": 0.35,
            "stability": 0.30,
            "win_rate": 0.20,
            "drawdown": 0.15,
        },
    }


def window_bounds(config: dict[str, Any]) -> tuple[date | None, date | None]:
    win = config.get("test_window", {}) or {}
    start = _parse_date(win.get("start")) if win.get("start") else None
    end = _parse_date(win.get("end")) if win.get("end") else None
    return start, end


# --------------------------------------------------------------------------- #
# Validation / normalization
# --------------------------------------------------------------------------- #
def _parse_date(value: str) -> date:
    # NOTE: avoid datetime.strptime here. It imports the stdlib "calendar"
    # module, which scripts/calendar.py shadows when scripts/ is on sys.path.
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise BacktestError(f"날짜는 YYYY-MM-DD 형식이어야 합니다: {value!r}")
    year, month, day = (int(part) for part in value.split("-"))
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise BacktestError(f"존재하지 않는 날짜입니다: {value!r}") from exc


def _coerce_number(field: str, value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BacktestError(f"{field}: 숫자가 필요합니다 (bool 받음)")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().rstrip("%").replace(",", "")
        try:
            return float(cleaned)
        except ValueError as exc:
            raise BacktestError(f"{field}: 숫자로 변환할 수 없습니다: {value!r}") from exc
    raise BacktestError(f"{field}: 숫자로 변환할 수 없습니다: {value!r}")


def normalize_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate one record and fill obvious derived fields."""
    if not isinstance(raw, dict):
        raise BacktestError("각 레코드는 JSON 객체여야 합니다.")

    rec: dict[str, Any] = {}
    rec["test_date"] = _parse_date(raw.get("test_date")).isoformat()

    strategy = raw.get("strategy") or raw.get("indicator")
    if not strategy or not str(strategy).strip():
        raise BacktestError("strategy(지표) 값이 필요합니다. 예: RSI, MACD, SMA, EMA, BOLLINGER")
    rec["strategy"] = str(strategy).strip()

    params = raw.get("params")
    if params is None:
        rec["params"] = None
    elif isinstance(params, (dict, list)):
        rec["params"] = json.dumps(params, ensure_ascii=False, sort_keys=True)
    else:
        rec["params"] = str(params).strip()

    rec["symbol"] = str(raw["symbol"]).strip() if raw.get("symbol") else None
    rec["notes"] = str(raw["notes"]).strip() if raw.get("notes") else None

    for field in NUMERIC_FIELDS:
        rec[field] = _coerce_number(field, raw.get(field))

    # Derive win_rate from wins/losses when missing.
    if rec["win_rate"] is None and rec["wins"] is not None and rec["losses"] is not None:
        total = rec["wins"] + rec["losses"]
        rec["win_rate"] = round(rec["wins"] / total * 100, 2) if total else None
    # A win_rate given as a fraction (<=1) is rescaled to percent.
    if rec["win_rate"] is not None and 0 < rec["win_rate"] <= 1:
        rec["win_rate"] = round(rec["win_rate"] * 100, 2)

    if rec["return_pct"] is None:
        raise BacktestError(
            f"{rec['test_date']} {rec['strategy']}: return_pct(수익률, %)는 필수입니다."
        )

    known = set(CORE_FIELDS) | {"indicator"}
    extra = {k: v for k, v in raw.items() if k not in known}
    rec["extra"] = json.dumps(extra, ensure_ascii=False, sort_keys=True) if extra else None
    return rec


def check_window(rec: dict[str, Any], config: dict[str, Any]) -> None:
    start, end = window_bounds(config)
    d = _parse_date(rec["test_date"])
    if start and d < start:
        raise BacktestError(f"{rec['test_date']}는 테스트 시작일({start}) 이전입니다.")
    if end and d > end:
        raise BacktestError(f"{rec['test_date']}는 테스트 종료일({end})을 초과합니다.")


# --------------------------------------------------------------------------- #
# JSONL store
# --------------------------------------------------------------------------- #
def read_records() -> list[dict[str, Any]]:
    if not JSONL_PATH.exists():
        return []
    records = []
    for line_no, line in enumerate(JSONL_PATH.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise BacktestError(f"results.jsonl {line_no}행 JSON 파싱 실패: {exc}") from exc
    return records


def append_records(new_records: Iterable[dict[str, Any]]) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    with JSONL_PATH.open("a", encoding="utf-8") as fh:
        for rec in new_records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def load_input_records(path: Path) -> list[dict[str, Any]]:
    """Read records from a .json (object or array), .jsonl, or .csv file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".csv":
        return list(csv.DictReader(text.splitlines()))
    if path.suffix.lower() == ".jsonl":
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    data = json.loads(text)
    if isinstance(data, dict):
        # Allow {"results": [...]} or {"records": [...]} wrappers.
        for key in ("results", "records", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    if isinstance(data, list):
        return data
    raise BacktestError("입력 JSON은 객체 또는 배열이어야 합니다.")


# --------------------------------------------------------------------------- #
# SQLite materialization
# --------------------------------------------------------------------------- #
def build_db(records: list[dict[str, Any]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(
        f"{f} TEXT" if f in ("test_date", "strategy", "params", "symbol", "notes") else f"{f} REAL"
        for f in CORE_FIELDS
    )
    conn.execute(f"CREATE TABLE results (id INTEGER PRIMARY KEY, {cols}, extra TEXT)")
    placeholders = ", ".join("?" for _ in CORE_FIELDS) + ", ?"
    insert = f"INSERT INTO results ({', '.join(CORE_FIELDS)}, extra) VALUES ({placeholders})"
    for raw in records:
        rec = normalize_record(raw)
        conn.execute(insert, [rec.get(f) for f in CORE_FIELDS] + [rec.get("extra")])
    conn.commit()
    return conn


def persist_db(records: list[dict[str, Any]]) -> None:
    """Write the SQLite file to disk (derived artifact, gitignored)."""
    mem = build_db(records)
    if DB_PATH.exists():
        DB_PATH.unlink()
    disk = sqlite3.connect(DB_PATH)
    mem.backup(disk)
    disk.close()
    mem.close()


# --------------------------------------------------------------------------- #
# Aggregation & scoring
# --------------------------------------------------------------------------- #
def config_label(rec: dict[str, Any]) -> str:
    parts = [rec["strategy"]]
    if rec.get("params"):
        parts.append(str(rec["params"]))
    sl = rec.get("stop_loss_pct")
    tp = rec.get("take_profit_pct")
    if sl is not None or tp is not None:
        parts.append(f"손절={sl} / 익절={tp}")
    return " | ".join(parts)


def aggregate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group normalized records by (strategy, params, stop, profit)."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for raw in records:
        rec = normalize_record(raw)
        key = (
            rec["strategy"],
            rec.get("params"),
            rec.get("stop_loss_pct"),
            rec.get("take_profit_pct"),
        )
        groups.setdefault(key, []).append(rec)

    aggregates = []
    for (strategy, params, sl, tp), recs in groups.items():
        returns = [r["return_pct"] for r in recs if r["return_pct"] is not None]
        drawdowns = [r["max_drawdown_pct"] for r in recs if r["max_drawdown_pct"] is not None]
        win_rates = [r["win_rate"] for r in recs if r["win_rate"] is not None]
        trades = [r["trades"] for r in recs if r["trades"] is not None]
        wins = [r["wins"] for r in recs if r["wins"] is not None]
        losses = [r["losses"] for r in recs if r["losses"] is not None]
        dates = sorted({r["test_date"] for r in recs})

        mean_ret = statistics.fmean(returns) if returns else 0.0
        std_ret = statistics.pstdev(returns) if len(returns) > 1 else 0.0
        cumulative = (math.prod(1 + r / 100 for r in returns) - 1) * 100 if returns else 0.0
        if wins and losses and (sum(wins) + sum(losses)) > 0:
            agg_win_rate = sum(wins) / (sum(wins) + sum(losses)) * 100
        elif win_rates:
            agg_win_rate = statistics.fmean(win_rates)
        else:
            agg_win_rate = None
        sharpe = mean_ret / std_ret if std_ret > 0 else (None if len(returns) <= 1 else 0.0)

        aggregates.append(
            {
                "strategy": strategy,
                "params": params,
                "stop_loss_pct": sl,
                "take_profit_pct": tp,
                "label": config_label(recs[0]),
                "days": len(dates),
                "date_range": f"{dates[0]} ~ {dates[-1]}" if dates else "",
                "total_trades": int(sum(trades)) if trades else None,
                "mean_return_pct": round(mean_ret, 4),
                "std_return_pct": round(std_ret, 4),
                "cumulative_return_pct": round(cumulative, 4),
                "win_rate_pct": round(agg_win_rate, 2) if agg_win_rate is not None else None,
                "avg_drawdown_pct": round(statistics.fmean(drawdowns), 4) if drawdowns else None,
                "worst_drawdown_pct": round(min(drawdowns), 4) if drawdowns else None,
                "sharpe_like": round(sharpe, 4) if sharpe is not None else None,
            }
        )
    return aggregates


def _zscores(values: list[float]) -> list[float]:
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return [0.0 for _ in values]
    mean = statistics.fmean(present)
    std = statistics.pstdev(present)
    if std == 0:
        return [0.0 for _ in values]
    return [((v - mean) / std if v is not None else 0.0) for v in values]


def score_configs(
    aggregates: list[dict[str, Any]], weights: dict[str, float]
) -> list[dict[str, Any]]:
    if not aggregates:
        return []
    z_ret = _zscores([a["cumulative_return_pct"] for a in aggregates])
    z_stab = _zscores([a["sharpe_like"] for a in aggregates])
    z_win = _zscores([a["win_rate_pct"] for a in aggregates])
    # Higher (less negative) drawdown is better.
    z_dd = _zscores([a["worst_drawdown_pct"] for a in aggregates])

    w_ret = weights.get("return", 0.35)
    w_stab = weights.get("stability", 0.30)
    w_win = weights.get("win_rate", 0.20)
    w_dd = weights.get("drawdown", 0.15)

    scored = []
    for agg, zr, zs, zw, zd in zip(aggregates, z_ret, z_stab, z_win, z_dd):
        score = w_ret * zr + w_stab * zs + w_win * zw + w_dd * zd
        scored.append({**agg, "score": round(score, 4)})
    scored.sort(key=lambda a: a["score"], reverse=True)
    return scored


def indicator_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate at the indicator level (ignoring params/stop/profit)."""
    groups: dict[str, list[float]] = {}
    dd_groups: dict[str, list[float]] = {}
    for raw in records:
        rec = normalize_record(raw)
        groups.setdefault(rec["strategy"], []).append(rec["return_pct"])
        if rec["max_drawdown_pct"] is not None:
            dd_groups.setdefault(rec["strategy"], []).append(rec["max_drawdown_pct"])
    out = []
    for strategy, returns in groups.items():
        mean_ret = statistics.fmean(returns)
        std_ret = statistics.pstdev(returns) if len(returns) > 1 else 0.0
        out.append(
            {
                "strategy": strategy,
                "samples": len(returns),
                "mean_return_pct": round(mean_ret, 4),
                "std_return_pct": round(std_ret, 4),
                "cumulative_return_pct": round(
                    (math.prod(1 + r / 100 for r in returns) - 1) * 100, 4
                ),
                "sharpe_like": round(mean_ret / std_ret, 4) if std_ret > 0 else None,
                "worst_drawdown_pct": round(min(dd_groups[strategy]), 4)
                if dd_groups.get(strategy)
                else None,
            }
        )
    out.sort(key=lambda a: (a["sharpe_like"] is not None, a["sharpe_like"] or -999), reverse=True)
    return out


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _ingest(raw_records: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = []
    for raw in raw_records:
        rec = normalize_record(raw)
        check_window(rec, config)
        normalized.append(rec)
    append_records(normalized)
    all_records = read_records()
    persist_db(all_records)
    return normalized


def cmd_add(args: argparse.Namespace) -> int:
    config = load_config()
    if args.json:
        raw = json.loads(args.json)
        raw_records = raw if isinstance(raw, list) else [raw]
    else:
        raw_records = [
            {
                "test_date": args.date,
                "strategy": args.strategy,
                "params": json.loads(args.params) if args.params else None,
                "stop_loss_pct": args.stop_loss,
                "take_profit_pct": args.take_profit,
                "symbol": args.symbol,
                "trades": args.trades,
                "win_rate": args.win_rate,
                "return_pct": args.return_pct,
                "max_drawdown_pct": args.max_drawdown,
                "sharpe": args.sharpe,
                "notes": args.notes,
            }
        ]
    normalized = _ingest(raw_records, config)
    print(f"{len(normalized)}건 저장 완료 (총 {len(read_records())}건). DB: {DB_PATH}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    config = load_config()
    raw_records = load_input_records(Path(args.file))
    normalized = _ingest(raw_records, config)
    print(f"{len(normalized)}건 import 완료 (총 {len(read_records())}건). DB: {DB_PATH}")
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    records = read_records()
    persist_db(records)
    print(f"results.jsonl {len(records)}건 → SQLite 재생성 완료: {DB_PATH}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    records = read_records()
    if args.date:
        records = [r for r in records if r.get("test_date") == args.date]
    if args.strategy:
        records = [
            r
            for r in records
            if str(r.get("strategy") or r.get("indicator", "")).upper() == args.strategy.upper()
        ]
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return 0
    if not records:
        print("저장된 레코드가 없습니다.")
        return 0
    for r in records:
        rec = normalize_record(r)
        print(
            f"{rec['test_date']}  {rec['strategy']:<10}  "
            f"손절={rec.get('stop_loss_pct')}  익절={rec.get('take_profit_pct')}  "
            f"수익률={rec.get('return_pct')}%  승률={rec.get('win_rate')}%  "
            f"MDD={rec.get('max_drawdown_pct')}%"
        )
    print(f"\n총 {len(records)}건")
    return 0


def _analysis_payload(config: dict[str, Any]) -> dict[str, Any]:
    records = read_records()
    aggregates = aggregate(records)
    weights = config.get("scoring_weights", {})
    scored = score_configs(aggregates, weights)
    indicators = indicator_summary(records)
    start, end = window_bounds(config)
    dates = sorted({r.get("test_date") for r in records if r.get("test_date")})
    return {
        "records": records,
        "scored": scored,
        "indicators": indicators,
        "weights": weights,
        "window": {"start": str(start) if start else None, "end": str(end) if end else None},
        "data_dates": dates,
    }


def cmd_analyze(args: argparse.Namespace) -> int:
    config = load_config()
    payload = _analysis_payload(config)
    if not payload["records"]:
        print("분석할 데이터가 없습니다. 먼저 'add' 또는 'import'로 결과를 저장하세요.")
        return 0
    if args.json:
        print(
            json.dumps(
                {k: v for k, v in payload.items() if k != "records"}, ensure_ascii=False, indent=2
            )
        )
        return 0
    print(_render_report(payload))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    config = load_config()
    payload = _analysis_payload(config)
    text = _render_report(payload)
    REPORT_PATH.write_text(text + "\n", encoding="utf-8")
    print(f"리포트 작성 완료: {REPORT_PATH}")
    return 0


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value}{suffix}"


def _render_report(payload: dict[str, Any]) -> str:
    scored = payload["scored"]
    indicators = payload["indicators"]
    window = payload["window"]
    dates = payload["data_dates"]
    weights = payload["weights"]
    lines = []
    lines.append("# 토스증권 트레이딩봇 백테스트 분석 리포트")
    lines.append("")
    lines.append(f"- 생성일: {date.today().isoformat()}")
    win_end = window.get("end") or "-"
    win_start = window.get("start") or (dates[0] if dates else "-")
    lines.append(f"- 테스트 기간: {win_start} ~ {win_end}")
    lines.append(f"- 수집된 데이터 일자: {len(dates)}일 ({', '.join(dates) if dates else '없음'})")
    lines.append(f"- 레코드 수: {len(payload['records'])}건 / 설정 조합: {len(scored)}개")
    lines.append(
        f"- 점수 가중치: 수익률 {weights.get('return')}, 안정성 {weights.get('stability')}, "
        f"승률 {weights.get('win_rate')}, MDD {weights.get('drawdown')}"
    )
    lines.append("")

    if scored:
        best = scored[0]
        lines.append("## 추천: 가장 안정적이고 수익률 높은 설정")
        lines.append("")
        lines.append(f"**{best['label']}**")
        lines.append("")
        lines.append(f"- 지표: `{best['strategy']}`  파라미터: `{best['params'] or '-'}`")
        lines.append(
            f"- 손익점: 손절 `{_fmt(best['stop_loss_pct'], '%')}` / "
            f"익절 `{_fmt(best['take_profit_pct'], '%')}`"
        )
        lines.append(
            f"- 누적 수익률: `{_fmt(best['cumulative_return_pct'], '%')}` "
            f"(일평균 {_fmt(best['mean_return_pct'], '%')}, 변동성 {_fmt(best['std_return_pct'], '%')})"
        )
        lines.append(
            f"- 승률: `{_fmt(best['win_rate_pct'], '%')}`  "
            f"안정성(Sharpe-like): `{_fmt(best['sharpe_like'])}`  "
            f"최대낙폭(MDD): `{_fmt(best['worst_drawdown_pct'], '%')}`"
        )
        lines.append(
            f"- 종합 점수: `{best['score']}`  (검증 {best['days']}일, 거래 {_fmt(best['total_trades'])}회)"
        )
        lines.append("")

    if scored:
        lines.append("## 설정 조합 순위 (Top 10)")
        lines.append("")
        lines.append(
            "| # | 지표 | 손절% | 익절% | 누적수익% | 일평균% | 변동성 | 승률% | MDD% | Sharpe | 점수 |"
        )
        lines.append(
            "|---|------|------|------|---------|--------|-------|------|------|--------|------|"
        )
        for i, a in enumerate(scored[:10], 1):
            lines.append(
                f"| {i} | {a['strategy']} | {_fmt(a['stop_loss_pct'])} | {_fmt(a['take_profit_pct'])} | "
                f"{_fmt(a['cumulative_return_pct'])} | {_fmt(a['mean_return_pct'])} | "
                f"{_fmt(a['std_return_pct'])} | {_fmt(a['win_rate_pct'])} | "
                f"{_fmt(a['worst_drawdown_pct'])} | {_fmt(a['sharpe_like'])} | {a['score']} |"
            )
        lines.append("")

    if indicators:
        lines.append("## 지표별 요약")
        lines.append("")
        lines.append("| 지표 | 샘플 | 누적수익% | 일평균% | 변동성 | Sharpe | MDD% |")
        lines.append("|------|------|---------|--------|-------|--------|------|")
        for a in indicators:
            lines.append(
                f"| {a['strategy']} | {a['samples']} | {_fmt(a['cumulative_return_pct'])} | "
                f"{_fmt(a['mean_return_pct'])} | {_fmt(a['std_return_pct'])} | "
                f"{_fmt(a['sharpe_like'])} | {_fmt(a['worst_drawdown_pct'])} |"
            )
        lines.append("")

    lines.append("---")
    lines.append(
        "_안정성(Sharpe-like) = 일평균수익률 / 수익률 표준편차. 종합 점수는 설정 조합 간 "
        "z-score 가중합이며, 데이터가 누적될수록 신뢰도가 올라갑니다._"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="단일 결과 1건 저장 (--json 또는 개별 옵션)")
    p_add.add_argument("--json", help="레코드 JSON 문자열(객체 또는 배열)")
    p_add.add_argument("--date", help="테스트 날짜 YYYY-MM-DD")
    p_add.add_argument("--strategy", help="지표명 (RSI, MACD, SMA, EMA, BOLLINGER 등)")
    p_add.add_argument("--params", help="지표 파라미터 JSON. 예: '{\"period\":14}'")
    p_add.add_argument("--stop-loss", type=float, help="손절점 %% (예: -3.0)")
    p_add.add_argument("--take-profit", type=float, help="익절점 %% (예: 5.0)")
    p_add.add_argument("--symbol", help="종목/유니버스 라벨")
    p_add.add_argument("--trades", type=int, help="거래 횟수")
    p_add.add_argument("--win-rate", type=float, help="승률 %%")
    p_add.add_argument("--return-pct", type=float, help="수익률 %% (필수)")
    p_add.add_argument("--max-drawdown", type=float, help="최대낙폭 MDD %%")
    p_add.add_argument("--sharpe", type=float, help="샤프지수(선택)")
    p_add.add_argument("--notes", help="메모")
    p_add.set_defaults(func=cmd_add)

    p_import = sub.add_parser("import", help="파일에서 다건 저장 (.json/.jsonl/.csv)")
    p_import.add_argument("file", help="입력 파일 경로")
    p_import.set_defaults(func=cmd_import)

    p_rebuild = sub.add_parser("rebuild", help="results.jsonl에서 SQLite 재생성")
    p_rebuild.set_defaults(func=cmd_rebuild)

    p_list = sub.add_parser("list", help="저장된 레코드 조회")
    p_list.add_argument("--date", help="특정 날짜만")
    p_list.add_argument("--strategy", help="특정 지표만")
    p_list.add_argument("--json", action="store_true", help="JSON으로 출력")
    p_list.set_defaults(func=cmd_list)

    p_analyze = sub.add_parser("analyze", help="설정 조합 순위/추천 분석 출력")
    p_analyze.add_argument("--json", action="store_true", help="JSON으로 출력")
    p_analyze.set_defaults(func=cmd_analyze)

    p_report = sub.add_parser("report", help="REPORT.md 파일로 리포트 작성")
    p_report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BacktestError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
