"""Cross-token rotation market-making backtest with random and fixed-hold controls.

Implements GOAL.md. Every position is a concentrated-liquidity range centred on the
pool price at entry; fees accrue only while the traded price sits inside the range,
and the position is marked against holding the identical opening basket so that the
token's own direction is differenced out.
"""
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/root/base_rotate_r0/src")
import params as P

Q96 = 2.0 ** 96
OUT = "/root/base_rotate_r0/out"


def load():
    z = np.load(f"{OUT}/panel.npz", allow_pickle=False)
    d = {k: z[k] for k in z.files}
    d["p"] = (d["sp_last"] / Q96) ** 2
    d["p_min"] = (d["sp_min"] / Q96) ** 2
    d["p_max"] = (d["sp_max"] / Q96) ** 2
    d["fee_rate"] = d["fee"] / 1e6
    return d


def vu_of(p, h, weth_t1):
    """WETH value of one unit of liquidity spread over [p*e^-h, p*e^h] at price p."""
    k = 2.0 * (1.0 - np.exp(-h / 2.0))
    return np.where(weth_t1, k * np.sqrt(p), k / np.sqrt(p))


def amounts_of(L, p, pa, pb):
    """Token amounts of a position of liquidity L at price p over [pa, pb]."""
    pc = np.clip(p, pa, pb)
    a0 = L * (1.0 / np.sqrt(pc) - 1.0 / np.sqrt(pb))
    a1 = L * (np.sqrt(pc) - np.sqrt(pa))
    return a0, a1


def to_weth(a0, a1, p, weth_t1):
    return a1 + a0 * p if weth_t1 else a0 + a1 / p


def sigma_matrix(p, win):
    """Trailing standard deviation of 15-minute log returns, causal."""
    lp = np.log(np.where(p > 0, p, np.nan))
    r = np.diff(lp, axis=1, prepend=lp[:, :1])
    r = np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0)
    c1 = np.cumsum(r, axis=1)
    c2 = np.cumsum(r * r, axis=1)
    z = np.zeros((r.shape[0], 1))
    c1 = np.concatenate([z, c1], axis=1)
    c2 = np.concatenate([z, c2], axis=1)
    lo = np.maximum(np.arange(r.shape[1] + 1) - win, 0)
    s1 = c1[:, 1:] - c1[:, lo[1:]]
    s2 = c2[:, 1:] - c2[:, lo[1:]]
    n = np.minimum(np.arange(1, r.shape[1] + 1), win).astype(float)
    var = np.maximum(s2 / n - (s1 / n) ** 2, 0.0)
    # shift by one bucket so sigma at t uses only returns strictly before t
    sd = np.sqrt(var)
    return np.concatenate([sd[:, :1], sd[:, :-1]], axis=1)


def trailing_sum(x, win):
    c = np.cumsum(x, axis=1)
    z = np.zeros((x.shape[0], 1))
    c = np.concatenate([z, c], axis=1)
    lo = np.maximum(np.arange(x.shape[1] + 1) - win, 0)
    return c[:, 1:] - c[:, lo[1:]]


def slip_bps(n_raw, Lq, p, weth_t1):
    """Average price impact of swapping n_raw of WETH-equivalent through liquidity Lq."""
    with np.errstate(divide="ignore", invalid="ignore"):
        f = np.where(weth_t1, n_raw / (Lq * np.sqrt(p)), n_raw * np.sqrt(p) / Lq)
    return np.where(np.isfinite(f), f * 1e4, 1e9)


class Sim:
    def __init__(self, d, delta, K, theta, c, entry0):
        self.d, self.delta, self.K, self.theta, self.c = d, delta, K, theta, c
        self.entry0 = entry0
        NP, NB = d["p"].shape
        self.NP, self.NB = NP, NB
        self.w1 = d["weth_is_token1"].astype(bool)[:, None]
        sig = sigma_matrix(d["p"], P.VOL_WIN_BUCKETS)
        self.h = np.clip(c * sig * np.sqrt(delta), P.MIN_H, P.MAX_H)
        self.vu = vu_of(d["p"], self.h, self.w1)
        vol24 = trailing_sum(d["vol_over_L"], P.SIGNAL_WIN_BUCKETS)
        # expected fee income per unit of position value over the trailing window
        with np.errstate(divide="ignore", invalid="ignore"):
            self.signal = d["fee_rate"][:, None] * vol24 / self.vu
        self.signal[~np.isfinite(self.signal)] = -1.0
        self.V_usd = P.TOTAL_CAP_USD / K
        self.notional_raw = self.V_usd / d["eth_usd"] * 1e18          # per bucket
        dob = d["day_of_bucket"]
        self.cost0 = d["cost_bps_d"][:, dob]
        feas = d["feasible_d"][:, dob] > 0
        inv = d["maxout_d"][:, dob] >= d["need_d"][:, dob] * (self.V_usd / 50.0)
        Lm = self.notional_raw[None, :] / self.vu
        cap = Lm <= P.MAX_SHARE * d["L_q10"]
        self.eligible = (feas & inv & cap & (self.cost0 <= P.COST_CAP_BPS)
                         & (self.signal > 0) & np.isfinite(d["p"]) & (d["p"] > 0))
        self.Lm = Lm

    def rank(self, t, n):
        s = np.where(self.eligible[:, t], self.signal[:, t], -1.0)
        if n >= self.NP:
            order = np.argsort(-s)
        else:
            part = np.argpartition(-s, n)[:n]
            order = part[np.argsort(-s[part])]
        return order[s[order] > 0]

    def fees(self, i, t, t1, L, pa, pb):
        """WETH-raw fees earned by liquidity L over buckets [t, t1) while in range.

        Σ|WETH|/L_act blows up on swaps that execute against near-zero active
        liquidity, so the per-bucket take is capped at the total fee the pool
        collected in that bucket -- a liquidity provider cannot earn more than
        every fee paid. The cap makes the fee figure an upper bound; how often it
        binds is reported alongside the returns.
        """
        d = self.d
        lo = np.log(np.maximum(d["p_min"][i, t:t1], 1e-300))
        hi = np.log(np.maximum(d["p_max"][i, t:t1], 1e-300))
        la, lb = np.log(pa), np.log(pb)
        span = hi - lo
        ov = np.maximum(np.minimum(hi, lb) - np.maximum(lo, la), 0.0)
        frac = np.where(span > 0, ov / np.maximum(span, 1e-300),
                        ((lo >= la) & (lo <= lb)).astype(float))
        fr = d["fee_rate"][i]
        raw = fr * d["vol_over_L"][i, t:t1] * L
        cap = fr * d["weth_vol"][i, t:t1] * 1e18
        took = np.minimum(np.nan_to_num(raw), np.nan_to_num(cap))
        self.cap_hits += int(np.sum((raw > cap) & (cap > 0)))
        self.cap_tries += int(np.sum(cap > 0))
        return float(np.nansum(took * frac))

    def run(self, mode="signal", rng=None, fixed=None):
        d = self.d
        rebal = np.arange(self.entry0, self.NB - self.delta, self.delta)
        slots = [None] * self.K          # each: dict(i, pa, pb, L, a0, a1, entry_raw)
        recs = []
        self.cap_hits = self.cap_tries = 0
        self.equity = []                 # (bucket, total USD across slots + idle cash)
        cash_raw = float(self.notional_raw[self.entry0]) * self.K
        n_switch = n_recenter = 0
        for t in rebal:
            t1 = t + self.delta
            if mode == "fixed":
                cand = [f for f in fixed if self.eligible[f, t]]
            else:
                cand = list(self.rank(t, max(self.K * 4, 20)))
                if mode == "random" and cand:
                    pool_set = np.flatnonzero(self.eligible[:, t])
                    if len(pool_set):
                        cand = list(rng.choice(pool_set, size=min(len(pool_set), self.K * 4),
                                               replace=False))
            taken = set(s["i"] for s in slots if s is not None)
            for k in range(self.K):
                cur = slots[k]
                tgt = None
                for j in cand:
                    if j == (cur["i"] if cur else -1) or j not in taken:
                        tgt = int(j)
                        break
                action = "hold"
                if cur is not None:
                    pnow = d["p"][cur["i"], t]
                    out_of_range = not (cur["pa"] <= pnow <= cur["pb"])
                    better = (tgt is not None and tgt != cur["i"]
                              and self.signal[tgt, t]
                              > self.theta * max(self.signal[cur["i"], t], 1e-300))
                    if better or not self.eligible[cur["i"], t]:
                        action = "switch"
                    elif out_of_range:
                        action = "recenter"
                else:
                    action = "open" if tgt is not None else "none"
                if action in ("hold", "none"):
                    continue

                # unwind the incumbent: swap the non-WETH leg back, book the record
                if cur is not None:
                    pnow = d["p"][cur["i"], t]
                    a0, a1 = amounts_of(cur["L"], pnow, cur["pa"], cur["pb"])
                    nonw = a0 * pnow if self.w1[cur["i"], 0] else a1 / pnow
                    cb = self.cost0[cur["i"], t] + slip_bps(
                        nonw, d["L_q10"][cur["i"], t], pnow, self.w1[cur["i"], 0])
                    exit_cost = nonw * min(cb, 5e3) / 1e4
                    val = to_weth(a0, a1, pnow, self.w1[cur["i"], 0])
                    recs.append(self.close_record(cur, t, val, exit_cost, 0.0))
                    cash_raw += max(val + cur["fees"] - exit_cost, 0.0)
                    taken.discard(cur["i"])
                    slots[k] = None

                i = cur["i"] if action == "recenter" else tgt
                if i is None or not np.isfinite(d["p"][i, t]) or d["p"][i, t] <= 0:
                    continue
                gas = P.GAS_RECENTER if action == "recenter" else P.GAS_SWITCH
                n_recenter += action == "recenter"
                n_switch += action in ("switch", "open")
                gas_raw = gas * P.GAS_USD_PER_UNIT / d["eth_usd"][t] * 1e18
                V = min(cash_raw, float(self.notional_raw[t])) - gas_raw
                # below a $50 floor a position is not worth minting, and dividing
                # returns by a near-zero base manufactures absurd basis points
                if V <= 50.0 / d["eth_usd"][t] * 1e18:
                    continue
                cash_raw -= V + gas_raw
                p0 = d["p"][i, t]
                h = self.h[i, t]
                pa, pb = p0 * np.exp(-h), p0 * np.exp(h)
                cb = self.cost0[i, t] + slip_bps(V / 2.0, d["L_q10"][i, t], p0, self.w1[i, 0])
                V -= V / 2.0 * min(cb, 5e3) / 1e4
                L = V / vu_of(p0, h, self.w1[i, 0])
                a0, a1 = amounts_of(L, p0, pa, pb)
                slots[k] = dict(i=int(i), pa=float(pa), pb=float(pb), L=float(L),
                                a0=float(a0), a1=float(a1), t0=int(t), fees=0.0,
                                gas_usd=float(gas * P.GAS_USD_PER_UNIT))
                taken.add(int(i))

            for k in range(self.K):
                s = slots[k]
                if s is not None:
                    s["fees"] += self.fees(s["i"], t, t1, s["L"], s["pa"], s["pb"])

            eq = cash_raw
            for s in slots:
                if s is None:
                    continue
                pn = d["p"][s["i"], min(t1, self.NB - 1)]
                a0, a1 = amounts_of(s["L"], pn, s["pa"], s["pb"])
                eq += to_weth(a0, a1, pn, self.w1[s["i"], 0]) + s["fees"]
            self.equity.append((int(t1), eq / 1e18 * d["eth_usd"][min(t1, self.NB - 1)]))
        # close out anything still open
        for k in range(self.K):
            s = slots[k]
            if s is None:
                continue
            t = int(rebal[-1] + self.delta)
            pnow = d["p"][s["i"], min(t, self.NB - 1)]
            a0, a1 = amounts_of(s["L"], pnow, s["pa"], s["pb"])
            recs.append(self.close_record(s, min(t, self.NB - 1),
                                          to_weth(a0, a1, pnow, self.w1[s["i"], 0]), 0.0, 0.0))
        return recs, n_switch, n_recenter

    def close_record(self, s, t, val_raw, exit_cost_raw, gas_usd):
        d = self.d
        pnow = d["p"][s["i"], t]
        hodl = to_weth(s["a0"], s["a1"], pnow, self.w1[s["i"], 0])
        v0 = to_weth(s["a0"], s["a1"], d["p"][s["i"], s["t0"]], self.w1[s["i"], 0])
        gas_raw = (s["gas_usd"] + gas_usd) / d["eth_usd"][t] * 1e18
        net = val_raw + s["fees"] - exit_cost_raw - gas_raw
        return dict(pool=int(s["i"]), t0=int(s["t0"]), t1=int(t), v0=float(v0),
                    val=float(val_raw), fees=float(s["fees"]), hodl=float(hodl),
                    gas_raw=float(gas_raw), exit_cost=float(exit_cost_raw),
                    net_vs_hodl_bps=float((net - hodl) / v0 * 1e4) if v0 > 0 else 0.0,
                    fee_bps=float(s["fees"] / v0 * 1e4) if v0 > 0 else 0.0,
                    il_bps=float((val_raw - hodl) / v0 * 1e4) if v0 > 0 else 0.0,
                    abs_bps=float((net - v0) / v0 * 1e4) if v0 > 0 else 0.0)
