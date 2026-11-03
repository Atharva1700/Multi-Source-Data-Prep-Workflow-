"""Generate five synthetic extracts with mismatched schemas, formats and injected duplicates.

All data is synthetic. The sources disagree on column names, key formats, date formats, money
formats and file formats, which is what the prep workflow has to reconcile.

Usage: python generate/make_sources.py --out data
"""
import argparse
import csv
import json
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SEED = 20261102
N_CUST = 2000
N_DUP_CUST = 100      # customers that also appear as a stale older CRM row
N_ACCT = 3000
N_TXN = 20000
N_BRANCH = 40
N_TICKET = 300
REGIONS = ["North", "South", "East", "West"]
SEVERITIES = ["High", "Medium", "Low"]
BASE = date(2026, 1, 1)


def fmt_date(d, rng):
    """70% ISO, 30% day-first, the mix a real merge has to cope with."""
    return d.isoformat() if rng.random() < 0.7 else d.strftime("%d/%m/%Y")


def cust_key(i, rng):
    """CRM uses C-000123. About 10% of references are lower-cased with stray whitespace."""
    key = f"C-{i:06d}"
    return f"{key.lower()} " if rng.random() < 0.1 else key


def write_crm(path, rng):
    rows = []
    for i in range(1, N_CUST + 1):
        d = BASE + timedelta(days=rng.randrange(150, 300))   # current record
        rows.append([cust_key(i, rng), f"Customer {i}", f"user{i}@example.test",
                     rng.choice(REGIONS), fmt_date(d, rng)])
    for i in rng.sample(range(1, N_CUST + 1), N_DUP_CUST):    # stale duplicate, older date
        d = BASE + timedelta(days=rng.randrange(0, 100))
        rows.append([cust_key(i, rng), f"Customer {i}", f"old{i}@example.test",
                     rng.choice(REGIONS), fmt_date(d, rng)])
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["CustKey", "Full Name", "E-mail", "Region", "Last Updated"])
        w.writerows(rows)
    return len(rows)


def write_accounts(path, rng):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["acct_no", "cust_ref", "bal", "open_dt"])
        for a in range(1, N_ACCT + 1):
            acct = f"{a:08d}" if rng.random() < 0.8 else str(a)       # some lost leading zeros
            cust = rng.randint(1, N_CUST)
            cust_ref = f" {cust} " if rng.random() < 0.2 else str(cust)
            cents = rng.randint(-50000, 2000000)
            bal = f"{cents / 100:,.2f}"                                  # "1,234.50" style
            opened = BASE - timedelta(days=rng.randrange(0, 2500))
            w.writerow([acct, cust_ref, bal, fmt_date(opened, rng)])
    return N_ACCT


def write_transactions(path, rng):
    t0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    with path.open("w", encoding="utf-8") as f:
        for t in range(1, N_TXN + 1):
            br = rng.randint(1, N_BRANCH)
            rec = {"id": t, "customer": rng.randint(1, N_CUST),
                   "amt_cents": rng.randint(-20000, 50000),          # negatives are refunds
                   "ts": t0 + rng.randrange(0, 300 * 86400),
                   "branch": f"br-{br:02d}" if rng.random() < 0.5 else f"BR-{br:02d}"}
            f.write(json.dumps(rec) + "\n")
    return N_TXN


def write_branches(path):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Branch Code", "Market"])
        for b in range(1, N_BRANCH + 1):
            w.writerow([f"BR-{b:02d}", f"Market-{'ABCDEFGH'[b % 8]}"])
    return N_BRANCH


def write_complaints(path, rng):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["ticket", "CustomerID", "Severity"])
        for t in range(1, N_TICKET + 1):
            sev = rng.choice(SEVERITIES)
            sev = rng.choice([sev, sev.upper(), sev.lower()])
            w.writerow([f"T{1000 + t}", cust_key(rng.randint(1, N_CUST), rng), sev])
    return N_TICKET


def generate(out_dir, seed=SEED):
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return {
        "crm_customers.csv": write_crm(out / "crm_customers.csv", rng),
        "core_accounts.csv": write_accounts(out / "core_accounts.csv", rng),
        "txn.jsonl": write_transactions(out / "txn.jsonl", rng),
        "branch_ref.csv": write_branches(out / "branch_ref.csv"),
        "complaints.tsv": write_complaints(out / "complaints.tsv", rng),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    args = ap.parse_args()
    print(json.dumps(generate(args.out)))
