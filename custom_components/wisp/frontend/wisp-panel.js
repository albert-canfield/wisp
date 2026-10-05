/*
 * Wisp panel: the live map, the rooms of each floor with their calibration, the nodes and the hive,
 * for admins. Registered by the integration in the sidebar, no build step. Live from
 * wisp/panel/subscribe; the buttons call the wisp services. The map is the map card itself.
 */

const DURATIONS = [30, 60, 90, 120, 180, 300]; // s to record
const DURATION = 60; // s, as the services
const LEAVE_S = 30; // s to leave the floor before the empty floor records
const PREFS = "wisp-panel"; // this browser's duration, map turn and mirror
// The logo's footprint, as in the card
const SOLE = "M0-13c4.6 0 6.4 4.4 6.4 8.6 0 4.6-2.2 7.9-6.4 7.9s-6.4-3.3-6.4-7.9C-6.4-8.6-4.6-13 0-13Z";
const HEEL = "M0 5.6c3.3 0 4.8 2.1 4.8 4.4 0 2.6-2 4-4.8 4s-4.8-1.4-4.8-4c0-2.3 1.5-4.4 4.8-4.4Z";
const FEET = `<svg class="feet" viewBox="-22 -18 44 36" aria-hidden="true"><g transform="translate(-7 4) rotate(-12) scale(.9)"><path d="${SOLE}"/><path d="${HEEL}"/></g><g transform="translate(8 -4) rotate(10) scale(.9)"><path d="${SOLE}"/><path d="${HEEL}"/></g></svg>`;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const secs = (s) => (s >= 120 && s % 60 === 0 ? `${s / 60} min` : `${s} s`);
const attr = (name, value) => (value == null ? "" : ` data-${name}="${esc(value)}"`);

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS)) || {};
  } catch {
    return {};
  }
}

function savePrefs(prefs) {
  try {
    localStorage.setItem(PREFS, JSON.stringify(prefs));
  } catch {
    // private browsing: the choice lasts until the page closes
  }
}

function navigate(path) {
  history.pushState(null, "", path);
  window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
}

/* The hub's own floor holds the nodes without a floor: "Home" alone, "No floor" beside real floors. */
const floorLabel = (f, floors) => (f.floor != null ? f.name : floors.length > 1 ? "No floor" : "Home");
const floorPhrase = (f) => (f.floor != null ? f.name : "the floor");

/* Replace only the children whose markup changed, so a control open in another one survives the update. */
function patch(container, items) {
  const old = new Map([...container.children].map((el) => [el.dataset.key, el]));
  const out = new Map();
  let prev = null;
  for (const [key, html] of items) {
    let el = old.get(key);
    old.delete(key);
    if (!el || el._html !== html) {
      const tpl = document.createElement("template");
      tpl.innerHTML = html.trim();
      const fresh = tpl.content.firstElementChild;
      fresh.dataset.key = key;
      fresh._html = html;
      if (el) el.replaceWith(fresh);
      el = fresh;
    }
    const at = prev ? prev.nextElementSibling : container.firstElementChild;
    if (at !== el) container.insertBefore(el, at);
    out.set(key, el);
    prev = el;
  }
  for (const el of old.values()) el.remove();
  return out;
}

class WispPanel extends HTMLElement {
  constructor() {
    super();
    this._prefs = loadPrefs();
    this._duration = DURATIONS.includes(this._prefs.duration) ? this._prefs.duration : DURATION;
    this._open = null; // the question open under a row: {kind, floor, area}
    this._busy = null; // the question whose action runs
    this._failure = null; // {open, message}
  }

  set hass(hass) {
    this._hass = hass;
    this._setup();
    const dark = !!hass.themes?.darkMode;
    if (dark !== this._dark) {
      this._dark = dark;
      this._page.classList.toggle("dark", dark);
    }
    this._menuButton();
    if (this._card) this._card.hass = hass;
    this._subscribe();
  }

  set narrow(narrow) {
    this._narrow = narrow;
    this._setup();
    this._menuButton();
  }

  set panel(panel) {
    this._cardUrl = panel?.config?.card;
    this._setup();
    this._loadCard();
  }

  connectedCallback() {
    this._setup();
    this._subscribe();
    this._timer = setInterval(() => this._tick(), 1000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    const sub = this._sub;
    this._sub = null;
    if (sub) sub.then((unsub) => unsub?.()).catch(() => {});
    this._keepAwake(false);
  }

  _setup() {
    if (this.shadowRoot) return;
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `<style>${STYLE}</style>
      <div class="page">
        <header class="toolbar">
          <button class="menu" data-act="menu" aria-label="Sidebar" hidden><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z"/></svg></button>
          <h1>Wisp</h1>
        </header>
        <div class="scroll">
          <div class="runs"></div>
          <div class="message"></div>
          <main hidden>
            <section class="map-col" aria-label="Map">
              <div class="map"></div>
              <div class="map-tools">
                <button data-act="turn">Turn 90°</button>
                <button data-act="mirror" aria-pressed="false">Mirror</button>
              </div>
            </section>
            <section class="rooms-col" aria-label="Rooms and calibration"></section>
            <section class="sheet nodes-col" aria-labelledby="wisp-nodes">
              <div class="head"><h2 id="wisp-nodes">Nodes</h2><span class="note nodes-note"></span></div>
              <div class="nodes"></div>
              <div class="foot">
                <p>Set the area each node stands in under Wisp, the node, Change node: its floor comes with it.</p>
                <button data-act="settings">Wisp settings</button>
              </div>
            </section>
            <section class="sheet hive-col" aria-labelledby="wisp-hive">
              <div class="head"><h2 id="wisp-hive">Hive</h2><span class="note hive-note"></span></div>
              <div class="hive"></div>
            </section>
          </main>
        </div>
      </div>`;
    this._page = root.querySelector(".page");
    root.addEventListener("click", (e) => this._click(e));
    root.addEventListener("change", (e) => this._change(e));
    root.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this._open) this._close();
    });
    this._mirrorButton();
    this._loadCard();
    this._render();
  }

  /* The map card, loaded by every dashboard; the panel loads it itself when opened first. */
  _loadCard() {
    if (!customElements.get("wisp-map-card") && this._cardUrl && !this._importing) {
      this._importing = import(this._cardUrl).catch(() => {});
    }
    if (this._cardWait) return;
    this._cardWait = customElements.whenDefined("wisp-map-card").then(() => {
      const card = document.createElement("wisp-map-card");
      card.setConfig(this._mapConfig());
      if (this._hass) card.hass = this._hass;
      this.shadowRoot.querySelector(".map").append(card);
      this._card = card;
    });
  }

  _mapConfig() {
    return { title: "Map", rotate: Number(this._prefs.rotate) || 0, flip: !!this._prefs.flip };
  }

  _mirrorButton() {
    this.shadowRoot.querySelector("[data-act=mirror]").setAttribute("aria-pressed", String(!!this._prefs.flip));
  }

  /* The sidebar's button where Home Assistant hides the sidebar: on a phone, or when set to hidden. */
  _menuButton() {
    this.shadowRoot.querySelector(".menu").hidden = !(this._narrow || this._hass?.dockedSidebar === "always_hidden");
  }

  _subscribe() {
    if (this._sub || !this._hass || !this.isConnected || Date.now() < (this._retryAt ?? 0)) return;
    this._sub = this._hass.connection.subscribeMessage((data) => this._update(data), { type: "wisp/panel/subscribe" });
    this._sub.catch((err) => {
      this._sub = null;
      this._retryAt = Date.now() + 30000;
      this._error = err?.message || "Wisp is not loaded";
      this._render();
    });
  }

  _update(data) {
    const before = this._data;
    this._data = data;
    this._received = Date.now();
    this._error = null;
    if (before?.loaded && data.loaded) this._finished(before, data);
    this._keepAwake(data.floors.some((f) => f.run));
    this._render();
  }

  /* A run that ran out since the last update: say how many samples the room or floor has now. */
  _finished(before, data) {
    for (const old of before.floors) {
      const run = old.run;
      const now = data.floors.find((f) => f.floor === old.floor);
      if (!run || run.seconds_left > 2 || now?.run) continue;
      let message;
      if (run.area) {
        const n = now?.areas.find((a) => a.area === run.area)?.samples ?? run.recorded;
        message = n >= data.min_samples
          ? `Done: the ${run.name} has ${plural(n, "sample", "samples")}.`
          : `The ${run.name} has ${plural(n, "sample", "samples")} and needs ${data.min_samples}: calibrate it again and keep moving.`;
      } else {
        message = `Done: the empty floor has ${plural(now?.empty_samples ?? run.recorded, "sample", "samples")}.`;
      }
      this.dispatchEvent(new CustomEvent("hass-notification", { detail: { message }, bubbles: true, composed: true }));
      navigator.vibrate?.(200);
    }
  }

  /* Keep a phone's screen on while a run counts down. The browser drops the lock when the page hides. */
  _keepAwake(on) {
    if (on && navigator.wakeLock && !this._locking && (!this._lock || this._lock.released)) {
      this._locking = true;
      navigator.wakeLock.request("screen")
        .then((lock) => { this._lock = lock; })
        .catch(() => {})
        .finally(() => { this._locking = false; });
    } else if (!on && this._lock) {
      this._lock.release().catch(() => {});
      this._lock = null;
    }
  }

  _tick() {
    const el = this.shadowRoot?.querySelector(".age");
    if (el) el.textContent = this._age();
  }

  _age() {
    const hive = this._data?.hive;
    return hive ? `${hive.age + Math.round((Date.now() - this._received) / 1000)} s ago` : "";
  }

  // Actions

  _click(e) {
    const button = e.target.closest?.("button[data-act]");
    if (!button || button.disabled) return;
    const { act, floor, area, target, device } = button.dataset;
    if (act === "menu") this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true }));
    else if (act === "ask-room") this._ask("room", floor, area);
    else if (act === "ask-clear") this._ask("clear", floor, area);
    else if (act === "ask-empty") this._ask("empty", floor);
    else if (act === "ask-clear-all") this._ask("clear-all");
    else if (act === "close") this._close();
    else if (act === "start-room") this._call("calibrate_room", { area, duration: this._duration });
    else if (act === "start-empty") this._call("calibrate_empty", { floor: target, duration: this._duration, delay: LEAVE_S });
    else if (act === "clear") this._call("clear_calibration", area ? { area } : {});
    else if (act === "stop") this._call("stop_calibration", { floor: target });
    else if (act === "device") navigate(`/config/devices/device/${device}`);
    else if (act === "settings") navigate("/config/integrations/integration/wisp");
    else if (act === "turn" || act === "mirror") {
      if (act === "turn") this._prefs.rotate = ((Number(this._prefs.rotate) || 0) + 90) % 360;
      else this._prefs.flip = !this._prefs.flip;
      savePrefs(this._prefs);
      this._mirrorButton();
      this._card?.setConfig(this._mapConfig());
    }
  }

  _change(e) {
    const el = e.target;
    if (el.dataset.act === "duration") {
      // No redraw: the select shows the choice already, and keeps its focus
      this._duration = Number(el.value) || DURATION;
      this._prefs.duration = this._duration;
      savePrefs(this._prefs);
    } else if (el.dataset.act === "pick") {
      if (el.value) this._ask("room", el.dataset.floor, el.value);
      else this._close();
    }
  }

  _ask(kind, floor, area) {
    this._open = { kind, floor, area };
    this._failure = null;
    this._render();
    this.shadowRoot.querySelector(".ask .primary, .ask .danger")?.focus();
  }

  _close() {
    this._open = null;
    this._failure = null;
    this._render();
  }

  async _call(service, data) {
    const open = this._open;
    this._busy = open;
    this._failure = null;
    this._render();
    try {
      await this._hass.callService("wisp", service, data, undefined, false);
      if (this._open === open) this._open = null;
    } catch (err) {
      this._failure = { open, message: err?.message || "Wisp could not do that." };
    } finally {
      this._busy = null;
      this._render();
    }
  }

  _isOpen(kind, floor, area) {
    const o = this._open;
    return !!o && o.kind === kind && o.floor === floor && o.area === area;
  }

  // Drawing

  _render() {
    const root = this.shadowRoot;
    if (!root) return;
    const d = this._data;
    const ready = !this._error && d?.loaded && d.nodes.length > 0;
    root.querySelector("main").hidden = !ready;
    root.querySelector(".runs").hidden = !ready;
    root.querySelector(".message").innerHTML = ready ? "" : this._message(d);
    if (!ready) return;
    patch(root.querySelector(".runs"), d.floors.filter((f) => f.run).map((f) => [f.floor ?? "", this._run(f)]));
    this._rooms(d);
    const added = d.nodes.filter((n) => n.added);
    root.querySelector(".nodes-note").textContent = `${added.filter((n) => n.online).length} of ${added.length} online`;
    patch(root.querySelector(".nodes"), d.nodes.map((n) => [n.mac, this._node(n, d)]));
    const note = root.querySelector(".hive-note");
    note.textContent = d.hive ? (d.hive.in_sync ? "in sync" : "syncing") : "";
    note.className = `note hive-note ${d.hive?.in_sync ? "sync" : "wait"}`;
    root.querySelector(".hive").innerHTML = this._hive(d);
  }

  _message(d) {
    const settings = `<button data-act="settings">Wisp settings</button>`;
    const [lead, text, more] = this._error
      ? ["Wisp is not available", this._error, ""]
      : !d
        ? ["Reading the grid", "", ""]
        : !d.loaded
          ? ["Wisp is not set up", "Add Wisp under Settings, Devices and services, or turn it back on there.", settings]
          : ["No nodes yet", "Wisp adds the nodes ESPHome finds running its firmware. To add one by its address, use Add node on the Wisp integration page.", settings];
    return `<div class="sheet empty">${FEET}<p class="lead">${esc(lead)}</p>${text ? `<p>${esc(text)}</p>` : ""}${more}</div>`;
  }

  _run(f) {
    const r = f.run;
    const waiting = r.starts_in > 0;
    const done = r.recorded + r.skipped;
    const total = done + r.seconds_left;
    const pct = total ? Math.round((100 * done) / total) : 100;
    let title, text;
    if (r.area) {
      title = `Walk around the ${r.name}, keep moving`;
      text = `${plural(r.recorded, "sample", "samples")} so far${r.skipped ? `, ${r.skipped} s too still to count` : ""}.`;
    } else if (waiting) {
      title = `Leave ${floorPhrase(f)} now`;
      text = `Or keep everyone still. Recording starts in ${r.starts_in} s and lasts ${secs(r.seconds_left - r.starts_in)}.`;
    } else {
      title = `Keep ${floorPhrase(f)} empty and still`;
      text = `${plural(r.recorded, "sample", "samples")} so far.`;
    }
    const left = waiting ? r.starts_in : r.seconds_left;
    return `<div class="run${waiting ? " wait" : ""}">
      <div class="run-text"><b>${esc(title)}</b><span>${esc(text)}</span></div>
      <div class="run-side"><div class="count" title="${waiting ? "Seconds until recording starts" : "Seconds left"}">${left}<small>s</small></div><button class="stop" data-act="stop"${attr("target", f.floor ?? f.name)} title="Stop now and keep what was recorded">Stop</button></div>
      ${waiting ? "" : `<div class="bar" role="progressbar" aria-label="Recorded" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>`}
    </div>`;
  }

  _rooms(d) {
    const col = this.shadowRoot.querySelector(".rooms-col");
    const items = d.floors.map((f) => [`floor:${f.floor ?? ""}`, `<section class="sheet floor" aria-label="${esc(floorLabel(f, d.floors))}"><div class="rows"></div></section>`]);
    if (d.elsewhere.length) items.push(["elsewhere", this._elsewhere(d)]);
    const calibrated = d.elsewhere.length || d.floors.some((f) => f.empty_samples || f.areas.some((a) => a.samples));
    if (calibrated) items.push(["clear-all", this._clearAll()]);
    const sheets = patch(col, items);
    for (const f of d.floors) patch(sheets.get(`floor:${f.floor ?? ""}`).querySelector(".rows"), this._floorRows(f, d));
  }

  _floorRows(f, d) {
    const key = f.floor ?? "";
    let now;
    if (!f.live_links) now = "no live links";
    else if (f.room === "none") now = "nobody moving";
    else if (f.room == null) now = f.areas.some((a) => a.samples >= d.min_samples) ? "cannot tell" : "not calibrated yet";
    else now = `${f.room}, ${Math.round((f.confidence ?? 0) * 100)}% sure`;
    const rows = [["head", `<div class="head"><h2>${esc(floorLabel(f, d.floors))}</h2><span class="note">${esc(now)}</span></div>`]];
    if (!f.areas.length && !f.other_areas.length) {
      rows.push(["hint", `<p class="hint">No rooms on this floor yet. Give each node the area it stands in, under Wisp, the node, Change node.</p>`]);
    } else if (!f.areas.some((a) => a.samples >= d.min_samples)) {
      rows.push(["hint", `<p class="hint">Teach Wisp each room: stand in it, tap Calibrate and walk around until the countdown ends. A room counts from ${d.min_samples} samples.</p>`]);
    }
    for (const a of f.areas) rows.push([`area:${a.area}`, this._area(f, a, d)]);
    if (f.other_areas.length) rows.push(["other", this._other(f, d)]);
    rows.push(["empty", this._empty(f)]);
    return rows.map(([k, html]) => [`${key}:${k}`, html]);
  }

  _area(f, a, d) {
    const key = f.floor ?? "";
    const recording = f.run?.area === a.area;
    const meta = [];
    if (a.nodes) meta.push(plural(a.nodes, "node", "nodes"));
    meta.push(!a.samples ? "not calibrated" : a.samples < d.min_samples ? `${a.samples} of ${d.min_samples} samples` : plural(a.samples, "sample", "samples"));
    const chip = recording ? `<span class="chip rec">recording</span>` : a.presence ? `<span class="chip on">occupied</span>` : "";
    const asking = this._isOpen("room", key, a.area) ? this._askRoom(f, a.area, a.name) : this._isOpen("clear", key, a.area) ? this._askClear(key, a) : "";
    return `<div class="row">
      <div class="line">
        <div class="what"><b>${esc(a.name)}</b><small>${esc(meta.join(", "))}</small></div>
        ${chip}
        <div class="acts">
          <button data-act="ask-room"${attr("floor", key)}${attr("area", a.area)}${asking || recording ? " disabled" : ""}>Calibrate</button>
          ${a.samples ? `<button class="quiet" data-act="ask-clear"${attr("floor", key)}${attr("area", a.area)}${asking ? " disabled" : ""}>Clear</button>` : ""}
        </div>
      </div>
      ${asking}
    </div>`;
  }

  _other(f) {
    const key = f.floor ?? "";
    const picked = this._open?.kind === "room" && this._open.floor === key ? f.other_areas.find((a) => a.area === this._open.area) : null;
    const options = f.other_areas.map((a) => `<option value="${esc(a.area)}"${picked === a ? " selected" : ""}>${esc(a.name)}</option>`).join("");
    const id = `wisp-other-${esc(key)}`;
    return `<div class="row">
      <div class="line">
        <label class="what" for="${id}"><b>Another room</b><small>areas on this floor without a node</small></label>
        <select id="${id}" data-act="pick"${attr("floor", key)}><option value="">Choose a room</option>${options}</select>
      </div>
      ${picked ? this._askRoom(f, picked.area, picked.name) : ""}
    </div>`;
  }

  _empty(f) {
    const key = f.floor ?? "";
    const asking = this._isOpen("empty", key);
    const run = f.run && !f.run.area ? f.run : null;
    const meta = f.empty_samples ? plural(f.empty_samples, "sample", "samples") : "optional, against fans and access points that change power";
    return `<div class="row">
      <div class="line">
        <div class="what"><b>Empty floor</b><small>${esc(meta)}</small></div>
        ${run ? `<span class="chip rec">${run.starts_in ? "starting" : "recording"}</span>` : ""}
        <div class="acts"><button data-act="ask-empty"${attr("floor", key)}${asking || run ? " disabled" : ""}>Calibrate empty floor</button></div>
      </div>
      ${asking ? this._askEmpty(f) : ""}
    </div>`;
  }

  _replaces(f, area) {
    const r = f.run;
    if (!r || (area && r.area === area)) return "";
    return `<p class="warn">This stops ${r.area ? `recording the ${esc(r.name)}` : "the empty floor recording"}; what it recorded is kept.</p>`;
  }

  _durations() {
    const options = DURATIONS.map((s) => `<option value="${s}"${s === this._duration ? " selected" : ""}>${secs(s)}</option>`).join("");
    return `<label>Record for <select data-act="duration">${options}</select></label>`;
  }

  /* Duration (for a recording), Cancel and the action; a failed call says why above them. */
  _buttons(act, label, cls, data) {
    const busy = !!this._busy && this._busy === this._open;
    const failure = this._failure && this._failure.open === this._open ? `<p class="fail" role="alert">${esc(this._failure.message)}</p>` : "";
    return `${failure}<div class="ask-line">${cls === "primary" ? this._durations() : ""}<span class="ask-buttons">
      <button data-act="close"${busy ? " disabled" : ""}>Cancel</button>
      <button class="${cls}" data-act="${act}"${data}${busy ? " disabled" : ""}>${busy ? (cls === "primary" ? "Starting" : "Clearing") : label}</button></span></div>`;
  }

  _askRoom(f, area, name) {
    return `<div class="ask" role="group" aria-label="Calibrate the ${esc(name)}">
      <p>Stand in the ${esc(name)}. After Start, walk around all of it and keep moving until the countdown ends. Still moments are left out.</p>
      ${this._replaces(f, area)}
      ${this._buttons("start-room", "Start", "primary", attr("area", area))}
    </div>`;
  }

  _askEmpty(f) {
    const where = floorPhrase(f);
    return `<div class="ask" role="group" aria-label="Calibrate the empty floor">
      <p>Everyone leaves ${esc(where)}, or keeps still. Recording starts ${LEAVE_S} s after Start, so there is time to go, and nobody should move on ${esc(where)} until it ends.</p>
      ${this._replaces(f, null)}
      ${this._buttons("start-empty", "Start", "primary", attr("target", f.floor ?? f.name))}
    </div>`;
  }

  _askClear(key, a) {
    return `<div class="ask danger" role="alertdialog" aria-label="Clear the ${esc(a.name)}">
      <p>Forget the ${plural(a.samples, "sample", "samples")} of the ${esc(a.name)}? Its presence sensor goes with them. It can be calibrated again any time.</p>
      ${this._buttons("clear", "Clear", "danger", attr("area", a.area))}
    </div>`;
  }

  _elsewhere(d) {
    const rows = d.elsewhere.map((a) => {
      const asking = this._isOpen("clear", undefined, a.area);
      return `<div class="row">
        <div class="line">
          <div class="what"><b>${esc(a.name)}</b><small>${esc(plural(a.samples, "sample", "samples"))}</small></div>
          <div class="acts"><button class="quiet" data-act="ask-clear"${attr("area", a.area)}${asking ? " disabled" : ""}>Clear</button></div>
        </div>
        ${asking ? this._askClear(undefined, a) : ""}
      </div>`;
    }).join("");
    return `<section class="sheet" aria-labelledby="wisp-elsewhere">
      <div class="head"><h2 id="wisp-elsewhere">Other calibrations</h2></div>
      <p class="hint">Rooms calibrated on a floor without Wisp nodes now, or no longer in Home Assistant.</p>
      ${rows}
    </section>`;
  }

  _clearAll() {
    if (!this._isOpen("clear-all")) return `<div class="clear-all"><button class="quiet" data-act="ask-clear-all">Clear all calibration</button></div>`;
    return `<div class="clear-all"><div class="ask danger" role="alertdialog" aria-label="Clear all calibration">
      <p>Forget every room and empty floor? Room presence starts over, and the room and presence sensors go until rooms are calibrated again.</p>
      ${this._buttons("clear", "Clear all", "danger", "")}
    </div></div>`;
  }

  _node(n, d) {
    const floor = n.added ? d.floors.find((f) => f.floor === n.floor) : null;
    const where = [n.added ? n.area_name ?? "no area" : null, floor ? floorLabel(floor, d.floors) : null, n.host].filter(Boolean).join(", ");
    const chips = [
      `<span class="chip${n.online ? " up" : ""}">${n.online ? "online" : "offline"}</span>`,
      `<span class="chip">${n.placed ? "on the layout" : "not placed yet"}</span>`,
      n.added ? "" : `<span class="chip">not added to Wisp</span>`,
    ].join("");
    return `<div class="row">
      <div class="line">
        <span class="dot${n.online ? " up" : ""}" aria-hidden="true"></span>
        <div class="what"><b>${esc(n.name)}</b>${where ? `<small>${esc(where)}</small>` : ""}<span class="chips">${chips}</span></div>
        ${n.device_id ? `<div class="acts"><button data-act="device"${attr("device", n.device_id)} aria-label="Open the ESPHome device of ${esc(n.name)}">ESPHome</button></div>` : ""}
      </div>
    </div>`;
  }

  _hive(d) {
    const h = d.hive;
    if (!h) return `<p class="hint">No hive report yet. Nodes send one every 5 s once they see each other.</p>`;
    return `<dl>
      <div><dt>Hash</dt><dd><code>${esc(h.hash)}</code></dd></div>
      <div><dt>Status</dt><dd>${h.in_sync ? "in sync" : "syncing"}</dd></div>
      <div><dt>Nodes</dt><dd>${h.nodes}</dd></div>
      <div><dt>Heard</dt><dd class="age">${this._age()}</dd></div>
    </dl>`;
  }
}

const STYLE = `
  :host { display: block; height: 100%; }
  .page {
    --wisp-paper-1: #f6e9c4; --wisp-paper-2: #ead39c; --wisp-paper-3: #c9a464;
    --wisp-ink: #4b2e16; --wisp-hot: #a3301f; --wisp-mark: #f3e3b7; --wisp-on-hot: #fff6e4;
    --wisp-burn: rgba(122, 77, 31, .32); --wisp-edge: rgba(107, 67, 32, .38);
    --wisp-serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
    display: flex; flex-direction: column; height: 100%;
    background: var(--primary-background-color); color: var(--primary-text-color);
  }
  .page.dark {
    --wisp-paper-1: #43362a; --wisp-paper-2: #33291e; --wisp-paper-3: #211a13;
    --wisp-ink: #ead6ab; --wisp-hot: #f0905e; --wisp-mark: #3b2f22; --wisp-on-hot: #2a1d12;
    --wisp-burn: rgba(0, 0, 0, .5); --wisp-edge: rgba(234, 214, 171, .2);
  }
  .toolbar {
    display: flex; align-items: center; flex: none; box-sizing: border-box; height: var(--header-height, 56px); padding: 0 12px;
    background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff);
    border-bottom: var(--app-header-border-bottom, none); font-family: var(--ha-font-family-body, Roboto, sans-serif);
  }
  .toolbar h1 { margin: 0 0 0 12px; font-size: 20px; font-weight: 400; }
  .toolbar .menu { display: grid; place-items: center; width: 48px; height: 48px; min-height: 0; padding: 0;
                   border: none; border-radius: 50%; background: none; color: inherit; }
  .toolbar .menu svg { width: 24px; height: 24px; fill: currentColor; }
  .toolbar .menu:not(:disabled):active { transform: none; }
  .scroll { flex: 1; overflow-y: auto; overscroll-behavior: contain; container-type: inline-size; }
  [hidden] { display: none !important; }

  main {
    display: grid; gap: 16px; box-sizing: border-box; max-width: 1200px; margin: 0 auto; padding: 16px;
    grid-template-columns: minmax(0, 1fr); grid-template-areas: "map" "rooms" "nodes" "hive";
  }
  @container (min-width: 760px) {
    main { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); grid-template-areas: "map rooms" "nodes rooms" "hive rooms";
           grid-template-rows: auto auto 1fr; align-items: start; }
  }
  .map-col { grid-area: map; }
  .rooms-col { grid-area: rooms; display: grid; gap: 16px; }
  .nodes-col { grid-area: nodes; }
  .hive-col { grid-area: hive; }
  .map-tools { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; color: var(--wisp-ink); font-family: var(--wisp-serif); }
  .map-tools button { min-height: 34px; font-size: .875rem; }
  .map-tools button[aria-pressed="true"] { background: var(--wisp-ink); color: var(--wisp-paper-1); }

  .sheet {
    position: relative; overflow: hidden; color: var(--wisp-ink); font-family: var(--wisp-serif);
    border: 1px solid var(--wisp-edge); border-radius: var(--ha-card-border-radius, 12px);
    background: radial-gradient(ellipse at 50% 30%, var(--wisp-paper-1) 0%, var(--wisp-paper-2) 70%, var(--wisp-paper-3) 160%);
    box-shadow: var(--ha-card-box-shadow, none), inset 0 0 42px var(--wisp-burn);
  }
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: 4px 12px; flex-wrap: wrap; padding: 14px 18px 8px; }
  h2 { margin: 0; font: 600 1.4rem/1.2 var(--wisp-serif); letter-spacing: .03em; font-variant: small-caps; }
  .note { font-style: italic; font-size: .875rem; opacity: .85; }
  .hive-note { display: inline-flex; align-items: center; gap: 6px; }
  .hive-note::before { content: ""; width: 7px; height: 7px; border-radius: 50%; border: 1.5px solid currentColor; }
  .hive-note:empty::before { display: none; }
  .hive-note.sync::before { background: currentColor; }
  .hive-note.wait::before { animation: wisp-blink 1.6s ease-in-out infinite; }
  .hint { margin: 0; padding: 0 18px 12px; font-size: .9rem; font-style: italic; line-height: 1.45; opacity: .85; }
  .row { padding: 10px 18px; border-top: 1px solid color-mix(in srgb, var(--wisp-ink) 14%, transparent); }
  .line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; }
  .what { flex: 1 1 9rem; min-width: 0; display: grid; gap: 2px; }
  .what b { font-size: 1.05rem; font-weight: 600; overflow-wrap: anywhere; }
  .what small { font-size: .85rem; font-style: italic; opacity: .8; overflow-wrap: anywhere; }
  .acts { display: flex; flex-wrap: wrap; gap: 8px; }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
  .chip { font-size: .8rem; font-style: italic; line-height: 1.5; padding: 0 8px; border-radius: 999px; white-space: nowrap;
          border: 1px solid color-mix(in srgb, var(--wisp-ink) 35%, transparent); }
  .chip.on { background: var(--wisp-hot); border-color: var(--wisp-hot); color: var(--wisp-on-hot); font-style: normal; }
  .chip.up { border-color: currentColor; font-style: normal; }
  .chip.rec::before { content: ""; display: inline-block; width: 7px; height: 7px; margin-right: 5px; border-radius: 50%;
                      background: var(--wisp-hot); animation: wisp-blink 1.6s ease-in-out infinite; }
  .dot { flex: none; width: 10px; height: 10px; border-radius: 50%; border: 1.5px dashed var(--wisp-ink); opacity: .6; }
  .dot.up { background: var(--wisp-ink); border-style: solid; opacity: 1; }
  .foot { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; padding: 12px 18px 14px;
          border-top: 1px solid color-mix(in srgb, var(--wisp-ink) 14%, transparent); }
  .foot p { flex: 1 1 14rem; margin: 0; font-size: .875rem; font-style: italic; line-height: 1.45; opacity: .85; }

  button, select {
    font: inherit; font-size: .95rem; color: var(--wisp-ink); min-height: 40px; padding: 6px 14px; border-radius: 8px;
    border: 1.5px solid color-mix(in srgb, var(--wisp-ink) 55%, transparent);
    background: color-mix(in srgb, var(--wisp-mark) 80%, transparent);
    cursor: pointer; touch-action: manipulation; -webkit-tap-highlight-color: transparent;
  }
  select { font-size: 16px; padding-inline: 10px; max-width: 100%; } /* 16px: no zoom on focus on a phone */
  button.primary { background: var(--wisp-ink); border-color: var(--wisp-ink); color: var(--wisp-paper-1); font-weight: 600; }
  button.danger { background: var(--wisp-hot); border-color: var(--wisp-hot); color: var(--wisp-on-hot); font-weight: 600; }
  button.quiet { background: transparent; border-color: transparent; text-decoration: underline; text-underline-offset: 3px; padding-inline: 8px; }
  button:disabled { opacity: .45; cursor: default; }
  button:focus-visible, select:focus-visible { outline: 2px solid var(--wisp-hot); outline-offset: 2px; }
  button:not(:disabled):active { transform: translateY(1px); }
  @media (hover: hover) {
    button:not(:disabled):hover { background: color-mix(in srgb, var(--wisp-ink) 12%, var(--wisp-mark)); }
    button.primary:not(:disabled):hover { background: color-mix(in srgb, var(--wisp-ink) 85%, var(--wisp-hot)); }
    button.danger:not(:disabled):hover { background: color-mix(in srgb, var(--wisp-hot) 85%, var(--wisp-ink)); }
    button.quiet:not(:disabled):hover { background: transparent; text-decoration-thickness: 2px; }
    .toolbar .menu:not(:disabled):hover { background: color-mix(in srgb, currentColor 12%, transparent); }
  }

  .ask { display: grid; gap: 10px; margin-top: 10px; padding: 12px 14px; border-radius: 10px;
         border: 1px dashed color-mix(in srgb, var(--wisp-ink) 45%, transparent); background: color-mix(in srgb, var(--wisp-mark) 60%, transparent); }
  .ask.danger { border: 1.5px solid var(--wisp-hot); }
  .ask p { margin: 0; line-height: 1.45; }
  .ask .warn, .ask .fail { font-style: italic; }
  .ask .fail { color: var(--wisp-hot); }
  .ask label { display: inline-flex; align-items: center; gap: 8px; }
  .ask-line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
  .ask-buttons { display: flex; gap: 8px; margin-left: auto; }
  .clear-all { display: grid; justify-items: end; color: var(--wisp-ink); font-family: var(--wisp-serif); }
  .clear-all .ask { justify-self: stretch; margin: 0; }

  .runs { position: sticky; top: 0; z-index: 2; display: grid; gap: 8px; padding: 12px 16px 4px; background: var(--primary-background-color); }
  .runs:empty { display: none; }
  .run {
    display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 8px 16px; box-sizing: border-box;
    width: 100%; max-width: 1168px; margin: 0 auto; padding: 12px 16px; color: var(--wisp-ink); font-family: var(--wisp-serif);
    border: 1px solid var(--wisp-edge); border-left: 4px solid var(--wisp-hot); border-radius: var(--ha-card-border-radius, 12px);
    background: radial-gradient(ellipse at 50% 40%, var(--wisp-paper-1), var(--wisp-paper-2));
    box-shadow: 0 2px 10px rgba(0, 0, 0, .15), inset 0 0 24px var(--wisp-burn);
  }
  .run.wait { border-left-color: var(--wisp-ink); }
  .run-text { display: grid; gap: 2px; }
  .run-side { display: flex; align-items: center; gap: 12px; }
  .run-text b { font-size: 1.15rem; }
  .run-text span { font-size: .9rem; font-style: italic; opacity: .85; }
  .count { font: 600 2.2rem/1 var(--wisp-serif); font-variant-numeric: tabular-nums; color: var(--wisp-hot); }
  .run.wait .count { color: var(--wisp-ink); }
  .count small { margin-left: 2px; font-size: .9rem; font-weight: 400; }
  .bar { grid-column: 1 / -1; height: 6px; border-radius: 3px; overflow: hidden; background: color-mix(in srgb, var(--wisp-ink) 15%, transparent); }
  .bar i { display: block; height: 100%; background: var(--wisp-hot); transition: width 1s linear; }

  .message { padding: 16px; max-width: 640px; margin: 0 auto; box-sizing: border-box; }
  .message:empty { display: none; }
  .empty { display: grid; justify-items: center; gap: 8px; padding: 28px 20px 30px; text-align: center; }
  .empty .feet { width: 56px; height: 46px; fill: var(--wisp-ink); opacity: .55; }
  .empty p { margin: 0; max-width: 40ch; font-size: .9rem; line-height: 1.45; opacity: .85; }
  .empty .lead { font-size: 1.1rem; font-style: italic; opacity: 1; }

  dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(7rem, 1fr)); gap: 8px 16px; margin: 0; padding: 4px 18px 16px; }
  dl div { display: grid; gap: 2px; }
  dt { font-size: .8rem; font-style: italic; opacity: .75; }
  dd { margin: 0; font-size: 1.05rem; font-variant-numeric: tabular-nums; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .95rem; }

  @keyframes wisp-blink { 50% { opacity: .25; } }
  @media (prefers-reduced-motion: reduce) {
    .chip.rec::before, .hive-note.wait::before { animation: none; }
    .bar i { transition: none; }
  }
`;

if (!customElements.get("wisp-panel")) customElements.define("wisp-panel", WispPanel);
