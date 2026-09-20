"use client";

import { AlertTriangle, Bot, Check, Clock3, History, Play, Radar, RefreshCw, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { FormEvent, useEffect, useRef, useState } from "react";

import { deskFetch } from "@/lib/api";

type OptionDiagnostics = { total_contracts: number; requested_type_contracts: number; strict_eligible_contracts: number; selected_contracts: number; rejection_counts: Record<string, number> };
type Analysis = { opportunity_id: string; scan_run_id: string | null; symbol: string; disposition: string; source: string; observed_at: string; expires_at: string; signal: Record<string, unknown>; candidate: Record<string, unknown> | null; order_intent: Record<string, unknown> | null; option_diagnostics: OptionDiagnostics | null; reason_codes: string[] };
type Workspace = { scanner_enabled: boolean; watchlist_count: number; status: string };
type ScanFailure = { symbol: string; code: "REAL_DATA_UNAVAILABLE" };
type ScanResult = { scan_run_id: string; trigger: string; started_at: string; completed_at: string; attempted: number; results: Analysis[]; failures: ScanFailure[] };
type ScanRun = { scan_run_id: string; trigger: string; source: string; started_at: string; completed_at: string | null; attempted: number; completed: number; failed: number };
type MarketClock = { is_open: boolean; timestamp: string; next_open: string; next_close: string; timezone: string; regular_session: string; source: string };
type WatchlistRecommendation = { symbol: string; action: "KEEP" | "DROP" | "WATCH"; rank: number; rationale: string; option_assessment: "EXECUTION_ELIGIBLE" | "REVIEW_ONLY" | "NOT_ELIGIBLE" | "INSUFFICIENT_DATA"; option_reason: string; risks: string[]; confidence: number; citations: { source_id: string; claim: string }[] };
type WatchlistResearch = { provider: string; model: string; scan_run_id: string; scan_completed_at: string | null; researched_at: string; universe: string[]; report: { summary: string; limitations: string[]; recommendations: WatchlistRecommendation[]; as_of: string } };

const easternTime = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short" });
const easternDateTime = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
const DEFAULT_WATCHLIST = ["AAPL", "AMZN", "META", "MSFT", "NVDA", "SPY", "TSLA"];

function formatCountdown(target: string | null, current: Date): string | null {
  if (!target) return null;
  const diffMs = new Date(target).getTime() - current.getTime();
  if (diffMs <= 0) return "in moments";
  const diffMins = Math.floor(diffMs / 60000);
  const hours = Math.floor(diffMins / 60);
  const mins = diffMins % 60;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

export function MarketScanner() {
  const [symbols, setSymbols] = useState<string[]>([]);
  const [watchlistSelection, setWatchlistSelection] = useState<string[]>([]);
  const [watchlistLoaded, setWatchlistLoaded] = useState(false);
  const [entry, setEntry] = useState("");
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [results, setResults] = useState<Analysis[]>([]);
  const [runs, setRuns] = useState<ScanRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [clock, setClock] = useState<MarketClock | null>(null);
  const [now, setNow] = useState<Date | null>(null);
  const [busy, setBusy] = useState(false);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [watchlistBusy, setWatchlistBusy] = useState(false);
  const [researchBusy, setResearchBusy] = useState(false);
  const [research, setResearch] = useState<WatchlistResearch | null>(null);
  const [researchSelection, setResearchSelection] = useState<string[]>([]);
  const [message, setMessage] = useState("");
  const watchlistMutationEpoch = useRef(0);


  async function selectRun(run: ScanRun) {
    setHistoryBusy(true);
    try {
      setResults(await deskFetch<Analysis[]>(`/desk/scanner/runs/${run.scan_run_id}`));
      setSelectedRunId(run.scan_run_id);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Scan history unavailable");
    } finally {
      setHistoryBusy(false);
    }
  }

  async function refreshStoredResults() {
    const nextRuns = await deskFetch<ScanRun[]>("/desk/scanner/runs");
    setRuns(nextRuns);
    if (nextRuns[0]) await selectRun(nextRuns[0]);
    else {
      setResults([]);
      setSelectedRunId(null);
    }
  }

  async function reloadWatchlistAfterFailure(expectedEpoch: number) {
    if (watchlistMutationEpoch.current !== expectedEpoch) return;
    try {
      const saved = await deskFetch<string[]>("/desk/watchlist");
      if (watchlistMutationEpoch.current !== expectedEpoch) return;
      setSymbols(saved);
      setWatchlistSelection(saved);
      setWatchlistLoaded(true);
    } catch {
      // Preserve the original mutation error when the recovery read also fails.
    }
  }

  useEffect(() => {
    let active = true;
    async function initialize() {
      const initialWatchlistEpoch = watchlistMutationEpoch.current;
      const watchlistRequest = deskFetch<string[]>("/desk/watchlist");
      const ancillaryRequests = Promise.all([
        deskFetch<Workspace>("/desk/workspace"),
        deskFetch<ScanRun[]>("/desk/scanner/runs"),
        deskFetch<MarketClock>("/desk/market-clock"),
      ]);
      try {
        const watchlist = await watchlistRequest;
        if (!active) return;
        if (watchlistMutationEpoch.current === initialWatchlistEpoch) {
          setSymbols(watchlist);
          setWatchlistSelection(watchlist);
          setWatchlistLoaded(true);
        }
      } catch (error) {
        if (active) setMessage(error instanceof Error ? error.message : "Watchlist unavailable");
      }
      try {
        const [space, scanRuns, marketClock] = await ancillaryRequests;
        if (!active) return;
        setWorkspace(space);
        setRuns(scanRuns);
        setClock(marketClock);
        if (scanRuns[0]) {
          const latest = await deskFetch<Analysis[]>(`/desk/scanner/runs/${scanRuns[0].scan_run_id}`);
          if (active) {
            setResults(latest);
            setSelectedRunId(scanRuns[0].scan_run_id);
          }
        }
      } catch (error) {
        if (active) setMessage(error instanceof Error ? error.message : "Scanner unavailable");
      }
    }
    void initialize();
    const timeTicker = window.setInterval(() => setNow(new Date()), 1000);
    const clockTicker = window.setInterval(() => {
      void deskFetch<MarketClock>("/desk/market-clock").then(setClock).catch(() => undefined);
    }, 60_000);
    return () => {
      active = false;
      window.clearInterval(timeTicker);
      window.clearInterval(clockTicker);
    };
  }, []);

  async function addSymbols(event: FormEvent) {
    event.preventDefault();
    if (watchlistBusy || researchBusy) return;
    const additions = Array.from(new Set(entry.split(/[\s,]+/).map((value) => value.trim().toUpperCase()).filter(Boolean)));
    if (additions.length === 0) {
      setMessage("Enter at least one symbol to add.");
      return;
    }
    const existingSymbols = new Set(symbols);
    const pendingSelection = new Set(watchlistSelection);
    const pendingRemovals = new Set(symbols.filter((symbol) => !pendingSelection.has(symbol)));
    watchlistMutationEpoch.current += 1;
    const mutationEpoch = watchlistMutationEpoch.current;
    setWatchlistBusy(true);
    try {
      await deskFetch<string[]>("/desk/watchlist", { method: "POST", body: JSON.stringify({ symbols: additions, source: "operator" }) });
      const saved = await deskFetch<string[]>("/desk/watchlist");
      setSymbols(saved);
      setWatchlistSelection(saved.filter((symbol) => !pendingRemovals.has(symbol)));
      setWatchlistLoaded(true);
      setEntry("");
      const addedCount = additions.filter((symbol) => !existingSymbols.has(symbol)).length;
      setMessage(`${addedCount} new symbol${addedCount === 1 ? "" : "s"} added. Existing symbols were kept.`);
    } catch (error) {
      await reloadWatchlistAfterFailure(mutationEpoch);
      setMessage(error instanceof Error ? error.message : "Unable to add symbols");
    } finally {
      setWatchlistBusy(false);
    }
  }

  async function saveWatchlistChanges() {
    if (watchlistBusy || researchBusy) return;
    const pendingRemovals = symbols.filter((symbol) => !watchlistSelection.includes(symbol));
    if (pendingRemovals.length === 0) return;
    watchlistMutationEpoch.current += 1;
    const mutationEpoch = watchlistMutationEpoch.current;
    setWatchlistBusy(true);
    try {
      const saved = await deskFetch<string[]>("/desk/watchlist/remove", { method: "POST", body: JSON.stringify({ symbols: pendingRemovals }) });
      setSymbols(saved);
      setWatchlistSelection(saved);
      setWatchlistLoaded(true);
      setMessage("Watchlist changes saved.");
    } catch (error) {
      await reloadWatchlistAfterFailure(mutationEpoch);
      setMessage(error instanceof Error ? error.message : "Watchlist changes failed");
    } finally {
      setWatchlistBusy(false);
    }
  }

  async function restoreDefaultWatchlist() {
    if (watchlistBusy || researchBusy) return;
    watchlistMutationEpoch.current += 1;
    const mutationEpoch = watchlistMutationEpoch.current;
    setWatchlistBusy(true);
    try {
      const saved = await deskFetch<string[]>("/desk/watchlist", { method: "POST", body: JSON.stringify({ symbols: DEFAULT_WATCHLIST, source: "default_restore" }) });
      setSymbols(saved);
      setWatchlistSelection(saved);
      setWatchlistLoaded(true);
      setMessage("Default watchlist restored. Review it, then run Scan now.");
    } catch (error) {
      await reloadWatchlistAfterFailure(mutationEpoch);
      setMessage(error instanceof Error ? error.message : "Unable to restore the default watchlist");
    } finally {
      setWatchlistBusy(false);
    }
  }

  async function researchWatchlist() {
    setResearchBusy(true);
    setMessage("Reviewing the latest real-data scan with the configured AI provider…");
    try {
      const result = await deskFetch<WatchlistResearch>("/desk/watchlist/research", { method: "POST" });
      setResearch(result);
      setResearchSelection(result.report.recommendations.filter((item) => item.action === "KEEP").map((item) => item.symbol));
      setMessage("Research complete. Review the advisory notes, then choose what to save.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Watchlist research unavailable");
    } finally {
      setResearchBusy(false);
    }
  }

  async function saveResearchSelection() {
    if (researchSelection.length === 0) {
      setMessage("Select at least one symbol before saving the researched watchlist.");
      return;
    }
    if (watchlistBusy || researchBusy) return;
    const additions = researchSelection.filter((symbol) => !symbols.includes(symbol));
    if (additions.length === 0) {
      setMessage("All selected recommendations are already on the watchlist.");
      return;
    }
    const pendingSelection = new Set(watchlistSelection);
    const pendingRemovals = new Set(symbols.filter((symbol) => !pendingSelection.has(symbol)));
    watchlistMutationEpoch.current += 1;
    const mutationEpoch = watchlistMutationEpoch.current;
    setWatchlistBusy(true);
    try {
      await deskFetch<string[]>("/desk/watchlist", { method: "POST", body: JSON.stringify({ symbols: additions, source: "ai_research" }) });
      const saved = await deskFetch<string[]>("/desk/watchlist");
      setSymbols(saved);
      setWatchlistSelection(saved.filter((symbol) => !pendingRemovals.has(symbol)));
      setWatchlistLoaded(true);
      setMessage(`${additions.length} researched symbol${additions.length === 1 ? "" : "s"} added. Existing symbols were kept.`);
    } catch (error) {
      await reloadWatchlistAfterFailure(mutationEpoch);
      setMessage(error instanceof Error ? error.message : "Watchlist save failed");
    } finally {
      setWatchlistBusy(false);
    }
  }

  async function scan() {
    if (busy || watchlistBusy || researchBusy) return;
    setBusy(true);
    setMessage("Scanning real Alpaca market, news, contract, quote, and Greek sources…");
    try {
      const scanResult = await deskFetch<ScanResult>("/desk/scanner/scan?scan_mode=PRE_SCAN", { method: "POST" });
      const run: ScanRun = { scan_run_id: scanResult.scan_run_id, trigger: scanResult.trigger, source: "ALPACA_REAL", started_at: scanResult.started_at, completed_at: scanResult.completed_at, attempted: scanResult.attempted, completed: scanResult.results.length, failed: scanResult.failures.length };
      setResults(scanResult.results);
      setSelectedRunId(run.scan_run_id);
      setRuns((previous) => [run, ...previous.filter((item) => item.scan_run_id !== run.scan_run_id)].slice(0, 10));
      setMessage(run.failed === 0 ? `Scan completed for all ${run.completed} symbols. Synthetic fallback was not used.` : `Scan processed ${run.completed} of ${run.attempted} symbols; ${run.failed} were unavailable from real-data sources. Synthetic fallback was not used.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Scan unavailable");
    } finally {
      setBusy(false);
    }
  }

  async function toggle() {
    try {
      setWorkspace(await deskFetch<Workspace>("/desk/scanner", { method: "PUT", body: JSON.stringify({ enabled: !workspace?.scanner_enabled }) }));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Scanner update failed");
    }
  }

  const selectedRun = runs.find((run) => run.scan_run_id === selectedRunId) ?? null;
  const latestSelected = Boolean(selectedRun && runs[0]?.scan_run_id === selectedRun.scan_run_id);
  const nextEvent = clock ? (clock.is_open ? clock.next_close : clock.next_open) : null;
  const countdown = now ? formatCountdown(nextEvent, now) : null;
  const dispositionCounts = results.reduce<Record<string, number>>((counts, result) => {
    counts[result.disposition] = (counts[result.disposition] ?? 0) + 1;
    return counts;
  }, {});
  const scoreFor = (result: Analysis) => {
    const value = Number(result.signal.score);
    return Number.isFinite(value) ? `${value.toFixed(2)} / 100` : "—";
  };
  const reasonLabels: Record<string, string> = { score_below_threshold: "score below threshold", weak_catalyst_confidence: "weak catalyst confidence", price_action_not_confirming: "price action not confirming", no_eligible_option_chain: "no eligible option chain", stale_quote: "stale quote", greeks_unavailable: "missing Greeks", open_interest_too_low: "low open interest" };
  const reasonCounts = results.reduce<Record<string, number>>((counts, result) => {
    for (const reason of result.reason_codes) counts[reason] = (counts[reason] ?? 0) + 1;
    for (const [reason, count] of Object.entries(result.option_diagnostics?.rejection_counts ?? {})) counts[reason] = (counts[reason] ?? 0) + count;
    return counts;
  }, {});
  const summary = { tracked: symbols.length, processed: results.length, candidates: results.filter((result) => result.disposition === "PRE_SCAN_CANDIDATE").length, noTrade: results.filter((result) => result.disposition === "NO_TRADE").length, unavailable: results.filter((result) => result.disposition === "UNAVAILABLE").length };

  return <>
    <section className={`market-clock-strip ${clock?.is_open ? "open" : "closed"}`} aria-live="polite">
      <div className="market-clock-state">
        <span className="mode-dot"/>
        <div>
          <small>US MARKET STATUS</small>
          <strong>{clock ? (clock.is_open ? "Regular session open" : "Regular session closed") : "Checking Alpaca clock…"}</strong>
          {countdown ? <span className="countdown-pill">{clock?.is_open ? `Closes in ${countdown}` : `Opens in ${countdown}`}</span> : null}
        </div>
      </div>
      <div><small>REGULAR SESSION</small><strong>{clock?.regular_session ?? "9:30 AM-4:00 PM ET"}</strong></div>
      <div><small>EASTERN TIME</small><strong>{now ? easternTime.format(now) : "—"}</strong></div>
      <div className="market-clock-guidance">{clock?.is_open ? <Clock3/> : <AlertTriangle/>}<span><strong>{clock?.is_open ? "Freshness checks active" : "After-hours notice"}</strong><small>{clock?.is_open ? `Next close ${nextEvent ? easternDateTime.format(new Date(nextEvent)) : ""}` : `Option quotes may be stale. Next open ${nextEvent ? easternDateTime.format(new Date(nextEvent)) : ""}.`}</small></span></div>
    </section>

    {clock && !clock.is_open ? (
      <div className="closed-market-banner" role="status">
        <AlertTriangle />
        <div>
          <strong>Regular Market Session is Currently Closed</strong>
          <p>
            The scan now keeps plausible option structures for review even when quotes are stale. Those candidates are labeled execution-pending; fresh quotes and every risk gate are required again at the next U.S. session before any paper order.
          </p>
        </div>
      </div>
    ) : null}

    {watchlistLoaded && symbols.length === 0 ? <div className="closed-market-banner" role="status"><AlertTriangle/><div><strong>Watchlist is empty</strong><p>Add symbols above or restore the default paper watchlist before running a scan.</p><button type="button" onClick={() => void restoreDefaultWatchlist()} disabled={busy || watchlistBusy || researchBusy}>Restore default watchlist</button></div></div> : null}

    <div className="scanner-sections">
    <p className="form-message">Primary scan action: discover next-session candidates (explicit PRE_SCAN mode).</p>
    <div className="scanner-toolbar"><div><small>{latestSelected ? "LATEST SCAN RESULTS" : "HISTORICAL SCAN RESULTS"}</small><strong>{selectedRun ? `${selectedRun.completed} / ${selectedRun.attempted} scan symbols` : `${symbols.length} / 50 active symbols`}</strong>{selectedRun ? <span>{easternDateTime.format(new Date(selectedRun.started_at))} · {selectedRun.trigger.toLowerCase()}</span> : null}<span>{Object.entries(dispositionCounts).map(([name, count]) => `${count} ${name.replaceAll("_", " ")}`).join(" · ") || "No dispositions yet"}</span></div><label className="toggle"><input disabled={busy || watchlistBusy || researchBusy} type="checkbox" checked={workspace?.scanner_enabled ?? false} onChange={toggle}/><span/>5-minute market-hours scan</label><button title={watchlistLoaded && symbols.length === 0 ? "Add at least one symbol to enable scanning" : undefined} disabled={busy || watchlistBusy || researchBusy || !watchlistLoaded || symbols.length === 0} onClick={scan}><Play/>{busy ? "Scanning…" : "Scan now"}</button></div>
    {results.length > 0 ? <section className="panel" aria-label="Candidate assessment summary"><h2>Candidate assessment summary</h2><p>{summary.tracked} tracked · {summary.processed} processed · {summary.candidates} pre-scan candidates · {summary.noTrade} no-trade · {summary.unavailable} unavailable</p><p>{Object.entries(reasonCounts).filter(([reason]) => reasonLabels[reason]).map(([reason, count]) => `${count} ${reasonLabels[reason]}`).join(" · ") || "No assessment reasons recorded"}</p><p>Pre-scan candidates are reviewable and may be approved for the next session, but fresh strict execution validation is still required. No-trade and unavailable symbols remain visible here and were not silently dropped.</p></section> : null}
    {message ? <p className="form-message">{message}</p> : null}
    <section className="watchlist-manager" aria-labelledby="active-watchlist-title">
      <div className="watchlist-manager-header"><div><small>ACTIVE WATCHLIST</small><h2 id="active-watchlist-title">{symbols.length} / 50 symbols</h2><p>Scroll this list to review the symbols used by the scanner. Uncheck symbols to remove them, then save the changes.</p></div><span className="watchlist-selection-count">{watchlistSelection.length} selected</span></div>
      <form className="watchlist-form" onSubmit={addSymbols}><label>Add symbols (comma or space separated)<input disabled={watchlistBusy || researchBusy} value={entry} onChange={(event) => setEntry(event.target.value)} placeholder="SPY, QQQ, AAPL, MSFT"/></label><button className="secondary-button" disabled={watchlistBusy || researchBusy}>{watchlistBusy ? "Saving…" : "Add to watchlist"}</button></form>
      <div className="watchlist-symbol-list" role="group" aria-label="Active watchlist symbols">
        {symbols.length === 0 ? <p className="watchlist-empty">No symbols yet. Add symbols manually or discover recommendations below.</p> : symbols.map((symbol) => <label className="watchlist-symbol" key={symbol}><input disabled={watchlistBusy || researchBusy} type="checkbox" checked={watchlistSelection.includes(symbol)} onChange={(event) => setWatchlistSelection((current) => event.target.checked ? [...current, symbol] : current.filter((item) => item !== symbol))}/><strong>{symbol}</strong><span>{watchlistSelection.includes(symbol) ? "Watching" : "Unselected"}</span></label>)}
      </div>
      <div className="button-row watchlist-actions"><button type="button" onClick={() => void saveWatchlistChanges()} disabled={watchlistBusy || researchBusy || (watchlistSelection.length === symbols.length && symbols.every((symbol) => watchlistSelection.includes(symbol)))}>Save watchlist changes</button><span role="status" aria-live="polite">Unchecking does not remove a symbol until you save.</span></div>
    </section>

    <section className="watchlist-research-panel">
      <header className="watchlist-research-header"><div><small>READ-ONLY AI RESEARCH</small><h2>Discover additions</h2><p>Scan the current list plus a broader liquid, option-relevant universe, then ask the configured provider to recommend up to 10 symbols. Research never replaces or changes this list until you explicitly add selections.</p></div><button onClick={() => void researchWatchlist()} disabled={researchBusy || watchlistBusy}><Bot/>{researchBusy ? "Researching…" : "Discover watchlist"}</button></header>
      <div className="research-safety-note"><ShieldCheck/><span><strong>Review first.</strong> Nothing is saved until you select symbols and press <b>Add selected to watchlist</b>. Recommendations are not trade approvals.</span></div>
      {research ? <>
        <div className="research-meta"><span>{research.provider} · {research.model}</span><span>{research.universe.length} symbols evaluated · up to 10 recommendations</span><span>Scan: {research.scan_completed_at ? easternDateTime.format(new Date(research.scan_completed_at)) : "completion time unavailable"}</span><span>Research: {easternDateTime.format(new Date(research.researched_at))}</span></div>
        <p className="research-summary">{research.report.summary}</p>
        <div className="research-grid">{[...research.report.recommendations].sort((a, b) => a.rank - b.rank).map((item) => <label className={`research-card ${researchSelection.includes(item.symbol) ? "selected" : ""}`} key={item.symbol}><input disabled={researchBusy || watchlistBusy} type="checkbox" checked={researchSelection.includes(item.symbol)} onChange={(event) => setResearchSelection((current) => event.target.checked ? [...current, item.symbol] : current.filter((symbol) => symbol !== item.symbol))}/><div><div className="research-card-heading"><strong>#{item.rank} {item.symbol}</strong><span className={`research-action ${item.action.toLowerCase()}`}>{item.action}</span><span className="research-confidence">{(item.confidence * 100).toFixed(0)}% confidence</span></div><p>{item.rationale}</p><small className={`research-option ${item.option_assessment.toLowerCase()}`}>Option review: {item.option_assessment.replaceAll("_", " ")} · {item.option_reason}</small><small>Risks: {item.risks.join(" · ")}</small><small>Cited evidence: {item.citations.map((citation) => citation.source_id).join(", ")}</small></div></label>)}</div>
        <div className="button-row research-save-row"><button onClick={() => void saveResearchSelection()} disabled={researchBusy || watchlistBusy || researchSelection.length === 0}><Check/>Add selected to watchlist</button><span>{researchSelection.length} selected · existing symbols stay in place</span></div>
        <div className="research-limitations"><strong>Limitations</strong>{research.report.limitations.map((limitation) => <span key={limitation}>· {limitation}</span>)}</div>
      </> : <p className="research-empty">Discovery runs a bounded real-data scan across your current list and additional liquid candidates, then evaluates stock signal and option suitability before showing recommendations.</p>}
    </section>

    <div className="scanner-results-layout">
      <section className="data-table"><header><span>OPPORTUNITY</span><span>DISPOSITION / SCORE</span><span>SOURCE</span><span>OBSERVED</span><span>ACTION</span></header>{results.length === 0 ? <div className="table-empty"><Radar/><strong>No scan results yet</strong><p>Run a scan to evaluate your watchlist using verified real Alpaca data.</p></div> : results.map((result) => <div className="data-row" key={result.opportunity_id}><strong>{result.symbol}</strong><div><span className={`status-pill ${
  result.disposition.replaceAll(" ", "_").toUpperCase() === "NO_TRADE" ? "bad" : ["TRADE", "RESEARCH_CANDIDATE", "PRE_SCAN_CANDIDATE"].includes(result.disposition.replaceAll(" ", "_").toUpperCase()) ? "good" : ""}`}>{result.disposition.replaceAll("_", " ")}</span><small style={{ display: "block", marginTop: "3px" }}>Score {scoreFor(result)}</small>{result.option_diagnostics ? <small style={{ display: "block", marginTop: "3px" }}>{result.option_diagnostics.selected_contracts} reviewable · {result.option_diagnostics.strict_eligible_contracts} executable now</small> : null}{result.reason_codes.length ? <small style={{ display: "block", marginTop: "3px" }}>{result.reason_codes.join(", ")}</small> : null}</div><span>{result.source}</span><time>{easternDateTime.format(new Date(result.observed_at))}</time><Link href={`/desk/opportunities/${result.opportunity_id}`}>Review →</Link></div>)}</section>
      <aside className="scan-history" aria-label="Scan history"><h2><History/>Scan history</h2>{runs.length === 0 ? <p>No completed scans.</p> : runs.slice(0, 5).map((run, index) => <button className={run.scan_run_id === selectedRunId ? "selected" : ""} disabled={historyBusy} key={run.scan_run_id} onClick={() => void selectRun(run)}><span>{index === 0 ? <b>Latest scan</b> : easternDateTime.format(new Date(run.started_at))}</span><strong>{run.completed} / {run.attempted}</strong><small>{run.failed ? `${run.failed} failed` : "all processed"}</small></button>)}</aside>
    </div>
    <button className="refresh-link" onClick={() => void refreshStoredResults()}><RefreshCw/>Refresh scan history</button>
    </div>
  </>;
}
