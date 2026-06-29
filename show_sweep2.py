import csv

with open('data/backtests/ensemble_sweep_20260625_234431.csv') as f:
    rows = list(csv.DictReader(f))

valid = [r for r in rows if int(r['n_trades']) >= 3]

# Current config
print('CURRENT CONFIG (thr=0.15 ma=2 hct=0.70 exclude=current):')
cur = [r for r in valid if abs(float(r['threshold'])-0.15)<0.01 and int(r['min_agreeing'])==2 and abs(float(r['hc_threshold'])-0.70)<0.01 and r['exclude']=='current']
if cur:
    c = cur[0]
    print(f'  n={c["n_trades"]} Sharpe={c["sharpe"]} Exp=${c["expectancy"]} PF={c["pf"]} DD={c["max_dd"]}% WR={c["win_rate"]}%')
print()

# Top 5 by Sharpe
valid.sort(key=lambda r: float(r['sharpe']), reverse=True)
print('TOP 5 BY SHARPE:')
print('  THR  MA  HCT  EXCL         n  Sharpe   Exp     PF   DD%  WR%')
for r in valid[:5]:
    print('  {:.2f} {:<3d} {:.2f} {:<12s} {:>3d} {:>7.3f} ${:>5.2f} {:>5.3f} {:>5.2f} {:>4.1f}'.format(
        float(r['threshold']), int(r['min_agreeing']), float(r['hc_threshold']),
        r['exclude'], int(r['n_trades']), float(r['sharpe']),
        float(r['expectancy']), float(r['pf']), float(r['max_dd']), float(r['win_rate'])))
print()

# Top 5 by expectancy
valid.sort(key=lambda r: float(r['expectancy']), reverse=True)
print('TOP 5 BY EXPECTANCY:')
print('  THR  MA  HCT  EXCL         n  Sharpe   Exp     PF   DD%  WR%')
for r in valid[:5]:
    print('  {:.2f} {:<3d} {:.2f} {:<12s} {:>3d} {:>7.3f} ${:>5.2f} {:>5.3f} {:>5.2f} {:>4.1f}'.format(
        float(r['threshold']), int(r['min_agreeing']), float(r['hc_threshold']),
        r['exclude'], int(r['n_trades']), float(r['sharpe']),
        float(r['expectancy']), float(r['pf']), float(r['max_dd']), float(r['win_rate'])))
print()

# Summary of all high Sharpe configs (Sharpe > 0)
pos = [r for r in valid if float(r['sharpe']) > 0]
print(f'Configs with Sharpe > 0: {len(pos)} out of {len(valid)} with >= 3 trades')
print()

# Config proposal: the one with highest Sharpe that's NOT the current
valid.sort(key=lambda r: float(r['sharpe']), reverse=True)
print('RECOMMENDED CONFIG (best Sharpe, not current):')
for r in valid:
    if not (abs(float(r['threshold'])-0.15)<0.01 and int(r['min_agreeing'])==2 and abs(float(r['hc_threshold'])-0.70)<0.01 and r['exclude']=='current'):
        print('  thr={} ma={} hct={} exclude={}'.format(
            r['threshold'], r['min_agreeing'], r['hc_threshold'], r['exclude']))
        print('  n={} Sharpe={} Exp=${} PF={} DD={}% WR={}%'.format(
            r['n_trades'], r['sharpe'], r['expectancy'], r['pf'], r['max_dd'], r['win_rate']))
        break