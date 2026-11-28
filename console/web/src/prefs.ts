import type { GroupBy, RangeKey, Scope } from "./api/types"

// What the console remembers per person (#283): whether the "At a glance"
// panel is open or folded, the grouping, the folded groups, the scope and the Stats page's
// time range (#284).
// Kept in this browser's localStorage under the operator's name (the login's
// own, so two people on one browser keep their own); storage can be missing
// or throw (a private window, blocked site data): then nothing is remembered
// and the console still works. A stored value that is no longer one of the
// known words is dropped, never trusted.

export interface Prefs {
  /** null: never picked, the window's width decides (folded when narrow). */
  panel: "open" | "folded" | null
  by: GroupBy
  scope: Scope
  /** `<by>:<group key>` to true (folded) or false (open): what the operator
   * picked; a group without an entry follows the scope's default. */
  folded: Record<string, boolean>
  /** The Stats page's time range (#284). */
  range: RangeKey
}

export const DEFAULT_PREFS: Prefs = { panel: null, by: "home", scope: "mine", folded: {}, range: "7d" }

const BYS: readonly string[] = ["home", "repo", "owner", "status", "none"]
const SCOPES: readonly string[] = ["mine", "trusting", "everyone"]
const RANGE_KEYS: readonly string[] = ["24h", "7d", "30d", "90d", "12m"]
const MAX_FOLDED = 500

const keyOf = (person: string): string => `choirbus.console.${person}`

/** The remembered choices of `person`, or the defaults. */
export function loadPrefs(person: string | null): Prefs {
  if (!person) return DEFAULT_PREFS
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(keyOf(person)) ?? "null")
    if (typeof raw !== "object" || raw === null) return DEFAULT_PREFS
    const r = raw as Record<string, unknown>
    const folded: Record<string, boolean> = {}
    if (typeof r.folded === "object" && r.folded !== null) {
      for (const [k, v] of Object.entries(r.folded).slice(0, MAX_FOLDED)) {
        if (typeof v === "boolean") folded[k] = v
      }
    }
    return {
      panel: r.panel === "open" || r.panel === "folded" ? r.panel : null,
      by: typeof r.by === "string" && BYS.includes(r.by) ? r.by as GroupBy : DEFAULT_PREFS.by,
      scope: typeof r.scope === "string" && SCOPES.includes(r.scope) ? r.scope as Scope : DEFAULT_PREFS.scope,
      folded,
      range: typeof r.range === "string" && RANGE_KEYS.includes(r.range) ? r.range as RangeKey : DEFAULT_PREFS.range,
    }
  } catch {
    return DEFAULT_PREFS
  }
}

export function savePrefs(person: string | null, prefs: Prefs): void {
  if (!person) return
  try {
    window.localStorage.setItem(keyOf(person), JSON.stringify(prefs))
  } catch {
    // a storage that refuses is no error: the choice lasts until the reload
  }
}

/** The group's folded state: the operator's pick, else the scope's default —
 * in "Everyone" the groups start folded, except those that need the operator. */
export function isFolded(prefs: Prefs, key: string, needsYou: boolean): boolean {
  const own = prefs.folded[`${prefs.by}:${key}`]
  return own ?? (prefs.scope === "everyone" && !needsYou)
}
