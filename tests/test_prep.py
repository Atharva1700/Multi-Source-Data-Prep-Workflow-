"""Tests for the prep workflow. Run: python3 -m pytest -q tests/"""
import csv
import json
import random
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from generate.make_sources import generate  # noqa: E402
from prep.workflow import ValidationError, norm_value, run  # noqa: E402

CONFIG = ROOT / "prep" / "config.json"


@pytest.fixture(scope="module")
def source_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    generate(d)
    return d


def test_customer_keys_normalise_to_integers():
    assert norm_value("cust", "C-000123") == 123
    assert norm_value("cust", "c-000123 ") == 123
    assert norm_value("cust", " 123 ") == 123
    assert norm_value("cust", "XYZ") is None


def test_account_numbers_pad_to_eight_digits():
    assert norm_value("acct", "1234") == "00001234"
    assert norm_value("acct", "00001234") == "00001234"
    assert norm_value("acct", "12345678901") is None


def test_dates_accept_iso_and_day_first_and_reject_impossible_dates():
    assert norm_value("date", "2026-03-04") == "2026-03-04"
    assert norm_value("date", "04/03/2026") == "2026-03-04"
    assert norm_value("date", "31/02/2026") is None


def test_money_strings_become_integer_cents():
    assert norm_value("money", "1,234.50") == 123450
    assert norm_value("money", "-55.00") == -5500
    assert norm_value("money", "abc") is None


def test_run_resolves_duplicates_to_latest_record(source_dir, tmp_path):
    report = run(source_dir, tmp_path / "out", CONFIG)
    assert report["rows_out"]["customers"] == 2000
    assert report["stats"]["customers.duplicates_dropped"] == 100
    emails = pd.read_sql("SELECT email FROM customers", _con(tmp_path / "out"))["email"]
    assert not emails.str.startswith("old").any()


def test_rerun_gives_identical_fingerprint(source_dir, tmp_path):
    a = run(source_dir, tmp_path / "a", CONFIG)["fingerprint"]
    b = run(source_dir, tmp_path / "b", CONFIG)["fingerprint"]
    assert a == b


def _shuffled_copy(src, dst, seed):
    """Rewrite every source file with its data rows in a different order, same format."""
    dst.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    for name in ("crm_customers.csv", "core_accounts.csv", "branch_ref.csv"):
        lines = (src / name).read_text(encoding="utf-8").splitlines(keepends=True)
        header, body = lines[0], lines[1:]
        rng.shuffle(body)
        (dst / name).write_text(header + "".join(body), encoding="utf-8")
    for name in ("txn.jsonl", "complaints.tsv"):
        lines = (src / name).read_text(encoding="utf-8").splitlines(keepends=True)
        header = lines[0] if name.endswith(".tsv") else None
        body = lines[1:] if header else lines
        rng.shuffle(body)
        (dst / name).write_text((header or "") + "".join(body), encoding="utf-8")


def test_shuffled_input_gives_identical_output(source_dir, tmp_path):
    shuffled = tmp_path / "shuffled"
    _shuffled_copy(source_dir, shuffled, seed=99)
    original = run(source_dir, tmp_path / "orig", CONFIG)["fingerprint"]
    again = run(shuffled, tmp_path / "shuf", CONFIG)["fingerprint"]
    assert original == again


def test_bad_row_aborts_and_writes_nothing(source_dir, tmp_path):
    bad = tmp_path / "bad"
    shutil.copytree(source_dir, bad)
    with (bad / "complaints.tsv").open("a", encoding="utf-8") as f:
        f.write("T9999\tNOT-A-KEY\tHigh\n")
    with pytest.raises(ValidationError, match="rejected rows"):
        run(bad, tmp_path / "out", CONFIG)
    assert not (tmp_path / "out" / "prep.db").exists()


def test_orphan_reference_aborts(source_dir, tmp_path):
    bad = tmp_path / "orphan"
    shutil.copytree(source_dir, bad)
    with (bad / "complaints.tsv").open("a", encoding="utf-8") as f:
        f.write("T9998\tC-999999\tHigh\n")
    with pytest.raises(ValidationError, match="orphan"):
        run(bad, tmp_path / "out", CONFIG)


def test_missing_source_column_aborts(source_dir, tmp_path):
    bad = tmp_path / "missing"
    shutil.copytree(source_dir, bad)
    text = (bad / "complaints.tsv").read_text(encoding="utf-8").replace("Severity", "Sev", 1)
    (bad / "complaints.tsv").write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError, match="missing source columns"):
        run(bad, tmp_path / "out", CONFIG)


def _con(out_dir):
    import sqlite3
    return sqlite3.connect(Path(out_dir) / "prep.db")
