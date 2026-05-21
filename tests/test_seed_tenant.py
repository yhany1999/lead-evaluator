import sys
import pytest
from tools import db as db_module
from tools.db import get_tenant_by_api_key, init_db


def run_seed(argv: list[str], tmp_path, monkeypatch):
    """Invoke seed_tenant.main() with patched DB and argv, return captured output."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(sys, "argv", ["seed_tenant.py"] + argv)
    from tools.seed_tenant import main
    main()


# ── core provisioning ─────────────────────────────────────────────────────────

def test_seed_creates_tenant_in_db(tmp_path, monkeypatch, capsys):
    run_seed(["agency-01", "Alpha Realty"], tmp_path, monkeypatch)
    tenant = get_tenant_by_api_key(capsys.readouterr().out.split("API Key: ")[1].split()[0])
    assert tenant is not None
    assert tenant.client_id == "agency-01"
    assert tenant.name == "Alpha Realty"


def test_seed_prints_api_key_to_stdout(tmp_path, monkeypatch, capsys):
    run_seed(["agency-01", "Alpha Realty"], tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert "API Key:" in out
    key_line = [l for l in out.splitlines() if l.startswith("API Key:")][0]
    key = key_line.split("API Key: ")[1].strip()
    assert len(key) > 20


def test_printed_key_authenticates(tmp_path, monkeypatch, capsys):
    run_seed(["agency-01", "Alpha Realty"], tmp_path, monkeypatch)
    out = capsys.readouterr().out
    key = [l for l in out.splitlines() if l.startswith("API Key:")][0].split("API Key: ")[1].strip()
    tenant = get_tenant_by_api_key(key)
    assert tenant is not None
    assert tenant.client_id == "agency-01"


def test_generated_keys_are_unique(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    init_db()

    monkeypatch.setattr(sys, "argv", ["seed_tenant.py", "agency-01", "Alpha"])
    from tools.seed_tenant import main
    main()
    key1 = [l for l in capsys.readouterr().out.splitlines() if l.startswith("API Key:")][0].split("API Key: ")[1].strip()

    monkeypatch.setattr(sys, "argv", ["seed_tenant.py", "agency-02", "Beta"])
    main()
    key2 = [l for l in capsys.readouterr().out.splitlines() if l.startswith("API Key:")][0].split("API Key: ")[1].strip()

    assert key1 != key2


def test_duplicate_client_id_exits_with_code_1(tmp_path, monkeypatch, capsys):
    run_seed(["agency-01", "Alpha Realty"], tmp_path, monkeypatch)
    capsys.readouterr()  # flush first call output
    with pytest.raises(SystemExit) as exc:
        run_seed(["agency-01", "Alpha Realty Duplicate"], tmp_path, monkeypatch)
    assert exc.value.code == 1


# ── custom options ────────────────────────────────────────────────────────────

def test_custom_currency_and_budget(tmp_path, monkeypatch, capsys):
    run_seed(
        ["agency-ae", "Dubai Properties",
         "--currency", "AED",
         "--budget-vip-min", "2000000",
         "--budget-medium-min", "800000"],
        tmp_path, monkeypatch,
    )
    out = capsys.readouterr().out
    key = [l for l in out.splitlines() if l.startswith("API Key:")][0].split("API Key: ")[1].strip()
    tenant = get_tenant_by_api_key(key)
    assert tenant.currency == "AED"
    assert tenant.budget_vip_min == 2_000_000
    assert tenant.budget_medium_min == 800_000


def test_custom_vip_locations(tmp_path, monkeypatch, capsys):
    run_seed(
        ["agency-ae", "Dubai Properties",
         "--vip-locations", "Palm Jumeirah,Downtown Dubai"],
        tmp_path, monkeypatch,
    )
    out = capsys.readouterr().out
    key = [l for l in out.splitlines() if l.startswith("API Key:")][0].split("API Key: ")[1].strip()
    tenant = get_tenant_by_api_key(key)
    assert "Palm Jumeirah" in tenant.vip_locations
    assert "Downtown Dubai" in tenant.vip_locations


def test_arabic_output_language(tmp_path, monkeypatch, capsys):
    run_seed(
        ["agency-ae", "Dubai Properties", "--output-language", "ar"],
        tmp_path, monkeypatch,
    )
    out = capsys.readouterr().out
    key = [l for l in out.splitlines() if l.startswith("API Key:")][0].split("API Key: ")[1].strip()
    tenant = get_tenant_by_api_key(key)
    assert tenant.output_language == "ar"
