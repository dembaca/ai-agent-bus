import type { BuildInfo } from "../api/types"
import "./Footer.css"

/** The commit as the footer shows it: its first seven characters. */
export function shortRevision(revision: string | null): string | null {
  return revision ? revision.slice(0, 7) : null
}

/** "version 0.103.0 · commit 28d6475": both from the image build (#182),
 * served by the console server in /v1/me; n/a where the server has none
 * (a development run), never a number made up here. */
export function buildLabel(build: BuildInfo | null): string {
  const version = build?.version ?? null
  const commit = shortRevision(build?.revision ?? null)
  return `version ${version ?? "n/a"} · commit ${commit ?? "n/a"}`
}

export function Footer({ build }: { build: BuildInfo | null }) {
  return (
    <footer className="footer">
      <div className="footer__inner">
        <span>Choirbus operator console · read-only pilot</span>
        <span className="footer__build" title={build?.revision ?? undefined}>{buildLabel(build)}</span>
      </div>
    </footer>
  )
}
