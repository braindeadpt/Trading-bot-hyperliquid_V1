import csv

with open('data/backtests/ensemble_sweep_20260625_234431.csv') as f:
    rows = list(csv.DictReader(f))

valid = [r for r in rows if int(r['n_trades']) >= 3]
valid.sort(key=lambda r: float(r['sharpe']), reverse=True)

print('TOP 10 BY SHARPE (validation Jun 23-25):')
print('  {:<5s} {:<3s} {:<5s} {:<15s} {:>4s} {:>7s} {:>8s} {:>6s} {:>6s}'.format(
    'THR', 'MA', 'HCT', 'EXCL', 'n', 'Sharpe', 'Exp', 'PF', 'DD%'))
print('  ' + '-'*65)
for i, r in enumerate(valid[:10]):
    print('  {:.2f} {:<3d} {:.2f} {:<15s} {:>4d} {:>7.3f} ${:>6.2f} {:>6.3f} {:>6.3f}%'.format(
        float(r['threshold']), int(r['min_agreeing']), float(r['hc_threshold']),
        r['exclude'], int(r['n_trades']), float(r['sharpe']),
        float(r['expectancy']), float(r['pf']), float(r['max_dd'])))

# By expectancy
valid.sort(key=lambda r: float(r['expectancy']), reverse=True)
print()
print('TOP 10 BY EXPECTANCY:')
print('  {:<5s} {:<3s} {:<5s} {:<15s} {:>4s} {:>7s} {:>8s} {:>6s} {:>6s}'.format(
    'THR', 'MA', 'HCT', 'EXCL', 'n', 'Sharpe', 'Exp', 'PF', 'DD%'))
print('  ' + '-'*65)
for i, r in enumerate(valid[:10]):
    print('  {:.2f} {:<3d} {:.2f} {:<15s} {:>4d} {:>7.3f} ${:>6.2f} {:>6.3f} {:>6.3f}%'.format(
        float(r['threshold']), int(r['min_agreeing']), float(r['hc_threshold']),
        r['exclude'], int(r['n_trades']), float(r['sharpe']),
        float(r['expectancy']), float(r['pf']), float(r['max_dd'])))