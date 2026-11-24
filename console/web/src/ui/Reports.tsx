import { useEffect, useState } from "react"
import type { ConsoleApi, Report } from "../api/types"
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
    <div className="reports">
      {reports.map((report) => (
        <section key={report.id} className="report">
          <header>
            <h3>{report.title}</h3>
            <span className="report__window">{report.window}</span>
          </header>
          {report.warn && <p className="report__warn">warn: {report.warn}</p>}
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
        </section>
      ))}
    </div>
  )
}
