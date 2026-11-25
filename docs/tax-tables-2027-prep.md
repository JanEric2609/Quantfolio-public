# Tax tables for 2027: what is known on 2026-11-25 and what to apply in January

Prepared ahead of the January update in `AGENTS.md` ("Tax cockpit"). Nothing in the code was changed: every 2027 value below is either a draft or not yet published. All outputs keep `estimate: True` and `not_tax_advice: True`.

## Summary

| Table (file) | 2027 value | Status | Action |
|---|---|---|---|
| `BASISZINS_BY_YEAR` (`vorabpauschale.py`) | not published | The Bundesbank computes it on the first trading day of January and the BMF letter follows. 2026: reference date 2 Jan 2026, letter of 13 Jan 2026, 3.20 % | Add the 2027 line when the BMF letter appears (expect mid-January 2027). Until then 2027 falls back to 3.20 %, flagged unknown |
| `GRUNDFREIBETRAG_BY_YEAR` (`gain_harvest.py`) and `Tariff.grundfreibetrag` | **12,564 EUR** (2028: 12,900) | Cabinet draft of the Einkommensteuerreformgesetz 2027 (2 Sep 2026). Parliament and Bundesrat steps not confirmed | Add 2027 only after promulgation |
| `TARIFF_BY_YEAR` (`income_tax.py`) | coefficients in the Regierungsentwurf (see below) | Draft only (BR-Drs 507/26, 4 Sep 2026) | Stage the numbers now, add only after promulgation. The `Tariff` dataclass will not fit (see below) |
| `FAMILIENVERSICHERUNG_MONTHLY_LIMIT_BY_YEAR` (`gain_harvest.py`) | **590 EUR** (4,130 / 7) | Bezugsgroesse 4,130 EUR a month (49,560 a year) is in section 1 of BR-Drs 576/26. Bundesrat consent obtained (Bundesregierung, 20 Nov 2026); the BGBl. publication was not checked | Add after promulgation in the BGBl. |

## Details and sources

**Grundfreibetrag.** "Der Grundfreibetrag steigt auf 12.564 Euro (2027) und 12.900 Euro (2028)." The PwC summary of the ministry draft says 12,348 up by 216 to 12,564 EUR and 2028 up by 336 to 12,900 EUR.
- Bundesregierung Q&A, 2 Sep 2026: https://www.bundesregierung.de/breg-de/service/fragen-und-anworten/einkommensteuerreform-2027-2451192
- PwC blog, 19 Aug 2026 (on the 18 Aug draft): https://blogs.pwc.de/de/steuern-und-recht/article/256501/bmf-entwurf-eines-einkommensteuerreformgesetzes-2027
- Bundestag Drucksache 21/8530, 13 Oct 2026 (still calls it the Regierungsentwurf): https://dserver.bundestag.de/btd/21/085/2108530.pdf

**Tariff (section 32a EStG).** The Regierungsentwurf (BR-Drs 507/26, Article 1 no. 4, Article 2 no. 2) gives the formulas. Only the first 60 % of the PDF, which includes the whole operative text, was read verbatim. Taxable income in EUR; y is one ten-thousandth of the part above the Grundfreibetrag, z one ten-thousandth of the part above 17,799, x the rounded income; the tax is rounded down.

| Zone (2027) | Formula |
|---|---|
| up to 12,564 | 0 |
| 12,565 to 17,799 | (952.24 y + 1,400) y |
| 17,800 to 70,600 | (170.74 z + 2,397) z + 993.86 |
| 70,601 to 249,999 | 0.42 x - 11,241.73 |
| 250,000 to 279,999 | 0.45 x - 18,741.70 |
| 280,000 and above | 0.47 x - 24,341.68 |

For 2028 the draft has a Grundfreibetrag of 12,900, 1,017.55 in zone 2, 930.08 in zone 3, and constants 11,305.52, 18,805.49 and 24,405.47 in zones 4 to 6. A continuity check at 70,600 EUR gives 18,410.4 (zone 3) and 18,410.27 (zone 4); both floor to 18,410.

Mapping to `income_tax.py`: the second field of `Tariff` (`zone2_end`) is the end of the first progression zone, 17,799, which does not change. The end of the second progression zone, 69,878 in 2026, sits in `zone3_end`; it becomes 70,600. The 2027 values map to `a2` = 952.24, `a3` = 170.74, `c3` = 993.86, `c4` = 11,241.73, and the 45 % band now starts at 250,000 (`zone5_start`, `c5` = 18,741.70). The 47 % band needs a new field pair (start 280,000, constant 24,341.68) and a branch in `income_tax()`. A hand-computed test belongs at each zone edge.

Sources: https://dserver.bundestag.de/brd/2026/0507-26.pdf (draft text), the Bundesregierung Q&A and the PwC summary above, and the Bundestag answer 21/8530, which refers to a shift of selected tariff corner values.

**Capital-income rules.** The three sources do not mention the Sparer-Pauschbetrag (1,000 / 2,000 EUR), the 25 % flat tax, the Solidaritaetszuschlag, the Teilfreistellung or the Vorabpauschale. The ministry draft is said to leave them unchanged; the cabinet text itself was not checked.

**Familienversicherung limit.** 2026 uses 565 EUR, one seventh of the 2026 Bezugsgroesse of 3,955 EUR. For 2027 the Bundesregierung page of 20 Nov 2026 confirms the Bundesrat consented to the Sozialversicherungsrechengroessen-Verordnung 2027 and lists the Beitragsbemessungsgrenze of the statutory health insurance at 6,375 EUR a month and the Versicherungspflichtgrenze at 84,150 EUR a year. The Bezugsgroesse itself is verbatim in section 1 of the regulation (BR-Drs 576/26, https://dserver.bundestag.de/brd/2026/0576-26.pdf): 49,560 EUR a year, 4,130 EUR a month. 4,130 / 7 = 590.
- https://www.bundesregierung.de/breg-de/aktuelles/beitragsbemessungsgrenzen-2027-2454638
- https://www.deutsche-rentenversicherung.de/DRV/DE/Ueber-uns-und-Presse/Presse/Meldungen/2026/261008-bundeskabinett-sv-rechengroessen-2027.html
- https://www.bmas.de/DE/Service/Presse/Pressemitteilungen/2026/bundeskabinett-beschliesst-sozialversicherungsrechengroessen-2027.html
- The cabinet date is stated as 7 Oct (DRV, BMAS) and 12 Oct (Bundesregierung); it does not affect the value.

**Basiszins.** 2026: the Bundesbank computed 3.20 % for 2 Jan 2026; BMF letter of 13 Jan 2026, IV C 1 - S 1980/00230/012/001 (Haufe, 16 Jan 2026): https://www.haufe.de/steuern/finanzverwaltung/18-abs-4-invstg-basiszins-zur-berechnung-der-vorabpauschale_164_481896.html. The 2026 Vorabpauschale is deemed received on 4 Jan 2027. The otto-schmidt page could not be fetched, and the dates of earlier letters were not confirmed.

## January checklist (when the values are final)

1. Basiszins 2027 into `BASISZINS_BY_YEAR` with the letter date in the comment.
2. After promulgation: Grundfreibetrag 2027 into `GRUNDFREIBETRAG_BY_YEAR`, the section 32a zones from the promulgated text (compare with the table above) into `income_tax.py` with the 47 % band, a hand-computed test at each zone edge.
3. Familienversicherung limit 590 EUR for 2027 once the regulation is in the BGBl.
4. Run `tests/test_tax_cockpit.py`, `tests/test_gain_harvest*.py` and the tariff tests; assert `estimate` and `not_tax_advice`.

## Limits of this research

Fifteen pages were opened and about thirty search results were listed; the request for 100+ sources was not reached for this item, because the decisive documents (the final law text, the BMF letter) do not exist yet. Re-run the search once the Bundestag has passed the reform and in the second week of January.
