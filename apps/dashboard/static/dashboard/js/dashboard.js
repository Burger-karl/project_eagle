/*
 * Eagle dashboard: a small single-page app in plain JavaScript.
 *
 * It talks to the existing API with the signed-in user's session (no token
 * needed). Every Tailwind class below is written out in full, never built by
 * string concatenation, so Tailwind can find it when it compiles the CSS.
 */
(function () {
  "use strict";

  const root = document.getElementById("app");
  if (!root) return;

  const EP = JSON.parse(root.dataset.endpoints);
  const CSRF = root.dataset.csrf;
  const LOGIN = root.dataset.login;
  const PAGE_SIZE = 50;
  const MAX_RETRIES = 5; // mirrors Invoice.is_retryable on the server

  const $ = (sel, el) => (el || document).querySelector(sel);
  const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v) => Number(v || 0);
  const money = (v) => "₦" + num(v).toLocaleString("en-NG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const moneyShort = (v) => {
    const n = num(v), a = Math.abs(n);
    if (a >= 1e9) return "₦" + (n / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return "₦" + (n / 1e6).toFixed(1) + "M";
    if (a >= 1e3) return "₦" + (n / 1e3).toFixed(0) + "K";
    return "₦" + n.toFixed(0);
  };
  const toDate = (v) => new Date(v && v.length === 10 ? v + "T00:00:00" : v);
  const fDate = (v) => (v ? toDate(v).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "—");
  const fDT = (v) => (v ? toDate(v).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");
  const rel = (v) => {
    if (!v) return "—";
    const m = Math.round((toDate(v) - Date.now()) / 60000), a = Math.abs(m);
    if (a < 1) return "just now";
    const t = a < 60 ? a + " min" : a < 1440 ? Math.round(a / 60) + " h" : Math.round(a / 1440) + " d";
    return m < 0 ? t + " ago" : "in " + t;
  };

  /* ---------- labels and colours ---------- */
  const LABEL = {
    PENDING: "Pending", SUBMITTING: "Submitting", CLEARED: "Cleared", WRITTEN_BACK: "Written back",
    FAILED: "Failed", REJECTED: "Rejected", CANCELLED: "Cancelled",
  };
  const UPLOAD_LABEL = {
    RECEIVED: "Received", PARSING: "Reading file", PARSED: "Ready to submit", PROCESSING: "Submitting",
    COMPLETE: "Complete", PARTIAL: "Partly cleared", FAILED: "Failed",
  };
  const METHOD = { FOLDER: "Watched folder", EMAIL: "Email attachment", UPLOAD: "Web upload" };
  const CHIP = {
    PENDING: "bg-pend-bg text-pend", SUBMITTING: "bg-sub-bg text-sub", CLEARED: "bg-clr-bg text-clr",
    WRITTEN_BACK: "bg-wb-bg text-wb", FAILED: "bg-fail-bg text-fail", REJECTED: "bg-rej-bg text-rej",
    CANCELLED: "bg-can-bg text-can",
    RECEIVED: "bg-sub-bg text-sub", PARSING: "bg-sub-bg text-sub", PARSED: "bg-sub-bg text-sub",
    PROCESSING: "bg-sub-bg text-sub", COMPLETE: "bg-wb-bg text-wb", PARTIAL: "bg-pend-bg text-pend",
    GOOD: "bg-clr-bg text-clr", WARN: "bg-pend-bg text-pend", BAD: "bg-fail-bg text-fail",
    INVALID: "bg-rej-bg text-rej", VALID: "bg-sub-bg text-sub", SUBMITTED: "bg-sub-bg text-sub",
  };
  const ROW_LABEL = { PENDING: "Pending", VALID: "Ready", INVALID: "Not sent", SUBMITTED: "Submitting", CLEARED: "Cleared", FAILED: "Failed" };
  const SOLID = {
    PENDING: "bg-pend-solid", SUBMITTING: "bg-sub-solid", CLEARED: "bg-clr-solid", WRITTEN_BACK: "bg-wb-solid",
    FAILED: "bg-fail-solid", REJECTED: "bg-rej-solid", CANCELLED: "bg-can-solid",
  };
  const TONE = {
    PENDING: "[--tone:var(--pend-solid)] [--tone-bg:var(--pend-bg)]",
    SUBMITTING: "[--tone:var(--sub-solid)] [--tone-bg:var(--sub-bg)]",
    CLEARED: "[--tone:var(--clr-solid)] [--tone-bg:var(--clr-bg)]",
    WRITTEN_BACK: "[--tone:var(--wb-solid)] [--tone-bg:var(--clr-bg)]",
  };
  const chip = (cls, text) => `<span class="chip ${cls}">${esc(text)}</span>`;
  const statusChip = (s) => chip(CHIP[s] || "bg-can-bg text-can", LABEL[s] || s);
  const uploadChip = (s) => chip(CHIP[s] || "bg-can-bg text-can", UPLOAD_LABEL[s] || s);
  const rowChip = (s) => chip(CHIP[s] || "bg-can-bg text-can", ROW_LABEL[s] || s);

  const isRetryable = (i) => i.status === "FAILED" && num(i.submission_attempts) < MAX_RETRIES;
  // Which action is safe to offer. The server's submit endpoint accepts almost any
  // status, so the page never offers it for invoices that are done or in flight.
  const actionFor = (i) =>
    isRetryable(i) ? "retry"
    : i.status === "PENDING" || i.status === "FAILED" || i.status === "REJECTED" ? "submit"
    : null;

  /* ---------- API ---------- */
  function flat(o) {
    return Object.keys(o)
      .map((k) => {
        const v = o[k];
        const t = Array.isArray(v) ? v.map(String).join(" ") : v && typeof v === "object" ? flat(v) : String(v);
        return k === "detail" || k === "non_field_errors" ? t : k + ": " + t;
      })
      .join(" ");
  }
  // Two error shapes exist: views return {"error": "text"}, while DRF errors pass
  // through utils.exceptions and arrive as {"error": true, "detail": ...}.
  function messageFrom(method, status, data) {
    if (data) {
      if (typeof data.error === "string") {
        return typeof data.detail === "string" && data.detail ? data.error + " " + data.detail : data.error;
      }
      const d = data.detail;
      if (typeof d === "string" && d) return d;
      if (d && typeof d === "object") {
        const t = flat(d);
        if (/CSRF/i.test(t)) return "This page is out of date. Reload it and try again.";
        if (t) return t;
      }
    }
    if (status === 401 || status === 403) return "Your session has ended, or this page is out of date.";
    if (status >= 500) return "The server hit an error (" + status + "). Please check the server logs.";
    return "Request failed (" + status + ").";
  }
  async function api(method, url, o) {
    o = o || {};
    const u = new URL(url, location.origin);
    Object.entries(o.params || {}).forEach(([k, v]) => { if (v !== "" && v != null) u.searchParams.set(k, v); });
    const headers = { Accept: "application/json" };
    const init = { method, headers, credentials: "same-origin" };
    if (method !== "GET") headers["X-CSRFToken"] = CSRF;
    if (o.form) init.body = o.form;
    else if (o.body) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(o.body); }
    let res;
    try { res = await fetch(u, init); }
    catch (e) { throw new Error("Could not reach the server. Check your connection and try again."); }
    if (o.blob && res.ok) return res.blob();
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (res.ok || (o.accept && o.accept.indexOf(res.status) >= 0 && data)) return data;
    const err = new Error(messageFrom(method, res.status, data));
    err.status = res.status;
    err.auth = res.status === 401 || res.status === 403;
    throw err;
  }
  const rows = (d) => (Array.isArray(d) ? d : (d && d.results) || []);

  /* ---------- state, toasts, small UI helpers ---------- */
  const VIEWS = ["overview", "invoices", "intake", "tins", "health"];
  const DEFAULT_FILTERS = { search: "", status: "", invoice_type: "", date_from: "", date_to: "", page: 1 };
  const S = { view: "overview", f: Object.assign({}, DEFAULT_FILTERS), seq: 0, timer: null, attn: 0, clients: null, tinPage: 1, lastFocus: null };

  function toast(msg, bad) {
    const t = document.createElement("div");
    t.className = "pointer-events-auto max-w-[min(90vw,480px)] rounded-xl px-4 py-2.5 text-sm shadow-lg " +
      (bad ? "bg-fail-solid text-white" : "bg-ink text-canvas");
    t.textContent = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => t.remove(), bad ? 7000 : 3500);
  }
  function setAttention(n) {
    S.attn = n;
    const b = $("[data-badge]");
    if (!b) return;
    b.hidden = !n;
    b.textContent = n;
    b.setAttribute("aria-label", n + " need attention");
  }
  function refreshBadge() {
    api("GET", EP.stats).then((st) => { const b = st.by_status || {}; setAttention((b.FAILED || 0) + (b.REJECTED || 0)); }).catch(() => {});
  }
  function markNav() {
    $$("[data-nav]").forEach((a) => {
      if (a.dataset.nav === S.view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }
  const pageHead = (title, sub, actions) =>
    `<div class="mb-5 flex flex-wrap items-end justify-between gap-x-5 gap-y-3">
       <div><h1 class="font-display text-[28px] font-bold leading-tight tracking-tight sm:text-[30px]">${esc(title)}</h1>
       ${sub ? `<p class="mt-1 max-w-[62ch] text-mute">${esc(sub)}</p>` : ""}</div>
       <div class="flex flex-wrap items-center gap-2.5">${actions || ""}</div></div>`;
  const empty = (title, text) =>
    `<div class="px-[18px] py-9 text-center text-mute"><b class="mb-1 block font-display text-[17px] font-bold text-ink">${esc(title)}</b>${esc(text || "")}</div>`;
  const errorBox = (e) =>
    `<div role="alert" class="m-[18px] rounded-xl bg-fail-bg p-4 text-fail"><b class="block">Something went wrong</b>${esc(e.message)}
      ${e.auth ? ` <a class="font-semibold underline" href="${esc(LOGIN)}?next=${encodeURIComponent(location.pathname)}">Sign in again</a>` : ""}</div>`;
  const pager = (count, page, attr) => {
    const pages = Math.max(1, Math.ceil(count / PAGE_SIZE));
    const from = count ? (page - 1) * PAGE_SIZE + 1 : 0, to = Math.min(count, page * PAGE_SIZE);
    return `<div class="flex items-center justify-between gap-3 border-t border-line px-[18px] py-3 text-[13.5px] text-mute">
      <span>${count ? `Showing ${from} to ${to} of ${count}` : "0 results"}</span>
      <div class="flex gap-2">
        <button type="button" class="btn btn-ghost btn-sm" ${attr}="${page - 1}" ${page <= 1 ? "disabled" : ""}>Previous</button>
        <button type="button" class="btn btn-ghost btn-sm" ${attr}="${page + 1}" ${page >= pages ? "disabled" : ""}>Next</button>
      </div></div>`;
  };
  async function getClients() {
    if (S.clients) return S.clients;
    try { S.clients = await api("GET", EP.clients); } catch (e) { S.clients = []; }
    return S.clients;
  }

  /* ---------- routing (#/view?filters) ---------- */
  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, "");
    const parts = raw.split("?");
    return { view: VIEWS.indexOf(parts[0]) >= 0 ? parts[0] : "overview", params: Object.fromEntries(new URLSearchParams(parts[1] || "")) };
  }
  function filtersHash() {
    const q = new URLSearchParams();
    Object.keys(S.f).forEach((k) => { if (S.f[k] && !(k === "page" && S.f[k] === 1)) q.set(k, S.f[k]); });
    const s = q.toString();
    return "#/invoices" + (s ? "?" + s : "");
  }
  function route() {
    const r = parseHash();
    S.view = r.view;
    if (r.view === "invoices") S.f = Object.assign({}, DEFAULT_FILTERS, r.params, { page: Number(r.params.page) || 1 });
    S.tinPage = 1;
    closeDrawer(true);
    markNav();
    render();
    if (r.view !== "overview") refreshBadge(); // the overview refreshes it itself
    window.scrollTo(0, 0);
  }

  /* ---------- render loop ---------- */
  async function render(silent) {
    clearTimeout(S.timer);
    const token = ++S.seq;
    const el = $("#view");
    if (!silent) el.innerHTML = `<p class="py-10 text-mute">Loading…</p>`;
    try {
      const out = await VIEW[S.view]();
      if (token !== S.seq) return; // the user moved on while we were loading
      el.innerHTML = out.html;
      if (out.after) out.after();
      if (out.refresh) S.timer = setTimeout(() => render(true), out.refresh);
    } catch (e) {
      if (token !== S.seq) return;
      const title = { overview: "Overview", invoices: "Invoices", intake: "Excel intake", tins: "Buyer TINs", health: "System health" }[S.view];
      el.innerHTML = pageHead(title) + `<div class="panel">${errorBox(e)}<div class="px-[18px] pb-[18px]"><button type="button" class="btn" data-act="reload">Try again</button></div></div>`;
    }
  }

  /* ---------- overview ---------- */
  async function viewOverview() {
    const [st, recent, failed, rejected, daily] = await Promise.all([
      api("GET", EP.stats),
      api("GET", EP.invoices, { params: { page: 1 } }),
      api("GET", EP.invoices, { params: { status: "FAILED" } }),
      api("GET", EP.invoices, { params: { status: "REJECTED" } }),
      api("GET", EP.daily),
    ]);
    const by = st.by_status || {};
    const c = (k) => by[k] || 0;
    const month = st.this_month || { count: 0, total_value: 0 };
    const done = c("CLEARED") + c("WRITTEN_BACK"), decided = done + c("FAILED") + c("REJECTED");
    const rate = decided ? Math.round((done / decided) * 1000) / 10 : null;
    const total = st.total || 0;
    const attention = rows(failed).concat(rows(rejected)).slice(0, 6);

    const stage = (k, title, text) =>
      `<a href="#/invoices?status=${k}" class="stage ${TONE[k]}">
         <div class="font-display text-[40px] font-bold leading-none tracking-tight tabular-nums sm:text-[44px]">${c(k)}</div>
         <div class="mt-1.5 font-semibold">${title}</div>
         <div class="mt-0.5 max-w-[24ch] text-[13px] text-mute">${text}</div></a>`;
    const lane = (k, cls, text) =>
      `<a href="#/invoices?status=${k}" class="flex items-center gap-2.5 rounded-xl px-3.5 py-2 hover:ring-1 hover:ring-current ${cls}">
         <b class="font-display text-[22px] font-bold leading-none tabular-nums">${c(k)}</b><span class="text-[13px] leading-tight">${text}</span></a>`;
    const mix = ["PENDING", "SUBMITTING", "CLEARED", "WRITTEN_BACK", "FAILED", "REJECTED", "CANCELLED"]
      .filter((k) => c(k))
      .map((k) => `<i class="block h-full border-l-2 border-panel first:border-l-0 ${SOLID[k]}" style="width:${(c(k) / total) * 100}%" title="${LABEL[k]}: ${c(k)}"></i>`)
      .join("");
    const kpi = (label, value, sub) =>
      `<div class="bg-panel p-4"><dt class="text-[13px] text-mute">${label}</dt>
       <dd class="mt-1 font-display text-[26px] font-bold leading-tight tracking-tight tabular-nums">${value}
       <small class="mt-0.5 block font-sans text-[13px] font-normal leading-snug tracking-normal text-mute">${sub}</small></dd></div>`;

    const html =
      pageHead("Overview", "Sage 200 and Sage 300 are checked for new invoices every 5 minutes.",
        `<span id="env-badge"></span>
         <button type="button" class="btn" data-act="pull-open"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12"/><path d="M7 11l5 5 5-5"/><path d="M4 20h16"/></svg>Pull from Sage 200</button>`) +
      `<div class="flex flex-col gap-[18px]">
        <section class="panel" aria-labelledby="h-pipe">
          <div class="panel-head"><h2 id="h-pipe" class="panel-title">Where invoices are right now</h2><small class="text-[13px] text-mute">${total} in total</small></div>
          <div class="grid grid-cols-2 gap-y-2 px-[18px] pt-1 lg:grid-cols-4">
            ${stage("PENDING", "Pending", "Pulled from Sage, waiting to go to FIRS")}
            ${stage("SUBMITTING", "Submitting", "Being sent to FIRS now")}
            ${stage("CLEARED", "Cleared", "FIRS approved and issued an IRN")}
            ${stage("WRITTEN_BACK", "Written back", "IRN saved into Sage")}
          </div>
          <div class="flex flex-wrap items-stretch gap-2.5 px-[18px] pb-3.5 pt-1">
            <span class="self-center pr-1 text-[13px] text-mute">Not moving forward</span>
            ${lane("FAILED", "bg-fail-bg text-fail", `Failed<br>${st.pending_retry || 0} will retry`)}
            ${lane("REJECTED", "bg-rej-bg text-rej", "Rejected<br>data needs fixing")}
            ${lane("CANCELLED", "bg-can-bg text-can", "Cancelled<br>in Sage")}
          </div>
          <div class="mx-[18px] mb-[18px] flex h-2.5 overflow-hidden rounded-full bg-sunken" role="img" aria-label="Share of invoices by status">${mix}</div>
        </section>

        <section class="panel overflow-hidden"><dl class="grid grid-cols-2 gap-px bg-line lg:grid-cols-4">
          ${kpi("Invoiced this month", month.count, moneyShort(month.total_value) + " gross")}
          ${kpi("Cleared today", st.cleared_today || 0, "waiting to be written back")}
          ${kpi("Waiting to retry", st.pending_retry || 0, "failed, under " + MAX_RETRIES + " attempts")}
          ${kpi("Clearance rate", rate == null ? "—" : rate + "%", "of invoices FIRS has decided on")}
        </dl></section>

        <div class="grid gap-[18px] lg:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
          <section class="panel" aria-labelledby="h-att">
            <div class="panel-head"><h2 id="h-att" class="panel-title">Needs attention</h2>
              ${attention.length ? `<a class="linkbtn" href="#/invoices?status=FAILED">See all failed</a>` : ""}</div>
            ${attention.length
              ? `<ul class="m-0 mt-1 list-none p-0">${attention.map(attentionRow).join("")}</ul>`
              : empty("Nothing needs attention", "Failed and rejected invoices show up here with a way to fix them.")}
          </section>
          <section class="panel" aria-labelledby="h-ch">
            <div class="panel-head"><h2 id="h-ch" class="panel-title">Invoices by day</h2><small class="text-[13px] text-mute">last ${daily.days} days</small></div>
            <div class="px-[18px] pb-[18px] pt-2">${dailyChart(daily.series)}
              <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1.5 text-[13px] text-mute">
                <span><i class="mr-1.5 inline-block h-2.5 w-2.5 rounded-[3px] bg-clr-solid"></i>Cleared</span>
                <span><i class="mr-1.5 inline-block h-2.5 w-2.5 rounded-[3px] bg-sub-solid"></i>In progress</span>
                <span><i class="mr-1.5 inline-block h-2.5 w-2.5 rounded-[3px] bg-fail-solid"></i>Failed or rejected</span>
              </div></div>
          </section>
        </div>

        <section class="panel" aria-labelledby="h-rec">
          <div class="panel-head"><h2 id="h-rec" class="panel-title">Latest invoices</h2><a class="linkbtn" href="#/invoices">Open all invoices</a></div>
          ${invoiceTable(rows(recent).slice(0, 10))}
        </section>
      </div>`;

    return {
      html,
      refresh: 30000,
      after() {
        setAttention(c("FAILED") + c("REJECTED"));
        // Health checks are slow (database, FIRS, Redis), so they never block the page.
        api("GET", EP.health, { accept: [503] }).then((h) => {
          const el = $("#env-badge");
          if (!el || !h) return;
          const prod = h.environment === "PRODUCTION";
          el.innerHTML = chip(prod ? "bg-fail-bg text-fail" : "bg-pend-bg text-pend", prod ? "Production" : "Sandbox");
        }).catch(() => {});
      },
    };
  }
  function attentionRow(i) {
    const act = actionFor(i);
    const tries = num(i.submission_attempts) + " of " + MAX_RETRIES + " tries" + (act === "retry" && i.next_retry_at ? ", next " + rel(i.next_retry_at) : "");
    return `<li class="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-line px-[18px] py-3">
      <div class="min-w-0 flex-1 basis-[230px]">
        <div class="flex flex-wrap items-center gap-x-2 gap-y-1"><button type="button" class="rowlink whitespace-nowrap" data-open="${esc(i.id)}">${esc(i.sage_invoice_number)}</button>${statusChip(i.status)}</div>
        <div class="text-[13px] text-mute">${esc(i.buyer_name)}</div>
        <div class="mt-0.5 line-clamp-2 text-[13px] text-mute">${esc(i.error_message || i.error_code || "No detail returned")}</div>
      </div>
      <div class="flex items-center gap-3"><span class="text-[13px] text-mute">${tries}</span>
        ${act ? `<button type="button" class="btn btn-sm ${act === "retry" ? "" : "btn-ghost"}" data-act="${act}" data-id="${esc(i.id)}" data-status="${esc(i.status)}" data-no="${esc(i.sage_invoice_number)}">${act === "retry" ? "Retry now" : "Submit again"}</button>` : ""}</div></li>`;
  }
  function dailyChart(series) {
    const W = 440, H = 230, pl = 28, pb = 24, pt = 8, ch = H - pb - pt;
    const days = series.length, bw = (W - pl) / days;
    const max = Math.max(4, ...series.map((d) => d.cleared + d.in_progress + d.failed));
    const y = (v) => pt + ch - (v / max) * ch;
    let grid = "", bars = "", labels = "";
    [0, 0.5, 1].forEach((f) => {
      const v = Math.round(max * f);
      grid += `<line class="stroke-line" x1="${pl}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text class="fill-mute text-[12px]" x="${pl - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
    });
    series.forEach((d, i) => {
      const x = pl + i * bw + 2, w = Math.max(2, bw - 4), total = d.cleared + d.in_progress + d.failed;
      let used = 0;
      const seg = (n, cls) => {
        if (!n) return "";
        const h = (n / max) * ch, r = `<rect class="${cls}" x="${x}" y="${pt + ch - ((used + n) / max) * ch}" width="${w}" height="${Math.max(h - 1, 1)}" rx="2"/>`;
        used += n;
        return r;
      };
      const day = toDate(d.date).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
      bars += `<g><title>${day}: ${total} invoice${total === 1 ? "" : "s"} (${d.cleared} cleared, ${d.in_progress} in progress, ${d.failed} failed or rejected)</title>
        ${seg(d.cleared, "fill-clr-solid")}${seg(d.in_progress, "fill-sub-solid")}${seg(d.failed, "fill-fail-solid")}
        <rect x="${x}" y="${pt}" width="${w}" height="${ch}" fill="transparent"/></g>`;
      if ((days - 1 - i) % 7 === 0) { // label today and every 7th day before it
        const last = i === days - 1;
        labels += `<text class="fill-mute text-[12px]" x="${last ? W : x + w / 2}" y="${H - 6}" text-anchor="${last ? "end" : "middle"}">${day}</text>`;
      }
    });
    return `<svg class="block h-auto w-full" viewBox="0 0 ${W} ${H}" role="img" aria-label="Stacked bars of invoices per day for the last ${days} days">${grid}${bars}${labels}</svg>`;
  }
  function invoiceTable(list) {
    if (!list.length) return empty("No invoices match", "Try clearing a filter, or poll Sage for new invoices.");
    const note = (t) => (t === "381" ? "Credit note" : t === "383" ? "Debit note" : "");
    return `<div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr>
      <th class="th">Invoice</th><th class="th">Date</th><th class="th">Buyer</th><th class="th">Type</th><th class="th text-right">Gross</th><th class="th">Status</th><th class="th">IRN</th></tr></thead><tbody>
      ${list.map((i) => `<tr class="cursor-pointer hover:bg-sunken" data-open="${esc(i.id)}">
        <td class="td"><button type="button" class="rowlink whitespace-nowrap" data-open="${esc(i.id)}">${esc(i.sage_invoice_number)}</button>${note(i.document_type) ? `<div class="text-[13px] text-mute">${note(i.document_type)}</div>` : ""}</td>
        <td class="td whitespace-nowrap text-[13px] text-mute">${fDate(i.invoice_date)}</td>
        <td class="td min-w-[170px]">${esc(i.buyer_name)}${i.buyer_tin ? `<div class="text-[13px] text-mute">${esc(i.buyer_tin)}</div>` : ""}</td>
        <td class="td">${esc(i.invoice_type)}</td>
        <td class="td whitespace-nowrap text-right font-semibold tabular-nums">${money(i.gross_amount)}</td>
        <td class="td">${statusChip(i.status)}</td>
        <td class="td break-all font-mono text-[12.5px]" title="${esc(i.irn)}">${i.irn ? esc(i.irn.length > 22 ? i.irn.slice(0, 22) + "…" : i.irn) : `<span class="font-sans text-[13px] text-mute">Not issued</span>`}</td></tr>`).join("")}
      </tbody></table></div>`;
  }

  /* ---------- invoices ---------- */
  async function viewInvoices() {
    const f = S.f;
    const opt = (v, label, cur) => `<option value="${v}" ${cur === v ? "selected" : ""}>${label}</option>`;
    const html =
      pageHead("Invoices", "Every invoice pulled from Sage and its journey through FIRS.",
        `<button type="button" class="btn btn-ghost" data-act="pull-open">Pull from Sage 200</button>`) +
      `<section class="panel">
        <div class="grid grid-cols-2 gap-3 border-b border-line p-[18px] lg:grid-cols-[minmax(200px,2fr)_repeat(4,minmax(120px,1fr))]">
          <div class="col-span-2 lg:col-span-1"><label class="label" for="f-q">Search</label><input id="f-q" type="search" class="input" placeholder="Invoice number, buyer, TIN or IRN" value="${esc(f.search)}"></div>
          <div><label class="label" for="f-s">Status</label><select id="f-s" class="input"><option value="">All statuses</option>${Object.keys(LABEL).map((k) => opt(k, LABEL[k], f.status)).join("")}</select></div>
          <div><label class="label" for="f-t">Type</label><select id="f-t" class="input"><option value="">All types</option>${opt("B2B", "Business to business", f.invoice_type)}${opt("B2C", "Business to consumer", f.invoice_type)}${opt("B2G", "Business to government", f.invoice_type)}</select></div>
          <div><label class="label" for="f-d1">From date</label><input id="f-d1" type="date" class="input" value="${esc(f.date_from)}"></div>
          <div><label class="label" for="f-d2">To date</label><input id="f-d2" type="date" class="input" value="${esc(f.date_to)}"></div>
        </div>
        <div id="inv-box"><p class="px-[18px] py-8 text-mute">Loading…</p></div>
      </section>`;
    return {
      html,
      after() {
        let t;
        const apply = () => {
          S.f = { search: $("#f-q").value.trim(), status: $("#f-s").value, invoice_type: $("#f-t").value, date_from: $("#f-d1").value, date_to: $("#f-d2").value, page: 1 };
          history.replaceState(null, "", filtersHash()); // keeps the URL shareable without re-rendering
          loadInvoices();
        };
        $("#f-q").addEventListener("input", () => { clearTimeout(t); t = setTimeout(apply, 300); });
        ["#f-s", "#f-t", "#f-d1", "#f-d2"].forEach((s) => $(s).addEventListener("change", apply));
        loadInvoices();
      },
    };
  }
  async function loadInvoices() {
    const box = $("#inv-box");
    if (!box) return;
    const f = S.f, mine = ++S.seq;
    try {
      const d = await api("GET", EP.invoices, { params: { search: f.search, status: f.status, invoice_type: f.invoice_type, date_from: f.date_from, date_to: f.date_to, page: f.page } });
      if (mine !== S.seq || !$("#inv-box")) return;
      const list = rows(d);
      box.innerHTML = invoiceTable(list) + pager(d.count != null ? d.count : list.length, f.page, "data-page");
    } catch (e) {
      if ($("#inv-box")) box.innerHTML = errorBox(e);
    }
  }

  /* ---------- invoice drawer ---------- */
  const TL = "relative pb-3.5 pl-6 text-sm before:absolute before:left-1 before:top-1.5 before:h-2.5 before:w-2.5 before:rounded-full after:absolute after:bottom-[-3px] after:left-2 after:top-4 after:w-0.5 after:bg-line last:after:hidden";
  const isDrawerOpen = () => !!$("#drawer-root").firstChild;
  function step(label, when, dot) {
    return when
      ? `<li class="${TL} ${dot}"><b>${label}</b><div class="text-[13px] text-mute">${fDT(when)}</div></li>`
      : `<li class="${TL} text-mute before:left-[3px] before:top-1 before:border-2 before:border-line before:bg-panel">${label}</li>`;
  }
  const closeBtn = `<button type="button" data-close class="grid h-8 w-8 flex-none place-items-center rounded-lg hover:bg-sunken" aria-label="Close"><svg viewBox="0 0 24 24" class="h-[18px] w-[18px] fill-none stroke-current stroke-2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>`;
  async function openInvoice(id) {
    S.lastFocus = document.activeElement;
    const dr = $("#drawer-root");
    dr.innerHTML = `<div class="fixed inset-0 z-40 bg-black/50" data-close></div>
      <aside role="dialog" aria-modal="true" aria-labelledby="drawer-title" class="fixed inset-y-0 right-0 z-[41] w-full max-w-[560px] overflow-y-auto bg-panel p-5 shadow-2xl motion-safe:animate-slidein sm:p-6"><p class="text-mute" id="drawer-title">Loading invoice…</p></aside>`;
    const box = $("aside", dr);
    try {
      const i = await api("GET", EP.invoice.replace("{id}", id));
      const act = actionFor(i);
      const sec = "mt-6";
      const h3 = "mb-2 font-display text-[15px] font-bold";
      box.innerHTML = `
        <div class="flex items-start justify-between gap-3"><div>
          <div class="text-[13px] text-mute">${i.document_type === "381" ? "Credit note" : i.document_type === "383" ? "Debit note" : "Invoice"} for ${esc(i.client_name)}</div>
          <h2 id="drawer-title" class="font-display text-2xl font-bold leading-tight tracking-tight">${esc(i.sage_invoice_number)}</h2>
          <div class="mt-2">${statusChip(i.status)}</div></div>${closeBtn}</div>
        ${i.error_message ? `<div class="${sec}"><div role="alert" class="rounded-xl bg-fail-bg p-3 text-sm text-fail"><b class="block">${esc(i.error_code || "Error")}</b>${esc(i.error_message)}</div></div>` : ""}
        <div class="${sec} flex flex-wrap gap-2.5">
          ${act ? `<button type="button" class="btn ${act === "retry" ? "" : "btn-ghost"}" data-act="${act}" data-id="${esc(i.id)}" data-status="${esc(i.status)}" data-no="${esc(i.sage_invoice_number)}">${act === "retry" ? "Retry now" : i.status === "PENDING" ? "Submit to FIRS" : "Submit again"}</button>` : ""}
          ${i.irn ? `<button type="button" class="btn btn-ghost" data-copy="${esc(i.irn)}">Copy IRN</button>` : ""}
        </div>
        <div class="${sec}"><h3 class="${h3}">Journey</h3><ol class="m-0 list-none p-0">
          ${step("Pulled from Sage", i.created_at, "before:bg-sub-solid")}
          ${step("Last sent to FIRS (" + num(i.submission_attempts) + " attempt" + (num(i.submission_attempts) === 1 ? "" : "s") + ")", i.last_attempt_at, "before:bg-pend-solid")}
          ${step("Cleared by FIRS", i.cleared_at, "before:bg-clr-solid")}
          ${step("IRN written back to Sage", i.written_back_at, "before:bg-wb-solid")}
        </ol></div>
        <div class="${sec}"><h3 class="${h3}">Buyer</h3><div>${esc(i.buyer_name)}</div>
          <div class="text-[13px] text-mute">${i.buyer_tin ? "TIN " + esc(i.buyer_tin) : "No TIN on file"} · ${esc(i.invoice_type)}</div></div>
        ${i.irn ? `<div class="${sec}"><h3 class="${h3}">FIRS reference</h3><div class="text-[13px] text-mute">IRN</div><div class="break-all font-mono text-[12.5px]">${esc(i.irn)}</div>
          ${i.csid ? `<div class="mt-2 text-[13px] text-mute">CSID</div><div class="break-all font-mono text-[12.5px]">${esc(i.csid)}</div>` : ""}</div>` : ""}
        <div class="${sec}"><h3 class="${h3}">Amounts</h3>
          <dl class="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1.5 text-sm tabular-nums">
            <dt>Net</dt><dd class="text-right">${money(i.net_amount)}</dd><dt>VAT</dt><dd class="text-right">${money(i.vat_amount)}</dd>
            <div class="col-span-2 mt-1 border-t border-line"></div><dt class="text-base font-bold">Gross</dt><dd class="text-right text-base font-bold">${money(i.gross_amount)}</dd></dl>
          <div class="mt-1.5 text-[13px] text-mute">Invoice date ${fDate(i.invoice_date)}${i.due_date ? ", due " + fDate(i.due_date) : ""}</div></div>
        <div class="${sec}"><h3 class="${h3}">Lines</h3>
          ${(i.lines || []).length
            ? `<div class="-mx-3.5 overflow-x-auto"><table class="w-full text-sm"><thead><tr><th class="th">Item</th><th class="th text-right">Qty</th><th class="th text-right">Net</th><th class="th text-right">VAT</th></tr></thead><tbody>
               ${i.lines.map((l) => `<tr><td class="td">${esc(l.description)}</td><td class="td text-right tabular-nums">${num(l.quantity)}</td><td class="td text-right tabular-nums">${money(l.line_net_amount)}</td><td class="td text-right tabular-nums">${num(l.vat_rate)}%</td></tr>`).join("")}</tbody></table></div>`
            : `<div class="text-[13px] text-mute">No lines recorded.</div>`}</div>
        <div class="${sec}"><h3 class="${h3}">FIRS submission log</h3>
          ${(i.submission_logs || []).length
            ? `<div class="-mx-3.5 overflow-x-auto"><table class="w-full text-sm"><thead><tr><th class="th">Try</th><th class="th">Result</th><th class="th text-right">Time</th></tr></thead><tbody>
               ${i.submission_logs.map((l) => `<tr><td class="td">${l.attempt_number}<div class="text-[13px] text-mute">${fDT(l.created_at)}</div></td>
                 <td class="td">${l.success ? chip(CHIP.CLEARED, "Accepted") : chip(CHIP.FAILED, "HTTP " + (l.response_status || "error"))}${l.error ? `<div class="mt-1 line-clamp-2 text-[13px] text-mute">${esc(l.error)}</div>` : ""}</td>
                 <td class="td text-right text-[13px] tabular-nums text-mute">${l.duration_ms != null ? (l.duration_ms / 1000).toFixed(1) + " s" : "—"}</td></tr>`).join("")}</tbody></table></div>`
            : `<div class="text-[13px] text-mute">Nothing has been sent to FIRS yet.</div>`}</div>`;
      $("[data-close]", box).focus();
    } catch (e) {
      box.innerHTML = `<div class="flex items-start justify-between gap-3"><h2 id="drawer-title" class="font-display text-xl font-bold">Could not open invoice</h2>${closeBtn}</div>${errorBox(e)}`;
    }
  }
  function closeDrawer(quiet) {
    const dr = $("#drawer-root");
    if (!dr.firstChild) return;
    dr.innerHTML = "";
    if (!quiet && S.lastFocus && document.contains(S.lastFocus)) S.lastFocus.focus();
  }
  function trapFocus(e) {
    const box = $("#modal-root [role=dialog]") || $("#drawer-root aside");
    if (e.key !== "Tab" || !box) return;
    const f = $$('a[href],button:not([disabled]),input,select,[tabindex]:not([tabindex="-1"])', box);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    else if (!box.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  }

  /* ---------- Excel intake ---------- */
  async function viewIntake() {
    const [h, u, clients] = await Promise.all([api("GET", EP.excelHealth), api("GET", EP.excelUploads), getClients()]);
    const list = rows(u);
    const busy = list.some((x) => ["RECEIVED", "PARSING", "PROCESSING"].indexOf(x.status) >= 0);
    const fileRow = (x) => {
      const t = num(x.total_rows) || 1;
      const via = (METHOD[x.method] || x.method) + (x.sender_email ? " from " + x.sender_email : "");
      return `<tr class="cursor-pointer hover:bg-sunken" data-upload="${esc(x.id)}">
        <td class="td"><button type="button" class="rowlink break-all" data-upload="${esc(x.id)}">${esc(x.original_filename)}</button>
          <div class="text-[13px] text-mute">${esc(via)}</div>
          <div class="text-[13px] text-mute">${esc(x.client_name)}, ${esc(rel(x.created_at))}</div>
          ${x.parse_error ? `<div class="mt-0.5 line-clamp-2 text-[13px] text-fail">${esc(x.parse_error)}</div>` : ""}</td>
        <td class="td">${uploadChip(x.status)}</td>
        <td class="td min-w-[170px]"><div class="flex h-2 overflow-hidden rounded bg-sunken" role="img" aria-label="${num(x.cleared_rows)} cleared, ${num(x.failed_rows)} failed, ${num(x.invalid_rows)} invalid of ${num(x.total_rows)} rows">
          <i class="block h-full bg-clr-solid" style="width:${(num(x.cleared_rows) / t) * 100}%"></i><i class="block h-full bg-fail-solid" style="width:${(num(x.failed_rows) / t) * 100}%"></i><i class="block h-full bg-pend-solid" style="width:${(num(x.invalid_rows) / t) * 100}%"></i></div>
          <div class="mt-1 text-[13px] text-mute">${num(x.cleared_rows)} of ${num(x.total_rows)} cleared${num(x.invalid_rows) ? `, <span class="text-pend">${num(x.invalid_rows)} invalid</span>` : ""}${num(x.failed_rows) ? `, <span class="text-fail">${num(x.failed_rows)} failed</span>` : ""}</div></td></tr>`;
    };
    const html =
      pageHead("Excel intake", "Invoices that arrive as spreadsheets, from a watched folder, an email inbox or a web upload.",
        `<button type="button" class="btn btn-ghost" data-act="template">Download template</button>`) +
      `<div class="grid gap-[18px] lg:grid-cols-[minmax(0,1fr)_320px]">
        <section class="panel"><div class="panel-head"><h2 class="panel-title">Recent files</h2><small class="text-[13px] text-mute">${num(h.total_uploads)} received in total</small></div>
          ${list.length
            ? `<div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr><th class="th">File</th><th class="th">Status</th><th class="th">Progress</th></tr></thead><tbody>${list.map(fileRow).join("")}</tbody></table></div>`
            : empty("No files yet", "Drop a spreadsheet on the right, or place one in a watched folder.")}
        </section>
        <div class="flex flex-col gap-[18px]">
          <section class="panel"><div class="panel-head"><h2 class="panel-title">Upload a spreadsheet</h2></div><div class="px-[18px] pb-[18px] pt-2">
            ${clients.length > 1 ? `<div class="mb-3"><label class="label" for="up-client">Client</label><select id="up-client" class="input">${clients.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("")}</select></div>` : ""}
            <label id="drop" class="block cursor-pointer rounded-xl border-2 border-dashed border-line px-4 py-7 text-center text-mute hover:border-brand hover:bg-sunken focus-within:ring-2 focus-within:ring-focus">
              <input type="file" id="file" class="sr-only" accept=".xlsx,.xls,.xlsm,.csv">
              <b class="mb-1 block text-ink" id="drop-title">Choose a file or drop it here</b>.xls, .xlsx or CSV, up to ${num(h.max_file_size_mb)} MB</label>
            <p class="mt-3 text-[13px] text-mute">An <b>.xls</b> file is read as Link Options print-style invoices (one invoice per sheet). Use <b>.xlsx</b> or <b>.csv</b> for the standard template. Click a file in the list to see every row and why any were not sent.</p>
          </div></section>
          <section class="panel"><div class="panel-head"><h2 class="panel-title">Intake status</h2></div><div class="px-[18px] pb-[18px] pt-2">
            <dl class="grid grid-cols-[1fr_auto] gap-x-3 gap-y-2 text-sm tabular-nums">
              <dt class="text-mute">Watched folders</dt><dd class="text-right font-semibold">${num(h.folder_watches)}</dd>
              <dt class="text-mute">Email inboxes</dt><dd class="text-right font-semibold">${num(h.email_configs)}</dd>
              <dt class="text-mute">Waiting to be read</dt><dd class="text-right font-semibold">${num(h.pending_uploads)}</dd>
              <dt class="text-mute">Files that failed</dt><dd class="text-right font-semibold ${num(h.failed_uploads) ? "text-fail" : ""}">${num(h.failed_uploads)}</dd></dl></div></section>
        </div></div>`;
    return {
      html,
      refresh: busy ? 4000 : 30000,
      after() {
        const drop = $("#drop"), file = $("#file");
        const send = async (f) => {
          if (!f) return;
          const fd = new FormData();
          fd.append("file", f);
          const sel = $("#up-client");
          if (sel) fd.append("client_id", sel.value);
          else if (clients.length === 1) fd.append("client_id", clients[0].id);
          $("#drop-title").textContent = "Uploading " + f.name + "…";
          try {
            const r = await api("POST", EP.excelUpload, { form: fd });
            toast((r && r.message) || f.name + " uploaded");
          } catch (e) { toast(e.message, true); }
          render();
        };
        file.addEventListener("change", () => send(file.files[0]));
        ["dragenter", "dragover"].forEach((n) => drop.addEventListener(n, (e) => { e.preventDefault(); drop.classList.add("border-brand", "bg-sunken"); }));
        ["dragleave", "drop"].forEach((n) => drop.addEventListener(n, (e) => { e.preventDefault(); drop.classList.remove("border-brand", "bg-sunken"); }));
        drop.addEventListener("drop", (e) => send(e.dataTransfer.files[0]));
      },
    };
  }

  /* ---------- buyer TINs ---------- */
  async function viewTins() {
    const [d, clients] = await Promise.all([api("GET", EP.buyerTins, { params: { page: S.tinPage } }), getClients()]);
    const list = rows(d), count = d.count != null ? d.count : list.length;
    const unvalidated = list.filter((t) => !t.is_validated).length;
    const html =
      pageHead("Buyer TINs", "Sage does not store buyer tax numbers, so each customer account is matched to its FIRS TIN here.") +
      `<div class="flex flex-col gap-[18px]">
        <section class="panel"><div class="panel-head"><h2 class="panel-title">Add a mapping</h2></div>
          <form id="tin-form" class="grid gap-3 px-[18px] pb-[18px] pt-2 sm:grid-cols-2 lg:grid-cols-[repeat(4,minmax(0,1fr))_auto] lg:items-end">
            <div><label class="label" for="t-client">Client</label>${clients.length
              ? `<select id="t-client" class="input" required>${clients.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("")}</select>`
              : `<input id="t-client" class="input" placeholder="No active client found" disabled>`}</div>
            <div><label class="label" for="t-acc">Sage account</label><input id="t-acc" class="input" placeholder="CUS001" required></div>
            <div><label class="label" for="t-name">Buyer name</label><input id="t-name" class="input" placeholder="Buyer Company Ltd" required></div>
            <div><label class="label" for="t-tin">FIRS TIN</label><input id="t-tin" class="input" placeholder="12345678-0001" required></div>
            <button class="btn" type="submit" ${clients.length ? "" : "disabled"}>Add and validate</button>
          </form></section>
        <section class="panel"><div class="panel-head"><h2 class="panel-title">Mappings</h2><small class="text-[13px] text-mute">${unvalidated ? unvalidated + " on this page not yet validated" : count + " in total"}</small></div>
          ${list.length
            ? `<div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr><th class="th">Sage account</th><th class="th">Buyer</th><th class="th">TIN</th><th class="th">Status</th><th class="th"></th></tr></thead><tbody>
               ${list.map((t) => `<tr><td class="td"><span class="font-semibold">${esc(t.sage_account_ref)}</span><div class="text-[13px] text-mute">${esc(t.client_name)}</div></td>
                 <td class="td">${esc(t.buyer_name)}</td><td class="td break-all font-mono text-[12.5px]">${esc(t.buyer_tin)}</td>
                 <td class="td">${t.is_validated ? chip(CHIP.GOOD, "Validated") + `<div class="mt-0.5 text-[13px] text-mute">${esc(fDate(t.validated_at))}</div>` : chip(CHIP.WARN, "Not validated")}</td>
                 <td class="td text-right"><button type="button" class="btn btn-ghost btn-sm" data-act="tin" data-tin="${esc(t.buyer_tin)}">Check with FIRS</button></td></tr>`).join("")}</tbody></table></div>` + pager(count, S.tinPage, "data-tinpage")
            : empty("No mappings yet", "Add the first customer above. B2B invoices need a buyer TIN before they can go to FIRS.")}
        </section></div>`;
    return {
      html,
      after() {
        $("#tin-form").addEventListener("submit", async (e) => {
          e.preventDefault();
          const btn = $("button[type=submit]", e.target);
          btn.disabled = true;
          btn.textContent = "Checking with FIRS…"; // the server validates the TIN before it replies
          try {
            await api("POST", EP.buyerTins, { body: { client: $("#t-client").value, sage_account_ref: $("#t-acc").value.trim(), buyer_name: $("#t-name").value.trim(), buyer_tin: $("#t-tin").value.trim() } });
            toast("Mapping added");
            render();
          } catch (er) { toast(er.message, true); btn.disabled = false; btn.textContent = "Add and validate"; }
        });
      },
    };
  }

  /* ---------- system health ---------- */
  async function viewHealth() {
    const [h, x] = await Promise.all([api("GET", EP.health, { accept: [503] }), api("GET", EP.excelHealth).catch(() => null)]);
    const state = (v) => {
      const t = String(v == null ? "" : v);
      if (t === "ok") return [CHIP.GOOD, "Working", ""];
      if (t === "unknown") return [CHIP.WARN, "Not checked", ""];
      if (t === "no_clients_configured") return [CHIP.WARN, "No active client is set up", ""];
      if (t === "unavailable") return [CHIP.BAD, "Not working", "Could not connect."];
      return [CHIP.BAD, "Not working", t.replace(/^failed:?\s*/i, "").trim() || "The check failed. See the server logs for details."];
    };
    const card = (title, v, note) => {
      const s = state(v);
      return `<div class="rounded-xl border border-line bg-sunken p-3.5"><h3 class="mb-2 font-display text-[15px] font-bold">${title}</h3>${chip(s[0], s[1])}
        ${note ? `<p class="mt-2 break-words text-[13px] text-mute">${esc(note)}</p>` : ""}${s[2] ? `<p class="mt-2 break-words text-[13px] text-mute">${esc(s[2])}</p>` : ""}</div>`;
    };
    const prod = h.environment === "PRODUCTION";
    const html =
      pageHead("System health", "Live checks of the services Eagle depends on.", `<button type="button" class="btn btn-ghost" data-act="reload">Run checks again</button>`) +
      `<section class="panel"><div class="panel-head"><h2 class="panel-title">Services</h2></div>
        <div class="grid gap-3.5 px-[18px] pb-[18px] pt-2 sm:grid-cols-2 xl:grid-cols-3">
          ${card("Eagle middleware", h.middleware)}
          ${card("Sage 200 database", h.sage200_odbc, h.sage200_db ? "Database " + h.sage200_db : "")}
          ${card("FIRS API", h.firs_api, h.firs_url)}
          ${card("Redis and Celery queue", h.redis)}
          <div class="rounded-xl border border-line bg-sunken p-3.5"><h3 class="mb-2 font-display text-[15px] font-bold">FIRS environment</h3>${chip(prod ? CHIP.BAD : CHIP.WARN, prod ? "Production" : "Sandbox")}
            <p class="mt-2 text-[13px] text-mute">${prod ? "Invoices are sent to the live FIRS system." : "Invoices go to the FIRS test system and are not legally issued."}</p></div>
          ${x ? card("Excel intake", x.status, num(x.folder_watches) + " folder watch" + (num(x.folder_watches) === 1 ? "" : "es") + ", " + num(x.email_configs) + " inbox" + (num(x.email_configs) === 1 ? "" : "es")) : ""}
        </div></section>`;
    return { html };
  }

  const VIEW = { overview: viewOverview, invoices: viewInvoices, intake: viewIntake, tins: viewTins, health: viewHealth };


  /* ---------- Excel file detail: every row and why it was or was not sent ---------- */
  async function openUpload(id) {
    S.lastFocus = document.activeElement;
    const dr = $("#drawer-root");
    dr.innerHTML = `<div class="fixed inset-0 z-40 bg-black/50" data-close></div>
      <aside role="dialog" aria-modal="true" aria-labelledby="drawer-title" class="fixed inset-y-0 right-0 z-[41] w-full max-w-[720px] overflow-y-auto bg-panel p-5 shadow-2xl motion-safe:animate-slidein sm:p-6"><p class="text-mute" id="drawer-title">Loading file…</p></aside>`;
    const box = $("aside", dr);
    try {
      const u = await api("GET", EP.excelUploadDetail.replace("{id}", id));
      const list = (u.invoices || []).slice().sort((a, b) => a.row_number - b.row_number);
      const reason = (r) => (r.validation_errors && r.validation_errors.length ? r.validation_errors.join(" ") : r.error_message || "");
      box.innerHTML = `
        <div class="flex items-start justify-between gap-3"><div>
          <div class="text-[13px] text-mute">${esc(METHOD[u.method] || u.method)} for ${esc(u.client_name)}, ${esc(rel(u.created_at))}</div>
          <h2 id="drawer-title" class="break-all font-display text-2xl font-bold leading-tight tracking-tight">${esc(u.original_filename)}</h2>
          <div class="mt-2">${uploadChip(u.status)}</div></div>${closeBtn}</div>
        ${u.parse_error ? `<div role="alert" class="mt-5 rounded-xl bg-fail-bg p-3 text-sm text-fail"><b class="block">Could not read this file</b>${esc(u.parse_error)}</div>` : ""}
        <dl class="mt-5 grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-line bg-line text-sm sm:grid-cols-4">
          ${[["Rows", u.total_rows], ["Cleared", u.cleared_rows], ["Failed", u.failed_rows], ["Not sent", u.invalid_rows]]
            .map((c) => `<div class="bg-panel p-3"><dt class="text-[13px] text-mute">${c[0]}</dt><dd class="font-display text-xl font-bold tabular-nums">${num(c[1])}</dd></div>`).join("")}
        </dl>
        <div class="mt-6"><h3 class="mb-2 font-display text-[15px] font-bold">Rows</h3>
        ${list.length
          ? `<div class="-mx-3.5 overflow-x-auto"><table class="w-full text-sm"><thead><tr><th class="th">#</th><th class="th">Invoice</th><th class="th">Customer</th><th class="th text-right">Total</th><th class="th">Result</th></tr></thead><tbody>
             ${list.map((r) => `<tr><td class="td tabular-nums text-mute">${num(r.row_number)}</td>
               <td class="td whitespace-nowrap font-semibold">${esc(r.invoice_number || "—")}</td>
               <td class="td min-w-[140px]">${esc(r.buyer_name || "—")}${r.buyer_tin ? `<div class="text-[13px] text-mute">TIN ${esc(r.buyer_tin)}</div>` : ""}</td>
               <td class="td whitespace-nowrap text-right tabular-nums">${r.gross_amount != null ? money(r.gross_amount) : "—"}</td>
               <td class="td min-w-[200px]">${rowChip(r.status)}${reason(r) ? `<div class="mt-1 text-[13px] text-mute">${esc(reason(r))}</div>` : ""}${r.irn ? `<div class="mt-1 break-all font-mono text-[12px] text-mute">${esc(r.irn)}</div>` : ""}</td></tr>`).join("")}
             </tbody></table></div>`
          : `<div class="text-[13px] text-mute">No rows were recorded for this file.</div>`}</div>`;
      $("[data-close]", box).focus();
    } catch (e) {
      box.innerHTML = `<div class="flex items-start justify-between gap-3"><h2 id="drawer-title" class="font-display text-xl font-bold">Could not open file</h2>${closeBtn}</div>${errorBox(e)}`;
    }
  }

  /* ---------- Pull from Sage 200: choose how many, preview, then pull ---------- */
  const PRESETS = [10, 25, 50, 100, 250, 500];
  const CHIP_ON = "border-brand bg-brand text-brand-on";
  const CHIP_OFF = "border-line bg-transparent text-ink hover:bg-sunken";
  const P = { clients: [], clientId: "", count: 50, preview: null, previewKey: "", busy: "", queued: null, timer: null };

  const pullClient = () => P.clients.find((c) => c.id === P.clientId) || P.clients[0];
  const pullKey = () => P.clientId + ":" + P.count;
  const validCount = () => Number.isInteger(P.count) && P.count >= 1 && P.count <= 1000;
  const envChip = (env) => (env === "PRODUCTION" ? chip("bg-fail-bg text-fail", "Live FIRS system") : chip("bg-pend-bg text-pend", "FIRS sandbox (test)"));

  function modalShell(inner) {
    return `<div class="fixed inset-0 z-50 bg-black/50" data-close-modal></div>
      <div role="dialog" aria-modal="true" aria-labelledby="pull-title" class="fixed left-1/2 top-1/2 z-[51] max-h-[calc(100vh-24px)] w-[min(700px,calc(100%-24px))] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-2xl border border-line bg-panel p-5 shadow-2xl sm:p-6">${inner}</div>`;
  }
  const closeModalBtn = `<button type="button" data-close-modal class="grid h-8 w-8 flex-none place-items-center rounded-lg hover:bg-sunken" aria-label="Close"><svg viewBox="0 0 24 24" class="h-[18px] w-[18px] fill-none stroke-current stroke-2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>`;

  async function openPull() {
    S.lastFocus = document.activeElement;
    const root = $("#modal-root");
    root.innerHTML = modalShell(`<p class="text-mute" id="pull-title">Loading…</p>`);
    try { P.clients = await api("GET", EP.clients); }
    catch (e) { root.innerHTML = modalShell(`<div class="flex items-start justify-between gap-3"><h2 id="pull-title" class="font-display text-xl font-bold">Pull from Sage 200</h2>${closeModalBtn}</div>${errorBox(e)}`); return; }
    P.clientId = (P.clients.find((c) => c.id === P.clientId) || P.clients[0] || {}).id || "";
    P.preview = null; P.previewKey = ""; P.busy = ""; P.queued = null;
    const c = pullClient();
    P.count = c && c.sage.imported === 0 ? 10 : 50; // a small first pull is the safe way to start
    renderPull();
  }
  function closeModal() {
    clearTimeout(P.timer);
    const root = $("#modal-root");
    if (!root.firstChild) return;
    root.innerHTML = "";
    if (S.lastFocus && document.contains(S.lastFocus)) S.lastFocus.focus();
  }
  const isModalOpen = () => !!$("#modal-root").firstChild;

  function lastPullHtml(l, at) {
    if (!l) return "";
    if (l.error) return `<div role="alert" class="mt-3 rounded-xl bg-fail-bg p-3 text-sm text-fail"><b class="block">The last pull did not run${at ? ", " + esc(rel(at)) : ""}</b>${esc(l.error)}</div>`;
    return `<div class="mt-3 rounded-xl bg-sunken p-3 text-sm"><b>Last pull${at ? ", " + esc(rel(at)) : ""}:</b> read ${num(l.pulled)}, cleared ${num(l.cleared)}, failed ${num(l.failed)}, already in Eagle ${num(l.skipped)}.
      ${(l.errors || []).length ? `<div class="mt-2 text-fail"><b>${l.errors.length} could not be read and were not saved:</b><ul class="mt-1 list-disc pl-5">${l.errors.slice(0, 8).map((e) => `<li>${esc(e.number)}: ${esc(e.reason)}</li>`).join("")}</ul></div>` : ""}
      ${num(l.failed) ? `<div class="mt-2"><a class="linkbtn" href="#/invoices?status=FAILED" data-close-modal>See failed invoices</a></div>` : ""}</div>`;
  }

  function renderPull() {
    const root = $("#modal-root");
    if (!root.firstChild) return;
    const c = pullClient();
    if (!c) {
      root.innerHTML = modalShell(`<div class="flex items-start justify-between gap-3"><h2 id="pull-title" class="font-display text-xl font-bold">Pull from Sage 200</h2>${closeModalBtn}</div>
        <p class="mt-4 text-mute">There is no active client yet. Add one in the admin (Core, Client companies) with <b>Is active</b> ticked and its Sage 200 server and database filled in.</p>`);
      return;
    }
    const sg = c.sage;
    root.innerHTML = modalShell(`
      <div class="flex items-start justify-between gap-3"><div>
        <h2 id="pull-title" class="font-display text-2xl font-bold leading-tight tracking-tight">Pull from Sage 200</h2>
        <p class="mt-1 text-sm text-mute">Choose how many invoices to pull. Each pull carries on after the last one.</p></div>${closeModalBtn}</div>
      ${P.clients.length > 1 ? `<div class="mt-4"><label class="label" for="pull-client">Client</label><select id="pull-client" class="input">${P.clients.map((x) => `<option value="${esc(x.id)}" ${x.id === P.clientId ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></div>` : `<div class="mt-4 text-sm text-mute">Client: <b class="text-ink">${esc(c.name)}</b></div>`}
      <div class="mt-3 rounded-xl border border-line bg-sunken p-3 text-sm">
        Eagle holds <b>${num(sg.imported).toLocaleString()}</b> invoice(s) from Sage 200. The next pull starts after Sage record <b>#${num(sg.bookmark).toLocaleString()}</b>${num(sg.bookmark) === 0 ? " (the very beginning)" : ""}.
        ${sg.running ? `<div class="mt-1 font-semibold text-sub">A pull is running right now. Wait for it to finish.</div>` : ""}</div>
      <div id="pull-last">${P.queued && P.queued.done ? "" : lastPullHtml(sg.last_pull, sg.last_pull_at)}</div>
      <fieldset class="mt-5"><legend class="label">How many invoices?</legend>
        <div class="flex flex-wrap items-center gap-2" id="pull-presets">
          ${PRESETS.map((n) => `<button type="button" data-preset="${n}" class="rounded-full border px-3.5 py-1.5 text-sm font-semibold ${P.count === n ? CHIP_ON : CHIP_OFF}">${n}</button>`).join("")}
          <label class="ml-1 flex items-center gap-2 text-sm text-mute" for="pull-count">or</label>
          <input id="pull-count" type="number" min="1" max="1000" inputmode="numeric" class="input !w-24" value="${esc(P.count)}" aria-describedby="pull-count-help">
        </div>
        <p id="pull-count-help" class="mt-1.5 text-[13px] text-mute">From 1 to 1,000 at a time. Start small to check the results.</p></fieldset>
      <div class="mt-5 flex flex-wrap items-center gap-3">
        <button type="button" class="btn btn-ghost" data-act="pull-preview" id="pull-preview">Preview</button>
        <button type="button" class="btn" data-act="pull-run" id="pull-run" disabled>Pull</button>
        <span class="text-[13px] text-mute" id="pull-hint">Preview reads Sage and shows exactly what would be sent. It saves and sends nothing.</span>
      </div>
      <div id="pull-result" aria-live="polite">${P.queued ? queuedHtml() : P.preview ? previewHtml(P.preview) : ""}</div>`);
    syncPullControls();
    const first = $("#pull-preview", root);
    if (first && !P.queued) first.focus();
  }

  // Keep the buttons truthful without re-rendering (re-rendering would steal focus while typing).
  function syncPullControls() {
    const root = $("#modal-root");
    if (!root.firstChild || !pullClient()) return;
    $$("[data-preset]", root).forEach((b) => {
      const on = Number(b.dataset.preset) === P.count;
      b.className = "rounded-full border px-3.5 py-1.5 text-sm font-semibold " + (on ? CHIP_ON : CHIP_OFF);
    });
    const running = pullClient().sage.running || !!P.queued;
    const ready = !!P.preview && P.previewKey === pullKey() && P.preview.new > 0 && validCount() && !running && !P.busy;
    const prev = $("#pull-preview", root), run = $("#pull-run", root), hint = $("#pull-hint", root);
    if (prev) { prev.disabled = !validCount() || running || !!P.busy; prev.textContent = P.busy === "preview" ? "Reading Sage…" : "Preview"; }
    if (run) {
      run.disabled = !ready;
      run.textContent = ready ? `Pull ${P.preview.new} invoice${P.preview.new === 1 ? "" : "s"}` : "Pull";
    }
    if (hint) {
      hint.textContent = !validCount() ? "Enter a whole number from 1 to 1,000."
        : P.preview && P.previewKey === pullKey() ? (P.preview.new > 0 ? "Check the preview below, then pull." : "Nothing new to pull from this position.")
        : "Preview reads Sage and shows exactly what would be sent. It saves and sends nothing.";
    }
  }

  function previewHtml(r) {
    const notable = (r.types || []).some((t) => (t.code || "").toLowerCase() !== "inv" && (t.code || "") !== "OInv");
    return `<div class="mt-5 rounded-xl border border-line p-4 text-sm">
      <div class="flex flex-wrap items-center justify-between gap-2"><b class="font-display text-[15px]">What this pull would take</b>${envChip(r.environment)}</div>
      ${r.returned === 0 ? `<p class="mt-2 text-mute">Sage has nothing more after record #${num(r.bookmark).toLocaleString()}.</p>` : `
      <p class="mt-2">Sage returned <b>${r.returned}</b> row(s)${r.date_from ? `, dated ${esc(fDate(r.date_from))} to ${esc(fDate(r.date_to))}` : ""}.
        <b>${r.new}</b> are new${r.already_in_eagle ? `, ${r.already_in_eagle} already in Eagle` : ""}.</p>
      ${r.new ? `<p class="mt-1">They would be reported as <b>${r.b2b}</b> business (buyer TIN known) and <b>${r.b2c}</b> consumer (no buyer TIN). A customer is only a business once its Sage account code is on the <a class="linkbtn" href="#/tins" data-close-modal>Buyer TINs</a> page.</p>` : ""}
      <div class="mt-3"><div class="label !mb-1">Sage's transaction types in these rows</div>
        <ul class="m-0 list-none p-0">${(r.types || []).map((t) => `<li class="flex justify-between gap-3 border-t border-line py-1"><span><b>${esc(t.code)}</b> <span class="text-mute">(code ${esc(t.code_id)})</span></span><span class="tabular-nums">${t.count}</span></li>`).join("")}</ul>
        <p class="mt-1.5 text-[13px] ${notable ? "font-semibold text-fail" : "text-mute"}">Rows with a debit are sent as invoices and rows with a credit as credit notes (${r.debit_rows} and ${r.credit_rows} here). ${notable ? "These include types other than invoices. A receipt or payment listed here would be sent to FIRS as a credit note. Do not pull until you have checked." : ""}</p></div>
      ${r.problem_total ? `<div class="mt-3 rounded-lg bg-fail-bg p-3 text-fail"><b>${r.problem_total} row(s) cannot be read and would not be saved:</b><ul class="mt-1 list-disc pl-5">${r.problems.map((p) => `<li>${p.count} x ${esc(p.reason)}</li>`).join("")}</ul></div>` : ""}
      ${r.older_than_since ? `<div class="mt-3 rounded-lg bg-fail-bg p-3 text-fail">Some rows are older than the date filter. Replace odbc_client.py with the updated version.</div>` : ""}
      ${(r.samples || []).length ? `<div class="mt-3 -mx-1 overflow-x-auto"><table class="w-full text-[13px]"><thead><tr><th class="th !py-1.5">Invoice</th><th class="th !py-1.5">Date</th><th class="th !py-1.5">Customer</th><th class="th !py-1.5 text-right">Gross</th></tr></thead><tbody>
        ${r.samples.map((s) => `<tr><td class="td !py-1.5 whitespace-nowrap font-semibold">${esc(s.number)}<span class="ml-1 font-normal text-mute">${esc(s.code)}</span></td><td class="td !py-1.5 whitespace-nowrap">${esc(fDate(s.date))}</td><td class="td !py-1.5">${esc(s.customer)}</td><td class="td !py-1.5 whitespace-nowrap text-right tabular-nums">${money(s.gross)}</td></tr>`).join("")}</tbody></table>
        <p class="mt-1 text-[13px] text-mute">First ${r.samples.length} of ${r.new} new rows.</p></div>` : ""}`}
    </div>`;
  }

  function queuedHtml() {
    const q = P.queued;
    if (q.done) return `<div class="mt-5">${q.done.error ? lastPullHtml(q.done, null) : `<div class="rounded-xl border border-line p-4 text-sm"><div class="flex flex-wrap items-center justify-between gap-2"><b class="font-display text-[15px]">Pull finished</b>${envChip(q.done.environment)}</div>${lastPullHtml(q.done, null).replace("mt-3", "mt-2")}
        <p class="mt-2 text-[13px] text-mute">Bookmark moved from #${num(q.done.bookmark_before).toLocaleString()} to #${num(q.done.bookmark_after).toLocaleString()}.${q.done.retries_scheduled === false ? " Redis was not reachable, so failed invoices were not queued for automatic retry; retry them from the Invoices page." : ""}</p></div>`}</div>`;
    return `<div class="mt-5 rounded-xl border border-line p-4 text-sm" role="status"><b class="font-display text-[15px]">Pulling ${num(q.count)} invoice(s)…</b>
      <p class="mt-1 text-mute">Each invoice is read from Sage, sent to FIRS and saved. This can take a few seconds per invoice. You can close this window; the pull carries on and invoices appear on the Overview as they are processed.</p></div>`;
  }

  async function runPreview() {
    if (!validCount() || P.busy) return;
    P.busy = "preview"; P.preview = null; syncPullControls();
    const key = pullKey(), box = $("#pull-result");
    box.innerHTML = `<p class="mt-5 text-mute">Reading Sage 200…</p>`;
    try {
      const r = await api("POST", EP.sagePreview, { body: { client: P.clientId, count: P.count } });
      if (!isModalOpen()) return;
      P.preview = r; P.previewKey = key;
      $("#pull-result").innerHTML = previewHtml(r);
    } catch (e) {
      if (isModalOpen()) $("#pull-result").innerHTML = errorBox(e);
    } finally { P.busy = ""; syncPullControls(); }
  }

  async function runPull() {
    const r = P.preview;
    if (!r || P.previewKey !== pullKey() || P.busy) return;
    if (r.environment === "PRODUCTION" && !window.confirm("This sends " + r.new + " invoice(s) to the LIVE FIRS system. These are real tax submissions. Continue?")) return;
    P.busy = "pull"; syncPullControls();
    const baseline = pullClient().sage.last_pull_at;
    try {
      const res = await api("POST", EP.sagePull, { body: { client: P.clientId, count: P.count } });
      toast((res && res.message) || "Pull queued");
      P.queued = { count: P.count, baseline, started: Date.now(), done: null };
      P.preview = null;
      if (isModalOpen()) { $("#pull-result").innerHTML = queuedHtml(); }
      schedulePullPoll();
    } catch (e) {
      toast(e.message, true);
      if (isModalOpen()) $("#pull-result").innerHTML = errorBox(e);
    } finally { P.busy = ""; syncPullControls(); }
  }

  function schedulePullPoll() {
    clearTimeout(P.timer);
    P.timer = setTimeout(async () => {
      if (!P.queued || P.queued.done) return;
      try {
        P.clients = await api("GET", EP.clients);
        const sg = pullClient().sage;
        if (sg.last_pull_at && sg.last_pull_at !== P.queued.baseline && !sg.running) {
          P.queued.done = sg.last_pull;
          if (isModalOpen()) { renderPull(); }
          render(true);
          refreshBadge();
          return;
        }
      } catch (e) { /* keep waiting; a blip should not end the wait */ }
      if (Date.now() - P.queued.started < 20 * 60 * 1000) schedulePullPoll();
      else if (isModalOpen()) $("#pull-result").innerHTML = `<p class="mt-5 text-mute">Still running. Close this window and check the Overview.</p>`;
    }, 3000);
  }

  document.addEventListener("input", (e) => {
    if (e.target && e.target.id === "pull-count") {
      const v = e.target.value.trim();
      P.count = /^\d+$/.test(v) ? Number(v) : NaN;
      syncPullControls();
    }
  });
  document.addEventListener("change", (e) => {
    if (e.target && e.target.id === "pull-client") {
      P.clientId = e.target.value; P.preview = null; P.previewKey = ""; P.queued = null;
      renderPull();
    }
  });

  /* ---------- events ---------- */
  document.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-open],[data-upload],[data-preset],[data-act],[data-close],[data-close-modal],[data-page],[data-tinpage],[data-copy]");
    if (!t) return;
    if (t.hasAttribute("data-close-modal")) { closeModal(); return; }
    if (t.hasAttribute("data-close")) { closeDrawer(); return; }
    if (t.dataset.preset) { P.count = Number(t.dataset.preset); P.preview = null; P.previewKey = ""; const i = $("#pull-count"); if (i) i.value = P.count; $("#pull-result").innerHTML = ""; syncPullControls(); return; }
    if (t.dataset.upload) { openUpload(t.dataset.upload); return; }
    if (t.dataset.page) { S.f.page = Number(t.dataset.page); history.replaceState(null, "", filtersHash()); loadInvoices(); return; }
    if (t.dataset.tinpage) { S.tinPage = Number(t.dataset.tinpage); render(); return; }
    if (t.dataset.copy) {
      try { await navigator.clipboard.writeText(t.dataset.copy); toast("IRN copied"); }
      catch (er) { toast("Copying is blocked in this browser. Select the IRN and copy it by hand.", true); }
      return;
    }
    if (t.dataset.open) { openInvoice(t.dataset.open); return; }

    const act = t.dataset.act;
    try {
      if (act === "reload") { render(); return; }
      if (act === "template") {
        const blob = await api("GET", EP.excelTemplate, { blob: true });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "LinkOptions_Invoice_Template.xlsx";
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 4000);
        return;
      }
      if (act === "pull-open") { openPull(); return; }
      if (act === "pull-preview") { runPreview(); return; }
      if (act === "pull-run") { runPull(); return; }
      if (act === "tin") {
        t.disabled = true;
        const r = await api("GET", EP.validateTin, { params: { tin: t.dataset.tin } });
        t.disabled = false;
        const ok = r && r.success;
        toast(ok ? "FIRS recognises TIN " + t.dataset.tin : (r && (r.error || r.message)) || "FIRS could not confirm TIN " + t.dataset.tin, !ok);
        return;
      }
      if (act === "retry" || act === "submit") {
        if (t.dataset.status === "REJECTED" && !window.confirm("FIRS rejected " + t.dataset.no + ". Submit it again only after the problem has been fixed. Continue?")) return;
        t.disabled = true;
        const r = await api("POST", (act === "retry" ? EP.retry : EP.submit).replace("{id}", t.dataset.id));
        toast((r && r.message) || "Queued " + t.dataset.no);
        closeDrawer(true);
        setTimeout(() => { render(true); refreshBadge(); }, 800);
      }
    } catch (er) {
      if (t) t.disabled = false;
      toast(er.message, true);
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isModalOpen()) closeModal();
    else if (e.key === "Escape" && isDrawerOpen()) closeDrawer();
    else trapFocus(e);
  });
  $("#theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.classList.toggle("dark");
    try { localStorage.setItem("eagle-theme", dark ? "dark" : "light"); } catch (e) { /* private mode */ }
  });
  window.addEventListener("hashchange", route);
  route();
})();
