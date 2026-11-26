import { useEffect, useState } from "react"
import type { ConsoleApi, Report, ReportStack, TokenCategory } from "../api/types"
import { seconds } from "../clock"
import "./Reports.css"

type Fail = (what: string, error: unknown) => void

/** The reports (ADR-015 point 8): from the STATS lines as RT-6.9 defines
 * them — counts and seconds only, no names, no post ids, no text. A field
 * a source line does not have shows as "n/a", never as 0. */
export function Reports({ api, onFail }: { api: ConsoleApi; onFail: Fail }) {
  const [reports, setReports] = useState<Report[]>([])
  useEffect(() => {
    let gone = false
    const load = async () => {
      try {
        const r = await api.stats()
        if (!gone) setReports(r)
      } catch (error: unknown) {
        if (!gone) onFail("reports", error)
      }
    }
    void load()
    const timer = setInterval(load, 30_000)
    return () => { gone = true; clearInterval(timer) }
  }, [api, onFail])

  return (
    <>
    <div className="page-head">
      <div className="page-head__title">
        <h1>Reports</h1>
        <span className="page-head__sub">counts and seconds from the agents' STATS lines; n/a where a line has no value</span>
      </div>
    </div>
    <div className="reports">
      {reports.map((report) => (
        <section key={report.id} className="report">
          <header>
            <h3>{report.title}</h3>
            <span className="report__window">{report.window}</span>
          </header>
          {report.warn && <p className="report__warn">warn: {report.warn}</p>}
          {report.note && <p className="report__note">{report.note}</p>}
          <table className="report__table">
            <tbody>
              {report.fields.map((field) => (
                <tr key={field.label}>
                  <th>{field.label}</th>
                  <td>{seconds(field.value, field.unit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {report.bars && (
            <div className="report__bars">
              {report.bars.map((bar) => (
                <div key={bar.label} className="report__bar" title={`${bar.label}: ${bar.value}`}>
                  <span className="report__bar-label">{bar.label}</span>
                  <span className="report__bar-track">
                    <span className="report__bar-fill"
                      style={{ width: `${Math.min(100, (bar.value / 6) * 100)}%` }} />
                  </span>
                  <span className="report__bar-value">{bar.value}</span>
                </div>
              ))}
            </div>
          )}
          {report.stacks && <Stacks stacks={report.stacks} />}
        </section>
      ))}
    </div>
    </>
  )
}

const CATEGORY_LABEL: Record<TokenCategory, string> = {
  task: "task work",
  overhead: "bus overhead",
  coordination: "coordination",
}

/** The tokens by category (RT-6.9 step 3): one stacked bar per row, the
 * percent on each segment; input-token equivalents by default, raw tokens
 * on request. Every bar of an estimate carries the "estimated" mark. */
function Stacks({ stacks }: { stacks: ReportStack[] }) {
  const [raw, setRaw] = useState(false)
  return (
    <div className="report__stacks">
      <div className="report__stack-head">
        <span className="report__legend">
          {(Object.keys(CATEGORY_LABEL) as TokenCategory[]).map((c) => (
            <span key={c} className={`report__key report__seg--${c}`}>{CATEGORY_LABEL[c]}</span>
          ))}
        </span>
        <button type="button" className="report__toggle" onClick={() => setRaw(!raw)}>
          {raw ? "raw tokens" : "input-token equivalents"}
        </button>
      </div>
      {stacks.length === 0 && <p className="report__note">n/a</p>}
      {stacks.map((stack) => {
        const segments = raw ? stack.raw : stack.segments
        return (
          <div key={stack.label} className="report__stack">
            <span className="report__bar-label">
              {stack.label}
              {stack.estimated && <span className="report__estimated">estimated</span>}
            </span>
            <span className="report__stack-track">
              {segments.map((seg) => {
                const pct = Math.min(100, Math.max(0, seg.percent ?? 0))
                return (
                  <span key={seg.category} className={`report__seg report__seg--${seg.category}`}
                    style={{ width: `${pct}%` }}
                    title={`${CATEGORY_LABEL[seg.category]}: ${seg.value} (${seg.percent ?? "n/a"} %)`}>
                    {pct > 0 && <span className="report__seg-label">{pct} %</span>}
                  </span>
                )
              })}
            </span>
          </div>
        )
      })}
    </div>
  )
}
