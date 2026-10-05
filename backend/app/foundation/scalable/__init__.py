"""Scalable Capital, read-only, through the official ``sc`` CLI.

Modules: ``cli`` (the only place ``sc`` runs; error types), ``mapping`` (pure
JSON → records), ``service`` (sync, probe, reconciliation), ``jobs`` (the
scheduled sync). The connection status lives in the flat
``app.foundation.broker_status`` so the Control Center reads it without this
package. Import the modules directly; this ``__init__`` stays import-free so
the package never joins an import cycle with the flat foundation modules.

See ``docs/scalable.md`` for the server setup (dedicated Unix user, root-owned
allow-list wrapper, read-only login).
"""
