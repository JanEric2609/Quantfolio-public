"""One-off cleanup of pytest pollution in the dev DB.

Run: python scripts/cleanup_test_pollution.py [--apply]

Dry-run by default. Pass --apply to commit the deletions.
"""
from __future__ import annotations

import sys

from app.foundation.core.db import SessionLocal
from app.foundation.models.entities import (
    ActivityLedgerEntry,
    ConnectedAccount,
    DkbAccount,
    DkbPosition,
    DkbSyncLog,
    DkbTransaction,
    Holding,
    PortfolioSnapshot,
)

FAKE_IBANS = [
    "DE12345678901234567890",
    "DE11111111111111111111",
    "DE22222222222222222222",
]


def _fmt_count(model_name: str, qty: int) -> str:
    return f"  {model_name}: {qty} rows"


def main(apply: bool) -> None:
    db = SessionLocal()
    try:
        # 1. Resolve fake account IDs
        fake_accounts = (
            db.query(DkbAccount).filter(DkbAccount.iban.in_(FAKE_IBANS)).all()
        )
        fake_account_ids = [a.id for a in fake_accounts]
        print(f"Fake DkbAccount rows: {len(fake_account_ids)}")

        if not fake_account_ids:
            print("No fake accounts found — nothing to clean.")
            return

        # 2. DkbSyncLog referencing fake test modules
        sync_log_q = db.query(DkbSyncLog).filter(
            DkbSyncLog.message.like("%FakeClient%")
            | DkbSyncLog.message.like("%tests/test_dkb%")
        )
        sync_log_count = sync_log_q.count()
        print(_fmt_count("DkbSyncLog (test artefacts)", sync_log_count))

        # 3. DkbTransaction referencing fake accounts or "Test" references
        tx_q = db.query(DkbTransaction).filter(
            DkbTransaction.account_id.in_(fake_account_ids)
            | DkbTransaction.reference.like("Test%")
        )
        tx_count = tx_q.count()
        print(_fmt_count("DkbTransaction", tx_count))

        # 4. DkbPosition for fake accounts
        pos_q = db.query(DkbPosition).filter(
            DkbPosition.account_id.in_(fake_account_ids)
        )
        pos_count = pos_q.count()
        print(_fmt_count("DkbPosition", pos_count))

        # 5. DkbAccount delete (FK cascade handles positions/transactions)
        # Need to delete positions/transactions explicitly first for control
        pos_q.delete(synchronize_session=False)
        tx_q.delete(synchronize_session=False)

        # Sync logs not cascaded from DkbAccount
        sync_log_q.delete(synchronize_session=False)

        # Delete the accounts (cascades to remaining positions/transactions)
        db.query(DkbAccount).filter(
            DkbAccount.id.in_(fake_account_ids)
        ).delete(synchronize_session=False)

        # 6. ActivityLedgerEntry whose dedupe_hash no longer matches remaining
        #    DkbTransaction rows. We match by checking if the activity's
        #    external_id references a now-deleted account.
        #    Simplest: delete activities where source='dkb' and connected_account
        #    was for a fake account.
        fake_connected = (
            db.query(ConnectedAccount)
            .filter(
                ConnectedAccount.source == "dkb",
                ConnectedAccount.external_id.in_(FAKE_IBANS),
            )
            .all()
        )
        fake_connected_ids = [c.id for c in fake_connected]
        act_q = db.query(ActivityLedgerEntry).filter(
            ActivityLedgerEntry.source == "dkb",
            ActivityLedgerEntry.connected_account_id.in_(fake_connected_ids),
        )
        act_count = act_q.count()
        print(_fmt_count("ActivityLedgerEntry (dkb, fake connected)", act_count))
        act_q.delete(synchronize_session=False)

        # 7. ConnectedAccount for dkb with fake IBANs
        ca_q = db.query(ConnectedAccount).filter(
            ConnectedAccount.source == "dkb",
            ConnectedAccount.external_id.in_(FAKE_IBANS),
        )
        ca_count = ca_q.count()
        print(_fmt_count("ConnectedAccount (dkb, fake)", ca_count))
        ca_q.delete(synchronize_session=False)

        # 8. PortfolioSnapshot where source='dkb_mirror'
        snap_q = db.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.source == "dkb_mirror"
        )
        snap_count = snap_q.count()
        print(_fmt_count("PortfolioSnapshot (dkb_mirror)", snap_count))
        snap_q.delete(synchronize_session=False)

        # 9. Holding where source='dkb_sync'
        hold_q = db.query(Holding).filter(Holding.source == "dkb_sync")
        hold_count = hold_q.count()
        print(_fmt_count("Holding (dkb_sync)", hold_count))
        hold_q.delete(synchronize_session=False)

        total = (
            sync_log_count
            + tx_count
            + pos_count
            + len(fake_account_ids)
            + act_count
            + ca_count
            + snap_count
            + hold_count
        )
        print(f"\nTotal rows to delete: {total}")

        if apply:
            db.commit()
            print("COMMITTED — fake data deleted.")
        else:
            db.rollback()
            print("DRY RUN — rerun with --apply to commit.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
