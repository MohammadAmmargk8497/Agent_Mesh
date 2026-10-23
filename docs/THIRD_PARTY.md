# Third-party notices

## OpenHuddle dashboard

AgentMesh's dashboard adapts the layout and presentation of
[OpenHuddle](https://github.com/nika110/openhuddle), by nika110, licensed under the
MIT License. The adaptation is bundled inline in both
`cloudflare/src/dashboard.html` and
`server/src/agentmesh_server/dashboard.html`.

Adapted upstream files:

- `src/hub/pages/dashboard.html` — dashboard panels and three-column layout.
- `src/hub/pages/dashboard.js` — task cards, huddle discussions, master-prompt
  status, questions, team status and live-feed presentation.
- `src/hub/pages/dashboard.css` — dashboard layout and responsive panel patterns.
- `src/hub/pages/base.css` — shared control, focus and accessibility patterns.

The adaptation uses AgentMesh's palette, font stacks, branding, authentication
and coordination transcript. OpenHuddle's `site.css`, favicon, mascot and
documentation screenshots were consulted as visual references, not copied into
AgentMesh. No OpenHuddle hub/server implementation is included.

### OpenHuddle MIT notice

```text
MIT License

Copyright (c) 2026 nika110

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
