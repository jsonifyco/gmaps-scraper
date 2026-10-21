"""Tests for gmaps_scraper CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from gmaps_scraper import __version__
from gmaps_scraper.benchmark import profile_input
from gmaps_scraper.cli import cmd_export, cmd_reconcile, cmd_resume, cmd_status, cmd_stop, cmd_validate, main
from gmaps_scraper.input_model import apify_schema
from gmaps_scraper.store import Store


def test_version_string():
    assert __version__ == "1.0.0"


def test_package_data_loaded():
    schema = apify_schema()
    assert "properties" in schema
    assert "searchStringsArray" in schema["properties"]

    for prof in ("discovery", "deep"):
        name, data = profile_input(prof)
        assert prof in name
        assert isinstance(data, dict)


def test_validate_examples(examples_dir: Path):
    json_files = list(examples_dir.glob("*.json"))
    assert len(json_files) >= 5, "Expected example JSON files"

    for path in json_files:
        args = argparse.Namespace(input=str(path))
        exit_code = cmd_validate(args)
        assert exit_code == 0, f"Validation failed for {path.name}"


def test_validate_invalid_input(tmp_path: Path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text('{"verifyLeadsEnrichmentEmails": true}', encoding="utf-8")

    args = argparse.Namespace(input=str(bad_file))
    exit_code = cmd_validate(args)
    assert exit_code == 2, "Excluded add-on should be rejected with code 2"


def test_cli_benchmark_validate_only(tmp_path: Path, capsys):
    for profile in ("discovery", "deep"):
        code = main(["benchmark", profile, "--job-dir", str(tmp_path / profile), "--validate-only"])
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["valid"] is True
        assert out["profile"] == profile


def test_cli_status_stop_and_export(tmp_path: Path, capsys):
    store = Store(tmp_path / "state.sqlite")
    store.init_job({"searchStringsArray": ["cafe"]}, {"searchStringsArray": ["cafe"]}, {})

    # Status JSON
    assert cmd_status(argparse.Namespace(job_dir=str(tmp_path), json=True)) == 0
    status_out = json.loads(capsys.readouterr().out)
    assert status_out["job"]["status"] == "created"

    # Stop request
    assert cmd_stop(argparse.Namespace(job_dir=str(tmp_path), reason="user_test")) == 0
    capsys.readouterr()
    assert store.job()["stop_requested"] == 1
    assert store.job()["stop_reason"] == "user_test"

    # Export Apify JSON
    out_json = tmp_path / "dataset.json"
    assert cmd_export(argparse.Namespace(job_dir=str(tmp_path), format="apify", out=str(out_json), jsonl=False, csv=False)) == 0
    capsys.readouterr()
    assert out_json.exists()
    assert json.loads(out_json.read_text(encoding="utf-8")) == []

    # Export Native
    out_native = tmp_path / "native_out"
    assert cmd_export(argparse.Namespace(job_dir=str(tmp_path), format="native", out=str(out_native), jsonl=False, csv=False)) == 0
    capsys.readouterr()
    assert (out_native / "places.jsonl").exists()


def test_cli_missing_job_errors(tmp_path: Path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert cmd_stop(argparse.Namespace(job_dir=str(empty_dir), reason="stop")) == 2
    assert cmd_export(argparse.Namespace(job_dir=str(empty_dir), format="apify", out=str(tmp_path / "out.json"), jsonl=False, csv=False)) == 2
    assert cmd_reconcile(argparse.Namespace(job_dir=str(empty_dir), env=None)) == 2
    assert cmd_resume(argparse.Namespace(job_dir=str(empty_dir), verbose=False, env=None, concurrency=None, retry_failed=None, export="none")) == 2

