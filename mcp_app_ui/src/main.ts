import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from '@modelcontextprotocol/ext-apps';
import { z } from 'zod';

interface Finding {
  file: string;
  line: number;
  severity: string;
  code: string;
  message: string;
}

interface SbomComponent {
  name: string;
  version: string;
  purl: string;
  license: string;
  policy: string;
}

interface RouteRow {
  method: string;
  path: string;
  framework: string;
  file: string;
}

interface WorkbenchState {
  version?: string;
  workspace: string;
  workspace_name: string;
  initial_tab?: string;
  docs: Record<string, string>;
  adrs: Array<{ path: string; content: string }>;
  security?: {
    files_scanned?: number;
    findings?: Finding[];
    solid_metrics?: Record<string, unknown>;
    notes?: string[];
  } | null;
  sbom?: {
    format?: string;
    total?: number;
    policy?: Record<string, number>;
    components?: SbomComponent[];
  } | null;
  diagram?: {
    files_count?: number;
    edges?: Record<string, string[]>;
    cycles?: string[][];
    routes?: RouteRow[];
    mermaid?: string;
  } | null;
  profiles?: {
    active?: string;
    fallback?: string;
    profiles?: Record<
      string,
      { provider: string; model: string; base_url?: string; mode?: string }
    >;
  };
  stacks?: string[];
}

const EDITABLE_DOCS = new Set([
  'docs/PLAN.md',
  'docs/ARCHITECTURE.md',
  'docs/CODING_STANDARDS.md',
  '.agent/TASKS.md',
]);

let state: WorkbenchState = {
  workspace: '',
  workspace_name: 'workspace',
  docs: {},
  adrs: [],
  stacks: ['adk-python', 'fastapi-service', 'adk-typescript', 'go-a2a', 'java-spring', 'mcp-app'],
};

let activeTab = 'overview';
let selectedDoc = 'docs/PLAN.md';
let severityFilter = 'ALL';
let sbomQuery = '';
let routeQuery = '';
let canMessage = false;
let canUpdateContext = false;

const app = new App({ name: 'ADK Coder Workbench', version: '1.0.0' }, {});

function esc(val: unknown): string {
  return String(val ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function showToast(kind: 'ok' | 'err', msg: string): void {
  const el = document.getElementById('toast');
  if (!el) return;
  el.className = `toast show ${kind}`;
  el.textContent = msg;
  window.setTimeout(() => {
    el.className = 'toast';
  }, 4500);
}

function mergePayload(payload: unknown): void {
  if (!payload || typeof payload !== 'object') return;
  const p = payload as Partial<WorkbenchState>;
  state = {
    ...state,
    ...p,
    docs: p.docs ? { ...state.docs, ...p.docs } : state.docs,
    adrs: p.adrs ?? state.adrs,
    stacks: p.stacks ?? state.stacks,
  };
  if (p.initial_tab) {
    activeTab = p.initial_tab;
  }
  const wsBadge = document.getElementById('wsBadge');
  if (wsBadge) wsBadge.textContent = state.workspace_name || 'workspace';
  const wsInput = document.getElementById('wsInput') as HTMLInputElement | null;
  if (wsInput && state.workspace && !wsInput.value) {
    wsInput.value = state.workspace;
  }
  render();
}

async function callTool(name: string, args: Record<string, unknown>): Promise<Record<string, unknown> | null> {
  const res = await app.callServerTool({ name, arguments: args });
  const sc = (res as { structuredContent?: Record<string, unknown> }).structuredContent;
  return sc ?? null;
}

async function openWorkspaceFile(relPath: string): Promise<void> {
  if (!state.workspace || !relPath) return;
  const normalized = `${state.workspace.replace(/\/+$/, '')}/${relPath.replace(/^\/+/, '')}`;
  const fileUri = normalized.startsWith('/') ? `file://${normalized}` : `file:///${normalized}`;
  try {
    const resp = await app.request(
      {
        method: 'com.google.antigravity/toggle-aux-pane',
        params: { tabType: 'fileView', uri: fileUri },
      },
      z.object({ isError: z.boolean().optional() }).passthrough(),
    );
    if (resp?.isError) {
      showToast('err', `Could not open ${relPath} in side pane on this view.`);
    }
  } catch {
    showToast('err', `Host does not support side-pane file navigation for ${relPath}.`);
  }
}

async function sendChatPrompt(text: string): Promise<void> {
  if (!canMessage) return;
  try {
    await app.sendMessage({ role: 'user', content: [{ type: 'text', text }] });
    showToast('ok', 'Sent request to conversation.');
  } catch (err) {
    showToast('err', `Failed to message chat: ${String(err)}`);
  }
}

async function attachFindingContext(finding: Finding): Promise<void> {
  if (!canUpdateContext) return;
  try {
    await app.updateModelContext({
      content: [
        {
          type: 'text',
          text: `ADK Coder finding [${finding.severity}] ${finding.file}:${finding.line} (${finding.code}): ${finding.message}`,
        },
      ],
    });
    showToast('ok', `Attached ${finding.file}:${finding.line} to next message context.`);
  } catch (err) {
    showToast('err', `Context attach failed: ${String(err)}`);
  }
}

function renderOverview(root: HTMLElement): void {
  const docKeys = Object.keys(state.docs);
  if (docKeys.length && !docKeys.includes(selectedDoc)) {
    selectedDoc = docKeys[0];
  }
  const currentContent = state.docs[selectedDoc] ?? '';
  const isEditable = EDITABLE_DOCS.has(selectedDoc);

  root.innerHTML = `
    <div class="grid-2" style="margin-bottom:12px">
      <div class="metric"><div class="label">Source Files</div><div class="value">${state.diagram?.files_count ?? 0}</div></div>
      <div class="metric"><div class="label">Security / SOLID Findings</div><div class="value">${state.security?.findings?.length ?? 0}</div></div>
      <div class="metric"><div class="label">SBOM Components</div><div class="value">${state.sbom?.total ?? 0}</div></div>
      <div class="metric"><div class="label">API Routes</div><div class="value">${state.diagram?.routes?.length ?? 0}</div></div>
    </div>
    <div class="card">
      <div class="card-head">
        <h3>Workspace Engineering Docs</h3>
        <div class="row">
          <select id="docSelect" style="width:220px">
            ${docKeys.map((k) => `<option value="${esc(k)}" ${k === selectedDoc ? 'selected' : ''}>${esc(k)}</option>`).join('')}
          </select>
          ${isEditable ? '<button id="saveDocBtn" class="btn primary" type="button">Save doc</button>' : '<span class="badge">Read-only report</span>'}
        </div>
      </div>
      ${
        isEditable
          ? `<textarea id="docEditor" placeholder="Document is empty — start writing...">${esc(currentContent)}</textarea>`
          : `<pre>${esc(currentContent || '(No report generated yet. Run /debug, /pr-review, or /analyze.)')}</pre>`
      }
    </div>
    ${
      state.adrs.length
        ? `<div class="card"><div class="card-head"><h3>Architecture Decision Records (${state.adrs.length})</h3></div>
           ${state.adrs.map((a) => `<details style="margin-bottom:8px"><summary><code>${esc(a.path)}</code></summary><pre>${esc(a.content)}</pre></details>`).join('')}
          </div>`
        : ''
    }
  `;

  document.getElementById('docSelect')?.addEventListener('change', (e) => {
    selectedDoc = (e.target as HTMLSelectElement).value;
    render();
  });

  document.getElementById('saveDocBtn')?.addEventListener('click', async () => {
    const editor = document.getElementById('docEditor') as HTMLTextAreaElement | null;
    if (!editor) return;
    try {
      await callTool('app_save_doc', {
        workspace: state.workspace,
        rel_path: selectedDoc,
        content: editor.value,
      });
      state.docs[selectedDoc] = editor.value;
      showToast('ok', `Saved ${selectedDoc}.`);
    } catch (err) {
      showToast('err', `Save failed: ${String(err)}`);
    }
  });
}

function renderSecurity(root: HTMLElement): void {
  const findings = state.security?.findings ?? [];
  const filtered =
    severityFilter === 'ALL'
      ? findings
      : findings.filter((f) => f.severity.toUpperCase() === severityFilter);

  root.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>Security (SAST / Secrets) &amp; SOLID Checks</h3>
        <div class="row">
          <select id="sevFilter" style="width:140px">
            ${['ALL', 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW']
              .map((s) => `<option value="${s}" ${s === severityFilter ? 'selected' : ''}>${s}</option>`)
              .join('')}
          </select>
          <button id="runSecBtn" class="btn primary" type="button">Run Scan</button>
        </div>
      </div>
      ${
        filtered.length
          ? `<table>
              <thead><tr><th>Severity</th><th>Location</th><th>Rule</th><th>Description</th><th>Actions</th></tr></thead>
              <tbody>
                ${filtered
                  .map(
                    (f, idx) => `<tr>
                    <td><span class="badge ${f.severity === 'CRITICAL' || f.severity === 'HIGH' ? 'danger' : f.severity === 'MEDIUM' ? 'warn' : ''}">${esc(f.severity)}</span></td>
                    <td><code>${esc(f.file)}:${f.line}</code></td>
                    <td><code>${esc(f.code)}</code></td>
                    <td>${esc(f.message)}</td>
                    <td class="row">
                      <button class="btn" data-open-file="${esc(f.file)}" type="button">Open</button>
                      ${canMessage ? `<button class="btn tonal" data-fix-idx="${idx}" type="button">Fix in Chat</button>` : ''}
                      ${canUpdateContext ? `<button class="btn" data-ctx-idx="${idx}" type="button">Attach</button>` : ''}
                    </td>
                  </tr>`,
                  )
                  .join('')}
              </tbody>
            </table>`
          : '<p class="muted">No matching findings. Click <strong>Run Scan</strong> to inspect the workspace.</p>'
      }
    </div>
  `;

  document.getElementById('sevFilter')?.addEventListener('change', (e) => {
    severityFilter = (e.target as HTMLSelectElement).value;
    render();
  });

  document.getElementById('runSecBtn')?.addEventListener('click', async () => {
    try {
      const data = await callTool('app_run_security', { workspace: state.workspace, target: '.' });
      if (data) mergePayload(data);
      showToast('ok', 'Security & SOLID scan finished.');
    } catch (err) {
      showToast('err', `Scan failed: ${String(err)}`);
    }
  });

  root.querySelectorAll<HTMLButtonElement>('[data-open-file]').forEach((btn) => {
    btn.addEventListener('click', () => openWorkspaceFile(btn.dataset.openFile || ''));
  });
  root.querySelectorAll<HTMLButtonElement>('[data-fix-idx]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const f = filtered[Number(btn.dataset.fixIdx)];
      if (f) {
        sendChatPrompt(
          `Please fix this ${f.severity} finding in ${f.file}:${f.line} (${f.code}): ${f.message}`,
        );
      }
    });
  });
  root.querySelectorAll<HTMLButtonElement>('[data-ctx-idx]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const f = filtered[Number(btn.dataset.ctxIdx)];
      if (f) attachFindingContext(f);
    });
  });
}

function renderSbom(root: HTMLElement): void {
  const comps = state.sbom?.components ?? [];
  const q = sbomQuery.trim().toLowerCase();
  const filtered = q
    ? comps.filter(
        (c) =>
          c.name.toLowerCase().includes(q) ||
          c.license.toLowerCase().includes(q) ||
          c.policy.toLowerCase().includes(q),
      )
    : comps;

  root.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>SBOM &amp; License Policy (${state.sbom?.format ?? 'CycloneDX'})</h3>
        <div class="row">
          <input id="sbomSearch" style="width:200px" value="${esc(sbomQuery)}" placeholder="Filter packages/licenses..." />
          <button id="runCdxBtn" class="btn primary" type="button">Generate CycloneDX</button>
          <button id="runSpdxBtn" class="btn tonal" type="button">SPDX</button>
        </div>
      </div>
      ${
        filtered.length
          ? `<table>
              <thead><tr><th>Package</th><th>Version</th><th>License</th><th>Policy</th></tr></thead>
              <tbody>
                ${filtered
                  .slice(0, 120)
                  .map(
                    (c) => `<tr>
                    <td><code>${esc(c.name)}</code></td>
                    <td>${esc(c.version)}</td>
                    <td>${esc(c.license)}</td>
                    <td><span class="badge ${c.policy === 'PERMISSIVE' ? 'ok' : c.policy === 'STRONG_COPYLEFT' ? 'danger' : 'warn'}">${esc(c.policy)}</span></td>
                  </tr>`,
                  )
                  .join('')}
              </tbody>
            </table>`
          : '<p class="muted">No SBOM loaded yet. Click <strong>Generate CycloneDX</strong>.</p>'
      }
    </div>
  `;

  document.getElementById('sbomSearch')?.addEventListener('input', (e) => {
    sbomQuery = (e.target as HTMLInputElement).value;
    render();
  });

  const runFmt = async (fmt: string) => {
    try {
      const data = await callTool('app_run_sbom', { workspace: state.workspace, fmt });
      if (data) mergePayload(data);
      showToast('ok', `Generated ${fmt.toUpperCase()} SBOM.`);
    } catch (err) {
      showToast('err', `SBOM failed: ${String(err)}`);
    }
  };
  document.getElementById('runCdxBtn')?.addEventListener('click', () => runFmt('cyclonedx'));
  document.getElementById('runSpdxBtn')?.addEventListener('click', () => runFmt('spdx'));
}

function renderDiagram(root: HTMLElement): void {
  const diag = state.diagram;
  const routes = diag?.routes ?? [];
  const cycles = diag?.cycles ?? [];
  const q = routeQuery.trim().toLowerCase();
  const filteredRoutes = q
    ? routes.filter(
        (r) =>
          r.path.toLowerCase().includes(q) ||
          r.file.toLowerCase().includes(q) ||
          r.method.toLowerCase().includes(q),
      )
    : routes;

  root.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>Circular Dependencies (${cycles.length})</h3>
        <button id="runDiagBtn" class="btn primary" type="button">Refresh Diagram</button>
      </div>
      ${
        cycles.length
          ? cycles.map((c) => `<div><span class="badge danger">CYCLE</span> <code>${esc(c.join(' -> '))}</code></div>`).join('')
          : '<span class="badge ok">No circular dependencies detected</span>'
      }
    </div>
    <div class="card">
      <div class="card-head">
        <h3>Detected HTTP API Routes (${filteredRoutes.length})</h3>
        <input id="routeSearch" style="width:200px" value="${esc(routeQuery)}" placeholder="Filter routes..." />
      </div>
      ${
        filteredRoutes.length
          ? `<table>
              <thead><tr><th>Method</th><th>Path</th><th>Framework</th><th>File</th></tr></thead>
              <tbody>
                ${filteredRoutes
                  .map(
                    (r) => `<tr>
                    <td><span class="badge">${esc(r.method)}</span></td>
                    <td><code>${esc(r.path)}</code></td>
                    <td>${esc(r.framework)}</td>
                    <td><button class="btn" data-open-route="${esc(r.file)}" type="button">${esc(r.file)}</button></td>
                  </tr>`,
                  )
                  .join('')}
              </tbody>
            </table>`
          : '<p class="muted">No HTTP endpoints detected.</p>'
      }
    </div>
    <div class="card">
      <div class="card-head"><h3>Mermaid Dependency Graph</h3></div>
      <pre>${esc(diag?.mermaid || '(No diagram data)')}</pre>
    </div>
  `;

  document.getElementById('runDiagBtn')?.addEventListener('click', async () => {
    try {
      const data = await callTool('app_run_diagram', { workspace: state.workspace, target: '.' });
      if (data) mergePayload(data);
      showToast('ok', 'Dependency diagram refreshed.');
    } catch (err) {
      showToast('err', `Diagram failed: ${String(err)}`);
    }
  });
  document.getElementById('routeSearch')?.addEventListener('input', (e) => {
    routeQuery = (e.target as HTMLInputElement).value;
    render();
  });
  root.querySelectorAll<HTMLButtonElement>('[data-open-route]').forEach((btn) => {
    btn.addEventListener('click', () => openWorkspaceFile(btn.dataset.openRoute || ''));
  });
}

function renderProfiles(root: HTMLElement): void {
  const profStore = state.profiles ?? { active: '', fallback: '', profiles: {} };
  const entries = Object.entries(profStore.profiles ?? {}).sort(([a], [b]) => a.localeCompare(b));

  root.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>Saved LLM Profiles &amp; Auto-Fallback</h3>
        <span class="badge ok">Zero Google API required</span>
      </div>
      ${
        entries.length
          ? `<table>
              <thead><tr><th>Profile</th><th>Model</th><th>Mode</th><th>Actions</th></tr></thead>
              <tbody>
                ${entries
                  .map(([name, p]) => {
                    const isAct = name === profStore.active;
                    const isFb = name === profStore.fallback;
                    return `<tr>
                      <td>
                        <strong>${esc(name)}</strong>
                        ${isAct ? '<span class="badge">Active</span>' : ''}
                        ${isFb ? '<span class="badge ok">Fallback</span>' : ''}
                      </td>
                      <td><code>${esc(p.model)}</code><div class="muted">${esc(p.provider)} ${esc(p.base_url || '')}</div></td>
                      <td>${esc(p.mode || 'ask')}</td>
                      <td class="row">
                        <button class="btn tonal" data-use-prof="${esc(name)}" type="button">Use</button>
                        <button class="btn" data-fb-prof="${esc(name)}" type="button">Set Fallback</button>
                      </td>
                    </tr>`;
                  })
                  .join('')}
              </tbody>
            </table>`
          : '<p class="muted">No saved profiles yet. Add profiles via <code>adk-coder --config</code> or <code>/profile save &lt;name&gt;</code>.</p>'
      }
    </div>
  `;

  root.querySelectorAll<HTMLButtonElement>('[data-use-prof]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        const data = await callTool('app_use_profile', {
          name: btn.dataset.useProf || '',
          workspace: state.workspace,
        });
        if (data) mergePayload(data);
        showToast('ok', `Switched active profile to ${btn.dataset.useProf}.`);
      } catch (err) {
        showToast('err', `Switch failed: ${String(err)}`);
      }
    });
  });

  root.querySelectorAll<HTMLButtonElement>('[data-fb-prof]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        const data = await callTool('app_set_fallback_profile', {
          name: btn.dataset.fbProf || '',
        });
        if (data) mergePayload(data);
        showToast('ok', `Set fallback profile to ${btn.dataset.fbProf}.`);
      } catch (err) {
        showToast('err', `Fallback update failed: ${String(err)}`);
      }
    });
  });
}

function renderScaffold(root: HTMLElement): void {
  const stacks = state.stacks ?? [
    'adk-python',
    'fastapi-service',
    'adk-typescript',
    'go-a2a',
    'java-spring',
    'mcp-app',
  ];

  root.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>Enterprise Scaffold Studio</h3>
        <span class="badge">SOLID + SDLC Docs + Security/SBOM CI</span>
      </div>
      <div class="grid-2" style="margin-bottom:10px">
        <div>
          <label class="muted">Stack Template</label>
          <select id="scafTemplate">
            ${stacks.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join('')}
          </select>
        </div>
        <div>
          <label class="muted">Project Name</label>
          <input id="scafName" placeholder="e.g. billing-service" />
        </div>
        <div>
          <label class="muted">Parent Directory (optional, relative to workspace)</label>
          <input id="scafParent" placeholder="." />
        </div>
        <div class="row" style="padding-top:18px">
          <label class="row"><input id="scafMinimal" type="checkbox" style="width:auto" /> Minimal starter (skip enterprise SDLC docs)</label>
        </div>
      </div>
      <button id="runScafBtn" class="btn primary" type="button">Scaffold Project</button>
    </div>
  `;

  document.getElementById('runScafBtn')?.addEventListener('click', async () => {
    const template = (document.getElementById('scafTemplate') as HTMLSelectElement).value;
    const name = (document.getElementById('scafName') as HTMLInputElement).value.trim();
    const parent = (document.getElementById('scafParent') as HTMLInputElement).value.trim();
    const minimal = (document.getElementById('scafMinimal') as HTMLInputElement).checked;
    if (!name) {
      showToast('err', 'Enter a project name.');
      return;
    }
    try {
      const data = await callTool('app_scaffold_project', {
        workspace: state.workspace,
        template,
        name,
        parent,
        minimal,
      });
      showToast('ok', String(data?.message || `Scaffolded ${name}.`));
    } catch (err) {
      showToast('err', `Scaffold failed: ${String(err)}`);
    }
  });
}

function render(): void {
  document.querySelectorAll<HTMLButtonElement>('.tab-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.tab === activeTab);
  });
  const view = document.getElementById('view');
  if (!view) return;
  if (activeTab === 'security') renderSecurity(view);
  else if (activeTab === 'sbom') renderSbom(view);
  else if (activeTab === 'diagram') renderDiagram(view);
  else if (activeTab === 'profiles') renderProfiles(view);
  else if (activeTab === 'scaffold') renderScaffold(view);
  else renderOverview(view);
}

async function refreshSnapshot(): Promise<void> {
  const wsInput = document.getElementById('wsInput') as HTMLInputElement | null;
  const ws = wsInput?.value.trim() || state.workspace;
  try {
    const data = await callTool('app_get_workspace_snapshot', { workspace: ws });
    if (data) mergePayload(data);
    showToast('ok', 'Workspace snapshot refreshed.');
  } catch (err) {
    showToast('err', `Refresh failed: ${String(err)}`);
  }
}

document.getElementById('tabBar')?.addEventListener('click', (e) => {
  const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('.tab-btn');
  if (!btn?.dataset.tab) return;
  activeTab = btn.dataset.tab;
  render();
});

document.getElementById('refreshBtn')?.addEventListener('click', () => {
  refreshSnapshot();
});

// Register all MCP Apps SDK handlers BEFORE connect()
app.ontoolinput = ({ arguments: args }) => {
  if (args && typeof args === 'object') {
    const tab = (args as { tab?: string }).tab;
    if (tab) activeTab = tab;
  }
};

app.ontoolresult = (result) => {
  const sc = (result as { structuredContent?: unknown }).structuredContent;
  if (sc) mergePayload(sc);
};

app.onhostcontextchanged = (ctx) => {
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
};

async function boot(): Promise<void> {
  render();
  await app.connect();
  const ctx = app.getHostContext();
  if (ctx?.theme) applyDocumentTheme(ctx.theme);
  if (ctx?.styles?.variables) applyHostStyleVariables(ctx.styles.variables);

  const caps = app.getHostCapabilities() as Record<string, unknown> | undefined;
  canMessage = Boolean(caps?.message);
  canUpdateContext = Boolean(caps?.updateModelContext);
  render();
}

boot().catch((err) => {
  showToast('err', `MCP App connect failed: ${String(err)}`);
});
