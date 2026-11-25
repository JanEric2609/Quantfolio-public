# Tax tables for 2027: what is known on 2026-11-25 and what to apply in January

Prepared ahead of the January update in `AGENTS.md` ("Tax cockpit"). Nothing in the code was changed: every 2027 value below is either a draft or not yet published. All outputs keep `estimate: True` and `not_tax_advice: True`.

## Summary

| Table (file) | 2027 value | Status | Action |
|---|---|---|---|
| `BASISZINS_BY_YEAR` (`vorabpauschale.py`) | not published | The Bundesbank computes it on the first trading day of January and the BMF letter follows. 2026: reference date 2 Jan 2026, letter of 13 Jan 2026, 3.20 % | Add the 2027 line when the BMF letter appears (expect mid-January 2027). Until then 2027 falls back to 3.20 %, flagged unknown |
| `GRUNDFREIBETRAG_BY_YEAR` (`gain_harvest.py`) and `Tariff.grundfreibetrag` | **12,564 EUR** (2028: 12,900) | Cabinet draft of the Einkommensteuerreformgesetz 2027 (2 Sep 2026). Parliament and Bundesrat steps not confirmed | Add 2027 only after promulgation |
| `TARIFF_BY_YEAR` (`income_tax.py`) | not available | The draft restructures the tariff; no coefficients were in any source read | Do not add. The `Tariff` dataclass will not fit (see below) |
| `FAMILIENVERSICHERUNG_MONTHLY_LIMIT_BY_YEAR` (`gain_harvest.py`) | probably **590 EUR** (4,130 / 7) | Bezugsgroesse 4,130 EUR came from a summary of the BMAS release and is **unverified**. Regulation: Bundesrat consent obtained (Bundesregierung, 20 Nov 2026) | Confirm the Bezugsgroesse in the regulation text, then add |

## Details and sources

**Grundfreibetrag.** "Der Grundfreibetrag steigt auf 12.564 Euro (2027) und 12.900 Euro (2028)." The PwC summary of the ministry draft says 12,348 up by 216 to 12,564 EUR and 2028 up by 336 to 12,900 EUR.
- Bundesregierung Q&A, 2 Sep 2026: https://www.bundesregierung.de/breg-de/service/fragen-und-anworten/einkommensteuerreform-2027-2451192
- PwC blog, 19 Aug 2026 (on the 18 Aug draft): https://blogs.pwc.de/de/steuern-und-recht/article/256501/bmf-entwurf-eines-einkommensteuerreformgesetzes-2027
- Bundestag Drucksache 21/8530, 13 Oct 2026 (still calls it the Regierungsentwurf): https://dserver.bundestag.de/btd/21/085/2108530.pdf

**Tariff (section 32a EStG).** The draft changes the tariff for the assessment year 2027:
- the second progression zone is flattened up to a taxable income of 70,600 EUR (the code's 2026 zone 2 ends at 69,878);
- 42 % from 70,601 to 249,999 EUR;
- the 45 % threshold falls from 277,826 to 250,000 EUR;
- a new 47 % rate from a taxable income of 280,000 EUR.

None of the pages gave the formulas or coefficients. `income_tax.py` models four zones and a 45 % top rate through the `Tariff` dataclass (`zone2_end`, `zone3_end`, `zone5_start`, `c4`, `c5`). A 47 % band needs a new zone and a new constant, so January means a code change as well as new numbers. Take the coefficients from the promulgated section 32a text (Bundesgesetzblatt) or the BMF Programmablaufplan for 2027, and check the result with a hand-computed case, as the tests for 2024 to 2026 do.

**Capital-income rules.** The three sources do not mention the Sparer-Pauschbetrag (1,000 / 2,000 EUR), the 25 % flat tax, the Solidaritaetszuschlag, the Teilfreistellung or the Vorabpauschale. The ministry draft is said to leave them unchanged; the cabinet text itself was not checked.

**Familienversicherung limit.** 2026 uses 565 EUR, one seventh of the 2026 Bezugsgroesse of 3,955 EUR. For 2027 the Bundesregierung page of 20 Nov 2026 confirms the Bundesrat consented to the Sozialversicherungsrechengroessen-Verordnung 2027 and lists the Beitragsbemessungsgrenze of the statutory health insurance at 6,375 EUR a month and the Versicherungspflichtgrenze at 84,150 EUR a year. The Bezugsgroesse itself (4,130 EUR a month, 49,560 a year) is only in a model summary of the BMAS release. 4,130 / 7 = 590.
- https://www.bundesregierung.de/breg-de/aktuelles/beitragsbemessungsgrenzen-2027-2454638
- https://www.deutsche-rentenversicherung.de/DRV/DE/Ueber-uns-und-Presse/Presse/Meldungen/2026/261008-bundeskabinett-sv-rechengroessen-2027.html
- https://www.bmas.de/DE/Service/Presse/Pressemitteilungen/2026/bundeskabinett-beschliesst-sozialversicherungsrechengroessen-2027.html
- The cabinet date is stated as 7 Oct (DRV, BMAS) and 12 Oct (Bundesregierung); it does not affect the value.

**Basiszins.** 2026: the Bundesbank computed 3.20 % for 2 Jan 2026; BMF letter of 13 Jan 2026, IV C 1 - S 1980/00230/012/001 (Haufe, 16 Jan 2026): https://www.haufe.de/steuern/finanzverwaltung/18-abs-4-invstg-basiszins-zur-berechnung-der-vorabpauschale_164_481896.html. The 2026 Vorabpauschale is deemed received on 4 Jan 2027. The otto-schmidt page could not be fetched, and the dates of earlier letters were not confirmed.

## January checklist (when the values are final)

1. Basiszins 2027 into `BASISZINS_BY_YEAR` with the letter date in the comment.
2. After promulgation: Grundfreibetrag 2027 into `GRUNDFREIBETRAG_BY_YEAR`, the new section 32a zones into `income_tax.py` (extend `Tariff` for the 47 % band), a hand-computed test at each zone edge.
3. Familienversicherung limit for 2027 once the Bezugsgroesse is confirmed in the regulation.
4. Run `tests/test_tax_cockpit.py`, `tests/test_gain_harvest*.py` and the tariff tests; assert `estimate` and `not_tax_advice`.

## Limits of this research

Eleven pages were opened and about thirty search results were listed; the request for 100+ sources was not reached for this item, because the decisive documents (the final law text, the BMF letter) do not exist yet. Re-run the search once the Bundestag has passed the reform and in the second week of January.
