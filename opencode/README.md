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
hook API it declares in `peerDependencies`. It covers the plugin context's domains and
`mcp`/`skill.transform`, but not `session.hook` and `session.synthetic`, which the
plugin calls through `as any`; those still need a session on a real host (RT-6.20).
