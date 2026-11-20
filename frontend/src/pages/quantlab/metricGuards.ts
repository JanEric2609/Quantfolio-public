// Annualising a short sample manufactures headline numbers: 5 daily returns
// rendered as "165% annual return, Sharpe 7.2". The Sharpe estimate's standard
// error is ~sqrt(252 / n) — about 1.0 even at a full year — so annualised
// figures are withheld below one year of aligned daily history.
export const MIN_ANNUALISED_SAMPLES = 252;
