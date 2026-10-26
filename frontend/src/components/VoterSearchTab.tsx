"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  findVoters, getVoterSearchOptions, VoterSearchError,
  type Constituency, type VoterSearchField, type VoterSearchMode,
  type VoterSearchOptions, type VoterSearchResponse,
} from "@/lib/api";
import { voterMessages, type Lang } from "@/i18n/messages";

const emptyDetails = { name: "", relative_name: "", house_no: "", epic_id: "" };
const inputClass = "mt-2 min-w-0 w-full max-w-full rounded-lg border border-slate-300 bg-white px-3 py-3 text-sm text-slate-950 focus:border-gold focus:outline-none focus:ring-2 focus:ring-gold/40 disabled:bg-slate-100";
const detailFields = ["name", "relative_name", "house_no", "epic_id"] as const;
const displayFields = ["relative_name", "relation_type", "house_no", "age", "gender", "epic_id", "region", "polling_station", "serial_no"] as const;

export default function VoterSearchTab({ constituency, token, lang, active }: {
  constituency: Constituency; token: string | null; lang: Lang; active: boolean;
}) {
  const copy = voterMessages[lang];
  const [options, setOptions] = useState<VoterSearchOptions | null>(null);
  const [optionsLoading, setOptionsLoading] = useState(true);
  const [optionsError, setOptionsError] = useState<VoterSearchError | null>(null);
  const [reload, setReload] = useState(0);
  const [region, setRegion] = useState("");
  const [booth, setBooth] = useState("");
  const [details, setDetails] = useState(emptyDetails);
  const [spelling, setSpelling] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<VoterSearchError | null>(null);
  const [result, setResult] = useState<VoterSearchResponse | null>(null);
  const [mode, setMode] = useState<VoterSearchMode>("standard");
  const controller = useRef<AbortController | null>(null);
  const revision = useRef(0);
  const resultsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!active) return;
    const request = new AbortController();
    setOptionsLoading(true);
    setOptionsError(null);
    getVoterSearchOptions(constituency.slug, token, request.signal)
      .then(value => {
        if (!request.signal.aborted) {
          setOptions(value);
          setResult(previous => previous && previous.source_version !== value.source_version ? null : previous);
        }
      })
      .catch(err => {
        if (!request.signal.aborted) {
          setResult(null);
          setOptionsError(err instanceof VoterSearchError ? err : new VoterSearchError("search_unavailable"));
        }
      })
      .finally(() => { if (!request.signal.aborted) setOptionsLoading(false); });
    return () => request.abort();
  }, [active, constituency.slug, token, reload]);

  useEffect(() => () => { controller.current?.abort(); }, []);
  useEffect(() => {
    if (!active) {
      controller.current?.abort();
      setLoading(false);
    }
  }, [active]);

  const invalidate = () => {
    revision.current += 1;
    controller.current?.abort();
    setResult(null);
    setError(null);
    setLoading(false);
    setMode("standard");
  };
  const reset = () => {
    invalidate();
    setRegion(""); setBooth(""); setDetails(emptyDetails); setSpelling(false); setMode("standard");
  };
  const errorText = (value: VoterSearchError) =>
    copy.errors[value.code as keyof typeof copy.errors] || copy.errors.search_unavailable;

  async function search(nextMode: VoterSearchMode = "standard") {
    invalidate();
    const fields = detailFields.filter(field => details[field].trim());
    if (nextMode === "standard" && !details.epic_id.trim() && fields.length < 2 && !(booth && (details.name.trim() || details.relative_name.trim()))) {
      setError(new VoterSearchError("invalid_input"));
      return;
    }
    const request = new AbortController();
    controller.current = request;
    const currentRevision = revision.current;
    setLoading(true); setMode(nextMode);
    try {
      const response = await findVoters({ ...details, constituency_id: constituency.slug, region: region || null, booth_id: booth || null, search_mode: nextMode, spelling_assistance: spelling }, token, request.signal);
      if (!request.signal.aborted && revision.current === currentRevision) {
        setResult(response);
        requestAnimationFrame(() => resultsRef.current?.focus());
      }
    } catch (err) {
      if (!request.signal.aborted && revision.current === currentRevision) {
        setError(err instanceof VoterSearchError ? err : new VoterSearchError("search_unavailable"));
      }
    } finally {
      if (!request.signal.aborted && revision.current === currentRevision) setLoading(false);
    }
  }

  function submit(event: FormEvent) { event.preventDefault(); void search(); }
  const coverage = result?.coverage || options?.coverage;
  const unavailable = coverage?.availability === "unavailable" || constituency.coverage?.voter_available === false;
  const visibleBooths = (options?.booths || []).filter(b => !region || b.region === region)
    .filter((b, index, all) => all.findIndex(other => other.booth_id === b.booth_id) === index);
  const locked = loading || unavailable;

  return (
    <div hidden={!active} className="flex-1 overflow-y-auto bg-slate-50 px-4 py-5">
      <div className="mx-auto max-w-5xl space-y-5">
        <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
          <p className="text-sm text-slate-500">{constituency.name}</p>
          <h1 className="mt-1 text-2xl font-semibold text-slate-950">{copy.title}</h1>
          <p className="mt-2 text-sm text-slate-600">{copy.hint}</p>
          <p className="mt-1 text-xs text-slate-500">{copy.scopeHint}</p>
        </div>

        {coverage && <p role="status" className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          {unavailable ? copy.unavailable : coverage.completeness === "partial" ? copy.partial : coverage.completeness === "declared" ? copy.declared : copy.unverified}
        </p>}
        {!coverage && unavailable && <p role="status" className="rounded-lg bg-amber-50 p-4 text-sm text-amber-900">{copy.unavailable}</p>}
        {optionsLoading && <p role="status" className="text-sm text-slate-600">{copy.loading}</p>}
        {optionsError && <div role="alert" className="rounded-lg bg-red-50 p-4 text-sm text-red-800">
          <p>{errorText(optionsError)}</p>
          <button type="button" className="mt-2 underline" onClick={() => setReload(value => value + 1)}>{copy.retry}</button>
        </div>}
        {options && !options.booth_list_available && <p className="text-sm text-slate-600">{copy.noBoothList}</p>}

        <form onSubmit={submit} className="space-y-5">
          <fieldset disabled={locked} className="min-w-0 rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
            <legend className="px-1 text-base font-semibold text-slate-950">{copy.area}</legend>
            <div className="grid gap-4 md:grid-cols-2">
              <label htmlFor="voter-region" className="min-w-0 text-sm font-medium text-slate-900"><span id="voter-region-label">{copy.region}</span>
                <select id="voter-region" aria-labelledby="voter-region-label" className={inputClass} value={region} disabled={optionsLoading || !!optionsError} onChange={e => { invalidate(); setRegion(e.target.value); setBooth(""); }}>
                  <option value="">{copy.allRegions}</option>
                  {region && !options?.regions.includes(region) && <option value={region} disabled>{region}</option>}
                  {options?.regions.map(value => <option key={value} value={value}>{value}</option>)}
                </select>
              </label>
              <label htmlFor="voter-booth" className="min-w-0 text-sm font-medium text-slate-900"><span id="voter-booth-label">{copy.booth}</span>
                <select id="voter-booth" aria-labelledby="voter-booth-label" className={inputClass} value={booth} disabled={optionsLoading || !!optionsError} onChange={e => { invalidate(); setBooth(e.target.value); }}>
                  <option value="">{copy.allBooths}</option>
                  {booth && !visibleBooths.some(b => b.booth_id === booth) && <option value={booth} disabled>{booth}</option>}
                  {visibleBooths.map(b => <option key={b.booth_id} value={b.booth_id} disabled={b.mapping_status !== "mapped"}>
                    {b.part_number} · {b.booth_name}{b.mapping_status === "mapped" ? "" : ` (${b.mapping_status === "ambiguous" ? copy.ambiguous : copy.unmapped})`}
                  </option>)}
                </select>
              </label>
            </div>
          </fieldset>
          <fieldset disabled={locked} className="min-w-0 rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
            <legend className="px-1 text-base font-semibold text-slate-950">{copy.details}</legend>
            <div className="grid gap-4 md:grid-cols-2">
              {detailFields.map(field => <label key={field} htmlFor={`voter-${field}`} className="text-sm font-medium text-slate-900">
                <span id={`voter-${field}-label`}>{copy[field]}</span>
                <input id={`voter-${field}`} aria-labelledby={`voter-${field}-label`} className={inputClass} value={details[field]} maxLength={field === "name" || field === "relative_name" ? 200 : 100}
                  autoComplete="off" spellCheck={false} aria-describedby={field === "relative_name" ? "voter-relative-hint" : undefined}
                  placeholder={field === "name" ? copy.namePlaceholder : field === "relative_name" ? copy.relativePlaceholder : field === "house_no" ? copy.housePlaceholder : copy.epicPlaceholder}
                  onChange={e => { invalidate(); setDetails(value => ({ ...value, [field]: e.target.value })); }} />
                {field === "relative_name" && <span id="voter-relative-hint" className="mt-1 block text-xs font-normal text-slate-500">{copy.relativeHint}</span>}
              </label>)}
            </div>
            <label className="mt-5 flex items-start gap-2 text-sm text-slate-700">
              <input type="checkbox" className="mt-1" checked={spelling} aria-describedby="voter-spelling-hint" onChange={e => { invalidate(); setSpelling(e.target.checked); }} />
              <span>{copy.spelling}<span id="voter-spelling-hint" className="mt-1 block text-xs text-slate-500">{copy.spellingHint}</span></span>
            </label>
          </fieldset>
          <div className="flex gap-3">
            <button type="submit" disabled={locked} className="rounded-lg bg-navy-950 px-6 py-3 text-sm font-semibold text-white disabled:opacity-50">{loading ? copy.searching : copy.search}</button>
            <button type="button" onClick={reset} className="rounded-lg border border-slate-300 bg-white px-5 py-3 text-sm font-medium text-slate-700">{copy.clear}</button>
          </div>
        </form>

        {loading && <p role="status" className="text-sm text-slate-600">{copy.searching}</p>}
        {error && <div role="alert" className="rounded-lg bg-red-50 p-4 text-sm text-red-800">
          <p>{errorText(error)}</p>
          {error.requestId && <p className="mt-1 text-xs">{copy.requestId}: {error.requestId}</p>}
          <button type="button" className="mt-2 underline" onClick={() => void search(mode)}>{copy.retry}</button>
        </div>}
        {result && <div ref={resultsRef} tabIndex={-1} aria-label={copy.results} className="space-y-4 focus:outline-none">
          <h2 className="text-lg font-semibold text-slate-950">{copy.results}</h2>
          <p role="status" className="text-sm text-slate-600">{result.outcome === "unavailable" ? copy.unavailable : result.outcome === "refine_search" ? copy.refine : result.outcome === "no_matches" ? copy.noMatches : `${result.candidates.length} ${copy.count}`}</p>
          {result.duplicate_epic && <p role="alert" className="rounded-lg bg-amber-50 p-4 text-sm text-amber-900">{copy.duplicate}</p>}
          {mode !== "standard" && <p className="text-sm text-amber-900">{copy.singleWarning}</p>}
          {result.spelling_status === "unavailable" && <p className="text-sm text-amber-900">{copy.spellingUnavailable}</p>}
          {Object.entries(result.spelling_variants).map(([field, values]) => <p key={field} className="text-sm text-slate-600">{copy.variants} ({copy[field as VoterSearchField]}): {values?.join(" · ")}</p>)}
          {result.candidates.map(candidate => <article key={candidate.record_id} className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
            <p className="text-sm font-semibold text-navy-900">{copy.tiers[candidate.match_tier]}</p>
            <h3 className="mt-2 text-xl font-semibold text-slate-950">{candidate.voter.name || copy.notRecorded}</h3>
            <p className="mt-1 text-xs text-slate-500">{candidate.matched_fields}/{candidate.supplied_fields} {copy.matched}</p>
            <ul className="mt-3 flex flex-wrap gap-2" aria-label={copy.details}>
              {Object.entries(candidate.field_matches).map(([field, status]) => <li key={field} className={`rounded-md px-2 py-1 text-xs ${status === "different" || status === "not_recorded" ? "bg-amber-50 text-amber-900" : "bg-slate-100 text-slate-700"}`}>
                {copy[field as VoterSearchField]}: {copy.statuses[status!]}
              </li>)}
            </ul>
            <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
              {displayFields.map(field => <div key={field}><dt className="text-xs text-slate-500">{copy[field]}</dt><dd className="mt-0.5 break-words text-slate-950">{candidate.voter[field]?.trim() || copy.notRecorded}</dd></div>)}
              {candidate.booth_id && <div><dt className="text-xs text-slate-500">{copy.booth}</dt><dd className="mt-0.5 text-slate-950">{candidate.booth_id}</dd></div>}
            </dl>
          </article>)}
          {result.truncated && result.outcome !== "refine_search" && <p className="text-sm text-amber-900">{copy.truncated}</p>}
          {(result.outcome === "no_matches" || result.outcome === "refine_search") && result.suggested_modes.length > 0 && <div className="rounded-lg border border-slate-200 bg-white p-4">
            <p className="text-sm text-slate-600">{copy.broaderHint}</p>
            <div className="mt-3 flex flex-wrap gap-3">{result.suggested_modes.map(value => <button key={value} type="button" className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-navy-900" onClick={() => void search(value)}>{value === "name_only" ? copy.broadenName : copy.broadenRelative}</button>)}</div>
          </div>}
          <p className="text-xs text-slate-500">{copy.sourceHint}</p>
        </div>}
      </div>
    </div>
  );
}
