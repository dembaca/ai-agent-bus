import { useEffect, useState } from "react"
import type { Activity, ConsoleApi, RangeKey, Report, ReportStack, TokenCategory } from "../api/types"
import { seconds } from "../clock"
import "./Stats.css"

type Fail = (what: string, error: unknown) => void

/** The time ranges of the page (#284), the one switch for every card. */
export const RANGES: readonly { key: RangeKey; label: string; named: string }[] = [
  { key: "24h", label: "24 h", named: "the last 24 hours" },
  { key: "7d", label: "7 days", named: "the last 7 days" },
  { key: "30d", label: "30 days", named: "the last 30 days" },
  { key: "90d", label: "90 days", named: "the last 90 days" },
  { key: "12m", label: "12 months", named: "the last 12 months" },
]

/** The stats (ADR-015 point 8, #284): from the STATS lines as RT-6.9 defines
 * them — counts and seconds only, no names, no post ids, no text — over one
 * time range for the whole page. A field a source line does not have shows
 * as "n/a", never as 0. */
export function Stats({ api, onFail, range, onRange }: {
  api: ConsoleApi; onFail: Fail; range: RangeKey; onRange: (range: RangeKey) => void
}) {
  const [reports, setReports] = useState<Report[]>([])
  useEffect(() => {
    let gone = false
    const load = async () => {
      try {
        const r = await api.stats(range)
        if (!gone) setReports(r)
      } catch (error: unknown) {
        if (!gone) onFail("stats", error)
      }
    }
    void load()
    const timer = setInterval(load, 30_000)
    return () => { gone = true; clearInterval(timer) }
  }, [api, onFail, range])

  const named = RANGES.find((r) => r.key === range)?.named ?? ""
  return (
    <>
    <div className="page-head">
      <div className="page-head__title">
        <h1>Stats</h1>
        <span className="page-head__sub">
          {named} · counts and seconds from the agents' STATS lines; n/a where a line has no value
        </span>
      </div>
      <div className="range" role="group" aria-label="Time range">
        {RANGES.map((r) => (
          <button key={r.key} type="button" className="range__btn"
            aria-pressed={range === r.key} onClick={() => onRange(r.key)}>{r.label}</button>
        ))}
      </div>
    </div>
    <div className="reports">
      {reports.map((report) => report.activity ? (
        <section key={report.id} className="report report--wide">
          <header>
            <h2>{report.title}</h2>
            <span className="report__window">{report.window}</span>
          </header>
          <ActivityChart activity={report.activity} />
        </section>
      ) : (
        <ReportCard key={report.id} report={report} />
      ))}
    </div>
    </>
  )
}

/** One card of figures (RT-6.9): a card whose range the stream does not hold keeps its
 * place with every figure n/a and says why. */
export function ReportCard({ report }: { report: Report }) {
  return (
    <section className="report">
      <header>
        <h2>{report.title}</h2>
        <span className="report__window">{report.window}</span>
      </header>
      {report.warn && <p className="report__warn">warn: {report.warn}</p>}
      {report.note && <p className="report__note">{report.note}</p>}
      {report.unavailable && <p className="report__note" role="status">n/a: {report.unavailable}</p>}
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
  )
}

/** The bus words each group bundles, as the legend explains them; a word outside
 * the lists (BLOCKED, DELEGATE, "other") counts as organising. */
const GROUP_WORDS: Record<string, string> = {
  working: "ACK, PROGRESS",
  finished: "RESULT, DONE",
  asking: "ask",
  organising: "TASK, HELLO, IDLE, BYE, plain posts",
}

/** A bar's first moment as a label, by UTC: the bucket is a day, a week or a month. */
export function barLabel(start: number, bucket: Activity["bucket"]): string {
  const d = new Date(start * 1000)
  if (bucket === "month") return d.toLocaleDateString([], { month: "short", year: "2-digit", timeZone: "UTC" })
  if (bucket === "hour") return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZone: "UTC" })
  const day = d.toLocaleDateString([], { day: "numeric", month: "short", timeZone: "UTC" })
  return bucket === "week" ? `week of ${day}` : day
}

/** The activity chart (#284): stacked bars, one per bucket, the agents' own posts in four
 * groups; the exact bus words show on hovering (or focusing) a bar. */
export function ActivityChart({ activity }: { activity: Activity }) {
  const { bars, groups, bucket } = activity
  const total = (b: Activity["bars"][number]) => groups.reduce((n, g) => n + (b.groups[g.id] ?? 0), 0)
  const peak = Math.max(0, ...bars.map(total))
  const step = Math.ceil(bars.length / 10) || 1
  return (
    <div className="activity">
      <span className="activity__legend">
        {groups.map((g) => (
          <span key={g.id} className={`activity__key activity__seg--${g.id}`}>{g.label}</span>
        ))}
      </span>
      {activity.unavailable ? (
        <>
          <p className="activity__empty" role="status">{activity.unavailable}</p>
          {activity.reason && <p className="report__note">{activity.reason}</p>}
        </>
      ) : peak === 0 ? (
        <p className="activity__empty" role="status">no posts counted in this range</p>
      ) : (
        <div className="activity__plot">
          <span className="activity__max">{peak}</span>
          <div className="activity__bars">
            {bars.map((bar, i) => {
              const words = Object.entries(bar.words).filter(([, n]) => n > 0)
                .map(([w, n]) => `${w} ${n}`).join(", ")
              const text = `${barLabel(bar.start, bucket)}: ${total(bar)} posts${words ? ` (${words})` : ""}`
              return (
                <div key={bar.start} className="activity__col" tabIndex={0} title={text} aria-label={text}>
                  <span className="activity__stack" style={{ height: `${(total(bar) / peak) * 100}%` }}>
                    {groups.map((g) => (bar.groups[g.id] ?? 0) > 0 && (
                      <span key={g.id} className={`activity__seg activity__seg--${g.id}`}
                        style={{ flexGrow: bar.groups[g.id] }} />
                    ))}
                  </span>
                  <span className="activity__label">{i % step === 0 ? barLabel(bar.start, bucket) : ""}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
      <ul className="activity__words">
        {groups.map((g) => (
          <li key={g.id}><span className={`activity__key activity__seg--${g.id}`}>{g.label}</span>
            {" "}({GROUP_WORDS[g.id] ?? ""})</li>
        ))}
      </ul>
      <p className="report__note">
        Every post of an agent starts with a bus word. A bar counts the agents' own posts by that word;
        people's posts are not in it. The exact words show on hovering a bar.
      </p>
    </div>
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
