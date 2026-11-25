# European insider dealings (managers' transactions)

Why: insider purchases are one of the few free signals with published evidence in Europe
(Afzali & Martikainen 2021, 16 countries; Zingg et al. 2007, Switzerland; Hvide & Nielsen,
Norway).

## Sources, and why only Sweden so far

| Country | Source | Decision |
|---|---|---|
| Sweden | Finansinspektionen PDMR register (`marknadssok.fi.se`) | **In use.** FI's open-data page: "may be used free of charge ... freely processed and re-used without special permission", state the source (FI) and the date. |
| Germany | BaFin portal (`portal.mvp.bafin.de`) | Not read: the portal disallows automated reads. Ask BaFin for permission first. |
| Germany and others | EQS issuer notices | Not read: EQS's legal notice forbids text-and-data-mining and storing its content without written consent. |
| Netherlands | AFM CSV/XML export | Possible later; CSV content not yet read, copyright reserved. |

## What runs

`foundation/data_engineering/pdmr_se.py`, job `pdmr_se_refresh` (04:45 UTC): pages through the
register newest first (one request every 2 s, at most 400 pages a night, anonymous
User-Agent), stores each notification in `pdmr_transactions` (`side` is `buy` for
"Acquisition", `sell` for "Disposal", `other` for allotments, gifts and the like). The first
nights walk back one year from a saved page (`pdmr_se_resume_page`); afterwards a night
stops at the first page it already stored. `recent_buys(db, isin, days=90)` is the read side.
Nothing uses it for a decision yet: the next step is a pre-registered forward-lab signal.

## First run on the server (not tested against the live site)

The table layout (headers, `DD/MM/YYYY` dates, comma thousands, period decimals) was read
from the live page on 2026-11-25, but the raw HTML was not available to the author's
sandbox. Run once and read the result:

```bash
cd backend && .venv/bin/python -c "
from app.foundation.core.db import SessionLocal
from app.foundation.data_engineering.pdmr_se import refresh_pdmr_se
with SessionLocal() as db: print(refresh_pdmr_se(db, max_pages=3))"
```

`pages` and `new` above zero and `failed_pages == 0` means the parser matches. A non-zero
`failed_pages` means the layout differs (or the host refused): send the page HTML.
Credit when showing the data: "Source: Finansinspektionen, <date>".
