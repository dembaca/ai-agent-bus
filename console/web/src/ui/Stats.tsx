import { Fragment, useEffect, useState } from "react"
import type { Activity, ConsoleApi, RangeKey, Report, ReportStack, TokenCategory } from "../api/types"
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
      <div className="stats-range" role="group" aria-label="Time range">
        {RANGES.map((r) => (
          <button key={r.key} type="button" className="stats-range__btn"
            aria-pressed={range === r.key} onClick={() => onRange(r.key)}>{r.label}</button>
        ))}
      </div>
    </div>
    <Cards reports={reports} />
    </>
  )
}

/** The cards of the page, in the order the server sends them. */
export function Cards({ reports }: { reports: Report[] }) {
  return (
    <div className="stats-cards">
      {reports.map((report) => {
        if (report.activity) {
          return (
            <section key={report.id} className="stats-card stats-card--wide" aria-label={report.title}>
              <h2 className="stats-card__title">{report.title}</h2>
              <ActivityChart activity={report.activity} />
            </section>
          )
        }
        if (report.id === "asks-before") return null
        if (report.id === "asks") {
          return <AsksCard key={report.id} now={report} before={reports.find((r) => r.id === "asks-before")} />
        }
        if (report.id === "share-per-version") return <ShareCard key={report.id} report={report} />
        if (report.id === "traffic-by-category") return <TrafficCard key={report.id} report={report} />
        if (report.id === "tokens-by-category") return <TokensCard key={report.id} report={report} />
        return <FigureCard key={report.id} report={report} />
      })}
    </div>
  )
}

const value = (report: Report | undefined, label: string): number | null =>
  report?.fields.find((f) => f.label === label)?.value ?? null

/** A count with thousands separators, or "n/a": a missing value is never 0. */
export const count = (n: number | null): string => n === null ? "n/a" : n.toLocaleString("en-US")
export const percent = (n: number | null): string => n === null ? "n/a" : `${n}%`

/** A wait in the unit that reads best: seconds, minutes, or hours and minutes. */
export function wait(s: number | null): string {
  if (s === null) return "n/a"
  if (s < 60) return `${s} s`
  if (s < 3600) return `${Math.round(s / 60)} min`
  return `${Math.floor(s / 3600)} h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")} min`
}

/** A card's n/a reason, when the whole card is n/a (a range the stream does not hold). */
function Unavailable({ report }: { report: Report }) {
  return report.unavailable ? <p className="stats-card__note" role="status">n/a: {report.unavailable}</p> : null
}

/** The small figures a card keeps beside its chart (lines counted, age of the oldest line):
 * every one shown, n/a where the stream has no value. */
function Facts({ items }: { items: { label: string; text: string }[] }) {
  return (
    <dl className="stats-facts">
      {items.map((i) => (
        <div key={i.label}><dt>{i.label}</dt><dd>{i.text}</dd></div>
      ))}
    </dl>
  )
}

const age = (report: Report): { label: string; text: string } =>
  ({ label: "oldest line", text: ageText(value(report, "age of the oldest line")) })
const ageText = (s: number | null): string => s === null ? "n/a" : s >= 86400 ? `${Math.floor(s / 86400)} d` : wait(s)

/** The share of the bus overhead in all tokens, per version (RT-6.9 step 3): a thin bar
 * whose full width is 20 %. */
export function ShareCard({ report }: { report: Report }) {
  const rows = report.fields.filter((f) => f.label.startsWith("version "))
  return (
    <section className="stats-card" aria-label={report.title}>
      <h2 className="stats-card__title">{report.title}</h2>
      <Unavailable report={report} />
      <div className="stats-bars stats-bars--share">
        {rows.map((row) => {
          const label = row.label.slice("version ".length)
          const v = row.value
          return (
            <Fragment key={row.label}>
              <span className="stats-bars__label stats-bars__label--mono">{label}</span>
              <span className="stats-bars__track" role="img"
                aria-label={v === null ? `${label}: n/a, no token counts` : `${label}: ${v} percent of all tokens`}>
                {v !== null && <span className="stats-bars__fill stats-bars__fill--share"
                  style={{ width: `${Math.min(100, (v / 20) * 100)}%` }} />}
              </span>
              <span className="stats-bars__value">{percent(v)}</span>
            </Fragment>
          )
        })}
      </div>
      {rows.length === 0 && !report.unavailable && <p className="stats-card__note" role="status">n/a: no version sent token counts</p>}
      <Facts items={[{ label: "lines", text: count(value(report, "lines")) }, age(report)]} />
      <p className="stats-card__note">
        The bar's full width is 20%. Sessions that sent no token counts are left out, not counted as zero.
      </p>
    </section>
  )
}

/** The asks to humans: a row for the chosen range and a muted row for the range before. */
export function AsksCard({ now, before }: { now: Report; before?: Report }) {
  const row = (r: Report, muted: boolean) => {
    const asked = value(r, "asks")
    const answered = value(r, "answered")
    const share = value(r, "answered share")
    return (
      <tr key={r.id} className={muted ? "stats-asks__row stats-asks__row--before" : "stats-asks__row"}>
        <th scope="row">{r.window}</th>
        <td data-label="Asked">{count(asked)}</td>
        <td data-label="Answered">{count(answered)}{answered !== null && share !== null ? ` · ${share}%` : ""}</td>
        <td data-label="Open">{count(value(r, "still open at the part's end"))}</td>
        <td data-label="Mean wait">{wait(value(r, "mean wait for an answer"))}</td>
        <td data-label="Longest wait">{wait(value(r, "longest wait for an answer"))}</td>
      </tr>
    )
  }
  return (
    <section className="stats-card" aria-label={now.title}>
      <h2 className="stats-card__title">{now.title}</h2>
      <Unavailable report={now} />
      {before?.unavailable && (
        <p className="stats-card__note" role="status">{before.window}: n/a, {before.unavailable}</p>
      )}
      <div className="stats-asks-wrap">
      <table className="stats-asks">
        <thead>
          <tr>
            <th scope="col">Window</th><th scope="col">Asked</th><th scope="col">Answered</th>
            <th scope="col">Open</th><th scope="col">Mean wait</th><th scope="col">Longest wait</th>
          </tr>
        </thead>
        <tbody>
          {row(now, false)}
          {before && row(before, true)}
        </tbody>
      </table>
      </div>
      <p className="stats-card__note">An ask that expired without an answer counts as asked, not as answered.</p>
    </section>
  )
}

/** The bus traffic by category (RT-6.9 step 3): bars relative to the largest of their group. */
export function TrafficCard({ report }: { report: Report }) {
  const group = (title: string, rows: { label: string; v: number | null }[]) => {
    const max = Math.max(0, ...rows.map((r) => r.v ?? 0))
    return (
      <div className="stats-bars stats-bars--traffic" role="group" aria-label={title}>
        {rows.map((r) => (
          <Fragment key={r.label}>
            <span className="stats-bars__label">{r.label}</span>
            <span className="stats-bars__track" role="img"
              aria-label={r.v === null ? `${title}, ${r.label}: n/a` : `${title}, ${r.label}: ${count(r.v)}`}>
              {r.v !== null && max > 0 && <span className="stats-bars__fill stats-bars__fill--traffic"
                style={{ width: `${(r.v / max) * 100}%` }} />}
            </span>
            <span className="stats-bars__value">{count(r.v)}</span>
          </Fragment>
        ))}
      </div>
    )
  }
  return (
    <section className="stats-card" aria-label={report.title}>
      <h2 className="stats-card__title">{report.title}</h2>
      <Unavailable report={report} />
      <h3 className="stats-card__sub">Items</h3>
      {group("Items", [
        { label: "bus overhead", v: value(report, "bus overhead, items") },
        { label: "coordination", v: value(report, "coordination, items") },
      ])}
      <h3 className="stats-card__sub">Characters</h3>
      {group("Characters", [
        { label: "bus overhead", v: value(report, "bus overhead, characters") },
        { label: "coordination", v: value(report, "coordination, characters") },
      ])}
      <Facts items={[
        { label: "overhead share of characters", text: percent(value(report, "bus overhead share of characters")) },
        { label: "polls", text: count(value(report, "polls")) },
        { label: "empty polls", text: percent(value(report, "empty polls share")) },
        { label: "lines with these counts", text: count(value(report, "lines with these counts")) },
        { label: "lines", text: count(value(report, "lines")) },
        age(report),
      ]} />
      <p className="stats-card__note">Counted from the STATS lines every session publishes.</p>
    </section>
  )
}

/** A card the design has no place for: its figures as facts, in the same shell. */
function FigureCard({ report }: { report: Report }) {
  return (
    <section className="stats-card" aria-label={report.title}>
      <h2 className="stats-card__title">{report.title}</h2>
      <Unavailable report={report} />
      <Facts items={report.fields.map((f) => ({
        label: f.label,
        text: f.unit === "s" ? wait(f.value) : f.unit === "%" ? percent(f.value) : count(f.value),
      }))} />
      {report.note && <p className="stats-card__note">{report.note}</p>}
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
    <div className="stats-activity">
      <span className="stats-activity__legend">
        {groups.map((g) => (
          <span key={g.id} className={`stats-activity__key stats-activity__seg--${g.id}`}>{g.label}</span>
        ))}
      </span>
      {activity.unavailable ? (
        <>
          <p className="stats-activity__empty" role="status">{activity.unavailable}</p>
          {activity.reason && <p className="stats-card__note">{activity.reason}</p>}
        </>
      ) : peak === 0 ? (
        <p className="stats-activity__empty" role="status">no posts counted in this range</p>
      ) : (
        <div className="stats-activity__plot">
          <span className="stats-activity__max">{peak}</span>
          <div className="stats-activity__bars">
            {bars.map((bar, i) => {
              const words = Object.entries(bar.words).filter(([, n]) => n > 0)
                .map(([w, n]) => `${w} ${n}`).join(", ")
              const text = `${barLabel(bar.start, bucket)}: ${total(bar)} posts${words ? ` (${words})` : ""}`
              return (
                <div key={bar.start} className="stats-activity__col" tabIndex={0} title={text} aria-label={text}>
                  <span className="stats-activity__stack" style={{ height: `${(total(bar) / peak) * 100}%` }}>
                    {groups.map((g) => (bar.groups[g.id] ?? 0) > 0 && (
                      <span key={g.id} className={`stats-activity__seg stats-activity__seg--${g.id}`}
                        style={{ flexGrow: bar.groups[g.id] }} />
                    ))}
                  </span>
                  <span className="stats-activity__label">{i % step === 0 ? barLabel(bar.start, bucket) : ""}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
      <ul className="stats-activity__words">
        {groups.map((g) => (
          <li key={g.id}><span className={`stats-activity__key stats-activity__seg--${g.id}`}>{g.label}</span>
            {" "}({GROUP_WORDS[g.id] ?? ""})</li>
        ))}
      </ul>
      {activity.hours && (
        <p className="stats-card__note stats-activity__hours" role="status">
          Hours from {activity.hours.lines} of {activity.hours.of} lines. Running sessions are still to come:
          their hours arrive only with their next daily part.
        </p>
      )}
      <p className="stats-card__note">
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

/** The tokens by category (RT-6.9 step 3): per row one stacked bar; input-token equivalents
 * by default, raw tokens on request. The split is an estimate, the card says so. */
export function TokensCard({ report }: { report: Report }) {
  const [raw, setRaw] = useState(false)
  const stacks: ReportStack[] = report.stacks ?? []
  const estimated = stacks.some((s) => s.estimated)
  return (
    <section className="stats-card" aria-label={report.title}>
      <div className="stats-card__head">
        <h2 className="stats-card__title">
          {report.title}
          {estimated && <span className="stats-chip">estimated</span>}
        </h2>
        <div className="stats-range stats-range--small" role="group" aria-label="Unit">
          <button type="button" className="stats-range__btn" aria-pressed={!raw} onClick={() => setRaw(false)}>
            Input-token equivalents
          </button>
          <button type="button" className="stats-range__btn" aria-pressed={raw} onClick={() => setRaw(true)}>
            Raw tokens
          </button>
        </div>
      </div>
      <Unavailable report={report} />
      {stacks.length === 0 && !report.unavailable && <p className="stats-card__note" role="status">n/a: no token split in this range</p>}
      <div className="stats-stacks">
        {stacks.map((stack) => {
          const segments = raw ? stack.raw : stack.segments
          const said = segments.map((seg) =>
            `${seg.percent === null ? "n/a" : `${seg.percent} percent`} ${CATEGORY_LABEL[seg.category]}`).join(", ")
          return (
            <Fragment key={stack.label}>
              <span className="stats-bars__label stats-bars__label--mono">{stack.label}</span>
              <span className="stats-stack" role="img" aria-label={`${stack.label}: ${said}`}>
                {segments.map((seg) => (seg.percent ?? 0) > 0 && (
                  <span key={seg.category} className={`stats-stack__seg stats-stack__seg--${seg.category}`}
                    style={{ flex: seg.percent ?? 0 }}
                    title={`${CATEGORY_LABEL[seg.category]}: ${count(seg.value)} (${seg.percent}%)`} />
                ))}
              </span>
            </Fragment>
          )
        })}
      </div>
      <span className="stats-legend">
        {(Object.keys(CATEGORY_LABEL) as TokenCategory[]).map((c) => (
          <span key={c} className={`stats-legend__key stats-stack__seg--${c}`}>{CATEGORY_LABEL[c]}</span>
        ))}
      </span>
      <Facts items={[
        { label: "lines with the split", text: `${count(value(report, "lines with the split"))} of ${count(value(report, "lines"))}` },
        { label: "requests driven by the bus alone", text: count(value(report, "requests driven by the bus alone")) },
        { label: "requests driven by coordination", text: count(value(report, "requests driven by coordination")) },
        age(report),
      ]} />
      <p className="stats-card__note">
        A request's tokens are split by what its context and output hold, not by what started the turn.
      </p>
    </section>
  )
}
