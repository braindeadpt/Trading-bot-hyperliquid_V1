import csv
from collections import Counter

with open('data/backtests/ensemble_sweep_20260625_234431.csv') as f:
    rows = list(csv.DictReader(f))

# Check for duplicates
signatures = [(r['threshold'], r['min_agreeing'], r['hc_threshold'], r['exclude'],
               r['n_trades'], r['sharpe'], r['expectancy']) for r in rows]
unique = set(signatures)
print(f"Total rows: {len(rows)}, Unique results: {len(unique)}")

# Show unique combos
print(f"\nUnique (n_trades, sharpe, expectancy):")
for sig in sorted(unique, key=lambda x: float(x[4]), reverse=True):
    print(f"  n={sig[4]}  Sharpe={sig[5]}  Exp=${sig[6]}")

# Show if strategy configs changed at all
print(f"\nSample first row: {dict(list(rows[0].items())[:8])}")
print(f"Sample last row: {dict(list(rows[-1].items())[:8])}")