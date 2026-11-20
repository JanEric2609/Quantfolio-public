import { EvidencePage } from "../EvidencePage";

/**
 * How many things were tried to find what the system shows (the trial ledger and
 * deflated Sharpe): the multiple-testing side of "can I trust it?". Reuses the
 * existing Evidence page, one heading level down: the Trust layout owns the h1.
 */
export function EvidenceTab() {
  return <EvidencePage headingLevel={2} />;
}
