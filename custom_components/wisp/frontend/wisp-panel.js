/*
 * Wisp panel: the live map, the rooms of each floor with their calibration and floor plan, the
 * nodes and the hive, for admins. Registered by the integration in the sidebar, no build step.
 * Live from wisp/panel/subscribe; the buttons call the wisp services and the wisp/floor commands.
 * The map is the map card itself; placing nodes and drawing rooms on a plan happen on a drawing of
 * the panel's own.
 */

const DURATIONS = [30, 60, 90, 120, 180, 300]; // s to record
const DURATION = 60; // s, as the services
const LEAVE_S = 30; // s to leave the floor before the empty floor records
const PREFS = "wisp-panel"; // this browser's duration, map turn, mirror and floor
const PW = 360; // viewBox width of the drawing to place nodes and draw rooms on
const PPAD = 28; // room around the plan for a mark on its edge: its touch circle and its name
const PMAX_H = 480;
const NUDGE = 0.1; // metres an arrow key moves a node, five times that with shift
const SNAP = 0.25; // metres a room's edges snap to, and its smallest side; an arrow key moves it as much
const MAX_RECTS = 16; // rectangles per room, as Wisp takes them
// The logo's footprint, as in the card
const SOLE = "M0-13c4.6 0 6.4 4.4 6.4 8.6 0 4.6-2.2 7.9-6.4 7.9s-6.4-3.3-6.4-7.9C-6.4-8.6-4.6-13 0-13Z";
const HEEL = "M0 5.6c3.3 0 4.8 2.1 4.8 4.4 0 2.6-2 4-4.8 4s-4.8-1.4-4.8-4c0-2.3 1.5-4.4 4.8-4.4Z";
const FEET = `<svg class="feet" viewBox="-22 -18 44 36" aria-hidden="true"><g transform="translate(-7 4) rotate(-12) scale(.9)"><path d="${SOLE}"/><path d="${HEEL}"/></g><g transform="translate(8 -4) rotate(10) scale(.9)"><path d="${SOLE}"/><path d="${HEEL}"/></g></svg>`;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const secs = (s) => (s >= 120 && s % 60 === 0 ? `${s / 60} min` : `${s} s`);
const attr = (name, value) => (value == null ? "" : ` data-${name}="${esc(value)}"`);
const n1 = (v) => Math.round(v * 10) / 10;
const metres = (v) => Math.round(v * 100) / 100;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const snap = (v) => Math.round(v / SNAP) * SNAP;
const short = (s, max) => (s.length > max ? `${s.slice(0, max - 1)}…` : s);
const size = (q) => `${metres(q.w)} by ${metres(q.h)} m`;
/* A gentle hue per area, the same on the map */
const hue = (area) => {
  let h = 2166136261; // FNV-1a, then mixed so that ids alike get hues apart
  for (const c of String(area)) h = Math.imul(h ^ c.codePointAt(0), 16777619);
  h = Math.imul(h ^ (h >>> 16), 0x85ebca6b);
  h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35);
  return ((h ^ (h >>> 16)) >>> 0) % 360;
};
const apLabel = (bssid) => `AP ${bssid.slice(-5)}`; // as the map
const NODE_MARK = `<circle class="ring" r="7.5"/><circle class="dot" r="2.6"/>`;
const AP_MARK = `<path class="waves" d="M-7.1-7.1A10 10 0 0 1 7.1-7.1M-9.9-9.9A14 14 0 0 1 9.9-9.9"/><rect class="ring" x="-5" y="-5" width="10" height="10" transform="rotate(45)"/><circle class="dot" r="2"/>`;

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
// A home without Home Assistant floors (a flat, a one-level house) is one group: its areas.
const oneLevel = (f, floors) => f.floor == null && floors.length === 1;
const floorLabel = (f, floors) => (f.floor != null ? f.name : floors.length > 1 ? "No floor" : "Areas");
const floorPhrase = (f) => (f.floor != null ? f.name : "the home");
const emptyName = (f, floors) => (oneLevel(f, floors) ? "home" : "floor"); // "Empty home" or "Empty floor"

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
    this._mode = "moving"; // how the room question records: someone "moving" or "still"
    this._busy = null; // the question whose action runs
    this._failure = null; // {open, message}
    this._planForm = null; // the floor plan form's values while it is open
    this._placing = null; // placing nodes on a plan: {floor, moved: Map(id, [x, y] or null), selected, focus, busy, failure}
    this._roomEdit = null; // drawing rooms on a plan: {floor, rects: [{id, area, x, y, w, h}], next, area, selected, focus, changed, busy, failure}
    this._drag = null; // the node, access point or room under the pointer
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
              <div class="tabs" role="group" aria-label="Floor on the map" hidden></div>
              <div class="map"></div>
              <div class="map-tools">
                <button data-act="turn">Turn 90°</button>
                <button data-act="mirror" aria-pressed="false">Mirror</button>
              </div>
              <section class="sheet placer" aria-labelledby="wisp-placer" hidden>
                <div class="head"><h2 id="wisp-placer">Place nodes</h2><span class="note placer-note"></span></div>
                <div class="plan-box"><img class="plan-img" alt=""><div class="plan-draw"></div></div>
                <div class="placer-info"></div>
              </section>
            </section>
            <section class="rooms-col" aria-label="Rooms and calibration"></section>
            <section class="sheet nodes-col" aria-labelledby="wisp-nodes">
              <div class="head"><h2 id="wisp-nodes">Nodes</h2><span class="note nodes-note"></span></div>
              <div class="nodes"></div>
              <div class="foot">
                <p>A node's floor comes with its area. Areas and floors are set up in Home Assistant's settings.</p>
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
    root.addEventListener("input", (e) => this._input(e));
    root.addEventListener("keydown", (e) => this._keydown(e));
    root.addEventListener("pointerdown", (e) => this._pointerDown(e));
    root.addEventListener("pointermove", (e) => this._pointerMove(e));
    root.addEventListener("pointerup", (e) => this._pointerUp(e));
    root.addEventListener("pointercancel", (e) => this._pointerUp(e));
    // A finger on a node, or drawing a room, drags it, not the page (Safari scrolls despite touch-action on SVG)
    root.addEventListener("touchstart", (e) => {
      if (e.target.closest?.(".placer .item, .placer.rooms .plan-draw svg")) e.preventDefault();
    }, { passive: false });
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

  /* The floor the map shows: the one picked in this browser, else the first. */
  _shownFloor(d = this._data) {
    const floors = d?.loaded ? d.floors.filter((f) => f.nodes.length) : [];
    if (!floors.length) return null;
    return floors.find((f) => (f.floor ?? "") === this._prefs.mapFloor) ?? floors[0];
  }

  _mapConfig() {
    const f = this._shownFloor();
    this._cardFloor = f ? f.floor ?? f.name : ""; // the card takes a floor id, or the hub's own floor by name
    return { title: "Map", rotate: Number(this._prefs.rotate) || 0, flip: !!this._prefs.flip, floor: this._cardFloor };
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
        const still = run.mode === "still", area = now?.areas.find((a) => a.area === run.area);
        const n = (still ? area?.still_samples : area?.samples) ?? run.recorded;
        const has = still ? plural(n, "still sample", "still samples") : plural(n, "sample", "samples");
        message = n >= data.min_samples
          ? `Done: the ${run.name} has ${has}.`
          : `The ${run.name} has ${has} and needs ${data.min_samples}: calibrate it again and keep ${still ? "still" : "moving"}.`;
      } else {
        message = `Done: the empty ${emptyName(now ?? {}, data.floors)} has ${plural(now?.empty_samples ?? run.recorded, "sample", "samples")}.`;
      }
      this._notify(message);
      navigator.vibrate?.(200);
    }
  }

  /* A short note at the bottom of Home Assistant's screen. */
  _notify(message) {
    this.dispatchEvent(new CustomEvent("hass-notification", { detail: { message }, bubbles: true, composed: true }));
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
    else if (act === "start-room") this._call("calibrate_room", { area, duration: this._duration, mode: this._mode });
    else if (act === "start-empty") this._call("calibrate_empty", { floor: target, duration: this._duration, delay: LEAVE_S });
    else if (act === "clear") this._call("clear_calibration", area ? { area } : {});
    else if (act === "stop") this._call("stop_calibration", { floor: target });
    else if (act === "device") navigate(`/config/devices/device/${device}`);
    else if (act === "settings") navigate("/config/integrations/integration/wisp");
    else if (act === "ask-plan") this._openPlanForm(floor);
    else if (act === "upload-plan") this.shadowRoot.querySelector(".plan-form [data-plan=file]")?.click();
    else if (act === "ask-remove-plan") this._ask("remove-plan", floor);
    else if (act === "save-plan") this._savePlan(floor);
    else if (act === "remove-plan") this._run({ type: "wisp/floor/clear", floor: floor || null }, () => this._mergeFloor(floor, null));
    else if (act === "place") this._startPlacing(floor);
    else if (act === "placer-cancel") this._stopPlacing();
    else if (act === "placer-save") this._savePlacing();
    else if (act === "unplace") this._unplace();
    else if (act === "place-middle") this._toMiddle();
    else if (act === "draw-rooms") this._startRooms(floor);
    else if (act === "rooms-cancel") this._stopRooms();
    else if (act === "rooms-save") this._saveRooms();
    else if (act === "rooms-area") this._pickRoom(area);
    else if (act === "rooms-delete") this._deleteRect(this._roomEdit?.selected);
    else if (act === "map-floor") {
      this._prefs.mapFloor = target;
      savePrefs(this._prefs);
      this._render();
    } else if (act === "turn" || act === "mirror") {
      if (act === "turn") this._prefs.rotate = ((Number(this._prefs.rotate) || 0) + 90) % 360;
      else this._prefs.flip = !this._prefs.flip;
      savePrefs(this._prefs);
      this._mirrorButton();
      this._card?.setConfig(this._mapConfig());
    }
  }

  _keydown(e) {
    if (e.key === "Escape" && this._open) this._close();
    const g = e.target.closest?.(".placer .item");
    if (g && this._placing && !this._placing.busy) this._nudge(g.dataset.id, e);
    const room = e.target.closest?.(".placer .room");
    if (room && this._roomEdit && !this._roomEdit.busy && !this._drag) this._roomKey(room.dataset.id, e);
  }

  _change(e) {
    const el = e.target;
    if (el.dataset.act === "duration") {
      // No redraw: the select shows the choice already, and keeps its focus
      this._duration = Number(el.value) || DURATION;
      this._prefs.duration = this._duration;
      savePrefs(this._prefs);
    } else if (el.dataset.act === "mode") {
      // Redrawn now, not by the next update, and the choice keeps its focus
      this._mode = el.value;
      this._render();
      this.shadowRoot.querySelector(`.ask [data-act=mode][value="${CSS.escape(el.value)}"]`)?.focus();
    } else if (el.dataset.act === "pick") {
      if (el.value) this._ask("room", el.dataset.floor, el.value);
      else this._close();
    } else if (el.dataset.act === "channel") {
      this._setChannel(el.dataset.floor ?? "", Number(el.value));
    } else if (el.dataset.act === "rect-area") {
      this._rectArea(el.value);
    } else if (el.dataset.act === "node-area") {
      this._setNodeArea(el.dataset.mac, el.value || null);
    } else if (el.dataset.plan === "file") {
      this._input(e); // browsers that send no input event for a chosen file
    }
  }

  /* A node's area, set on its devices as anywhere in Home Assistant; its floor follows. The choice
     shows at once and stays until the next update brings it. */
  async _setNodeArea(mac, area) {
    const refocus = this.shadowRoot.activeElement?.dataset.mac === mac; // the redraw takes the focus from it
    this._areaChoice = { mac, area, busy: true };
    this._areaFailure = null;
    this._render();
    try {
      await this._hass.callWS({ type: "wisp/node/set_area", mac, area });
      if (this._areaChoice?.mac === mac) this._areaChoice.busy = false;
    } catch (err) {
      this._areaChoice = null;
      this._areaFailure = { mac, message: err?.message || "Wisp could not set the area." };
    }
    this._render();
    if (refocus && !this.shadowRoot.activeElement) this.shadowRoot.querySelector(`select[data-act=node-area][data-mac="${CSS.escape(mac)}"]`)?.focus();
  }

  /* The floor's WiFi channel (0: automatic), set on each of its nodes. The choice shows until the
     update brings it, for a while at most; the nodes that did not take it say why under it. */
  async _setChannel(key, channel) {
    const refocus = this.shadowRoot.activeElement?.dataset.act === "channel";
    this._channelChoice = { floor: key, channel, busy: true };
    this._channelFailure = null;
    this._render();
    try {
      const { set = [], failed = [] } = await this._hass.callWS({ type: "wisp/floor/set_channel", floor: key || null, channel });
      if (failed.length) {
        this._channelChoice = null; // each node's own setting shows
        this._channelFailure = { floor: key, set: set.length, failed };
      } else {
        if (this._channelChoice?.floor === key) Object.assign(this._channelChoice, { busy: false, until: Date.now() + 30000 });
        const f = this._data?.floors.find((x) => (x.floor ?? "") === key);
        const where = f ? floorPhrase(f) : "the floor";
        this._notify(channel ? `The nodes of ${where} move to channel ${channel}.` : `The nodes of ${where} follow the strongest access point.`);
      }
    } catch (err) {
      this._channelChoice = null;
      this._channelFailure = { floor: key, message: err?.message || "Wisp could not set the channel." };
    }
    this._render();
    if (refocus && !this.shadowRoot.activeElement) this.shadowRoot.querySelector(`select[data-act=channel][data-floor="${CSS.escape(key)}"]`)?.focus();
  }

  _ask(kind, floor, area) {
    if (kind === "room") this._mode = "moving"; // each room starts from walking around, its main calibration
    this._open = { kind, floor, area };
    this._failure = null;
    this._planForm = null;
    this._render();
    this.shadowRoot.querySelector(".ask .primary, .ask .danger")?.focus();
  }

  _close() {
    this._open = null;
    this._failure = null;
    this._planForm = null;
    this._render();
  }

  _call(service, data) {
    return this._run(() => this._hass.callService("wisp", service, data, undefined, false));
  }

  /* Runs the open question's action (a function, or a websocket message); done gets its result. */
  async _run(action, done) {
    const open = this._open;
    this._busy = open;
    this._failure = null;
    this._render();
    try {
      const result = await (typeof action === "function" ? action() : this._hass.callWS(action));
      if (this._open === open) {
        this._open = null;
        this._planForm = null;
      }
      done?.(result);
    } catch (err) {
      this._failure = { open, message: err?.message || "Wisp could not do that." };
    } finally {
      this._busy = null;
      this._render();
    }
  }

  /* A floor's plan as a command returned it (null: removed), until the next update brings it. */
  _mergeFloor(key, view) {
    const f = this._data?.floors?.find((x) => (x.floor ?? "") === key);
    if (!f) return;
    if (view) Object.assign(f, view);
    else for (const k of ["plan", "rooms", "positions", "access_points", "fit"]) delete f[k];
  }

  // Floor plan form

  _openPlanForm(key) {
    const plan = this._data?.floors.find((f) => (f.floor ?? "") === key)?.plan;
    this._open = { kind: "plan", floor: key };
    this._failure = null;
    this._planForm = {
      url: plan?.url ?? "",
      width: plan ? String(plan.width) : "",
      height: plan ? String(plan.height) : "",
      keep: !plan, // a new plan takes the image's proportions; a saved one keeps its size
      blank: plan?.url === "", // no image: a grid of metres
      status: "",
    };
    this._render();
    if (plan?.url) this._loadPlanImage();
    // The address, or the width when a grid leaves the address off
    this.shadowRoot.querySelector(".plan-form [data-plan=url]:enabled, .plan-form [data-plan=width]")?.focus();
  }

  _input(e) {
    const el = e.target, form = this._planForm, field = el.dataset?.plan;
    if (!field || !form) return;
    if (field === "file") {
      const file = el.files?.[0];
      el.value = "";
      if (file) this._uploadPlan(file);
      return;
    }
    if (field === "blank") {
      form.blank = el.checked;
      if (form.blank) {
        form.keep = false;
        Object.assign(form, { aspect: null, size: null, loaded: null, status: "" });
      } else if (form.url.trim()) this._loadPlanImage();
      this._fillPlanForm();
      return;
    }
    if (field === "keep") {
      form.keep = el.checked;
      if (!form.keep && !form.height && form.aspect && Number(form.width) > 0) form.height = String(metres(form.width * form.aspect));
    } else {
      form[field] = el.value;
    }
    if (field === "url") this._loadPlanImage();
    else this._fillPlanForm();
  }

  /* An image from this device, kept by Wisp in Home Assistant: its address goes in the form. */
  async _uploadPlan(file) {
    const form = this._planForm;
    Object.assign(form, { blank: false, status: "uploading", uploadError: "" });
    this._fillPlanForm();
    try {
      const body = new FormData();
      body.append("file", file, file.name);
      const res = await this._hass.fetchWithAuth("/api/wisp/plan_image", { method: "POST", body });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(json.message || `The upload failed (${res.status}).`);
      if (this._planForm !== form) return;
      form.url = json.url;
      form.keep = true;
      this._loadPlanImage();
    } catch (err) {
      if (this._planForm !== form) return;
      Object.assign(form, { status: "upload-error", uploadError: err?.message || "The upload failed." });
      this._fillPlanForm();
    }
  }

  /* The image's size in pixels, for its proportions, a little after the address stops changing. */
  _loadPlanImage() {
    const form = this._planForm;
    const url = form.url.trim();
    clearTimeout(this._imageTimer);
    Object.assign(form, { aspect: null, size: null, loaded: null, status: url ? "loading" : "" });
    this._fillPlanForm();
    if (!url) return;
    this._imageTimer = setTimeout(() => {
      const img = new Image();
      const done = (ok) => {
        if (this._planForm !== form || form.url.trim() !== url) return;
        const w = img.naturalWidth, h = img.naturalHeight;
        form.status = !ok ? "error" : w && h ? "ok" : "nosize";
        if (ok) form.loaded = url;
        if (form.status === "ok") Object.assign(form, { aspect: h / w, size: [w, h] });
        this._fillPlanForm();
      };
      img.onload = () => done(true);
      img.onerror = () => done(false);
      img.src = url;
    }, 350);
  }

  /* The form's values live here, not in its markup: a redraw puts them back, and leaves alone the
     field being typed in. */
  _fillPlanForm() {
    const form = this._planForm;
    const box = form && this.shadowRoot.querySelector(".plan-form");
    if (!box) return;
    const focused = this.shadowRoot.activeElement;
    const input = (name) => box.querySelector(`[data-plan="${name}"]`);
    const put = (el, value) => {
      if (el !== focused && el.value !== value) el.value = value;
    };
    put(input("url"), form.url);
    put(input("width"), form.width);
    input("url").disabled = form.blank || form.status === "uploading";
    input("blank").checked = form.blank;
    input("keep").checked = form.keep;
    input("keep").disabled = form.blank;
    box.querySelector("[data-act=upload-plan]").disabled = form.status === "uploading";
    const height = input("height");
    height.disabled = form.keep;
    if (form.keep) height.value = form.aspect && Number(form.width) > 0 ? String(metres(form.width * form.aspect)) : "";
    else put(height, form.height);
    const status = box.querySelector(".plan-status");
    status.textContent = form.blank ? "Wisp draws a grid of metres to place the nodes on." : {
      uploading: "Uploading the image…",
      "upload-error": form.uploadError,
      loading: "Loading the image…",
      ok: form.size ? `The image is ${form.size[0]} by ${form.size[1]} pixels.` : "",
      nosize: "The image has no size of its own: untick Keep the image's proportions and give the height.",
      error: "The image could not be loaded: check its address.",
    }[form.status] ?? "";
    status.classList.toggle("bad", !form.blank && (form.status === "error" || form.status === "upload-error"));
    const preview = box.querySelector(".preview");
    if (form.loaded && preview.getAttribute("src") !== form.loaded) preview.setAttribute("src", form.loaded);
    preview.hidden = !form.loaded || form.blank;
  }

  _savePlan(key) {
    const form = this._planForm;
    if (!form || this._busy) return;
    const url = form.blank ? "" : form.url.trim();
    const width = Number(form.width);
    const height = form.keep && !form.blank ? (form.aspect ? metres(width * form.aspect) : NaN) : Number(form.height);
    const waiting = {
      loading: "The image is still loading: try again in a moment.",
      error: "Check the image's address, or untick Keep the image's proportions and give the height.",
      nosize: "The image has no size of its own: untick Keep the image's proportions and give the height.",
    };
    const problem = form.status === "uploading" && !form.blank ? "The image is still uploading: try again in a moment."
      : !url && !form.blank ? "Upload an image, give its address, or tick No image."
      : !(width >= 1 && width <= 500) ? "Give the width in metres, from 1 to 500."
        : form.keep && !form.blank && !form.aspect ? waiting[form.status] ?? "The image has not loaded yet."
          : !(height >= 1 && height <= 500) ? "Give the height in metres, from 1 to 500."
            : null;
    if (problem) {
      this._failure = { open: this._open, message: problem };
      this._render();
      return;
    }
    const floor = this._data.floors.find((f) => (f.floor ?? "") === key);
    const fresh = !floor?.plan && !!floor?.nodes.length;
    this._run({ type: "wisp/floor/set_plan", floor: key || null, url, width, height }, (view) => {
      this._mergeFloor(key, view);
      if (fresh) this._startPlacing(key); // a new plan on a floor with nodes: on to placing them
    });
  }

  // Placing nodes on a plan

  _startPlacing(key) {
    this._roomEdit = null; // one editor at a time
    this._placing = { floor: key, moved: new Map(), selected: null, focus: null, busy: false, failure: null };
    this._openEditor(key);
  }

  /* The plan's editor takes the map's place, scrolled to; the floor's question closes. */
  _openEditor(key) {
    this._drag = null;
    this._prefs.mapFloor = key;
    savePrefs(this._prefs);
    this._open = null;
    this._failure = null;
    this._planForm = null;
    this._render();
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.shadowRoot.querySelector(".map-col").scrollIntoView({ block: "start", behavior: still ? "auto" : "smooth" });
  }

  _stopPlacing() {
    this._placing = null;
    this._drag = null;
    this._render();
  }

  _placingFloor(d = this._data) {
    const p = this._placing;
    return p && d?.loaded ? d.floors.find((f) => (f.floor ?? "") === p.floor) : null;
  }

  /* What can go on the floor's plan, where it is now with the changes not saved yet, and whether
     the user placed it. Without a position it waits below the plan. */
  _items(f, d) {
    const moved = this._placing.moved;
    const names = new Map(d.nodes.map((n) => [n.mac, n.name]));
    const all = [
      ...f.nodes.map((id) => ({ id, kind: "node", name: names.get(id) ?? id })),
      ...(f.access_points ?? []).map((id) => ({ id, kind: "ap", name: apLabel(id) })),
    ];
    return all.map((i) => {
      const saved = f.positions?.[i.id];
      const change = moved.get(i.id);
      const pos = Array.isArray(change) ? { x: change[0], y: change[1] } : saved ? { x: saved.x, y: saved.y } : null;
      if (pos) { // always on the plan, where it can be reached and dragged
        pos.x = clamp(pos.x, 0, f.plan.width);
        pos.y = clamp(pos.y, 0, f.plan.height);
      }
      return { ...i, pos, placed: Array.isArray(change) || (change === undefined && !!saved?.placed) };
    });
  }

  _where(i) {
    if (i.placed) return `is ${i.pos.x.toFixed(2)} m from the left and ${i.pos.y.toFixed(2)} m from the top.`;
    if (!i.pos) return "is not on the plan yet: drag it onto the plan.";
    return i.kind === "ap"
      ? "is not placed: Wisp puts it where the nodes hear it best. Drag it to where it is."
      : "is not placed: it follows the placed nodes. Drag it to where it stands.";
  }

  _unplace() {
    const p = this._placing, f = this._placingFloor();
    if (!p || !f || !p.selected) return;
    if (f.positions?.[p.selected]?.placed) p.moved.set(p.selected, null);
    else p.moved.delete(p.selected);
    this._render();
  }

  /* The selected node or access point to the middle of the plan, to drag from there. */
  _toMiddle() {
    const p = this._placing, f = this._placingFloor();
    if (!p || !f?.plan || !p.selected) return;
    p.moved.set(p.selected, [metres(f.plan.width / 2), metres(f.plan.height / 2)]);
    this._render();
  }

  _nudge(id, e) {
    const p = this._placing, f = this._placingFloor();
    const step = (e.shiftKey ? 5 : 1) * NUDGE;
    const move = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (!f || (!move && e.key !== "Enter" && e.key !== " ")) return;
    e.preventDefault();
    p.selected = p.focus = id;
    if (move) {
      const plan = f.plan;
      const from = this._items(f, this._data).find((i) => i.id === id)?.pos ?? { x: plan.width / 2, y: plan.height / 2 };
      p.moved.set(id, [clamp(metres(from.x + move[0]), 0, plan.width), clamp(metres(from.y + move[1]), 0, plan.height)]);
    }
    this._render();
  }

  _svgPoint(svg, e) {
    return new DOMPoint(e.clientX, e.clientY).matrixTransform(svg.getScreenCTM().inverse());
  }

  _pointerDown(e) {
    if (this._roomEdit) return this._roomDown(e);
    const g = e.target.closest?.(".placer .item");
    if (!g || !this._placing || this._placing.busy || e.button > 0) return;
    e.preventDefault();
    const svg = g.ownerSVGElement;
    const at = this._svgPoint(svg, e);
    const [x, y] = g.getAttribute("transform").match(/-?[\d.]+/g).map(Number);
    g.parentNode.appendChild(g); // on top while it moves
    try {
      g.setPointerCapture(e.pointerId);
    } catch {
      // the pointer is gone already; its move and up events still come to the page
    }
    g.classList.add("dragging");
    this._drag = { id: g.dataset.id, g, svg, pointer: e.pointerId, dx: x - at.x, dy: y - at.y, x0: at.x, y0: at.y, x, y, moved: false };
  }

  _pointerMove(e) {
    const drag = this._drag;
    if (!drag || e.pointerId !== drag.pointer) return;
    const at = this._svgPoint(drag.svg, e);
    if (!drag.moved && Math.hypot(at.x - drag.x0, at.y - drag.y0) < 3) return; // a tap, so far
    drag.moved = true;
    if (drag.room) return this._roomMove(drag, at);
    const f = this._frame;
    drag.x = clamp(at.x + drag.dx, f.x, f.x + f.w);
    drag.y = clamp(at.y + drag.dy, f.y, f.y + f.h);
    drag.g.setAttribute("transform", `translate(${n1(drag.x)} ${n1(drag.y)})`);
  }

  _pointerUp(e) {
    const drag = this._drag, p = this._placing;
    if (!drag || e.pointerId !== drag.pointer) return;
    this._drag = null;
    this._redraw = true; // the drawing changed under the pointer: draw it again from the state
    if (drag.room) this._roomUp(drag, e.type === "pointerup");
    else if (p && e.type === "pointerup") {
      const f = this._frame;
      if (drag.moved) {
        p.moved.set(drag.id, [
          clamp(metres((drag.x - f.x) / f.s), 0, f.plan.width),
          clamp(metres((drag.y - f.y) / f.s), 0, f.plan.height),
        ]);
      }
      p.selected = drag.id;
      p.focus = null;
    }
    this._render();
  }

  async _savePlacing() {
    const p = this._placing, f = this._placingFloor();
    if (!p || !f || p.busy) return;
    const nodes = {}, access_points = {};
    for (const [id, at] of p.moved) (f.nodes.includes(id) ? nodes : access_points)[id] = at;
    p.busy = true;
    p.failure = null;
    this._render();
    try {
      const view = await this._hass.callWS({ type: "wisp/floor/place", floor: p.floor || null, nodes, access_points });
      this._mergeFloor(p.floor, view);
      if (this._placing === p) this._placing = null;
    } catch (err) {
      p.failure = err?.message || "Wisp could not save the positions.";
    } finally {
      p.busy = false;
      this._render();
    }
  }

  // Drawing rooms on a plan

  _startRooms(key) {
    const f = this._data?.floors.find((x) => (x.floor ?? "") === key);
    if (!f?.plan) return;
    this._placing = null; // one editor at a time
    const rects = (f.rooms ?? []).flatMap((room) => room.rects.map(([x, y, w, h]) => ({ area: room.area, x, y, w, h })));
    rects.forEach((q, i) => { q.id = `r${i + 1}`; });
    // To begin with, the first room not drawn yet
    const areas = this._roomAreas(f);
    const area = (areas.find((a) => !rects.some((q) => q.area === a.area)) ?? areas[0])?.area ?? null;
    this._roomEdit = { floor: key, rects, next: rects.length + 1, area, selected: null, focus: null, changed: false, busy: false, failure: null };
    this._openEditor(key);
  }

  _stopRooms() {
    this._roomEdit = null;
    this._drag = null;
    this._render();
  }

  _roomsFloor(d = this._data) {
    const r = this._roomEdit;
    return r && d?.loaded ? d.floors.find((f) => (f.floor ?? "") === r.floor) : null;
  }

  /* The areas to draw on the floor, by name: all of its areas, and any drawn on it already. */
  _roomAreas(f) {
    const out = new Map();
    for (const a of [...f.areas, ...f.other_areas, ...(f.rooms ?? [])]) if (!out.has(a.area)) out.set(a.area, { area: a.area, name: a.name });
    return [...out.values()].sort((a, b) => a.name.localeCompare(b.name));
  }

  _roomName(f, area) {
    return this._roomAreas(f).find((a) => a.area === area)?.name ?? area;
  }

  /* Why the room cannot take more rectangles than it has, or null. */
  _full(f, area, more = 1) {
    const n = this._roomEdit.rects.filter((q) => q.area === area).length;
    return n + more > MAX_RECTS ? `The ${this._roomName(f, area)} has ${MAX_RECTS} rectangles, the most a room takes.` : null;
  }

  /* The room to draw next. The selection lets go, so picking a room never changes one drawn. */
  _pickRoom(area) {
    const r = this._roomEdit;
    if (!r || r.busy) return;
    Object.assign(r, { area, selected: null, failure: null });
    this._render();
  }

  /* The selected rectangle goes to another room. */
  _rectArea(area) {
    const r = this._roomEdit, f = this._roomsFloor();
    const q = r?.rects.find((x) => x.id === r.selected);
    if (!f || !q || q.area === area || r.busy) return;
    r.failure = this._full(f, area);
    if (!r.failure) {
      q.area = r.area = area;
      r.changed = true;
    }
    this._render();
  }

  _deleteRect(id) {
    const r = this._roomEdit;
    if (!r || r.busy || !r.rects.some((q) => q.id === id)) return;
    r.rects = r.rects.filter((q) => q.id !== id);
    Object.assign(r, { selected: null, changed: true, failure: null });
    this._render();
  }

  /* Keys on a rectangle: the arrows move it by a snap, a metre with shift; Delete takes it away. */
  _roomKey(id, e) {
    const r = this._roomEdit, f = this._roomsFloor();
    const q = r.rects.find((x) => x.id === id);
    if (!f || !q) return;
    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      const rest = r.rects.filter((x) => x !== q);
      r.focus = rest[Math.min(r.rects.indexOf(q), rest.length - 1)]?.id ?? null; // the next one
      this._deleteRect(id);
      return;
    }
    if (e.key === "Escape" && r.selected) {
      Object.assign(r, { selected: null, focus: id });
      this._render();
      return;
    }
    const step = e.shiftKey ? 1 : SNAP;
    const move = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (!move && e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    Object.assign(r, { selected: id, focus: id, area: q.area });
    if (move) {
      const x = clamp(metres(q.x + move[0]), 0, metres(f.plan.width - q.w));
      const y = clamp(metres(q.y + move[1]), 0, metres(f.plan.height - q.h));
      if (x !== q.x || y !== q.y) {
        Object.assign(q, { x, y });
        r.changed = true;
      }
    }
    this._render();
  }

  /* A press on the plan: on a corner of the selected rectangle it resizes it, on a rectangle it
     moves it, anywhere else it draws a new one for the room picked. */
  _roomDown(e) {
    const r = this._roomEdit, svg = e.target.closest?.(".placer .plan-draw svg");
    const f = this._roomsFloor(), frame = this._frame;
    if (!svg || !f || r.busy || this._drag || e.button > 0) return;
    const at = this._svgPoint(svg, e);
    const m = { x: (at.x - frame.x) / frame.s, y: (at.y - frame.y) / frame.s };
    const corner = e.target.closest(".handle")?.dataset.corner;
    const g = e.target.closest(".room");
    let q, mode, dx = 0, dy = 0;
    if (corner) {
      mode = "size";
      q = r.rects.find((x) => x.id === r.selected);
      if (!q) return;
      dx = q.x + (corner[1] === "e" ? q.w : 0) - m.x; // the corner does not jump to the pointer
      dy = q.y + (corner[0] === "s" ? q.h : 0) - m.y;
    } else if (g) {
      mode = "move";
      q = r.rects.find((x) => x.id === g.dataset.id);
      if (!q) return;
      dx = q.x - m.x;
      dy = q.y - m.y;
      Object.assign(r, { selected: q.id, area: q.area });
    } else {
      if (!r.area) return; // no area to draw
      mode = "draw";
      q = { id: `r${r.next++}`, area: r.area, x: clamp(snap(m.x), 0, f.plan.width), y: clamp(snap(m.y), 0, f.plan.height), w: 0, h: 0 };
      r.rects.push(q);
      r.selected = q.id;
    }
    e.preventDefault();
    try {
      svg.setPointerCapture(e.pointerId);
    } catch {
      // the pointer is gone already; its move and up events still come to the page
    }
    r.failure = null;
    this._drag = { room: true, mode, q, before: { ...q }, corner, dx, dy, svg, pointer: e.pointerId, x0: at.x, y0: at.y, moved: false };
    this._paintRooms();
  }

  _roomMove(drag, at) {
    const frame = this._frame, { width, height } = frame.plan, q = drag.q, b = drag.before;
    const x = snap((at.x - frame.x) / frame.s + drag.dx), y = snap((at.y - frame.y) / frame.s + drag.dy);
    if (drag.mode === "move") {
      q.x = clamp(x, 0, metres(width - q.w));
      q.y = clamp(y, 0, metres(height - q.h));
    } else if (drag.mode === "draw") {
      // From where the press began to the pointer
      const x1 = clamp(x, 0, width), y1 = clamp(y, 0, height);
      Object.assign(q, { x: Math.min(x1, b.x), y: Math.min(y1, b.y), w: metres(Math.abs(x1 - b.x)), h: metres(Math.abs(y1 - b.y)) });
    } else {
      // The corner follows the pointer, the opposite one stays
      const right = b.x + b.w, bottom = b.y + b.h;
      if (drag.corner[1] === "w") {
        q.x = clamp(x, 0, metres(right - SNAP));
        q.w = metres(right - q.x);
      } else q.w = metres(clamp(x, b.x + SNAP, width) - b.x);
      if (drag.corner[0] === "n") {
        q.y = clamp(y, 0, metres(bottom - SNAP));
        q.h = metres(bottom - q.y);
      } else q.h = metres(clamp(y, b.y + SNAP, height) - b.y);
    }
    this._paintRooms();
  }

  _roomUp(drag, done) {
    const r = this._roomEdit, q = drag.q, f = this._roomsFloor();
    if (!r || !f) return;
    const drawn = drag.mode === "draw";
    const tiny = drawn && (q.w < SNAP - 1e-9 || q.h < SNAP - 1e-9);
    const full = drawn && done && !tiny ? this._full(f, q.area, 0) : null;
    if (full) r.failure = full;
    if (!done || tiny || full) {
      // Cancelled, a tap on the plan, too small to keep or one too many: as before, and a tap lets go
      if (drawn) {
        r.rects = r.rects.filter((x) => x !== q);
        r.selected = null;
      } else Object.assign(q, drag.before);
      return;
    }
    if (["x", "y", "w", "h"].some((k) => q[k] !== drag.before[k])) r.changed = true;
    r.selected = r.focus = q.id; // the keys work on it next
  }

  /* Only the rooms, while the pointer draws or moves one. */
  _paintRooms() {
    const layer = this._drag?.svg.querySelector(".room-layer");
    if (layer) layer.innerHTML = this._roomShapes(this._roomsFloor(), this._frame);
  }

  async _saveRooms() {
    const r = this._roomEdit;
    if (!r || r.busy) return;
    const rooms = {};
    for (const q of r.rects) (rooms[q.area] ??= []).push([q.x, q.y, q.w, q.h].map(metres));
    Object.assign(r, { busy: true, failure: null });
    this._render();
    try {
      const view = await this._hass.callWS({ type: "wisp/floor/set_rooms", floor: r.floor || null, rooms });
      this._mergeFloor(r.floor, view);
      if (this._roomEdit === r) this._roomEdit = null;
    } catch (err) {
      r.failure = err?.message || "Wisp could not save the rooms.";
    } finally {
      r.busy = false;
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
    patch(root.querySelector(".runs"), d.floors.filter((f) => f.run).map((f) => [f.floor ?? "", this._runBanner(f)]));
    this._mapTools(d);
    this._renderPlacer(d);
    this._rooms(d);
    this._fillPlanForm();
    const added = d.nodes.filter((n) => n.added);
    root.querySelector(".nodes-note").textContent = `${added.filter((n) => n.online).length} of ${added.length} online`;
    const rows = patch(root.querySelector(".nodes"), d.nodes.map((n) => [n.mac, this._node(n, d)]));
    // The signal changes every second: its line changes alone, so the row's area list stays open
    for (const n of d.nodes) {
      const el = rows.get(n.mac).querySelector(".wifi"), text = this._wifi(n);
      if (el.textContent !== text) el.textContent = text;
      el.hidden = !text;
    }
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

  _runBanner(f) {
    const r = f.run;
    const waiting = r.starts_in > 0;
    const done = r.recorded + r.skipped;
    const total = done + r.seconds_left;
    const pct = total ? Math.round((100 * done) / total) : 100;
    let title, text;
    if (r.area) {
      const still = r.mode === "still";
      title = still ? `Sit still in the ${r.name}` : `Walk around the ${r.name}, keep moving`;
      const skipped = r.skipped ? `, ${r.skipped} s ${still ? "of motion left out" : "too still to count"}` : "";
      text = `${plural(r.recorded, "sample", "samples")} so far${skipped}.`;
    } else if (waiting) {
      title = `Leave ${floorPhrase(f)} now`;
      text = `Everyone must stay out until it ends. Recording starts in ${r.starts_in} s and lasts ${secs(r.seconds_left - r.starts_in)}.`;
    } else {
      title = `Keep ${floorPhrase(f)} empty`;
      text = `${plural(r.recorded, "sample", "samples")} so far.`;
    }
    const left = waiting ? r.starts_in : r.seconds_left;
    return `<div class="run${waiting ? " wait" : ""}">
      <div class="run-text"><b>${esc(title)}</b><span>${esc(text)}</span></div>
      <div class="run-side"><div class="count" title="${waiting ? "Seconds until recording starts" : "Seconds left"}">${left}<small>s</small></div><button class="stop" data-act="stop"${attr("target", f.floor ?? f.name)} title="Stop now and keep what was recorded">Stop</button></div>
      ${waiting ? "" : `<div class="bar" role="progressbar" aria-label="Recorded" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>`}
    </div>`;
  }

  /* A button per floor over the map when there are several; turn and mirror under it, plan and all.
     Placing nodes or drawing rooms hides them: the editor draws the plan as it is, in its own metres. */
  _mapTools(d) {
    const root = this.shadowRoot;
    const shown = this._shownFloor(d);
    const placing = !!(this._placing || this._roomEdit);
    const tabs = root.querySelector(".tabs");
    const mapped = d.floors.filter((f) => f.nodes.length); // the map draws floors with nodes
    tabs.hidden = placing || mapped.length < 2;
    const html = mapped.map((f) => `<button data-act="map-floor"${attr("target", f.floor ?? "")} aria-pressed="${f === shown}">${esc(floorLabel(f, d.floors))}</button>`).join("");
    if (tabs._html !== html) {
      tabs.innerHTML = html;
      tabs._html = html;
    }
    root.querySelector(".map").hidden = placing;
    root.querySelector(".map-tools").hidden = placing;
    if (this._card && (shown ? shown.floor ?? shown.name : "") !== this._cardFloor) this._card.setConfig(this._mapConfig());
  }

  /* The plan's editor: placing nodes (the plan, its nodes and access points to drag, and what is
     still to do) or drawing rooms (the plan, its rooms, and the room to draw). */
  _renderPlacer(d) {
    const root = this.shadowRoot;
    if (this._placing && !this._placingFloor(d)?.plan) this._placing = null; // the plan or the floor went away
    if (this._roomEdit && !this._roomsFloor(d)?.plan) this._roomEdit = null;
    const p = this._placing ?? this._roomEdit;
    const sheet = root.querySelector(".placer");
    sheet.hidden = !p;
    if (!p || this._drag) return; // nothing moves under a finger
    const f = this._placing ? this._placingFloor(d) : this._roomsFloor(d);
    sheet.classList.toggle("rooms", !this._placing);
    root.querySelector("#wisp-placer").textContent = this._placing ? "Place nodes" : "Draw rooms";
    root.querySelector(".placer-note").textContent = floorLabel(f, d.floors);
    const items = this._placing ? this._items(f, d) : null;
    const { svg, frame } = items ? this._placerSvg(f, items) : this._roomsSvg(f);
    this._frame = frame;
    const draw = root.querySelector(".plan-draw"), info = root.querySelector(".placer-info");
    if (draw._html !== svg || this._redraw) {
      draw.innerHTML = svg;
      draw._html = svg;
      this._redraw = false;
      // The node or room moved with the keys or the pointer keeps the focus in the new drawing, once
      if (p.focus) draw.querySelector(`[data-id="${CSS.escape(p.focus)}"]`)?.focus({ preventScroll: true });
    }
    p.focus = null;
    const text = items ? this._placerInfo(f, items) : this._roomsInfo(f);
    if (info._html !== text) {
      info.innerHTML = text;
      info._html = text;
    }
    const img = root.querySelector(".plan-img");
    img.hidden = !f.plan.url; // no image: the drawing has a grid
    if (f.plan.url && img.getAttribute("src") !== f.plan.url) img.setAttribute("src", f.plan.url);
    const pct = (v) => `${(100 * v).toFixed(3)}%`;
    Object.assign(img.style, {
      left: pct(frame.x / PW), top: pct(frame.y / frame.vh), width: pct(frame.w / PW), height: pct(frame.h / frame.vh),
    });
  }

  /* The plan scaled into the drawing, with room below it for what is not on it yet. */
  _planFrame(plan, below = 0) {
    const s = Math.min((PW - 2 * PPAD) / plan.width, (PMAX_H - 2 * PPAD) / plan.height);
    const frame = { x: (PW - plan.width * s) / 2, y: PPAD, w: plan.width * s, h: plan.height * s, s, plan };
    frame.vh = Math.round(frame.y + frame.h + PPAD + below);
    return frame;
  }

  /* A plan without an image gets a grid of metres; then the plan's edge. */
  _planBase(frame) {
    const { plan, s } = frame;
    let grid = "";
    if (!plan.url) {
      const step = [0.5, 1, 2, 5, 10].find((v) => v * s >= 14) ?? 10;
      for (let x = step; x * s < frame.w - 0.5; x += step) grid += `M${n1(frame.x + x * s)} ${n1(frame.y)}v${n1(frame.h)}`;
      for (let y = step; y * s < frame.h - 0.5; y += step) grid += `M${n1(frame.x)} ${n1(frame.y + y * s)}h${n1(frame.w)}`;
      grid = `<path class="grid" d="${grid}"/>`;
    }
    return `${grid}<rect class="edge" x="${n1(frame.x)}" y="${n1(frame.y)}" width="${n1(frame.w)}" height="${n1(frame.h)}"/>`;
  }

  _placerSvg(f, items) {
    const plan = f.plan, selected = this._placing.selected;
    const tray = items.filter((i) => !i.pos);
    const frame = this._planFrame(plan, tray.length ? 58 : 0), s = frame.s;
    const gap = Math.min(84, (PW - 2 * PPAD) / Math.max(tray.length, 1));
    const at = (i) => (i.pos
      ? { x: frame.x + i.pos.x * s, y: frame.y + i.pos.y * s }
      : { x: PW / 2 + (tray.indexOf(i) - (tray.length - 1) / 2) * gap, y: frame.y + frame.h + 32 });
    const order = [...items.filter((i) => i.id !== selected), ...items.filter((i) => i.id === selected)];
    const marks = order.map((i) => {
      const { x, y } = at(i);
      const cls = `item ${i.kind}${i.placed ? " placed" : ""}${i.id === selected ? " sel" : ""}`;
      const label = `${i.name} ${this._where(i)} Drag it, or move it with the arrow keys.`;
      return `<g class="${cls}" data-id="${esc(i.id)}" transform="translate(${n1(x)} ${n1(y)})" tabindex="0" role="button" aria-label="${esc(label)}"><circle class="hit" r="22"/>${i.kind === "ap" ? AP_MARK : NODE_MARK}<text y="24">${esc(i.name)}</text></g>`;
    }).join("");
    const below = tray.length ? `<text class="tray" x="${PW / 2}" y="${n1(frame.y + frame.h + 13)}">Not on the plan yet: drag onto it</text>` : "";
    const svg = `<svg viewBox="0 0 ${PW} ${frame.vh}" role="group" aria-label="The plan of ${esc(floorPhrase(f))}, ${n1(plan.width)} by ${n1(plan.height)} m">${this._planBase(frame)}${below}${marks}</svg>`;
    return { svg, frame };
  }

  _placerInfo(f, items) {
    const p = this._placing;
    const nodes = items.filter((i) => i.kind === "node");
    const placed = nodes.filter((i) => i.placed).length;
    const loose = nodes.filter((i) => !i.placed).map((i) => i.name);
    const sel = items.find((i) => i.id === p.selected);
    const pending = p.moved.size > 0;
    const hint = placed === 0
      ? "Drag each node to where it stands on the plan, and the access points too where you know them. Tap one to select it."
      : placed === 1
        ? "Place one more node: from two on, Wisp turns and scales its own layout to fit them, and the nodes you leave follow."
        : placed === 2
          ? "The nodes you leave follow these two. A third one, away from the line between them, tells Wisp which way round its layout goes."
          : "The nodes you leave follow the placed ones.";
    // Two placed nodes always match exactly; from three the error says how well the layout agrees
    const fit = f.fit && f.fit.nodes > 2 && !pending ? `<p class="say">The placed nodes match the hive's own layout within ${f.fit.error} m.</p>` : "";
    return `${sel ? `<div class="sel-line"><p><b>${esc(sel.name)}</b> ${esc(this._where(sel))}</p><span class="sel-buttons"><button class="quiet" data-act="place-middle"${p.busy ? " disabled" : ""}>Move to the middle</button>${sel.placed ? `<button class="quiet" data-act="unplace"${p.busy ? " disabled" : ""}>Let Wisp place it</button>` : ""}</span></div>` : ""}
      <p class="say">${esc(hint)}${pending && loose.length ? " Save to see them follow." : ""}</p>
      <p class="unplaced${loose.length ? "" : " done"}">${loose.length ? `<b>Not placed yet:</b> ${esc(loose.join(", "))}.` : "Every node is placed."}</p>
      ${fit}
      ${p.failure ? `<p class="fail" role="alert">${esc(p.failure)}</p>` : ""}
      <div class="ask-line"><span class="ask-buttons">
        <button data-act="placer-cancel"${p.busy ? " disabled" : ""}>Cancel</button>
        <button class="primary" data-act="placer-save"${p.busy || !pending ? " disabled" : ""}>${p.busy ? "Saving" : "Save"}</button></span></div>`;
  }

  _roomsSvg(f) {
    const plan = f.plan, frame = this._planFrame(plan);
    const label = `The plan of ${floorPhrase(f)}, ${n1(plan.width)} by ${n1(plan.height)} m. Drag across it to draw a room.`;
    const svg = `<svg viewBox="0 0 ${PW} ${frame.vh}" role="group" aria-label="${esc(label)}">${this._planBase(frame)}<g class="room-layer">${this._roomShapes(f, frame)}</g></svg>`;
    return { svg, frame };
  }

  /* Each rectangle washed in its room's hue, the selected one on top with its corners to drag, and
     each room's name in the corner of its largest rectangle. */
  _roomShapes(f, frame) {
    const r = this._roomEdit, s = frame.s;
    const box = (q) => ({ x: frame.x + q.x * s, y: frame.y + q.y * s, w: q.w * s, h: q.h * s });
    const names = new Map(this._roomAreas(f).map((a) => [a.area, a.name]));
    const sel = r.rects.find((q) => q.id === r.selected);
    const largest = new Map();
    let shapes = "", labels = "", handles = "";
    for (const q of [...r.rects.filter((x) => x !== sel), ...(sel ? [sel] : [])]) {
      const b = box(q), name = names.get(q.area) ?? q.area;
      const tip = `${name}, ${size(q)}, ${metres(q.x)} m from the left and ${metres(q.y)} m from the top. Drag it or move it with the arrow keys; Delete takes it away.`;
      shapes += `<g class="room${q === sel ? " sel" : ""}" data-id="${q.id}" style="--hue:${hue(q.area)}" tabindex="0" role="button" aria-label="${esc(tip)}"><rect x="${n1(b.x)}" y="${n1(b.y)}" width="${n1(b.w)}" height="${n1(b.h)}"/></g>`;
      const big = largest.get(q.area);
      if (!big || q.w * q.h > big.q.w * big.q.h) largest.set(q.area, { q, b });
    }
    for (const [area, { b }] of largest) {
      const fit = Math.floor((b.w - 10) / 5.6); // characters of 11 px italic
      if (fit >= 3 && b.h >= 18) labels += `<text class="room-name" x="${n1(b.x + 5)}" y="${n1(b.y + 14)}">${esc(short(names.get(area) ?? area, fit))}</text>`;
    }
    if (sel && this._drag?.mode !== "draw") {
      const b = box(sel);
      handles = [["nw", b.x, b.y], ["ne", b.x + b.w, b.y], ["sw", b.x, b.y + b.h], ["se", b.x + b.w, b.y + b.h]]
        .map(([c, x, y]) => `<g class="handle" data-corner="${c}" transform="translate(${n1(x)} ${n1(y)})" aria-hidden="true"><rect class="hit" x="-15" y="-15" width="30" height="30"/><rect class="knob" x="-4" y="-4" width="8" height="8"/></g>`)
        .join("");
    }
    return shapes + labels + handles;
  }

  _roomsInfo(f) {
    const r = this._roomEdit, areas = this._roomAreas(f);
    const off = r.busy ? " disabled" : "";
    const picks = areas.map((a) => {
      const drawn = r.rects.some((q) => q.area === a.area);
      return `<button data-act="rooms-area"${attr("area", a.area)} aria-pressed="${a.area === r.area}" style="--hue:${hue(a.area)}"${off}><i class="${drawn ? "" : "none"}" aria-hidden="true"></i>${esc(a.name)}</button>`;
    }).join("");
    const sel = r.rects.find((q) => q.id === r.selected);
    const options = areas.map((a) => `<option value="${esc(a.area)}"${sel?.area === a.area ? " selected" : ""}>${esc(a.name)}</option>`).join("");
    const selLine = sel
      ? `<div class="sel-line"><p><b>${esc(this._roomName(f, sel.area))}</b> ${esc(size(sel))}, ${metres(sel.x)} m from the left and ${metres(sel.y)} m from the top.</p>
          <span class="sel-acts"><select data-act="rect-area" aria-label="Room of this rectangle"${off}>${options}</select><button class="quiet" data-act="rooms-delete"${off}>Delete</button></span></div>`
      : "";
    const hint = areas.length
      ? "Pick a room, then drag across the plan to draw it; an L-shaped room takes two rectangles. Tap a rectangle to move it, resize it by its corners or delete it. Wisp keeps someone moving inside the rooms, and inside the room it is sure of."
      : "This floor has no areas yet. Add them in Home Assistant's settings under Areas, then draw them here.";
    return `${areas.length ? `<div class="room-picks" role="group" aria-label="Room to draw"><span class="picks-label">Draw</span>${picks}</div>` : ""}
      ${selLine}
      <p class="say">${esc(hint)}</p>
      ${r.failure ? `<p class="fail" role="alert">${esc(r.failure)}</p>` : ""}
      <div class="ask-line"><span class="ask-buttons">
        <button data-act="rooms-cancel"${off}>Cancel</button>
        <button class="primary" data-act="rooms-save"${r.busy || !r.changed ? " disabled" : ""}>${r.busy ? "Saving" : "Save"}</button></span></div>`;
  }

  _planRow(f) {
    const key = f.floor ?? "";
    const plan = f.plan;
    const form = this._isOpen("plan", key), removing = this._isOpen("remove-plan", key);
    const placed = plan ? f.nodes.filter((mac) => f.positions?.[mac]?.placed).length : 0;
    const kind = plan && !plan.url ? "a grid, " : "";
    const rooms = f.rooms?.length ? `, ${plural(f.rooms.length, "room", "rooms")} drawn` : "";
    const meta = plan
      ? `${kind}${metres(plan.width)} by ${metres(plan.height)} m, ${f.nodes.length ? `${placed} of ${plural(f.nodes.length, "node", "nodes")} placed` : "no nodes on this floor yet"}${rooms}`
      : f.nodes.length ? "none yet: the map shows the hive's own layout" : "none yet";
    const placing = this._placing?.floor === key, drawing = this._roomEdit?.floor === key;
    const acts = plan
      ? `<button data-act="place"${attr("floor", key)}${placing || form || removing || !f.nodes.length ? " disabled" : ""}>Place nodes</button>
         <button data-act="draw-rooms"${attr("floor", key)}${drawing || form || removing ? " disabled" : ""}>Draw rooms</button>
         <button data-act="ask-plan"${attr("floor", key)}${form ? " disabled" : ""}>Change</button>
         <button class="quiet" data-act="ask-remove-plan"${attr("floor", key)}${removing ? " disabled" : ""}>Remove</button>`
      : `<button data-act="ask-plan"${attr("floor", key)}${form ? " disabled" : ""}>Add floor plan</button>`;
    return `<div class="row">
      <div class="line">
        <div class="what"><b>Floor plan</b><small>${esc(meta)}</small></div>
        <div class="acts">${acts}</div>
      </div>
      ${form ? this._askPlan(f) : removing ? this._askRemovePlan(f) : ""}
    </div>`;
  }

  _askPlan(f) {
    const key = f.floor ?? "";
    return `<div class="ask plan-form" role="group" aria-label="Floor plan">
      <p>An image of ${esc(floorPhrase(f))} seen from above, and its size in metres: upload one, give its address, or use no image and place the nodes on a grid of metres.</p>
      <div class="upload-line"><button data-act="upload-plan">Upload an image</button><small>PNG, JPEG, GIF or WebP, up to 20 MB</small>
        <input data-plan="file" type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden></div>
      <label class="field"><span>or its address</span><input data-plan="url" type="text" inputmode="url" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="/local/wisp/plan.png"></label>
      <label class="check"><input data-plan="blank" type="checkbox">No image: a grid of metres</label>
      <div class="fields">
        <label class="field"><span>Width, m</span><input data-plan="width" type="number" inputmode="decimal" min="1" max="500" step="0.01"></label>
        <label class="field"><span>Height, m</span><input data-plan="height" type="number" inputmode="decimal" min="1" max="500" step="0.01"></label>
      </div>
      <label class="check"><input data-plan="keep" type="checkbox">Keep the image's proportions</label>
      <p class="plan-status" aria-live="polite"></p>
      <img class="preview" alt="The image" hidden>
      ${this._buttons("save-plan", "Save", "primary", attr("floor", key), { busy: "Saving", duration: false })}
    </div>`;
  }

  _askRemovePlan(f) {
    const which = f.floor != null ? `the floor plan of ${esc(f.name)}` : "the floor plan";
    return `<div class="ask danger" role="alertdialog" aria-label="Remove the floor plan">
      <p>Remove ${which}? The positions placed on it go too, and the map shows the hive's own layout again. The image itself stays where it is.</p>
      ${this._buttons("remove-plan", "Remove", "danger", attr("floor", f.floor ?? ""), { busy: "Removing" })}
    </div>`;
  }

  _rooms(d) {
    const col = this.shadowRoot.querySelector(".rooms-col");
    const items = d.floors.map((f) => [`floor:${f.floor ?? ""}`, `<section class="sheet floor" aria-label="${esc(floorLabel(f, d.floors))}"><div class="rows"></div></section>`]);
    if (d.elsewhere.length) items.push(["elsewhere", this._elsewhere(d)]);
    const calibrated = d.elsewhere.length || d.floors.some((f) => f.empty_samples || f.areas.some((a) => a.samples || a.still_samples));
    if (calibrated) items.push(["clear-all", this._clearAll()]);
    const sheets = patch(col, items);
    for (const f of d.floors) patch(sheets.get(`floor:${f.floor ?? ""}`).querySelector(".rows"), this._floorRows(f, d));
  }

  _floorRows(f, d) {
    const key = f.floor ?? "";
    const still = f.areas.find((a) => a.presence && a.still);
    let now;
    if (!f.nodes.length) now = "no nodes yet";
    else if (!f.live_links) now = "no live links";
    else if (f.room === "none") now = still ? `${still.name}, someone keeping still` : "nobody moving";
    else if (f.room == null) now = f.areas.some((a) => a.samples >= d.min_samples) ? "cannot tell" : "not calibrated yet";
    else now = `${f.room}, ${Math.round((f.confidence ?? 0) * 100)}% sure`;
    const rows = [["head", `<div class="head"><h2>${esc(floorLabel(f, d.floors))}</h2><span class="note">${esc(now)}</span></div>`]];
    if (!f.nodes.length) {
      rows.push(["hint", `<p class="hint">No node on ${esc(floorPhrase(f))} yet: give a node an area on this floor under Nodes. Its floor plan can be added already.</p>`]);
    } else if (!f.areas.length && !f.other_areas.length) {
      rows.push(["hint", `<p class="hint">No rooms on this floor yet. Give each node the area it stands in, under Nodes.</p>`]);
    } else if (!f.areas.some((a) => a.samples >= d.min_samples)) {
      rows.push(["hint", `<p class="hint">Teach Wisp each room: stand in it, tap Calibrate, and walk around (or sit still) until the countdown ends. A room counts from ${d.min_samples} samples.</p>`]);
    }
    for (const a of f.areas) rows.push([`area:${a.area}`, this._area(f, a, d)]);
    if (f.other_areas.length && f.nodes.length) rows.push(["other", this._other(f, d)]);
    if (f.nodes.length) rows.push(["empty", this._empty(f)]);
    rows.push(["plan", this._planRow(f)]);
    if (f.nodes.length && this._channelShown(f, d)) rows.push(["channel", this._channelRow(f, d)]);
    return rows.map(([k, html]) => [`${key}:${k}`, html]);
  }

  /* The WiFi channel the floor's nodes form their grid on, one setting for all of them, and the
     channels they are on now. */
  /* Only where it helps: several floors, several access points, or a channel already chosen.
     One router, or a router with extenders on one floor, needs no choice: Automatic does it. */
  _channelShown(f, d) {
    const aps = new Set(d.nodes.map((n) => n.wifi?.ap).filter(Boolean));
    const levels = d.floors.filter((x) => x.nodes.length).length;
    const chosen = d.nodes.some((n) => f.nodes.includes(n.mac) && n.wifi?.fixed);
    return levels > 1 || aps.size > 1 || chosen || this._channelChoice?.floor === (f.floor ?? "");
  }

  _channelRow(f, d) {
    const key = f.floor ?? "";
    let choice = this._channelChoice?.floor === key ? this._channelChoice : null;
    if (choice && !choice.busy && (f.channel === choice.channel || Date.now() > choice.until)) choice = this._channelChoice = null; // arrived, or never will
    const wifi = d.nodes.filter((n) => f.nodes.includes(n.mac) && n.wifi).map((n) => n.wifi);
    const current = choice ? choice.channel : f.channel;
    const settings = new Set(wifi.map((w) => w.fixed).filter((v) => v != null));
    const placeholder = current == null ? `<option value="" disabled selected>${settings.size > 1 ? "Mixed" : "Unknown"}</option>` : "";
    const options = Array.from({ length: 14 }, (_, c) => `<option value="${c}"${c === current ? " selected" : ""}>${c ? `Channel ${c}` : "Automatic"}</option>`).join("");
    const on = [...new Set(wifi.map((w) => w.channel).filter((v) => v != null))].sort((a, b) => a - b);
    const meta = on.length ? `nodes on channel${on.length > 1 ? "s" : ""} ${on.length > 1 ? `${on.slice(0, -1).join(", ")} and ${on.at(-1)}` : on[0]} now` : "";
    const fail = this._channelFailure?.floor === key ? this._channelFailure : null;
    const failures = !fail ? ""
      : fail.message ? `<p class="fail" role="alert">${esc(fail.message)}</p>`
        : `<div role="alert">${fail.set ? `<p class="fail">Set on ${fail.set} of ${plural(fail.set + fail.failed.length, "node", "nodes")}.</p>` : ""}${fail.failed.map((x) => `<p class="fail">${esc(`${x.name && !String(x.error).includes(x.name) ? `${x.name}: ` : ""}${x.error}`)}</p>`).join("")}</div>`;
    const id = `wisp-channel-${esc(key)}`;
    return `<div class="row channel">
      <div class="line">
        <label class="what" for="${id}"><b>WiFi channel</b>${meta ? `<small>${esc(meta)}</small>` : ""}</label>
        <select id="${id}" data-act="channel"${attr("floor", key)}${choice?.busy ? " disabled" : ""}>${placeholder}${options}</select>
      </div>
      <p class="about">The nodes form their grid on this channel. Automatic follows the strongest access point; when nodes pick up another floor's access point, choose the channel of this floor's.</p>
      ${failures}
    </div>`;
  }

  _area(f, a, d) {
    const key = f.floor ?? "";
    const recording = f.run?.area === a.area;
    const meta = [];
    if (a.nodes) meta.push(plural(a.nodes, "node", "nodes"));
    // Walking samples, then still ones: "40 walking, 12 of 20 still samples", or one kind alone
    const min = d.min_samples, still = a.still_samples || 0;
    const count = (n) => (n < min ? `${n} of ${min}` : `${n}`);
    const kinds = [a.samples ? `${count(a.samples)} walking` : "", still ? `${count(still)} still` : ""].filter(Boolean);
    const one = kinds.length === 1 && (a.samples || still) === 1 && min <= 1 ? "sample" : "samples";
    meta.push(kinds.length ? `${kinds.join(", ")} ${one}` : "not calibrated");
    const chip = recording ? `<span class="chip rec">recording</span>` : a.presence ? `<span class="chip on">${a.still ? "someone keeping still" : "occupied"}</span>` : "";
    const asking = this._isOpen("room", key, a.area) ? this._askRoom(f, a.area, a.name) : this._isOpen("clear", key, a.area) ? this._askClear(key, a) : "";
    return `<div class="row">
      <div class="line">
        <div class="what"><b>${esc(a.name)}</b><small>${esc(meta.join(", "))}</small></div>
        ${chip}
        <div class="acts">
          <button data-act="ask-room"${attr("floor", key)}${attr("area", a.area)}${asking || recording || !f.nodes.length ? " disabled" : ""}>Calibrate</button>
          ${a.samples || still ? `<button class="quiet" data-act="ask-clear"${attr("floor", key)}${attr("area", a.area)}${asking ? " disabled" : ""}>Clear</button>` : ""}
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
        <label class="what" for="${id}"><b>Another room</b><small>${oneLevel(f, this._data.floors) ? "areas" : "areas on this floor"} without a node</small></label>
        <select id="${id}" data-act="pick"${attr("floor", key)}><option value="">Choose a room</option>${options}</select>
      </div>
      ${picked ? this._askRoom(f, picked.area, picked.name) : ""}
    </div>`;
  }

  _empty(f) {
    const key = f.floor ?? "";
    const asking = this._isOpen("empty", key);
    const run = f.run && !f.run.area ? f.run : null;
    const meta = f.empty_samples ? plural(f.empty_samples, "sample", "samples") : "needed to find someone keeping still; also helps against fans and access points that change power";
    return `<div class="row">
      <div class="line">
        <div class="what"><b>Empty ${emptyName(f, this._data.floors)}</b><small>${esc(meta)}</small></div>
        ${run ? `<span class="chip rec">${run.starts_in ? "starting" : "recording"}</span>` : ""}
        <div class="acts"><button data-act="ask-empty"${attr("floor", key)}${asking || run ? " disabled" : ""}>Calibrate empty ${emptyName(f, this._data.floors)}</button></div>
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
  _buttons(act, label, cls, data, { busy: working = cls === "primary" ? "Starting" : "Clearing", duration = cls === "primary" } = {}) {
    const busy = !!this._busy && this._busy === this._open;
    const failure = this._failure && this._failure.open === this._open ? `<p class="fail" role="alert">${esc(this._failure.message)}</p>` : "";
    return `${failure}<div class="ask-line">${duration ? this._durations() : ""}<span class="ask-buttons">
      <button data-act="close"${busy ? " disabled" : ""}>Cancel</button>
      <button class="${cls}" data-act="${act}"${data}${busy ? " disabled" : ""}>${busy ? working : label}</button></span></div>`;
  }

  /* Walking around or sitting still: each is a class of its own, recorded apart. */
  _askRoom(f, area, name) {
    const mode = (value, title, text) => `<label class="mode"><input type="radio" name="wisp-mode" value="${value}" data-act="mode"${this._mode === value ? " checked" : ""}><span><b>${title}</b><small>${text}</small></span></label>`;
    return `<div class="ask" role="group" aria-label="Calibrate the ${esc(name)}">
      <p>Stand in the ${esc(name)}, pick one, and after Start keep at it until the countdown ends.</p>
      <div class="modes" role="radiogroup" aria-label="How to calibrate">
        ${mode("moving", "Walking around", "Walk around the whole room and keep moving.")}
        ${mode("still", "Sitting still", "Sit or stand still where you usually are in it, like the desk or the sofa.")}
      </div>
      ${this._replaces(f, area)}
      ${this._buttons("start-room", "Start", "primary", attr("area", area))}
    </div>`;
  }

  _askEmpty(f) {
    const where = floorPhrase(f);
    return `<div class="ask" role="group" aria-label="Calibrate the empty floor">
      <p>Everyone leaves ${esc(where)} until it ends: keeping still is not enough, since Wisp finds someone still too. Recording starts ${LEAVE_S}&nbsp;s after Start, so there is time to go.</p>
      ${this._replaces(f, null)}
      ${this._buttons("start-empty", "Start", "primary", attr("target", f.floor ?? f.name))}
    </div>`;
  }

  _askClear(key, a) {
    return `<div class="ask danger" role="alertdialog" aria-label="Clear the ${esc(a.name)}">
      <p>Forget the ${plural(a.samples + (a.still_samples || 0), "sample", "samples")} of the ${esc(a.name)}? Its presence sensor goes with them. It can be calibrated again any time.</p>
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
    const where = [floor && !oneLevel(floor, d.floors) ? floorLabel(floor, d.floors) : null, n.host].filter(Boolean).join(", ");
    const onPlan = floor?.plan ? !!floor.positions?.[n.mac]?.placed : null;
    const chips = [
      `<span class="chip${n.online ? " up" : ""}">${n.online ? "online" : "offline"}</span>`,
      `<span class="chip">${n.placed ? "on the layout" : "not placed yet"}</span>`,
      onPlan == null ? "" : `<span class="chip${onPlan ? "" : " todo"}">${onPlan ? "placed on the plan" : "not placed on the plan"}</span>`,
      n.added ? "" : `<span class="chip">not added to Wisp</span>`,
    ].join("");
    return `<div class="row">
      <div class="line">
        <span class="dot${n.online ? " up" : ""}" aria-hidden="true"></span>
        <div class="what"><b>${esc(n.name)}</b>${where ? `<small>${esc(where)}</small>` : ""}<small class="wifi" hidden></small><span class="chips">${chips}</span></div>
        ${n.device_id ? `<div class="acts"><button data-act="device"${attr("device", n.device_id)} aria-label="Open the ESPHome device of ${esc(n.name)}">ESPHome</button></div>` : ""}
      </div>
      ${n.added ? this._nodeArea(n, d) : ""}
    </div>`;
  }

  /* The node's grid channel, the access point it hears and how strongly: "Channel 11 · AP 6d:70 · -52 dBm". */
  _wifi(n) {
    const w = n.wifi ?? {};
    return [w.channel != null ? `Channel ${w.channel}` : "", w.ap ? apLabel(w.ap) : "", w.rssi != null ? `${w.rssi} dBm` : ""].filter(Boolean).join(" · ");
  }

  /* The area the node stands in: Home Assistant's areas by floor. Setting it moves the node to
     that floor, its rooms and its plan. */
  _nodeArea(n, d) {
    const choice = this._areaChoice?.mac === n.mac ? this._areaChoice : null;
    if (choice && !choice.busy && (n.area ?? null) === choice.area) this._areaChoice = null; // arrived
    const current = choice ? choice.area : n.area ?? null;
    const groups = new Map();
    for (const a of d.areas ?? []) {
      const key = a.floor_name ?? "Areas without a floor";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(a);
    }
    const options = [...groups].map(([floor, list]) => `<optgroup label="${esc(floor)}">${list.map((a) => `<option value="${esc(a.area)}"${a.area === current ? " selected" : ""}>${esc(a.name)}</option>`).join("")}</optgroup>`).join("");
    const id = `wisp-area-${n.mac.replace(/:/g, "")}`;
    const failed = this._areaFailure?.mac === n.mac ? `<p class="fail" role="alert">${esc(this._areaFailure.message)}</p>` : "";
    return `<div class="node-area"><label for="${id}">Area</label><select id="${id}" data-act="node-area"${attr("mac", n.mac)}${choice?.busy ? " disabled" : ""}><option value=""${current ? "" : " selected"}>No area</option>${options}</select></div>${failed}`;
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
    display: flex; flex-direction: column; height: 100%; color-scheme: light;
    background: var(--primary-background-color); color: var(--primary-text-color);
  }
  .page.dark {
    --wisp-paper-1: #43362a; --wisp-paper-2: #33291e; --wisp-paper-3: #211a13;
    --wisp-ink: #ead6ab; --wisp-hot: #f0905e; --wisp-mark: #3b2f22; --wisp-on-hot: #2a1d12;
    --wisp-burn: rgba(0, 0, 0, .5); --wisp-edge: rgba(234, 214, 171, .2);
    color-scheme: dark; /* checkboxes and the lists of the selects in the dark too */
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
  .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; color: var(--wisp-ink); font-family: var(--wisp-serif); }
  .tabs button { min-height: 34px; font-size: .875rem; }
  .tabs button[aria-pressed="true"] { background: var(--wisp-ink); color: var(--wisp-paper-1); }

  .plan-box { position: relative; margin: 4px 14px 0; border: 1.5px solid color-mix(in srgb, var(--wisp-ink) 55%, transparent); }
  /* The plan inked onto the parchment, as on the map */
  .plan-img { position: absolute; display: block; object-fit: fill; pointer-events: none; mix-blend-mode: multiply; opacity: .9; }
  .page.dark .plan-img { filter: invert(1) hue-rotate(180deg); mix-blend-mode: screen; opacity: .7; }
  .plan-draw { position: relative; }
  .plan-draw svg { display: block; width: 100%; height: auto; user-select: none; -webkit-user-select: none; -webkit-touch-callout: none; }
  .plan-draw .edge { fill: none; stroke: var(--wisp-ink); stroke-width: 1; opacity: .35; }
  .plan-draw .grid { fill: none; stroke: var(--wisp-ink); stroke-width: .6; opacity: .16; }
  .upload-line { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 8px 0; }
  .upload-line small { opacity: .7; }
  .node-area { display: flex; align-items: center; gap: 8px; margin: 6px 0 2px 25px; } /* under the name: dot and gap */
  .node-area label { font-size: .85em; opacity: .8; }
  .node-area select { flex: 1; min-width: 0; max-width: 280px; }
  .node-area + .fail { margin: 6px 0 0 25px; font-size: .9rem; font-style: italic; line-height: 1.45; color: var(--wisp-hot); }
  .channel .about { margin: 6px 0 0; font-size: .85rem; font-style: italic; line-height: 1.45; opacity: .8; }
  .channel .fail { margin: 6px 0 0; font-size: .9rem; font-style: italic; line-height: 1.45; color: var(--wisp-hot); }
  .plan-draw text { font-family: var(--wisp-serif); fill: var(--wisp-ink); text-anchor: middle;
                    paint-order: stroke; stroke: var(--wisp-paper-1); stroke-width: 3.5px; stroke-linejoin: round; }
  .plan-draw .tray { font-size: 11px; font-style: italic; opacity: .8; }
  .item { cursor: grab; touch-action: none; outline: none; }
  .item.dragging { cursor: grabbing; }
  .item .hit { fill: transparent; }
  .item .ring { fill: var(--wisp-mark); stroke: var(--wisp-ink); stroke-width: 2.2; stroke-dasharray: 2.5 2; }
  .item.placed .ring { stroke-dasharray: none; stroke-width: 2.6; }
  .item .dot { fill: var(--wisp-ink); }
  .item .waves { fill: none; stroke: var(--wisp-ink); stroke-width: 1.5; stroke-linecap: round; opacity: .75; }
  .item text { font-size: 12px; font-weight: 600; }
  .item:not(.placed) text { font-weight: 400; font-style: italic; }
  .item.sel .hit, .item:focus-visible .hit { fill: color-mix(in srgb, var(--wisp-hot) 16%, transparent); stroke: var(--wisp-hot); stroke-width: 1.5; }
  .placer-info { display: grid; gap: 8px; padding: 12px 18px 14px; }
  .placer-info p { margin: 0; line-height: 1.45; }
  .placer-info .say { font-size: .9rem; font-style: italic; opacity: .85; }
  .placer-info .fail { color: var(--wisp-hot); font-style: italic; }
  .unplaced b { color: var(--wisp-hot); }
  .unplaced.done { font-style: italic; }
  .sel-line { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 12px; }
  .sel-line p { flex: 1 1 12rem; }
  .sel-buttons { display: flex; flex-wrap: wrap; gap: 4px 12px; }
  .sel-acts { display: flex; align-items: center; gap: 4px; }
  /* Drawing rooms: a wash per room in its own gentle hue, over the plan */
  .placer.rooms .plan-draw svg { touch-action: none; cursor: crosshair; }
  .room { cursor: move; outline: none; }
  .room rect { fill: hsl(var(--hue) 45% 52% / .18); stroke: hsl(var(--hue) 40% 32% / .8); stroke-width: 1.4; }
  .room.sel rect { fill: hsl(var(--hue) 45% 52% / .3); stroke: hsl(var(--hue) 45% 26%); stroke-width: 2.2; }
  .page.dark .room rect { fill: hsl(var(--hue) 50% 62% / .18); stroke: hsl(var(--hue) 55% 74% / .8); }
  .page.dark .room.sel rect { fill: hsl(var(--hue) 50% 62% / .3); stroke: hsl(var(--hue) 60% 80%); }
  .page .room:focus-visible:not(.sel) rect { stroke: var(--wisp-hot); stroke-width: 2.2; } /* the selected one shows already */
  .plan-draw .room-name { font-size: 11px; font-style: italic; text-anchor: start; pointer-events: none; }
  .handle .hit { fill: transparent; }
  .handle .knob { fill: var(--wisp-paper-1); stroke: var(--wisp-ink); stroke-width: 1.5; }
  .handle[data-corner=nw], .handle[data-corner=se] { cursor: nwse-resize; }
  .handle[data-corner=ne], .handle[data-corner=sw] { cursor: nesw-resize; }
  .room-picks { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
  .picks-label { font-size: .9rem; font-style: italic; opacity: .85; margin-right: 2px; }
  .room-picks button { display: inline-flex; align-items: center; gap: 7px; min-height: 34px; padding: 4px 12px 4px 9px; font-size: .9rem; }
  .room-picks button[aria-pressed="true"] { background: var(--wisp-ink); color: var(--wisp-paper-1); }
  .room-picks i { flex: none; box-sizing: border-box; width: 12px; height: 12px; border-radius: 3px;
                  background: hsl(var(--hue) 55% 55% / .55); border: 1.5px solid hsl(var(--hue) 45% 30%); }
  .page.dark .room-picks i { background: hsl(var(--hue) 50% 62% / .55); border-color: hsl(var(--hue) 55% 74%); }
  .page .room-picks [aria-pressed="true"] i { border-color: var(--wisp-paper-1); }
  .page .room-picks i.none { background: transparent; border-style: dashed; }

  .ask .field { display: grid; gap: 4px; flex: 1 1 8rem; }
  .field span { font-size: .85rem; font-style: italic; opacity: .85; }
  .fields { display: flex; flex-wrap: wrap; gap: 8px 12px; }
  input[type=text], input[type=number] {
    font: inherit; font-size: 16px; color: var(--wisp-ink); box-sizing: border-box; width: 100%; min-height: 40px; padding: 6px 10px;
    border-radius: 8px; border: 1.5px solid color-mix(in srgb, var(--wisp-ink) 55%, transparent);
    background: color-mix(in srgb, var(--wisp-paper-1) 85%, transparent);
  }
  input:disabled { opacity: .6; }
  input:focus-visible { outline: 2px solid var(--wisp-hot); outline-offset: 2px; }
  .ask .check { justify-self: start; }
  .ask .check:has(:disabled) { opacity: .6; }
  .check input:disabled { opacity: 1; }
  .check input { width: 18px; height: 18px; margin: 0; accent-color: var(--wisp-ink); }
  .plan-status { font-size: .9rem; font-style: italic; }
  .plan-status:empty { display: none; }
  .plan-status.bad { color: var(--wisp-hot); }
  .preview { justify-self: start; max-width: 100%; max-height: 120px; border: 1px solid var(--wisp-edge); border-radius: 4px; background: #fff; }
  .chip.todo { border-style: dashed; }

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
  /* A room's two ways to calibrate, side by side where they fit */
  .modes { display: flex; flex-wrap: wrap; gap: 8px; }
  .ask .mode { flex: 1 1 13rem; display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: start; gap: 10px;
               padding: 8px 12px; border-radius: 8px; cursor: pointer; border: 1.5px solid color-mix(in srgb, var(--wisp-ink) 25%, transparent); }
  .ask .mode:has(:checked) { border-color: var(--wisp-ink); background: color-mix(in srgb, var(--wisp-paper-1) 70%, transparent); }
  .mode input { width: 18px; height: 18px; margin: 2px 0 0; accent-color: var(--wisp-ink); }
  .mode input:focus-visible { outline: 2px solid var(--wisp-hot); outline-offset: 2px; }
  .mode span { display: grid; gap: 2px; }
  .mode small { font-size: .85rem; font-style: italic; line-height: 1.4; opacity: .85; }
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
