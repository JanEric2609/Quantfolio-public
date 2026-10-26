"""Trading 212 Invest, read-only, through the public REST API (v0, beta).

Modules: ``client`` (the only place an HTTP request is built; GET-only
allow-list, rate limits, pagination; error types), ``mapping`` (pure JSON →
records), ``service`` (probe, sync, pies, tax events), ``jobs`` (the scheduled
sync). The connection status lives in the flat
``app.foundation.trading212_status`` so the Control Center reads it without
this package. Import the modules directly; this ``__init__`` stays import-free so the
package never joins an import cycle with the flat foundation modules.

See ``docs/trading212.md`` for the key setup (no Orders permission, IP
restriction).
"""
