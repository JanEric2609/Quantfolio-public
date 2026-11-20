"""Portfolio advisor context (bounded): LLM portfolio analysis reports.

Produces on-demand portfolio analyses in two depths: a fast pulse check
(``pulse.run_pulse_check``) and a full deep analysis with pluggable LLM
backend (``deep.run_deep_analysis``, ``deep.make_llm_func``). Registered as
scheduled jobs by app.worker; served to the UI via app.interface.api.portfolio_advisor.

Public surface (consumed cross-context today):
    - pulse.run_pulse_check
    - deep.run_deep_analysis
    - deep.make_llm_func

Deliberately NO eager re-exports here: this root previously had no imports,
and importing ``deep`` at package-init time would add an
``app.services -> portfolio_advisor`` edge in the cycle ledger's package
aggregation; combined with the pre-existing ``deep -> llm.router`` edge it
would weld this context into the golden services tangle (new package SCC).
Consumers therefore import the submodules directly until the tangle dissolves.
"""
