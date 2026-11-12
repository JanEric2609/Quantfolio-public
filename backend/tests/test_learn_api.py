from app.interface.api.learn import SOURCE_ALLOW_LIST, function_source, sale_tax


def test_every_allow_listed_function_resolves_to_its_own_source() -> None:
    for name, (_module, func) in SOURCE_ALLOW_LIST.items():
        out = function_source(name=name, _user=None)  # type: ignore[arg-type]
        assert out.source.lstrip().startswith(("def ", "@")), name
        assert func in out.source
        assert out.file.startswith("app/") and out.line > 0


def test_unknown_function_is_refused_not_imported() -> None:
    import pytest
    from fastapi import HTTPException

    for bad in ("os.system", "../../etc/passwd", "app.foundation.settings.get_secret", ""):
        with pytest.raises(HTTPException) as exc:
            function_source(name=bad, _user=None)  # type: ignore[arg-type]
        assert exc.value.status_code == 404


def test_sale_tax_uses_the_tax_module_and_carries_the_flags() -> None:
    out = sale_tax(proceeds_eur=10000, cost_eur=8000, fund_class="aktien", _user=None)  # type: ignore[arg-type]
    # 2000 gain, 30 % exempt -> 1400 taxable, 25 % -> 350.00, soli 5.5 % -> 19.25
    assert out.taxable_gain_eur == 1400.0
    assert round(out.tax_eur, 2) == 369.25
    assert out.estimate is True and out.not_tax_advice is True


def test_sale_tax_with_a_loss_is_zero() -> None:
    out = sale_tax(proceeds_eur=7000, cost_eur=8000, fund_class="aktien", _user=None)  # type: ignore[arg-type]
    assert out.tax_eur == 0.0 and out.gain_eur == -1000.0
