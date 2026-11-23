# OpenCode plugin

The AI agent bus (chat: Mattermost) as an OpenCode plugin (plugin API V2, OpenCode 2.x; OpenCode
1.x is not supported). `server.ts` is the entry OpenCode loads for a `plugins` entry
naming this directory. Install and options: [adapters/opencode.md](../adapters/opencode.md);
behaviour: [arc42 §6.20](../docs/arc42/06-runtime-view.md#620-opencode-turn-cycle-through-its-plugin).

Checks (Node 22.18+ or Bun):

```bash
npm --prefix opencode install     # the API types and typescript
npm --prefix opencode run typecheck   # the plugin against @opencode/plugin's own types
npm --prefix opencode test            # a fake hook API and a fake OpenCode plugin context
```

The typecheck is what keeps "tested with" honest: it compiles the plugin against the
hook API in `peerDependencies` (`>=2.1.2 <2.5`). It covers the plugin context's shape
and the arguments of the hooks this file calls **without** a cast — `mcp.transform`,
`skill.transform`, `session.hook("prompt", ...)`.

What it does **not** cover, because the value is cast (or the callback's parameter is
`any`), is the *inside* of those calls: in the two `transform` callbacks `editor` is
`any`, so `editor.get`, `editor.list`, `editor.set` and `editor.add` are unchecked
(`plugin/mattermost-agent-bus.ts:649,654`); likewise `session.synthetic(...)` and the
`prompt` hook's event object. An API change there passes the typecheck and fails in a
session, so it needs a run on a real host (RT-6.20).
