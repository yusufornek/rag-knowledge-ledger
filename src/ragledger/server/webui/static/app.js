/* RAG Knowledge Ledger web UI.
 *
 * Dependency-free by design: hash routing, fetch with a bearer token,
 * and DOM building through textContent only -- no HTML string
 * interpolation anywhere, so API data can never inject markup. The
 * token lives in sessionStorage for this tab's lifetime; disconnecting
 * clears it.
 */
"use strict";

const TOKEN_KEY = "ragledger.token";
const WORKSPACE_KEY = "ragledger.workspace";

const state = {
  token: sessionStorage.getItem(TOKEN_KEY),
  workspaceId: sessionStorage.getItem(WORKSPACE_KEY),
  workspaceName: "",
};

/* ------------------------------------------------------------------ */
/* DOM helpers (textContent only)                                      */
/* ------------------------------------------------------------------ */

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "text") node.textContent = value;
    else if (key === "onclick") node.addEventListener("click", value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children || []) {
    node.append(child);
  }
  return node;
}

function badge(text, tone) {
  return el("span", { class: "badge " + (tone || ""), text: String(text) });
}

function toneFor(value) {
  const v = String(value).toLowerCase();
  if (["completed", "complete", "active", "pass", "valid_trusted", "success", "ok"].includes(v)) return "ok";
  if (["failed", "fail", "invalid", "tombstone", "cancelled", "revoked"].includes(v)) return "fail";
  if (["running", "pending", "queued", "leased", "warn", "incomplete", "inconclusive",
       "valid_untrusted"].includes(v)) return "warn";
  return "";
}

function table(headers, rows, captionText) {
  const thead = el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { scope: "col", text: h })))]);
  const tbody = el("tbody", {}, rows.map((cells) =>
    el("tr", {}, cells.map((cell) => {
      const td = el("td", {});
      if (cell instanceof Node) td.append(cell);
      else if (Array.isArray(cell)) td.append(...cell);
      else td.textContent = cell === null || cell === undefined ? "" : String(cell);
      return td;
    }))));
  const parts = [];
  if (captionText) parts.push(el("caption", { text: captionText }));
  parts.push(thead, tbody);
  return el("table", {}, parts);
}

function shortId(value) {
  const s = String(value || "");
  return s.length > 12 ? s.slice(0, 12) : s;
}

function fmtTime(value) {
  if (!value) return "";
  return String(value).replace("T", " ").replace(/\.\d+.*$/, "Z").replace("+00:00", "Z");
}

function jsonView(value) {
  return el("pre", { class: "json-view", text: JSON.stringify(value, null, 2) });
}

function errorBox(err) {
  const detail = err && err.detail ? err.detail : String(err);
  return el("div", { class: "notice error-box", role: "alert" }, [
    el("strong", { text: "Request failed" }),
    el("div", { text: detail }),
  ]);
}

/* ------------------------------------------------------------------ */
/* API client                                                          */
/* ------------------------------------------------------------------ */

async function api(method, path, body) {
  const response = await fetch("/api/v1" + path, {
    method,
    headers: Object.assign(
      { Authorization: "Bearer " + state.token },
      body !== undefined ? { "Content-Type": "application/json" } : {},
    ),
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const err = typeof payload === "object" && payload ? payload : { detail: String(payload) };
    err.status = response.status;
    throw err;
  }
  return payload;
}

const ws = () => "/workspaces/" + state.workspaceId;

/* SSE via fetch: EventSource cannot send an Authorization header. */
async function streamJobEvents(path, onEvent) {
  const response = await fetch("/api/v1" + path, {
    headers: { Authorization: "Bearer " + state.token },
  });
  if (!response.ok || !response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      const eventLine = chunk.split("\n").find((l) => l.startsWith("event: "));
      const dataLine = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (eventLine && dataLine) {
        onEvent(eventLine.slice(7), JSON.parse(dataLine.slice(6)));
      }
    }
  }
}

/* ------------------------------------------------------------------ */
/* Shell: login, navigation, routing                                   */
/* ------------------------------------------------------------------ */

const loginView = document.getElementById("login-view");
const appView = document.getElementById("app-view");
const main = document.getElementById("main");

async function connect(token) {
  state.token = token;
  const workspaces = await api("GET", "/workspaces");
  if (!workspaces.length) throw { detail: "token accepted but its workspace no longer exists" };
  state.workspaceId = workspaces[0].id;
  state.workspaceName = workspaces[0].name + " (" + workspaces[0].slug + ")";
  sessionStorage.setItem(TOKEN_KEY, token);
  sessionStorage.setItem(WORKSPACE_KEY, state.workspaceId);
}

function disconnect() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(WORKSPACE_KEY);
  window.location.hash = "";
  window.location.reload();
}

document.getElementById("login-origin").textContent = window.location.origin;
document.getElementById("logout-button").addEventListener("click", disconnect);
document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorNode = document.getElementById("login-error");
  errorNode.hidden = true;
  try {
    await connect(document.getElementById("login-token").value.trim());
    showApp();
  } catch (err) {
    errorNode.textContent = "Could not connect: " + (err.detail || "unknown error");
    errorNode.hidden = false;
  }
});

function showApp() {
  loginView.hidden = true;
  appView.hidden = false;
  document.getElementById("workspace-label").textContent = state.workspaceName;
  if (!window.location.hash) window.location.hash = "#/overview";
  route();
}

const routes = {
  overview: renderOverview,
  sources: renderSources,
  builds: renderBuilds,
  manifests: renderManifests,
  targets: renderTargets,
  snapshots: renderSnapshots,
  reconciliations: renderReconciliations,
  policies: renderPolicies,
  settings: renderSettings,
};

async function route() {
  const segments = window.location.hash.replace(/^#\//, "").split("/");
  const page = segments[0] || "overview";
  for (const link of document.querySelectorAll(".sidenav a")) {
    if (link.getAttribute("href") === "#/" + page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  main.replaceChildren(el("p", { class: "muted", text: "Loading..." }));
  try {
    const handler = routes[page] || renderOverview;
    await handler(segments.slice(1));
  } catch (err) {
    main.replaceChildren(errorBox(err));
    if (err && err.status === 401) {
      main.append(el("p", {}, [el("button", { text: "Reconnect", onclick: disconnect })]));
    }
  }
  main.focus({ preventScroll: true });
}

window.addEventListener("hashchange", route);

/* ------------------------------------------------------------------ */
/* Screens                                                             */
/* ------------------------------------------------------------------ */

function heading(text) {
  return el("h1", { text });
}

function statCard(label, valueNode) {
  const value = valueNode instanceof Node ? valueNode : el("span", { text: String(valueNode) });
  return el("div", { class: "card" }, [
    el("div", { class: "stat-label", text: label }),
    el("div", { class: "stat-value" }, [value]),
  ]);
}

async function renderOverview() {
  const [manifests, snapshots, reconciliations, jobs] = await Promise.all([
    api("GET", ws() + "/manifests"),
    api("GET", ws() + "/targets").then((targets) =>
      Promise.all(targets.map((t) => api("GET", ws() + "/targets/" + t.id + "/snapshots")))
        .then((lists) => lists.flat())),
    api("GET", ws() + "/reconciliations"),
    api("GET", ws() + "/jobs?limit=10").catch(() => []),
  ]);
  const latestManifest = manifests[0];
  const latestReconciliation = reconciliations[0];
  const verdict = latestReconciliation && latestReconciliation.summary
    ? latestReconciliation.summary.verdict : "none yet";
  main.replaceChildren(
    heading("Overview"),
    el("div", { class: "cards" }, [
      statCard("Manifests", manifests.length),
      statCard("Latest manifest signed",
        latestManifest ? badge(latestManifest.signed ? "signed" : "unsigned",
          latestManifest.signed ? "ok" : "warn") : el("span", { text: "none yet" })),
      statCard("Snapshots", snapshots.length),
      statCard("Reconciliations", reconciliations.length),
      statCard("Latest verdict", badge(verdict, toneFor(verdict))),
    ]),
    el("h2", { text: "Recent jobs" }),
    table(
      ["Type", "Status", "Attempts", "Created", "Error"],
      jobs.map((job) => [
        job.job_type,
        badge(job.status, toneFor(job.status)),
        job.attempt_count,
        fmtTime(job.created_at),
        job.last_error || "",
      ]),
      jobs.length ? "" : "No jobs yet."),
  );
}

async function renderSources() {
  const collections = await api("GET", ws() + "/source-collections");
  const sources = await api("GET", ws() + "/sources");
  const scanForm = el("form", { class: "panel", "aria-label": "Add source collection" }, [
    el("h2", { text: "Add a source collection" }),
    el("label", { for: "sc-name", text: "Name" }),
    el("input", { id: "sc-name", required: "" }),
    el("label", { for: "sc-namespace", text: "Namespace" }),
    el("input", { id: "sc-namespace", required: "" }),
    el("label", { for: "sc-root", text: "Server-side root directory (absolute path)" }),
    el("input", { id: "sc-root", required: "", placeholder: "/data/documents" }),
    el("button", { type: "submit", text: "Create collection" }),
  ]);
  scanForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("POST", ws() + "/source-collections", {
        name: document.getElementById("sc-name").value,
        namespace: document.getElementById("sc-namespace").value,
        root: document.getElementById("sc-root").value,
      });
      route();
    } catch (err) { main.prepend(errorBox(err)); }
  });
  main.replaceChildren(
    heading("Sources"),
    table(
      ["Collection", "Namespace", "Root", "Actions"],
      collections.map((c) => [
        c.name, c.namespace, el("code", { text: c.root }),
        el("button", {
          text: "Scan now",
          onclick: async () => {
            try { await api("POST", ws() + "/source-collections/" + c.id + ":scan"); route(); }
            catch (err) { main.prepend(errorBox(err)); }
          },
        }),
      ]),
      collections.length ? "" : "No source collections yet."),
    el("h2", { text: "Discovered sources" }),
    table(
      ["URI", "Status", "Portable id"],
      sources.map((s) => [
        el("code", { text: s.uri }),
        badge(s.status, toneFor(s.status)),
        el("code", { text: s.portable_id }),
      ]),
      sources.length ? "" : "Nothing discovered yet; scan a collection."),
    scanForm,
  );
}

async function renderBuilds() {
  const [builds, collections, configs] = await Promise.all([
    api("GET", ws() + "/builds"),
    api("GET", ws() + "/source-collections"),
    api("GET", ws() + "/pipeline-configs"),
  ]);
  const form = el("form", { class: "panel", "aria-label": "Start build" }, [
    el("h2", { text: "Start a build" }),
    el("label", { for: "b-collection", text: "Source collection" }),
    el("select", { id: "b-collection" },
      collections.map((c) => el("option", { value: c.id, text: c.name + " (" + c.namespace + ")" }))),
    el("label", { for: "b-config", text: "Pipeline config" }),
    el("select", { id: "b-config" },
      configs.map((c) => el("option", { value: c.id, text: shortId(c.config_hash) }))),
    el("label", { for: "b-epoch", text: "Reproducible epoch (optional, unix seconds)" }),
    el("input", { id: "b-epoch", inputmode: "numeric", placeholder: "leave empty for wall-clock" }),
    el("button", { type: "submit", text: "Start build" }),
    configs.length ? el("span", {}) : el("p", { class: "muted",
      text: "No pipeline config yet; a default deterministic one is created on first use." }),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      let configId = document.getElementById("b-config").value;
      if (!configId) {
        const created = await api("POST", ws() + "/pipeline-configs",
          { config: { embedding: { mode: "deterministic", revision_file: null } } });
        configId = created.id;
      }
      const body = {
        source_collection_id: document.getElementById("b-collection").value,
        pipeline_config_id: configId,
      };
      const epoch = document.getElementById("b-epoch").value.trim();
      if (epoch) body.epoch = Number(epoch);
      await api("POST", ws() + "/builds", body);
      route();
    } catch (err) { main.prepend(errorBox(err)); }
  });
  // Live progress (design specification 18.4): stream SSE for any build
  // still in flight and refresh the screen when its job finishes.
  for (const build of builds) {
    if (build.state === "pending" || build.state === "running") {
      streamJobEvents(ws() + "/builds/" + build.id + "/events", (event) => {
        if (event === "done" && window.location.hash === "#/builds") route();
      }).catch(() => {});
    }
  }
  main.replaceChildren(
    heading("Builds"),
    table(
      ["Build", "State", "Sources", "Chunks", "Manifest", "Created", "Actions"],
      builds.map((b) => [
        el("code", { text: shortId(b.id) }),
        badge(b.state, toneFor(b.state)),
        b.counters.sources ?? "",
        b.counters.chunks ?? "",
        b.manifest_hash ? el("code", { text: shortId(b.manifest_hash) }) : "",
        fmtTime(b.created_at),
        b.state === "pending" || b.state === "running"
          ? el("button", {
              class: "danger", text: "Cancel",
              onclick: async () => {
                try { await api("POST", ws() + "/builds/" + b.id + ":cancel"); route(); }
                catch (err) { main.prepend(errorBox(err)); }
              },
            })
          : "",
      ]),
      builds.length ? "" : "No builds yet."),
    form,
  );
}

async function renderManifests() {
  const manifests = await api("GET", ws() + "/manifests");
  const detail = el("div", {});
  main.replaceChildren(
    heading("Manifests"),
    table(
      ["Hash", "Namespace", "Sources", "Chunks", "Embeddings", "Signed", "Actions"],
      manifests.map((m) => [
        el("code", { text: shortId(m.manifest_hash) }),
        m.namespace, m.source_count, m.chunk_count, m.embedding_count,
        badge(m.signed ? "signed" : "unsigned", m.signed ? "ok" : "warn"),
        [
          el("button", {
            text: "Verify",
            onclick: async () => {
              try {
                const result = await api("POST", ws() + "/manifests/" + m.id + ":verify");
                detail.replaceChildren(
                  el("h2", { text: "Verification of " + shortId(m.manifest_hash) }),
                  el("p", {}, [badge(result.overall, toneFor(result.overall)),
                    el("span", { text: " hash_valid=" + result.hash_valid })]),
                  jsonView(result.signatures));
              } catch (err) { detail.replaceChildren(errorBox(err)); }
            },
          }),
          el("button", {
            text: "Sign",
            onclick: async () => {
              try { await api("POST", ws() + "/manifests/" + m.id + ":sign"); route(); }
              catch (err) { detail.replaceChildren(errorBox(err)); }
            },
          }),
        ],
      ]),
      manifests.length ? "" : "No manifests yet; run a build."),
    detail,
  );
}

async function renderTargets() {
  const targets = await api("GET", ws() + "/targets");
  const form = el("form", { class: "panel", "aria-label": "Register target" }, [
    el("h2", { text: "Register a vector target" }),
    el("label", { for: "t-name", text: "Name" }),
    el("input", { id: "t-name", required: "" }),
    el("label", { for: "t-type", text: "Type" }),
    el("select", { id: "t-type" }, [
      el("option", { value: "qdrant", text: "qdrant" }),
      el("option", { value: "pgvector", text: "pgvector" }),
    ]),
    el("label", { for: "t-endpoint", text: "Endpoint URL" }),
    el("input", { id: "t-endpoint", required: "", placeholder: "https://qdrant.example.com:6333" }),
    el("label", { for: "t-credential", text: "Credential (API key or DSN; stored encrypted, never shown again)" }),
    el("input", { id: "t-credential", type: "password", required: "", autocomplete: "off" }),
    el("label", { for: "t-mapping", text: "Mapping config (JSON)" }),
    el("textarea", { id: "t-mapping", text: '{"collection": "my_collection"}' }),
    el("button", { type: "submit", text: "Register target" }),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("POST", ws() + "/targets", {
        name: document.getElementById("t-name").value,
        target_type: document.getElementById("t-type").value,
        endpoint_url: document.getElementById("t-endpoint").value,
        credential: document.getElementById("t-credential").value,
        mapping_config: JSON.parse(document.getElementById("t-mapping").value || "{}"),
      });
      route();
    } catch (err) { main.prepend(errorBox(err)); }
  });
  main.replaceChildren(
    heading("Targets"),
    table(
      ["Name", "Type", "Endpoint", "Credential", "Allowlist", "Actions"],
      targets.map((t) => [
        t.name, t.target_type, el("code", { text: t.endpoint_redacted }),
        badge(t.credential_configured ? "configured (v" + t.credential_version + ")" : "missing",
          t.credential_configured ? "ok" : "fail"),
        t.allowlist_decision || "",
        el("button", {
          text: "Snapshot now",
          onclick: async () => {
            try { await api("POST", ws() + "/targets/" + t.id + "/snapshots"); window.location.hash = "#/snapshots"; }
            catch (err) { main.prepend(errorBox(err)); }
          },
        }),
      ]),
      targets.length ? "" : "No targets registered."),
    form,
  );
}

async function renderSnapshots() {
  const targets = await api("GET", ws() + "/targets");
  const lists = await Promise.all(
    targets.map((t) => api("GET", ws() + "/targets/" + t.id + "/snapshots")
      .then((rows) => rows.map((r) => ({ target: t, snapshot: r })))));
  const rows = lists.flat().sort((a, b) => (a.snapshot.created_at < b.snapshot.created_at ? 1 : -1));
  main.replaceChildren(
    heading("Snapshots"),
    table(
      ["Snapshot", "Target", "Status", "Points", "Content hash", "Created", "Actions"],
      rows.map(({ target, snapshot }) => [
        el("code", { text: shortId(snapshot.id) }),
        target.name,
        badge(snapshot.status, toneFor(snapshot.status)),
        snapshot.point_count ?? "",
        snapshot.content_hash ? el("code", { text: shortId(snapshot.content_hash) }) : "",
        fmtTime(snapshot.created_at),
        ["pending", "running"].includes(snapshot.status)
          ? el("button", {
              class: "danger", text: "Cancel",
              onclick: async () => {
                try { await api("POST", ws() + "/snapshots/" + snapshot.id + ":cancel"); route(); }
                catch (err) { main.prepend(errorBox(err)); }
              },
            })
          : "",
      ]),
      rows.length ? "" : "No snapshots yet; register a target and snapshot it."),
  );
}

async function renderReconciliations(segments) {
  if (segments && segments[0]) return renderReconciliationDetail(segments[0]);
  const [reconciliations, manifests, snapshotsByTarget, policies] = await Promise.all([
    api("GET", ws() + "/reconciliations"),
    api("GET", ws() + "/manifests"),
    api("GET", ws() + "/targets").then((targets) =>
      Promise.all(targets.map((t) => api("GET", ws() + "/targets/" + t.id + "/snapshots")))
        .then((lists) => lists.flat())),
    api("GET", ws() + "/policies").catch(() => []),
  ]);
  const completedSnapshots = snapshotsByTarget.filter((s) => s.status === "completed");
  const form = el("form", { class: "panel", "aria-label": "Start reconciliation" }, [
    el("h2", { text: "Start a reconciliation" }),
    el("label", { for: "r-manifest", text: "Manifest" }),
    el("select", { id: "r-manifest" },
      manifests.map((m) => el("option", { value: m.id, text: m.namespace + " " + shortId(m.manifest_hash) }))),
    el("label", { for: "r-snapshot", text: "Snapshot (completed only)" }),
    el("select", { id: "r-snapshot" },
      completedSnapshots.map((s) => el("option", { value: s.id,
        text: shortId(s.id) + " (" + (s.point_count ?? "?") + " points)" }))),
    el("label", { for: "r-policy", text: "Policy (optional)" }),
    el("select", { id: "r-policy" },
      [el("option", { value: "", text: "none" })]
        .concat(policies.map((p) => el("option", { value: p.id, text: p.name })))),
    el("button", { type: "submit", text: "Start reconciliation" }),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const body = {
        manifest_id: document.getElementById("r-manifest").value,
        snapshot_id: document.getElementById("r-snapshot").value,
      };
      const policyId = document.getElementById("r-policy").value;
      if (policyId) body.policy_id = policyId;
      await api("POST", ws() + "/reconciliations", body);
      route();
    } catch (err) { main.prepend(errorBox(err)); }
  });
  main.replaceChildren(
    heading("Reconciliations"),
    table(
      ["Run", "State", "Verdict", "Findings", "Created", ""],
      reconciliations.map((r) => [
        el("code", { text: shortId(r.id) }),
        badge(r.state, toneFor(r.state)),
        r.summary ? badge(r.summary.verdict, toneFor(r.summary.verdict)) : "",
        r.finding_count,
        fmtTime(r.created_at),
        el("a", { href: "#/reconciliations/" + r.id, text: "Detail" }),
      ]),
      reconciliations.length ? "" : "No reconciliations yet."),
    form,
  );
}

async function renderReconciliationDetail(id) {
  const [reconciliation, findings] = await Promise.all([
    api("GET", ws() + "/reconciliations/" + id),
    api("GET", ws() + "/reconciliations/" + id + "/findings?limit=200"),
  ]);
  const summary = reconciliation.summary || {};
  const remediation = el("div", {});
  main.replaceChildren(
    heading("Reconciliation " + shortId(id)),
    el("p", {}, [el("a", { href: "#/reconciliations", text: "Back to list" })]),
    el("div", { class: "cards" }, [
      statCard("State", badge(reconciliation.state, toneFor(reconciliation.state))),
      statCard("Verdict", badge(summary.verdict || "n/a", toneFor(summary.verdict || ""))),
      statCard("Findings", reconciliation.finding_count),
      statCard("Snapshot completeness",
        summary.consistency ? summary.consistency.completeness : "n/a"),
    ]),
    summary.ratios ? el("div", {}, [el("h2", { text: "Ratios" }), jsonView(summary.ratios)]) : el("span", {}),
    el("h2", { text: "Findings" }),
    table(
      ["Code", "Severity", "Source", "Chunk", "Observed evidence"],
      findings.map((f) => [
        el("code", { text: f.code }),
        badge(f.severity, f.severity === "critical" || f.severity === "high" ? "fail"
          : f.severity === "medium" ? "warn" : ""),
        f.source_hash ? el("code", { text: shortId(f.source_hash) }) : "",
        f.chunk_hash ? el("code", { text: shortId(f.chunk_hash) }) : "",
        f.observed_evidence ? el("code", { text: JSON.stringify(f.observed_evidence).slice(0, 120) }) : "",
      ]),
      findings.length ? "First " + findings.length + " findings, engine order." : "No findings."),
    el("div", { class: "toolbar" }, [
      el("button", {
        text: "Show remediation plan",
        onclick: async () => {
          try {
            const plan = await api("POST", ws() + "/reconciliations/" + id + "/remediation-plans");
            remediation.replaceChildren(
              el("h2", { text: "Remediation plan (read-only candidates; nothing is executed)" }),
              table(["Action", "Destructive", "Candidates", "Rationale"],
                plan.actions.map((a) => [
                  a.action,
                  badge(a.destructive ? "destructive" : "safe", a.destructive ? "fail" : "ok"),
                  a.candidates.length,
                  a.rationale || a.caution || "",
                ])));
          } catch (err) { remediation.replaceChildren(errorBox(err)); }
        },
      }),
    ]),
    remediation,
  );
}

async function renderPolicies() {
  const policies = await api("GET", ws() + "/policies");
  const form = el("form", { class: "panel", "aria-label": "Create policy" }, [
    el("h2", { text: "Create a policy" }),
    el("label", { for: "p-name", text: "Name" }),
    el("input", { id: "p-name", required: "" }),
    el("label", { for: "p-document", text: "Policy document (JSON, policy v1 schema)" }),
    el("textarea", { id: "p-document",
      text: JSON.stringify({ version: 1, name: "ci-gate", requirements: {},
        findings: { fail_on_severity: ["critical", "high"] } }, null, 2) }),
    el("button", { type: "submit", text: "Create policy" }),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("POST", ws() + "/policies", {
        name: document.getElementById("p-name").value,
        document: JSON.parse(document.getElementById("p-document").value),
      });
      route();
    } catch (err) { main.prepend(errorBox(err)); }
  });
  main.replaceChildren(
    heading("Policies"),
    table(
      ["Name", "Latest revision", "Content hash", "Created"],
      policies.map((p) => [
        p.name,
        p.latest_revision ? "r" + p.latest_revision.revision_number : "",
        p.latest_revision ? el("code", { text: shortId(p.latest_revision.config_hash) }) : "",
        fmtTime(p.created_at),
      ]),
      policies.length ? "Revisions are immutable; posting the same name again is rejected."
        : "No policies yet."),
    form,
  );
}

async function renderSettings() {
  const [tokens, auditEvents] = await Promise.all([
    api("GET", ws() + "/api-tokens").catch(() => null),
    api("GET", ws() + "/audit-events?limit=25").catch(() => null),
  ]);
  const created = el("div", {});
  const tokenForm = el("form", { class: "panel", "aria-label": "Create API token" }, [
    el("h2", { text: "Create an API token" }),
    el("label", { for: "k-name", text: "Name" }),
    el("input", { id: "k-name", required: "" }),
    el("label", { for: "k-scopes", text: "Scopes (comma separated: sources, builds, targets, snapshots, reconciliations, policies, admin)" }),
    el("input", { id: "k-scopes", value: "builds,sources" }),
    el("button", { type: "submit", text: "Create token" }),
  ]);
  tokenForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const token = await api("POST", ws() + "/api-tokens", {
        name: document.getElementById("k-name").value,
        scopes: document.getElementById("k-scopes").value.split(",").map((s) => s.trim()).filter(Boolean),
      });
      created.replaceChildren(el("div", { class: "notice" }, [
        el("strong", { text: "Token created; copy it now, it is never shown again: " }),
        el("code", { text: token.token }),
      ]));
    } catch (err) { created.replaceChildren(errorBox(err)); }
  });
  const sections = [heading("Settings")];
  if (tokens === null) {
    sections.push(el("p", { class: "muted",
      text: "Token management and the audit trail require the admin scope." }));
  } else {
    sections.push(
      el("h2", { text: "API tokens" }),
      table(
        ["Name", "Selector", "Scopes", "Status", "Last used", "Actions"],
        tokens.map((t) => [
          t.name,
          el("code", { text: t.selector }),
          t.scopes.join(", "),
          badge(t.revoked_at ? "revoked" : "active", t.revoked_at ? "fail" : "ok"),
          fmtTime(t.last_used_at),
          t.revoked_at ? "" : el("button", {
            class: "danger", text: "Revoke",
            onclick: async () => {
              try { await api("DELETE", ws() + "/api-tokens/" + t.id); route(); }
              catch (err) { created.replaceChildren(errorBox(err)); }
            },
          }),
        ])),
      created,
      tokenForm,
      el("h2", { text: "Workspace export" }),
      el("p", {}, [
        el("span", { text: "A JSON document of configuration and result metadata; secrets and raw documents are excluded by construction. " }),
        el("button", {
          text: "Download export",
          onclick: async () => {
            const data = await api("GET", ws() + "/export");
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
            const link = el("a", { href: URL.createObjectURL(blob), download: "workspace-export.json" });
            link.click();
            URL.revokeObjectURL(link.href);
          },
        }),
      ]),
      el("h2", { text: "Recent audit events" }),
      table(
        ["Time", "Actor", "Action", "Entity", "Result"],
        (auditEvents || []).map((event) => [
          fmtTime(event.created_at),
          event.actor_type,
          el("code", { text: event.action }),
          event.entity_type ? event.entity_type + " " + shortId(event.entity_id || "") : "",
          badge(event.result, toneFor(event.result)),
        ])),
    );
  }
  main.replaceChildren(...sections);
}

/* ------------------------------------------------------------------ */
/* Boot                                                                */
/* ------------------------------------------------------------------ */

(async function boot() {
  if (state.token) {
    try {
      await connect(state.token);
      showApp();
      return;
    } catch {
      sessionStorage.removeItem(TOKEN_KEY);
    }
  }
  loginView.hidden = false;
})();
