"""Small hardening items: model registry integrity, FinTS wire-log masking, SMTP TLS,
LLM API key, DB password handoff in the archive script."""
import importlib.util
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from conftest import _memory_db

BACKEND = Path(__file__).resolve().parents[1]


# -- model registry ----------------------------------------------------------


def _pipeline():
    import numpy as np
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    pipe = Pipeline([("scale", StandardScaler())])
    pipe.fit(np.arange(12, dtype=float).reshape(6, 2))
    return pipe


def test_registry_roundtrip_records_and_checks_a_sha256(tmp_path):
    from app.lab.quant_ml import registry

    path = Path(registry.save_model(_pipeline(), "m1", base=str(tmp_path)))

    digest_file = path.with_name("pipeline.joblib.sha256")
    assert digest_file.read_text().strip() == registry._file_sha256(path)
    assert not list(tmp_path.rglob("*.tmp"))
    assert registry.load_model("m1", base=str(tmp_path)) is not None
    assert registry._load_model_unvalidated("m1", base=str(tmp_path)) is not None


def test_tampered_artefact_is_refused_before_joblib_load(tmp_path, monkeypatch):
    import joblib

    from app.lab.quant_ml import registry

    path = Path(registry.save_model(_pipeline(), "m1", base=str(tmp_path)))
    path.write_bytes(path.read_bytes() + b"\x00evil-pickle")

    def must_not_run(*_a, **_kw):
        raise AssertionError("joblib.load ran on an artefact that failed verification")

    monkeypatch.setattr(joblib, "load", must_not_run)
    with pytest.raises(ValueError, match="does not match its recorded SHA-256"):
        registry.load_model("m1", base=str(tmp_path))
    with pytest.raises(ValueError, match="does not match its recorded SHA-256"):
        registry._load_model_unvalidated("m1", base=str(tmp_path))


def test_artefact_outside_the_registry_dir_is_refused(tmp_path, monkeypatch):
    import joblib

    from app.lab.quant_ml import registry

    registry_dir = tmp_path / "registry"
    elsewhere = tmp_path / "elsewhere"
    registry.save_model(_pipeline(), "real", base=str(elsewhere))
    # registry/linked -> ../elsewhere/real : the file is inside the registry only by name.
    (registry_dir / "linked").parent.mkdir(parents=True)
    (registry_dir / "linked").symlink_to(elsewhere / "real", target_is_directory=True)
    monkeypatch.setattr(joblib, "load", lambda *a, **k: pytest.fail("joblib.load must not run"))

    with pytest.raises(ValueError, match="outside the registry directory"):
        registry.load_model("linked", base=str(registry_dir))


def test_legacy_artefact_without_a_digest_is_recorded_on_first_load(tmp_path, caplog):
    from app.lab.quant_ml import registry

    path = Path(registry.save_model(_pipeline(), "old", base=str(tmp_path)))
    digest_file = path.with_name("pipeline.joblib.sha256")
    digest_file.unlink()

    with caplog.at_level(logging.WARNING, logger="app.lab.quant_ml.registry"):
        assert registry.load_model("old", base=str(tmp_path)) is not None

    assert "no recorded SHA-256" in caplog.text
    assert digest_file.read_text().strip() == registry._file_sha256(path)
    # From now on a swap is caught.
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ValueError):
        registry.load_model("old", base=str(tmp_path))


def test_traversal_in_model_id_is_still_refused(tmp_path):
    from app.lab.quant_ml import registry

    for bad in ("../x", "/abs", ""):
        with pytest.raises(ValueError, match="Invalid model_id"):
            registry.load_model(bad, base=str(tmp_path))


def test_missing_artefact_is_still_file_not_found(tmp_path):
    from app.lab.quant_ml import registry

    with pytest.raises(FileNotFoundError):
        registry.load_model("nope", base=str(tmp_path))


def test_delete_model_removes_the_digest_too(tmp_path):
    from app.lab.quant_ml import registry

    path = Path(registry.save_model(_pipeline(), "m1", base=str(tmp_path)))

    assert registry.delete_model("m1", base=str(tmp_path)) is True
    assert not path.exists()
    assert not path.with_name("pipeline.joblib.sha256").exists()


# -- FinTS wire-log masking --------------------------------------------------


def test_wire_log_masks_ibans_and_balances():
    from app.foundation.dkb.utils import _mask_financial_data

    wire = "HISPA:5:1:4+J:DE89370400440532013000:COBADEFFXXX:0532013000::280:37040044'"
    assert "DE89370400440532013000" not in _mask_financial_data(wire)
    assert "DE**...3000" in _mask_financial_data(wire)
    assert _mask_financial_data("iban DE89 3704 0044 0532 0130 00 ok") == "iban DE**...3000 ok"
    assert _mask_financial_data(":60F:C240102EUR1234,56") == ":60F:C240102EUR***"
    assert _mask_financial_data("balance 1.234,56 EUR") == "balance *** EUR"
    assert _mask_financial_data("Decimal('1234.56') and 99.10 EUR") == "Decimal('***') and *** EUR"
    # Protocol framing that only looks numeric is left alone.
    assert _mask_financial_data("HNHBK:1:3+000000000123+300+abc'") == "HNHBK:1:3+000000000123+300+abc'"


def test_captured_debug_log_masks_financial_data_and_secrets():
    from app.foundation.dkb import _capture_dkb_logs

    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)

    with _capture_dkb_logs(True, "supersecretpin", "myusername") as lines:
        fints_logger.debug("HIUPD user=myusername pin=supersecretpin iban=DE89370400440532013000 saldo=C:1234,56:EUR")

    assert len(lines) == 1
    for leaked in ("supersecretpin", "myusername", "DE89370400440532013000", "1234,56"):
        assert leaked not in lines[0]
    assert "DE**...3000" in lines[0]


def test_wire_lines_do_not_reach_the_root_handlers_while_capturing(caplog):
    """Propagating the raw lines would write the unmasked IBANs to the service journal."""
    from app.foundation.dkb import _capture_dkb_logs

    fints_logger = logging.getLogger("fints")
    fints_logger.setLevel(logging.WARNING)

    with caplog.at_level(logging.DEBUG), _capture_dkb_logs(True) as lines:
        fints_logger.debug("iban=DE89370400440532013000")

    assert len(lines) == 1
    assert "DE89370400440532013000" not in caplog.text
    assert fints_logger.propagate is True  # restored afterwards


# -- SMTP ---------------------------------------------------------------------


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        pass

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    import smtplib

    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    return _FakeSMTP


def _config(**over):
    base = {
        "host": "mail.example",
        "port": 587,
        "use_tls": True,
        "from_address": "a@example.org",
        "username": "u",
        "password": "p",
        "allow_plaintext": False,
    }
    return {**base, **over}


def test_smtp_refuses_the_no_tls_branch_by_default(fake_smtp, caplog):
    from app.foundation.email import send_email

    with caplog.at_level(logging.ERROR):
        sent = send_email(_memory_db(), "x@example.org", "s", "b", config_override=_config(use_tls=False, port=25))

    assert sent is False
    assert fake_smtp.instances == []
    assert "unencrypted SMTP" in caplog.text


def test_smtp_plaintext_needs_the_explicit_setting(fake_smtp):
    from app.foundation.email import send_email

    sent = send_email(
        _memory_db(), "x@example.org", "s", "b", config_override=_config(use_tls=False, port=25, allow_plaintext=True)
    )

    assert sent is True
    assert fake_smtp.instances[0].started_tls is False


def test_smtp_starttls_and_ssl_paths_are_unaffected(fake_smtp):
    from app.foundation.email import send_email

    assert send_email(_memory_db(), "x@example.org", "s", "b", config_override=_config()) is True
    assert fake_smtp.instances[-1].started_tls is True
    assert send_email(_memory_db(), "x@example.org", "s", "b", config_override=_config(port=465, use_tls=False)) is True


def test_smtp_config_reads_the_allow_plaintext_setting():
    from app.foundation.email import get_smtp_config
    from app.foundation.settings import get_public_settings, upsert_public_settings

    db = _memory_db()
    upsert_public_settings(db, {"smtp_host": "mail.example"})
    assert get_public_settings(db)["smtp_allow_plaintext"] is False
    assert get_smtp_config(db)["allow_plaintext"] is False

    upsert_public_settings(db, {"smtp_allow_plaintext": True})
    assert get_smtp_config(db)["allow_plaintext"] is True


def test_test_email_explains_a_plaintext_refusal(fake_smtp):
    from app.foundation.email import send_test_email
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    upsert_public_settings(db, {"smtp_host": "mail.example", "smtp_port": 25, "smtp_use_tls": False})

    ok, message = send_test_email(db, "x@example.org")

    assert ok is False
    assert "encryption" in message.lower()
    assert fake_smtp.instances == []


# -- LLM API key ---------------------------------------------------------------


def test_llm_api_key_resolution_order(monkeypatch):
    from app.foundation.settings import llm_auth_headers, resolve_llm_api_key, set_secret

    db = _memory_db()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert resolve_llm_api_key(db) is None
    assert llm_auth_headers(db) == {}

    monkeypatch.setenv("LLM_API_KEY", "env-key")
    assert resolve_llm_api_key(db) == "env-key"
    assert resolve_llm_api_key(None) == "env-key"

    set_secret(db, "llm", "local")  # the setup wizard's placeholder is not a key
    assert resolve_llm_api_key(db) == "env-key"

    set_secret(db, "llm", "saved-key")
    assert resolve_llm_api_key(db) == "saved-key"
    assert llm_auth_headers(db) == {"Authorization": "Bearer saved-key"}


def test_router_hands_the_key_to_the_local_backend(monkeypatch):
    from app.foundation.llm.router import RouterConfig
    from app.foundation.settings import set_secret

    db = _memory_db()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    set_secret(db, "llm", "saved-key")

    backend, degraded = RouterConfig(db).get_backend_for_task("interactive")

    assert degraded is False
    assert backend.api_key == "saved-key"


def test_local_backend_sends_the_key_to_the_openai_client(monkeypatch):
    import openai

    from app.foundation.llm.local_llama import LocalLlamaBackend

    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)

    LocalLlamaBackend("http://llm:8080/v1", "m", "secret-key")._get_client()
    assert seen["api_key"] == "secret-key"
    LocalLlamaBackend("http://llm:8080/v1", "m")._get_client()
    assert seen["api_key"] == "local"


def test_review_call_carries_the_bearer_header_only_when_configured(monkeypatch):
    """Direct httpx callers add headers only when a key exists, so keyless setups are untouched."""
    import httpx

    from app.decision.llm_portfolio import review
    from app.foundation.settings import set_secret

    db = _memory_db()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    posts = []

    def fake_post(url, **kwargs):
        posts.append(kwargs)
        return MagicMock(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    messages = [{"role": "user", "content": "hi"}]

    review._local_llm_sync(db, messages)
    assert "headers" not in posts[-1]

    set_secret(db, "llm", "saved-key")
    review._local_llm_sync(db, messages)
    assert posts[-1]["headers"] == {"Authorization": "Bearer saved-key"}


# -- archive script ------------------------------------------------------------


def _load_archive_script():
    spec = importlib.util.spec_from_file_location("archive_reset_decision_loop", BACKEND / "scripts" / "archive_reset_decision_loop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_archive_script_passes_the_db_password_via_pgpassword_not_argv(monkeypatch):
    script = _load_archive_script()
    monkeypatch.delenv("PGPASSWORD", raising=False)

    url, env = script._pg_dump_target("postgresql+psycopg://quantfolio:p%40ss%2Fword@db.internal:5432/quantfolio?sslmode=require")

    assert "p%40ss" not in url and "p@ss" not in url and "word" not in url
    assert url == "postgresql://quantfolio@db.internal:5432/quantfolio?sslmode=require"
    assert env["PGPASSWORD"] == "p@ss/word"


def test_archive_script_without_a_password_leaves_the_environment_alone(monkeypatch):
    script = _load_archive_script()
    monkeypatch.setenv("PGPASSWORD", "from-outside")

    url, env = script._pg_dump_target("postgresql://quantfolio@db/quantfolio")

    assert url == "postgresql://quantfolio@db/quantfolio"
    assert env["PGPASSWORD"] == "from-outside"
