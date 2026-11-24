"""Standalone Local Browser Workbench (`adk-coder --ui`) — Google/Gmail Material 3 theme.

Works in Chrome/Edge/Safari on macOS, Linux, and Windows with zero Antigravity and zero
Google API keys required. Unifies Docs, Security/SOLID, SBOM, Architecture/API Diagrams,
Model Settings & Profiles, Scaffold Studio, and MCP status in one local web app.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config_ui import (
    _http_post_json,
    default_env_path,
)
from .config_ui import (
    dispatch_request as dispatch_config_request,
)
from .diagram import analyze_structure, render_diagram_markdown
from .harness import AgentHarness
from .mcp_plugin import default_plugin_dir, install_mcp_plugin
from .mcp_server import (
    ALLOWED_EDITABLE_DOCS,
    _collect_snapshot,
    _compact_sbom,
    _resolve_workspace,
)
from .sbom import build_cyclonedx, build_spdx, discover_components
from .scaffolder import scaffold
from .scrubber import scrub
from .security import security_scan
from .tools import atomic_write

WORKBENCH_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>ADK Coder — Workbench</title>
<style>
  :root {
    --bg: #f6f8fc;
    --surface: #ffffff;
    --surface-hover: #edf2fa;
    --pill-active: #d3e3fd;
    --pill-active-text: #041e49;
    --border: #e0e3eb;
    --border-focus: #0b57d0;
    --text: #1f1f1f;
    --muted: #5f6368;
    --primary: #0b57d0;
    --primary-hover: #0842a0;
    --tonal-bg: #c2e7ff;
    --tonal-text: #001d35;
    --ok-bg: #e6f4ea;
    --ok-text: #137333;
    --warn-bg: #fef7e0;
    --warn-text: #b06000;
    --err-bg: #fce8e6;
    --err-text: #b3261e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: "Google Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px;
    line-height: 1.45;
  }
  .topbar {
    height: 60px;
    padding: 0 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 20;
  }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 16px; }
  .logo-dot {
    width: 30px; height: 30px; border-radius: 10px;
    background: var(--primary); color: #fff;
    display: inline-flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 14px;
  }
  .pill {
    display: inline-flex; align-items: center; padding: 3px 10px;
    border-radius: 999px; font-size: 12px; font-weight: 500;
    background: var(--pill-active); color: var(--pill-active-text);
  }
  .pill.ok { background: var(--ok-bg); color: var(--ok-text); }
  .pill.warn { background: var(--warn-bg); color: var(--warn-text); }
  .pill.err { background: var(--err-bg); color: var(--err-text); }
  .shell {
    max-width: 1200px;
    margin: 18px auto 40px;
    padding: 0 18px;
    display: grid;
    grid-template-columns: 220px 1fr;
    gap: 18px;
  }
  @media (max-width: 820px) { .shell { grid-template-columns: 1fr; } }
  .rail { display: flex; flex-direction: column; gap: 4px; }
  .rail-title {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--muted); padding: 6px 14px;
  }
  .nav-btn {
    border: none; background: transparent; color: var(--text);
    font: inherit; font-weight: 500; text-align: left;
    padding: 10px 14px; border-radius: 999px; cursor: pointer;
    display: flex; align-items: center; justify-content: space-between;
  }
  .nav-btn:hover { background: var(--surface-hover); }
  .nav-btn.active { background: var(--pill-active); color: var(--pill-active-text); font-weight: 600; }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 18px;
    padding: 18px 20px;
    margin-bottom: 14px;
  }
  .card-head {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; margin-bottom: 12px; flex-wrap: wrap;
  }
  .card-head h2 { margin: 0; font-size: 16px; font-weight: 600; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .grid-4 { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin-bottom: 14px; }
  .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
  .metric {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 14px; padding: 12px 14px;
  }
  .metric .k { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .metric .v { font-size: 22px; font-weight: 600; margin-top: 2px; }
  label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; font-weight: 500; }
  input, select, textarea {
    font: inherit; color: var(--text); background: #fff;
    border: 1px solid var(--border); border-radius: 10px;
    padding: 8px 11px; width: 100%;
  }
  input:focus, select:focus, textarea:focus { outline: 2px solid var(--border-focus); border-color: transparent; }
  textarea { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; min-height: 260px; }
  .btn {
    font: inherit; font-weight: 500; padding: 8px 16px;
    border-radius: 999px; border: 1px solid var(--border);
    background: #fff; color: var(--text); cursor: pointer;
  }
  .btn:hover { background: var(--surface-hover); }
  .btn.primary { background: var(--primary); color: #fff; border-color: transparent; }
  .btn.primary:hover { background: var(--primary-hover); }
  .btn.tonal { background: var(--tonal-bg); color: var(--tonal-text); border-color: transparent; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-size: 11px; text-transform: uppercase; font-weight: 600; }
  code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
  pre { background: var(--bg); border: 1px solid var(--border); border-radius: 12px; padding: 12px; overflow-x: auto; white-space: pre-wrap; }
  .toast { display: none; padding: 10px 14px; border-radius: 12px; margin-bottom: 12px; font-size: 13px; }
  .toast.show { display: block; }
  .toast.ok { background: var(--ok-bg); color: var(--ok-text); }
  .toast.err { background: var(--err-bg); color: var(--err-text); }
  .muted { color: var(--muted); font-size: 13px; }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <span class="logo-dot">A</span>
    <span>ADK Coder Workbench</span>
    <span id="wsPill" class="pill">Loading...</span>
  </div>
  <div class="row">
    <input id="wsInput" style="width:320px" placeholder="Workspace directory path..." />
    <button id="loadWsBtn" class="btn tonal" type="button">Load</button>
    <a href="/settings" class="btn" style="text-decoration:none">Settings</a>
  </div>
</header>

<div class="shell">
  <nav class="rail" id="rail">
    <div class="rail-title">Workbench</div>
    <button class="nav-btn active" data-tab="docs" type="button"><span>Plan &amp; Docs</span></button>
    <button class="nav-btn" data-tab="security" type="button"><span>Security &amp; SOLID</span><span id="secCount" class="pill">0</span></button>
    <button class="nav-btn" data-tab="sbom" type="button"><span>SBOM &amp; Licenses</span><span id="sbomCount" class="pill">0</span></button>
    <button class="nav-btn" data-tab="diagram" type="button"><span>Diagram &amp; APIs</span><span id="routeCount" class="pill">0</span></button>
    <button class="nav-btn" data-tab="profiles" type="button"><span>Profiles &amp; Model</span></button>
    <button class="nav-btn" data-tab="scaffold" type="button"><span>Scaffold Studio</span></button>
    <button class="nav-btn" data-tab="mcp" type="button"><span>MCP Plugin</span></button>
  </nav>
  <main>
    <div id="toast" class="toast"></div>
    <div id="view"></div>
  </main>
</div>

<script>
let snap = { workspace: '', workspace_name: 'workspace', docs: {}, adrs: [], stacks: [], profiles: { active: '', fallback: '', profiles: {} } };
let activeTab = 'docs';
let selectedDoc = 'docs/PLAN.md';
let sevFilter = 'ALL';
let sbomQuery = '';
let routeQuery = '';
const EDITABLE = new Set(['docs/PLAN.md', 'docs/ARCHITECTURE.md', 'docs/CODING_STANDARDS.md', '.agent/TASKS.md']);

function esc(v) {
  return String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
}
function toast(kind, msg) {
  const el = document.getElementById('toast');
  el.className = 'toast show ' + kind;
  el.textContent = msg;
  setTimeout(() => { el.className = 'toast'; }, 4500);
}
async function postJson(url, body) {
  const res = await fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  const data = await res.json();
  if (!res.ok || data.ok === false) throw new Error(data.error || data.message || ('HTTP ' + res.status));
  return data;
}
async function loadSnapshot(wsOverride) {
  const ws = (wsOverride ?? document.getElementById('wsInput').value.trim()) || snap.workspace || '';
  const q = ws ? ('?workspace=' + encodeURIComponent(ws)) : '';
  const res = await fetch('/api/workbench/snapshot' + q);
  const data = await res.json();
  if (!res.ok) { toast('err', data.error || 'Failed to load workspace'); return; }
  snap = data;
  document.getElementById('wsPill').textContent = snap.workspace_name + ' · v' + (snap.version || '1.0.0');
  document.getElementById('wsInput').value = snap.workspace || '';
  document.getElementById('secCount').textContent = (snap.security?.findings || []).length;
  document.getElementById('sbomCount').textContent = snap.sbom?.total || 0;
  document.getElementById('routeCount').textContent = (snap.diagram?.routes || []).length;
  render();
}

function renderDocs(el) {
  const keys = Object.keys(snap.docs || {});
  if (keys.length && !keys.includes(selectedDoc)) selectedDoc = keys[0];
  const content = (snap.docs || {})[selectedDoc] || '';
  const editable = EDITABLE.has(selectedDoc);
  el.innerHTML = `
    <div class="grid-4">
      <div class="metric"><div class="k">Source Files</div><div class="v">${snap.diagram?.files_count || 0}</div></div>
      <div class="metric"><div class="k">Security / SOLID</div><div class="v">${(snap.security?.findings || []).length}</div></div>
      <div class="metric"><div class="k">Dependencies</div><div class="v">${snap.sbom?.total || 0}</div></div>
      <div class="metric"><div class="k">API Routes</div><div class="v">${(snap.diagram?.routes || []).length}</div></div>
    </div>
    <div class="card">
      <div class="card-head">
        <h2>Workspace Engineering Docs</h2>
        <div class="row">
          <select id="docSel" style="width:230px">${keys.map(k => `<option value="${esc(k)}" ${k===selectedDoc?'selected':''}>${esc(k)}</option>`).join('')}</select>
          ${editable ? '<button id="saveDocBtn" class="btn primary" type="button">Save doc</button>' : '<span class="pill">Read-only report</span>'}
        </div>
      </div>
      ${editable ? `<textarea id="docEd">${esc(content)}</textarea>` : `<pre>${esc(content || '(No report generated yet. Run /debug, /pr-review, or /analyze in adk-coder.)')}</pre>`}
    </div>`;
  document.getElementById('docSel').onchange = (e) => { selectedDoc = e.target.value; render(); };
  const btn = document.getElementById('saveDocBtn');
  if (btn) btn.onclick = async () => {
    try {
      const val = document.getElementById('docEd').value;
      await postJson('/api/workbench/save-doc', { workspace: snap.workspace, rel_path: selectedDoc, content: val });
      snap.docs[selectedDoc] = val;
      toast('ok', 'Saved ' + selectedDoc);
    } catch (e) { toast('err', String(e)); }
  };
}

function renderSecurity(el) {
  const list = snap.security?.findings || [];
  const rows = sevFilter === 'ALL' ? list : list.filter(f => (f.severity||'').toUpperCase() === sevFilter);
  el.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h2>Security (SAST / Secrets) &amp; SOLID Audit</h2>
        <div class="row">
          <select id="sevSel" style="width:140px">${['ALL','CRITICAL','HIGH','MEDIUM','LOW'].map(s => `<option ${s===sevFilter?'selected':''}>${s}</option>`).join('')}</select>
          <button id="scanBtn" class="btn primary" type="button">Run Scan</button>
        </div>
      </div>
      ${rows.length ? `<table><thead><tr><th>Severity</th><th>Location</th><th>Rule</th><th>Description</th></tr></thead><tbody>
        ${rows.map(f => `<tr><td><span class="pill ${(f.severity==='CRITICAL'||f.severity==='HIGH')?'err':f.severity==='MEDIUM'?'warn':''}">${esc(f.severity)}</span></td><td><code>${esc(f.file)}:${f.line}</code></td><td><code>${esc(f.code)}</code></td><td>${esc(f.message)}</td></tr>`).join('')}
      </tbody></table>` : '<p class="muted">No findings loaded. Click <strong>Run Scan</strong> to scan offline.</p>'}
    </div>`;
  document.getElementById('sevSel').onchange = (e) => { sevFilter = e.target.value; render(); };
  document.getElementById('scanBtn').onclick = async () => {
    try {
      const r = await postJson('/api/workbench/security', { workspace: snap.workspace, target: '.' });
      snap.security = r.security;
      document.getElementById('secCount').textContent = (snap.security?.findings || []).length;
      toast('ok', 'Security & SOLID scan completed.');
      render();
    } catch (e) { toast('err', String(e)); }
  };
}

function renderSbom(el) {
  const comps = snap.sbom?.components || [];
  const q = sbomQuery.trim().toLowerCase();
  const rows = q ? comps.filter(c => (c.name+' '+c.license+' '+c.policy).toLowerCase().includes(q)) : comps;
  el.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h2>SBOM &amp; License Policy (${esc(snap.sbom?.format || 'CycloneDX')})</h2>
        <div class="row">
          <input id="sbomQ" style="width:200px" value="${esc(sbomQuery)}" placeholder="Filter packages/licenses..." />
          <button id="cdxBtn" class="btn primary" type="button">Generate CycloneDX</button>
          <button id="spdxBtn" class="btn tonal" type="button">SPDX</button>
        </div>
      </div>
      ${rows.length ? `<table><thead><tr><th>Package</th><th>Version</th><th>License</th><th>Policy</th></tr></thead><tbody>
        ${rows.slice(0,150).map(c => `<tr><td><code>${esc(c.name)}</code></td><td>${esc(c.version)}</td><td>${esc(c.license)}</td><td><span class="pill ${c.policy==='PERMISSIVE'?'ok':c.policy==='STRONG_COPYLEFT'?'err':'warn'}">${esc(c.policy)}</span></td></tr>`).join('')}
      </tbody></table>` : '<p class="muted">Click <strong>Generate CycloneDX</strong> to scan manifests and dependencies.</p>'}
    </div>`;
  document.getElementById('sbomQ').oninput = (e) => { sbomQuery = e.target.value; render(); };
  const run = async (fmt) => {
    try {
      const r = await postJson('/api/workbench/sbom', { workspace: snap.workspace, fmt });
      snap.sbom = r.sbom;
      document.getElementById('sbomCount').textContent = snap.sbom?.total || 0;
      toast('ok', 'Generated ' + fmt.toUpperCase() + ' SBOM.');
      render();
    } catch (e) { toast('err', String(e)); }
  };
  document.getElementById('cdxBtn').onclick = () => run('cyclonedx');
  document.getElementById('spdxBtn').onclick = () => run('spdx');
}

function renderDiagram(el) {
  const d = snap.diagram || {};
  const cycles = d.cycles || [];
  const routes = d.routes || [];
  const q = routeQuery.trim().toLowerCase();
  const fRoutes = q ? routes.filter(r => (r.method+' '+r.path+' '+r.file).toLowerCase().includes(q)) : routes;
  el.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Circular Dependencies (${cycles.length})</h2><button id="diagBtn" class="btn primary" type="button">Refresh Diagram</button></div>
      ${cycles.length ? cycles.map(c => `<div><span class="pill err">CYCLE</span> <code>${esc(c.join(' -> '))}</code></div>`).join('') : '<span class="pill ok">No circular dependencies</span>'}
    </div>
    <div class="card">
      <div class="card-head"><h2>HTTP API Routes (${fRoutes.length})</h2><input id="rQ" style="width:220px" value="${esc(routeQuery)}" placeholder="Filter routes..." /></div>
      ${fRoutes.length ? `<table><thead><tr><th>Method</th><th>Path</th><th>Framework</th><th>File</th></tr></thead><tbody>
        ${fRoutes.map(r => `<tr><td><span class="pill">${esc(r.method)}</span></td><td><code>${esc(r.path)}</code></td><td>${esc(r.framework)}</td><td><code>${esc(r.file)}</code></td></tr>`).join('')}
      </tbody></table>` : '<p class="muted">No HTTP endpoints detected.</p>'}
    </div>
    <div class="card"><div class="card-head"><h2>Mermaid Source</h2></div><pre>${esc(d.mermaid || '')}</pre></div>`;
  document.getElementById('rQ').oninput = (e) => { routeQuery = e.target.value; render(); };
  document.getElementById('diagBtn').onclick = async () => {
    try {
      const r = await postJson('/api/workbench/diagram', { workspace: snap.workspace, target: '.' });
      snap.diagram = r.diagram;
      document.getElementById('routeCount').textContent = (snap.diagram?.routes || []).length;
      toast('ok', 'Diagram refreshed.');
      render();
    } catch (e) { toast('err', String(e)); }
  };
}

function renderProfiles(el) {
  const p = snap.profiles || { active: '', fallback: '', profiles: {} };
  const entries = Object.entries(p.profiles || {}).sort(([a],[b]) => a.localeCompare(b));
  el.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h2>Saved LLM Profiles &amp; Auto-Fallback</h2>
        <div class="row">
          <span class="pill ok">No Google API required</span>
          <a href="/settings" class="btn primary" style="text-decoration:none">Open Full Provider Settings</a>
        </div>
      </div>
      ${entries.length ? `<table><thead><tr><th>Profile</th><th>Model &amp; Endpoint</th><th>Mode</th><th>Actions</th></tr></thead><tbody>
        ${entries.map(([n, pr]) => `<tr>
          <td><strong>${esc(n)}</strong> ${n===p.active?'<span class="pill">Active</span>':''} ${n===p.fallback?'<span class="pill ok">Fallback</span>':''}</td>
          <td><code>${esc(pr.model)}</code><div class="muted">${esc(pr.provider)} ${esc(pr.base_url||'')}</div></td>
          <td>${esc(pr.mode||'ask')}</td>
          <td class="row">
            <button class="btn tonal" data-use="${esc(n)}" type="button">Use</button>
            <button class="btn" data-fb="${esc(n)}" type="button">Fallback</button>
          </td>
        </tr>`).join('')}
      </tbody></table>` : '<p class="muted">No saved profiles yet. Click <strong>Open Full Provider Settings</strong> to add one.</p>'}
    </div>`;
  el.querySelectorAll('[data-use]').forEach(b => b.onclick = async () => {
    try {
      await postJson('/api/profiles/use', { name: b.dataset.use, workspace: snap.workspace });
      toast('ok', 'Switched profile to ' + b.dataset.use);
      await loadSnapshot(snap.workspace);
    } catch (e) { toast('err', String(e)); }
  });
  el.querySelectorAll('[data-fb]').forEach(b => b.onclick = async () => {
    try {
      await postJson('/api/profiles/fallback', { name: b.dataset.fb });
      toast('ok', 'Fallback set to ' + b.dataset.fb);
      await loadSnapshot(snap.workspace);
    } catch (e) { toast('err', String(e)); }
  });
}

function renderScaffold(el) {
  const stacks = snap.stacks || ['adk-python','fastapi-service','adk-typescript','go-a2a','java-spring','mcp-app'];
  el.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Enterprise Scaffold Studio</h2><span class="pill">SOLID + SDLC Docs + Security/SBOM CI</span></div>
      <div class="grid-2" style="margin-bottom:12px">
        <div><label>Stack Template</label><select id="scTpl">${stacks.map(s => `<option>${esc(s)}</option>`).join('')}</select></div>
        <div><label>Project Name</label><input id="scName" placeholder="e.g. billing-service" /></div>
        <div><label>Parent Directory (optional)</label><input id="scPar" placeholder="." /></div>
        <div class="row" style="padding-top:20px"><label class="row"><input id="scMin" type="checkbox" style="width:auto" /> Minimal starter</label></div>
      </div>
      <button id="scBtn" class="btn primary" type="button">Scaffold Project</button>
    </div>`;
  document.getElementById('scBtn').onclick = async () => {
    const name = document.getElementById('scName').value.trim();
    if (!name) { toast('err', 'Enter a project name.'); return; }
    try {
      const r = await postJson('/api/workbench/scaffold', {
        workspace: snap.workspace,
        template: document.getElementById('scTpl').value,
        name,
        parent: document.getElementById('scPar').value.trim(),
        minimal: document.getElementById('scMin').checked,
      });
      toast('ok', r.message || ('Scaffolded ' + name));
    } catch (e) { toast('err', String(e)); }
  };
}

function renderMcp(el) {
  const m = snap.mcp_plugin || {};
  el.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h2>Antigravity MCP App Plugin (Optional)</h2>
        <span class="pill ${m.installed ? 'ok' : 'warn'}">${m.installed ? 'Installed' : 'Not installed'}</span>
      </div>
      <p class="muted">You do <strong>not</strong> need MCP or Antigravity to use this browser Workbench or the CLI. Installing the MCP plugin additionally embeds these tabs inside the Antigravity IDE side pane.</p>
      <p>Plugin path: <code>${esc(m.path || '')}</code></p>
      <button id="instMcpBtn" class="btn primary" type="button">Install / Refresh Antigravity Plugin</button>
    </div>`;
  document.getElementById('instMcpBtn').onclick = async () => {
    try {
      const r = await postJson('/api/workbench/install-mcp', {});
      toast('ok', 'Installed plugin at ' + r.path);
      await loadSnapshot(snap.workspace);
    } catch (e) { toast('err', String(e)); }
  };
}

function render() {
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === activeTab));
  const el = document.getElementById('view');
  if (activeTab === 'security') renderSecurity(el);
  else if (activeTab === 'sbom') renderSbom(el);
  else if (activeTab === 'diagram') renderDiagram(el);
  else if (activeTab === 'profiles') renderProfiles(el);
  else if (activeTab === 'scaffold') renderScaffold(el);
  else if (activeTab === 'mcp') renderMcp(el);
  else renderDocs(el);
}

document.getElementById('rail').onclick = (e) => {
  const b = e.target.closest('.nav-btn');
  if (!b) return;
  activeTab = b.dataset.tab;
  render();
};
document.getElementById('loadWsBtn').onclick = () => loadSnapshot();
loadSnapshot();
</script>
</body>
</html>
"""


def dispatch_workbench_request(
    method: str,
    raw_path: str,
    payload: dict[str, Any],
    env_path: Path,
    workspace: Path | None = None,
    *,
    poster=_http_post_json,
) -> tuple[int, str, bytes]:
    """Pure HTTP router for the standalone Browser Workbench (unit-testable without sockets)."""
    parsed = urlparse(raw_path)
    path = parsed.path
    qs = parse_qs(parsed.query)

    # Delegate settings page & config/profile APIs directly to config_ui
    if method == "GET" and path == "/settings":
        return dispatch_config_request("GET", "/", {}, env_path, workspace, poster=poster)
    if path.startswith("/api/") and not path.startswith("/api/workbench/"):
        return dispatch_config_request(method, path, payload, env_path, workspace, poster=poster)

    if method == "GET":
        if path in {"/", "/index.html"}:
            return HTTPStatus.OK, "text/html; charset=utf-8", WORKBENCH_HTML.encode("utf8")
        if path == "/api/workbench/snapshot":
            try:
                ws_arg = (qs.get("workspace") or [""])[0] or (str(workspace) if workspace else "")
                snap = _collect_snapshot(ws_arg)
                pdir = default_plugin_dir()
                snap["mcp_plugin"] = {
                    "installed": (pdir / "plugin.json").is_file(),
                    "path": str(pdir),
                }
                return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(snap).encode("utf8")
            except Exception as exc:  # noqa: BLE001
                return (
                    HTTPStatus.BAD_REQUEST,
                    "application/json; charset=utf-8",
                    json.dumps({"ok": False, "error": scrub(str(exc))}).encode("utf8"),
                )
        return HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", b'{"error":"Not found"}'

    if method == "POST":
        try:
            ws_arg = str(payload.get("workspace") or "") or (str(workspace) if workspace else "")
            if path == "/api/workbench/security":
                root = _resolve_workspace(ws_arg)
                h = AgentHarness(root, approve=lambda _: True)
                report = security_scan(h, str(payload.get("target") or "."))
                data = {"ok": True, "security": report}
            elif path == "/api/workbench/sbom":
                root = _resolve_workspace(ws_arg)
                fmt = "spdx" if str(payload.get("fmt") or "").lower() == "spdx" else "cyclonedx"
                comps = discover_components(root)
                doc = build_spdx(root, comps) if fmt == "spdx" else build_cyclonedx(root, comps)
                state_dir = root / ".agent"
                state_dir.mkdir(mode=0o700, exist_ok=True)
                atomic_write(
                    state_dir / ("sbom.spdx.json" if fmt == "spdx" else "sbom.cdx.json"),
                    json.dumps(doc, indent=2),
                    mode=0o600,
                )
                data = {"ok": True, "sbom": _compact_sbom(doc)}
            elif path == "/api/workbench/diagram":
                root = _resolve_workspace(ws_arg)
                target = str(payload.get("target") or ".")
                d = analyze_structure(root, target)
                data = {
                    "ok": True,
                    "diagram": {
                        "files_count": len(d["files"]),
                        "edges": dict(list(d["edges"].items())[:50]),
                        "cycles": d["cycles"][:25],
                        "routes": d["routes"][:100],
                        "mermaid": render_diagram_markdown(root, target, d)[:14000],
                    },
                }
            elif path == "/api/workbench/save-doc":
                root = _resolve_workspace(ws_arg)
                rel = str(payload.get("rel_path") or "").replace("\\", "/").strip().lstrip("./")
                if rel not in ALLOWED_EDITABLE_DOCS:
                    raise ValueError(f"Document not writable: {rel}")
                target = (root / rel).resolve()
                target.relative_to(root.resolve())
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, str(payload.get("content") or ""))
                data = {"ok": True, "path": rel}
            elif path == "/api/workbench/scaffold":
                root = _resolve_workspace(ws_arg)
                h = AgentHarness(root, approve=lambda _: True)
                name = str(payload.get("name") or "").strip()
                tpl = str(payload.get("template") or "fastapi-service").strip()
                parent = str(payload.get("parent") or "").strip()
                cli_args = [tpl, name]
                if parent:
                    cli_args.append(parent)
                if payload.get("minimal"):
                    cli_args.append("--minimal")
                msg = scaffold(h, cli_args)
                data = {"ok": True, "message": msg}
            elif path == "/api/workbench/install-mcp":
                pdir = install_mcp_plugin()
                data = {"ok": True, "path": str(pdir)}
            else:
                return HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", b'{"error":"Not found"}'
            return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(data).encode("utf8")
        except Exception as exc:  # noqa: BLE001
            return (
                HTTPStatus.BAD_REQUEST,
                "application/json; charset=utf-8",
                json.dumps({"ok": False, "error": scrub(str(exc)) or type(exc).__name__}).encode("utf8"),
            )

    return HTTPStatus.METHOD_NOT_ALLOWED, "application/json; charset=utf-8", b'{"error":"Method not allowed"}'


def create_workbench_server(
    env_path: Path,
    workspace: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8090,
) -> ThreadingHTTPServer:
    class WorkbenchHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def _respond(self, status: int, content_type: str, raw: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            status, ctype, raw = dispatch_workbench_request("GET", self.path, {}, env_path, workspace)
            self._respond(status, ctype, raw)

        def do_POST(self) -> None:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 512_000)
            body = self.rfile.read(length).decode("utf8", errors="replace") if length else "{}"
            try:
                payload = json.loads(body or "{}")
            except json.JSONDecodeError:
                payload = {}
            status, ctype, raw = dispatch_workbench_request("POST", self.path, payload, env_path, workspace)
            self._respond(status, ctype, raw)

    return ThreadingHTTPServer((host, port), WorkbenchHandler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="adk-coder-ui",
        description="Launch the standalone ADK Coder Browser Workbench (works in any browser on Mac/Windows/Linux).",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Target workspace directory.")
    parser.add_argument("--env", type=Path, default=default_env_path(), help="Path to .env file.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser.")
    args = parser.parse_args(argv)

    env_path = args.env.expanduser().resolve()
    workspace = args.workspace.expanduser().resolve()
    server = create_workbench_server(env_path, workspace, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}"
    print(f"ADK Coder Workbench v{__version__} running at {url}")
    print(f"Workspace: {workspace}")
    print(f"Settings:  {url}/settings")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
