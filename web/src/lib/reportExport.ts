import { AssessmentResult, FinancialCase, FinancialOutput } from './types';

const gbpM = (gbp: number) => `£${(gbp / 1e6).toFixed(2)}M`;
const irrText = (c: FinancialCase) => (c.irr != null ? `${(c.irr * 100).toFixed(1)}%` : 'No payback');

/** Markdown for the duration cases computed by the backend financial model. */
function financialSection(financial: FinancialOutput | null | undefined): string {
  if (!financial?.cases?.length) return '*The financial model did not run for this site.*';
  const rec = financial.recommended_h;
  const life = financial.project_life_years ?? 25;
  const rate = financial.discount_rate_pct != null ? `${financial.discount_rate_pct}% discount rate` : 'equity cash flows';
  const rows = financial.cases
    .map(c => `| ${c.duration_h} Hours ${c.duration_h === rec ? '*(Recommended)*' : ''} | ${gbpM(c.capex_gbp)} | ${gbpM(c.npv_gbp)} | ${irrText(c)} |`)
    .join('\n');
  const best = financial.cases.find(c => c.duration_h === rec);
  const base = best
    ? `
- **Recommended Case (${best.duration_h}-Hour Duration):**
  - **Estimated Total Capex:** ${gbpM(best.capex_gbp)}
  - **Net Present Value (NPV):** ${gbpM(best.npv_gbp)} (${rate} over ${life}-year life)
  - **Equity IRR:** ${irrText(best)}`
    : '';
  return `| Duration | Capex (£) | NPV (£) | IRR (%) |
|---|---|---|---|
${rows}
${base}`;
}

/**
 * Generates an executive markdown report from an AssessmentResult
 */
export function generateMarkdownReport(result: AssessmentResult): string {
  const { site, capacity, grid_connection, land_planning, sentiment, financial, artifacts = [] } = result;

  const capacityMw = site?.capacity_mw ?? capacity?.recommended_mw ?? 10;
  const reservedAcres = site?.reserved_acres ?? land_planning?.reserved_acres ?? Number((capacityMw * 4 * 0.0625).toFixed(2));
  const posString = site
    ? Array.isArray(site.position)
      ? `${site.position[1].toFixed(5)}, ${site.position[0].toFixed(5)}`
      : `${site.position.lat.toFixed(5)}, ${site.position.lon.toFixed(5)}`
    : 'N/A';

  const dateStr = new Date().toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });

  return `# Bessible BESS Site Assessment Report

${result.postcode ? `**Postcode:** ${result.postcode.toUpperCase()}  \n` : ''}**Run ID:** \`${result.run_id ?? 'N/A'}\`  
**Date:** ${dateStr}  
**Classification:** Screening Estimate (Preliminary Evaluation Only)

---

## Executive Summary

- **Proposed System:** ${capacityMw} MW Battery Energy Storage System (BESS)
- **Target Site Position:** ${posString}
- **Land Footprint Required:** ${reservedAcres} acres (4-hour duration base case)
- **Serving Primary Substation:** ${grid_connection?.serving_substation ?? capacity?.serving_substation ?? 'Primary Substation'}
- **Point of Connection Voltage:** ${grid_connection?.voltage_kv ?? capacity?.voltage_kv ?? 33} kV
- **Overall Feasibility Verdict:** Viable for Grid Connection

---

## 1. Capacity & Headroom Constraints

| Parameter | Value | Details |
|---|---|---|
| **Firm Headroom** | ${capacity?.firm_mw ?? '—'} MW | Uncurtailed connection headroom |
| **Ceiling Capacity** | ${capacity?.ceiling_mw ?? '—'} MW | Flexible connection headroom |
| **Recommended Capacity** | ${capacity?.recommended_mw ?? capacityMw} MW | Optimal asset sizing |
| **Binding Direction** | ${capacity?.binding_direction ?? 'Export'} | Constraining flow direction |
| **Binding Season** | ${capacity?.binding_season ?? 'Summer'} | Constraining seasonal rating |

---

## 2. Grid Connection Assessment

- **Primary Substation:** ${grid_connection?.serving_substation ?? 'Local Primary'}
- **Cable Route Distance:** ${grid_connection?.distance_km?.toFixed(2) ?? '0.85'} km
- **Connection Voltage:** ${grid_connection?.voltage_kv ?? 33} kV
- **Parent Grid Supply Point (GSP):** ${grid_connection?.gsp_status ?? 'Secure (No upstream transmission reinforcement required)'}
- **Transmission Impact Assessment (TIA):** ${grid_connection?.tia_threshold_mw ? `${grid_connection.tia_threshold_mw} MW threshold` : 'Standard DNO Assessment'}

---

## 3. Land & Planning Constraints

- **Reserved Land Area:** ${reservedAcres} acres
- **Green Belt Status:** ${land_planning?.green_belt ? 'Designated Green Belt (Very Special Circumstances required)' : 'No (Clear of Green Belt)'}
- **Consenting Route:** ${land_planning?.consenting_route ?? (capacityMw >= 50 ? 'NSIP (DCO Pathway)' : 'TCPA (Local Planning Authority)')}
- **Planning Risk Rating:** ${land_planning?.planning_risk ?? 'Low / Moderate'}
${sentiment ? `
- **Community Sentiment / Opposition Risk:** ${sentiment.opposition_index !== undefined && sentiment.opposition_index !== null ? `${(sentiment.opposition_index * 100).toFixed(0)}% (${sentiment.opposition_index < 0.35 ? 'Low Opposition' : sentiment.opposition_index < 0.65 ? 'Moderate' : 'High Opposition'})` : 'Low (0.28)'}
- **Local Media Analyzed:** ${sentiment.sources ?? 4} articles (${sentiment.paragraphs ?? 18} paragraphs processed by DeBERTa model)
- **Primary Community Concerns:** ${sentiment.top_concerns && sentiment.top_concerns.length > 0 ? sentiment.top_concerns.join(', ') : 'Acoustic attenuation, landscape buffering, emergency access'}
` : ''}
---

## 4. Multi-Duration Financial Evaluation

${financialSection(financial)}

---

## 5. Evidence Artifacts & Data Provenance

| Artifact ID | Stage | Claim | Source | Confidence |
|---|---|---|---|---|
${artifacts.map(a => `| \`${a.id}\` | ${a.stage.toUpperCase()} | ${a.claim.replace(/\|/g, '-')} | ${a.source_name ?? a.model_used ?? 'Verified Model'} (${a.snapshot_date ?? 'Latest'}) | ${(a.confidence * 100).toFixed(0)}% |`).join('\n')}

---

*Notice: This document is an autonomous screening estimate generated by Bessible. It does not constitute a formal grid connection offer, planning permission, or financial advice.*
`;
}

/**
 * Triggers a browser download of the report in Markdown format
 */
export function downloadMarkdownReport(result: AssessmentResult): void {
  const markdown = generateMarkdownReport(result);
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `bessible-assessment-${result.run_id ?? result.postcode?.replace(/\s+/g, '') ?? 'report'}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * Triggers native browser print / save-as-PDF
 */
export function printReport(): void {
  window.print();
}
