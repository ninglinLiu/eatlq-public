import json
from collections import defaultdict

pl = json.load(open('_priced_live.json'))
sz = json.load(open('_size.json'))
SIZES = [100, 300, 1000, 3000]
FRIC = -0.05

traded = [p for p, v in pl.items()
          if v.get('why') == 'ok'
          and sz.get(f'{p}:100') and sz[f'{p}:100'].get('net') is not None]
n = len(traded)

by = defaultdict(list)
for p in traded:
    by[pl[p]['exwhy']].append(pl[p]['net'])
nets = [pl[p]['net'] for p in traded]
wins = len([x for x in nets if x > 0])
fatal = len([x for x in nets if x <= -0.5])
pos = sum(x for x in nets if x > 0)
neg = abs(sum(x for x in nets if x < 0))

print(f'TRADED SET (entered + QuoterV2-priced): {n}')
print(f'win {wins}/{n}={100*wins/n:.0f}%  fatal<=-50%: {fatal}={100*fatal/n:.1f}%  '
      f'PF={pos/neg:.2f}  avg={100*sum(nets)/n:+.0f}%  median={100*sorted(nets)[n//2]:+.0f}%')
print()

tot = sum(nets)
order = sorted(by.items(), key=lambda kv: -sum(kv[1]))
print(f'{"exit":<10}{"n":>4}{"share":>7}{"avg%":>8}{"win%":>7}{"contrib%":>9}')
for k, a in order:
    m = len(a)
    print(f'{k:<10}{m:>4}{100*m/n:>6.0f}%{100*sum(a)/m:>7.0f}%'
          f'{100*len([x for x in a if x>0])/m:>6.0f}%{100*sum(a)/tot:>8.0f}%')
print()


def dyn(cap):
    ladder = [s for s in sorted(SIZES, reverse=True) if s <= cap]
    t = 0.0
    cnt = 0
    for p in traded:
        for s in ladder:
            r = sz.get(f'{p}:{s}')
            if r and r.get('net') is not None and r.get('fric') is not None and r['fric'] >= FRIC:
                t += s * r['net']
                cnt += 1
                break
    return t, cnt


def static(s):
    t = 0.0
    cnt = 0
    for p in traded:
        r = sz.get(f'{p}:{s}')
        if r and r.get('net') is not None and r.get('fric') is not None and r['fric'] >= FRIC:
            t += s * r['net']
            cnt += 1
    return t, cnt


print('STATIC vs DYNAMIC (120d window):')
for s in [100, 300, 1000]:
    t, c = static(s)
    print(f'  static ${s:<5} 120d=${t:>9,.0f}  /mo=${t/4:>8,.0f}  trades={c}')
for cap in [300, 1000, 3000]:
    t, c = dyn(cap)
    print(f'  dyn cap=${cap:<4} 120d=${t:>9,.0f}  /mo=${t/4:>8,.0f}  trades={c}')
