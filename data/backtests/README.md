# 트레이딩봇 백테스트 결과 저장소

토스증권 트레이딩봇의 일별 백테스트 결과를 저장하고, 가장 안정적이고 수익률이
높은 **지표 + 손익점(손절/익절)** 설정을 찾기 위한 데이터 디렉터리입니다.

> 이 도구는 순수 로컬 기록/분석 도구입니다. 토스증권 API를 호출하지 않으며,
> 실거래·주문과 무관합니다. 사용자가 제공한 백테스트 수치를 정리/집계할 뿐입니다.

## 구성

| 파일 | 역할 |
| --- | --- |
| `results.jsonl` | **원본 저장소(source of truth).** 한 줄에 레코드 1건. git에 커밋되어 영속. |
| `results.db` | `results.jsonl`에서 생성되는 SQLite (파생물, git 제외). |
| `config.json` | 테스트 기간(종료일 2026-07-17)과 점수 가중치 설정. |
| `REPORT.md` | `report` 명령으로 생성되는 분석 리포트. |

## 레코드 형식

한 레코드 = **하루 + 하나의 설정(지표/파라미터/손절/익절)** 의 성과입니다.
여러 설정을 동시에 비교하려면 같은 날짜로 설정마다 한 건씩 넣으세요.

```json
{
  "test_date": "2026-06-26",
  "strategy": "RSI",
  "params": {"period": 14, "entry": 30, "exit": 70},
  "stop_loss_pct": -3.0,
  "take_profit_pct": 5.0,
  "symbol": "A005930",
  "trades": 12,
  "wins": 7,
  "losses": 5,
  "win_rate": 58.3,
  "return_pct": 1.2,
  "max_drawdown_pct": -4.0,
  "sharpe": 1.3,
  "profit_factor": 1.8,
  "notes": "장 초반 변동성 구간"
}
```

### 필드

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `test_date` | ✅ | 테스트 날짜 `YYYY-MM-DD`. 종료일(2026-07-17) 이후는 거부됨. |
| `strategy` | ✅ | 지표명. 예: `RSI`, `MACD`, `SMA`, `EMA`, `BOLLINGER`, `RSI+MACD`. (`indicator`도 허용) |
| `return_pct` | ✅ | 해당 일/설정의 수익률(%). |
| `params` | 선택 | 지표 파라미터(객체/문자열). 같은 지표라도 파라미터가 다르면 다른 설정으로 집계. |
| `stop_loss_pct` | 선택 | 손절점(%). 예: `-3.0`. |
| `take_profit_pct` | 선택 | 익절점(%). 예: `5.0`. |
| `trades` / `wins` / `losses` | 선택 | 거래·승·패 횟수. `win_rate` 미입력 시 자동 계산. |
| `win_rate` | 선택 | 승률(%). 0~1 비율로 주면 자동으로 %로 변환. |
| `max_drawdown_pct` | 선택 | 최대낙폭 MDD(%), 보통 음수. |
| `sharpe` / `profit_factor` | 선택 | 추가 지표. |
| `symbol` / `notes` | 선택 | 종목/유니버스 라벨, 메모. |
| 그 외 필드 | 선택 | 무엇이든 `extra`로 보존됨(유실 없음). |

## 사용법

```bash
# 1건 저장 (개별 옵션)
python3 scripts/backtest_db.py add --date 2026-06-26 --strategy RSI \
  --params '{"period":14}' --stop-loss -3 --take-profit 5 \
  --trades 12 --win-rate 58.3 --return-pct 1.2 --max-drawdown -4

# 1건 저장 (JSON 문자열)
python3 scripts/backtest_db.py add --json '{"test_date":"2026-06-26","strategy":"MACD","return_pct":0.8}'

# 파일에서 다건 import (.json 배열 / .jsonl / .csv)
python3 scripts/backtest_db.py import day_2026-06-26.json

# 저장된 레코드 조회
python3 scripts/backtest_db.py list
python3 scripts/backtest_db.py list --date 2026-06-26
python3 scripts/backtest_db.py list --strategy RSI

# 분석(설정 순위/추천) 출력 / JSON
python3 scripts/backtest_db.py analyze
python3 scripts/backtest_db.py analyze --json

# REPORT.md 리포트 생성
python3 scripts/backtest_db.py report

# JSONL에서 SQLite 재생성
python3 scripts/backtest_db.py rebuild
```

## 분석 기준

설정 조합을 `(지표, 파라미터, 손절, 익절)` 단위로 묶어 테스트 기간 전체를 집계합니다.

- **누적 수익률**: 일별 수익률 복리 누적
- **안정성(Sharpe-like)**: 일평균수익률 / 수익률 표준편차 (높을수록 안정적)
- **승률**, **최대낙폭(MDD)**
- **종합 점수**: 위 지표를 설정 조합 간 z-score로 정규화한 가중합
  (`config.json`의 `scoring_weights`로 조정 — 기본 수익률 0.35 / 안정성 0.30 / 승률 0.20 / MDD 0.15)

데이터가 누적될수록(특히 며칠 이상) 표준편차/Sharpe 추정이 안정화되어 추천 신뢰도가 올라갑니다.
