# PK-25: out-of-sample validation of Smart Money Accumulation Score v1.0

Research outputs only. Nothing here is read by the app. The full write-up is
`docs/OPUS55_PK25_IMPLEMENTATION_REPORT.md`.

**Result: LIMITED** (from the pre-declared rules in `validation_manifest.json`). The rest of
this file lists the outputs and how to reproduce them.

## Files

| File | What it is |
|---|---|
| `validation_manifest.json` | See **Manifest contents** below. |
| `signal_dataset.csv` | 410 historical strategy signals (2024-09-03 to 2026-09-17), one row per signal, sha256 `1ccadace…31f0`. Columns in order: PK-23 features, PK-24 score, point-in-time market context, then every `outcome_*` column last. The file holds market data only; no account or ticket data. |
| `dataset_summary.json` | Row counts, exclusions, labels and segment sizes. |
| `*_pre_holdout.csv`, `pre_holdout_results.json` | Stage 1. Two evaluation sets: `development` (2024-09-01 to 2025-08-31, descriptive) and `validation` (2025-09-01 to 2026-01-31, models trained on development). Also covers the walk-forward folds before the holdout. |
| `*_holdout.csv`, `holdout_results.json` | Stage 2. The holdout (2026-02-01 onwards), models trained on development plus validation, the full walk-forward, and the classification. |

**Manifest contents.** The manifest records three events:
- **Model freeze:** the score and feature versions, weights, ramps, bands, and the normalised sha256 of the score, feature, strategy, risk, execution and backtest code.
- **Design freeze:** the dataset sha256, split dates, hypotheses, buckets, decision criteria, bootstrap settings, the analysis-code hash, and the hashes of the pre-holdout outputs.
- **Holdout evaluation:** its timestamp and output hashes.

**Output tables, per stage:**
- `baselines`, `buckets`, `monotonicity`, `models` (fits and paired ΔIC), `walk_forward`;
- `strategies`, `regimes`, `robustness`, `contrarian`;
- `extreme_bars` (the cohort summary) and `extreme_bar_members` (every cohort bar);
- `filter_simulation` (PK-21 portfolio runs: all signals, score ≥ 60, score ≥ 75).

## Reproduce

Work on a **copy** of the database. It is opened read-only and immutable.

```
python -m backend.smart_money_validation build   --db <copy.db> --dataset <scratch>/ds.csv
python -m backend.smart_money_validation analyze --stage pre_holdout --db <copy.db> --dataset research/pk25/signal_dataset.csv
python -m backend.smart_money_validation verify  --db <copy.db> --dataset research/pk25/signal_dataset.csv
```

What to expect:
- **`build`** reproduces `signal_dataset.csv` byte-for-byte, from `kite_terminal.db.pre_pk25_bak` (sha256 `01a99048…7216`).
- **`analyze --stage pre_holdout`** refuses to run now that the design is frozen. To reproduce stage 1, point `--out` at a directory that holds only a copy of the manifest's `model_freeze`.
- **`verify`** recomputes the holdout stage in a temporary directory and compares every file with the recorded hashes.

`analyze --stage holdout` refuses to run a second time into this directory. A new experiment must start from a new output directory and a new manifest.
