"""Cleanse and merge five extracts with mismatched schemas into one SQLite database.

Per source: read (csv, tsv or jsonl), rename columns from config, normalise every value by its
declared kind, then reject the run if any row fails. Then resolve duplicate keys (latest
keep_latest_by wins), check primary keys and foreign keys, and only then write SQLite. Nothing
is written unless every check passes.

Output is sorted by primary key and fingerprinted (SHA-256 over canonical CSV per table), so a
re-run on the same or shuffled input gives the same bytes.

Usage: python prep/workflow.py --data data --out out --config prep/config.json
"""
import argparse
import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd


class ValidationError(Exception):
    pass


CUST_RE = re.compile(r"^\s*c?-?\s*0*(\d+)\s*$", re.IGNORECASE)
ACCT_RE = re.compile(r"^\s*(\d{1,8})\s*$")
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y")
DAY_FIRST_RE = re.compile(r"^\s*\d{2}/\d{2}/\d{4}\s*$")
NORMALISED_KINDS = {"cust", "acct", "code", "lower"}


def norm_value(kind, raw):
    """Return the normalised value, or None if the raw value is not valid for its kind."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw)
    if kind == "cust":
        m = CUST_RE.match(s)
        return int(m.group(1)) if m else None
    if kind == "acct":
        m = ACCT_RE.match(s)
        return f"{int(m.group(1)):08d}" if m else None
    if kind == "int":
        try:
            return int(s.strip())
        except ValueError:
            return None
    if kind == "code":
        return s.strip().upper() or None
    if kind == "lower":
        return s.strip().lower() or None
    if kind == "text":
        return s.strip() or None
    if kind == "money":
        try:
            cents = Decimal(s.replace(",", "").strip()) * 100
        except InvalidOperation:
            return None
        return int(cents.to_integral_value())
    if kind == "date":
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(s.strip(), fmt).date().isoformat()
            except ValueError:
                continue
        return None
    if kind == "epoch":
        try:
            return pd.Timestamp(int(s), unit="s", tz="UTC").isoformat()
        except (ValueError, TypeError):
            return None
    raise ValueError(f"unknown kind {kind!r}")


def load_source(spec, data_dir):
    path = Path(data_dir) / spec["file"]
    if spec["format"] == "csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    if spec["format"] == "tsv":
        return pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False)
    if spec["format"] == "jsonl":
        return pd.read_json(path, lines=True, dtype=False)
    raise ValueError(f"{spec['name']}: unknown format {spec['format']!r}")


def normalise_source(spec, raw, stats):
    name = spec["name"]
    missing = [c for c in spec["columns"] if c not in raw.columns]
    if missing:
        raise ValidationError(f"{name}: missing source columns {missing}")
    df = raw[list(spec["columns"])].rename(columns=spec["columns"])
    cleaned, bad = {}, pd.Series(False, index=df.index)
    for col in df.columns:
        kind = spec["kinds"][col]
        values = []
        for raw_value in df[col]:
            value = norm_value(kind, raw_value)
            if value is not None:
                if kind in NORMALISED_KINDS and str(raw_value) != str(value):
                    stats[f"{name}.{col}.normalised"] += 1
                if kind == "date" and DAY_FIRST_RE.match(str(raw_value)):
                    stats[f"{name}.{col}.day_first_dates"] += 1
                if kind == "money" and "," in str(raw_value):
                    stats[f"{name}.{col}.thousands_separators"] += 1
            values.append(value)
        cleaned[col] = values
        bad |= pd.Series([v is None for v in values], index=df.index)
    if bad.any():
        examples = df.index[bad][:5].tolist()
        raise ValidationError(f"{name}: {int(bad.sum())} rejected rows (first raw indices {examples})")
    stats[f"{name}.rows_in"] = len(raw)
    return pd.DataFrame(cleaned)


def resolve_duplicates(spec, df, stats):
    """Keep one row per primary key. The latest keep_latest_by wins; ties sort by content."""
    key = spec["primary_key"]
    latest = spec.get("keep_latest_by")
    if latest is None:
        return df
    order = [key, latest] + [c for c in df.columns if c not in (key, latest)]
    ascending = [True, False] + [True] * (len(order) - 2)
    ordered = df.sort_values(order, ascending=ascending, kind="mergesort")
    kept = ordered.drop_duplicates(key, keep="first")
    stats[f"{spec['table']}.duplicates_dropped"] = len(df) - len(kept)
    return kept.reset_index(drop=True)


def build(data_dir, config, stats):
    tables = {}
    for spec in config["sources"]:
        raw = load_source(spec, data_dir)
        df = normalise_source(spec, raw, stats)
        tables[spec["table"]] = resolve_duplicates(spec, df, stats)
    return tables


def validate(tables, config, stats):
    passed = []
    for spec in config["sources"]:
        t = tables[spec["table"]]
        pk = spec["primary_key"]
        dups = int(t[pk].duplicated().sum())
        if dups:
            raise ValidationError(f"{spec['table']}: {dups} duplicate {pk} after resolution")
        expected = stats[f"{spec['name']}.rows_in"] - stats.get(f"{spec['table']}.duplicates_dropped", 0)
        if len(t) != expected:
            raise ValidationError(f"{spec['table']}: row count {len(t)} != expected {expected}")
        passed.append(f"{spec['table']}: primary key unique, row count reconciles")
    for child, ccol, parent, pcol in config["foreign_keys"]:
        orphans = int((~tables[child][ccol].isin(tables[parent][pcol])).sum())
        if orphans:
            raise ValidationError(f"{child}.{ccol}: {orphans} orphan references to {parent}.{pcol}")
        passed.append(f"{child}.{ccol} -> {parent}.{pcol}: no orphans")
    return passed


def sorted_tables(tables, config):
    return {spec["table"]: tables[spec["table"]].sort_values(spec["primary_key"]).reset_index(drop=True)
            for spec in config["sources"]}


def fingerprint(tables):
    h = hashlib.sha256()
    for name in sorted(tables):
        h.update(name.encode())
        h.update(tables[name].to_csv(index=False, lineterminator="\n").encode())
    return h.hexdigest()


def write_db(tables, config, db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    pks = {spec["table"]: spec["primary_key"] for spec in config["sources"]}
    con = sqlite3.connect(db_path)
    try:
        for name in sorted(tables):
            tables[name].to_sql(name, con, index=False)
            con.execute(f"CREATE UNIQUE INDEX ix_{name}_pk ON {name}({pks[name]})")
        con.commit()
    finally:
        con.close()


def run(data_dir, out_dir, config_path):
    t0 = time.perf_counter()
    config = json.loads(Path(config_path).read_text())
    stats = Counter()
    tables = sorted_tables(build(data_dir, config, stats), config)
    checks = validate(tables, config, stats)
    write_db(tables, config, Path(out_dir) / "prep.db")
    report = {
        "rows_out": {name: len(df) for name, df in sorted(tables.items())},
        "stats": dict(sorted(stats.items())),
        "checks_passed": checks,
        "fingerprint": fingerprint(tables),
        "seconds": round(time.perf_counter() - t0, 3),
    }
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "report.json").write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="out")
    ap.add_argument("--config", default="prep/config.json")
    args = ap.parse_args()
    print(json.dumps(run(args.data, args.out, args.config), indent=2))
