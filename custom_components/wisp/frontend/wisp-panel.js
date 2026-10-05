/*
 * Wisp panel: the live map, the rooms of each floor with their calibration and floor plan, the
 * nodes and the hive, for admins. Registered by the integration in the sidebar, no build step.
 * Live from wisp/panel/subscribe; the buttons call the wisp services and the wisp/floor commands.
 * The map is the map card itself; placing nodes on a plan happens on a drawing of the panel's own.
 */

const DURATIONS = [30, 60, 90, 120, 180, 300]; // s to record
const DURATION = 60; // s, as the services
const LEAVE_S = 30; // s to leave the floor before the empty floor records
const PREFS = "wisp-panel"; // this browser's duration, map turn, mirror and floor
const PW = 360; // viewBox width of the drawing to place nodes on
const PPAD = 16;
const PMAX_H = 480;
const NUDGE = 0.1; // metres an arrow key moves a node, five times that with shift
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
    this._planForm = null; // the floor plan form's values while it is open
    this._placing = null; // placing nodes on a plan: {floor, moved: Map(id, [x, y] or null), selected, focus, busy, failure}
    this._drag = null; // the node or access point under the pointer
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
    root.addEventListener("input", (e) => this._input(e));
    root.addEventListener("keydown", (e) => this._keydown(e));
    root.addEventListener("pointerdown", (e) => this._pointerDown(e));
    root.addEventListener("pointermove", (e) => this._pointerMove(e));
    root.addEventListener("pointerup", (e) => this._pointerUp(e));
    root.addEventListener("pointercancel", (e) => this._pointerUp(e));
    // A finger on a node drags it, not the page (Safari scrolls despite touch-action on SVG)
    root.addEventListener("touchstart", (e) => {
      if (e.target.closest?.(".placer .item")) e.preventDefault();
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
    if (!d?.loaded || !d.floors.length) return null;
    return d.floors.find((f) => (f.floor ?? "") === this._prefs.mapFloor) ?? d.floors[0];
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
    else if (act === "ask-plan") this._openPlanForm(floor);
    else if (act === "ask-remove-plan") this._ask("remove-plan", floor);
    else if (act === "save-plan") this._savePlan(floor);
    else if (act === "remove-plan") this._run({ type: "wisp/floor/clear", floor: floor || null }, () => this._mergeFloor(floor, null));
    else if (act === "place") this._startPlacing(floor);
    else if (act === "placer-cancel") this._stopPlacing();
    else if (act === "placer-save") this._savePlacing();
    else if (act === "unplace") this._unplace();
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
    else for (const k of ["plan", "positions", "access_points", "fit"]) delete f[k];
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
      status: "",
    };
    this._render();
    if (plan) this._loadPlanImage();
    this.shadowRoot.querySelector(".plan-form [data-plan=url]")?.focus();
  }

  _input(e) {
    const el = e.target, form = this._planForm, field = el.dataset?.plan;
    if (!field || !form) return;
    if (field === "keep") {
      form.keep = el.checked;
      if (!form.keep && !form.height && form.aspect && Number(form.width) > 0) form.height = String(metres(form.width * form.aspect));
    } else {
      form[field] = el.value;
    }
    if (field === "url") this._loadPlanImage();
    else this._fillPlanForm();
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
    input("keep").checked = form.keep;
    const height = input("height");
    height.disabled = form.keep;
    if (form.keep) height.value = form.aspect && Number(form.width) > 0 ? String(metres(form.width * form.aspect)) : "";
    else put(height, form.height);
    const status = box.querySelector(".plan-status");
    status.textContent = {
      loading: "Loading the image…",
      ok: form.size ? `The image is ${form.size[0]} by ${form.size[1]} pixels.` : "",
      nosize: "The image has no size of its own: untick Keep the image's proportions and give the height.",
      error: "The image could not be loaded: check its address.",
    }[form.status] ?? "";
    status.classList.toggle("bad", form.status === "error");
    const preview = box.querySelector(".preview");
    if (form.loaded && preview.getAttribute("src") !== form.loaded) preview.setAttribute("src", form.loaded);
    preview.hidden = !form.loaded;
  }

  _savePlan(key) {
    const form = this._planForm;
    if (!form || this._busy) return;
    const url = form.url.trim();
    const width = Number(form.width);
    const height = form.keep ? (form.aspect ? metres(width * form.aspect) : NaN) : Number(form.height);
    const waiting = {
      loading: "The image is still loading: try again in a moment.",
      error: "Check the image's address, or untick Keep the image's proportions and give the height.",
      nosize: "The image has no size of its own: untick Keep the image's proportions and give the height.",
    };
    const problem = !url ? "Give the address of the image."
      : !(width >= 1 && width <= 500) ? "Give the width in metres, from 1 to 500."
        : form.keep && !form.aspect ? waiting[form.status] ?? "The image has not loaded yet."
          : !(height >= 1 && height <= 500) ? "Give the height in metres, from 1 to 500."
            : null;
    if (problem) {
      this._failure = { open: this._open, message: problem };
      this._render();
      return;
    }
    const fresh = !this._data.floors.find((f) => (f.floor ?? "") === key)?.plan;
    this._run({ type: "wisp/floor/set_plan", floor: key || null, url, width, height }, (view) => {
      this._mergeFloor(key, view);
      if (fresh) this._startPlacing(key); // a new plan: on to placing the nodes
    });
  }

  // Placing nodes on a plan

  _startPlacing(key) {
    this._placing = { floor: key, moved: new Map(), selected: null, focus: null, busy: false, failure: null };
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
    if (p && e.type === "pointerup") {
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

  _runBanner(f) {
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

  /* A button per floor over the map when there are several; turn and mirror only without a plan. */
  _mapTools(d) {
    const root = this.shadowRoot;
    const shown = this._shownFloor(d);
    const placing = !!this._placing;
    const tabs = root.querySelector(".tabs");
    tabs.hidden = placing || d.floors.length < 2;
    const html = d.floors.map((f) => `<button data-act="map-floor"${attr("target", f.floor ?? "")} aria-pressed="${f === shown}">${esc(floorLabel(f, d.floors))}</button>`).join("");
    if (tabs._html !== html) {
      tabs.innerHTML = html;
      tabs._html = html;
    }
    root.querySelector(".map").hidden = placing;
    root.querySelector(".map-tools").hidden = placing || !!shown?.plan;
    if (this._card && (shown ? shown.floor ?? shown.name : "") !== this._cardFloor) this._card.setConfig(this._mapConfig());
  }

  /* Placing nodes: the plan, its nodes and access points to drag, and what is still to do. */
  _renderPlacer(d) {
    const root = this.shadowRoot;
    const f = this._placingFloor(d);
    if (this._placing && !f?.plan) this._placing = null; // the plan or the floor went away
    const p = this._placing;
    root.querySelector(".placer").hidden = !p;
    if (!p || this._drag) return; // nothing moves under a finger
    root.querySelector(".placer-note").textContent = floorLabel(f, d.floors);
    const items = this._items(f, d);
    const { svg, frame } = this._placerSvg(f, items);
    this._frame = frame;
    const draw = root.querySelector(".plan-draw"), info = root.querySelector(".placer-info");
    if (draw._html !== svg || this._redraw) {
      draw.innerHTML = svg;
      draw._html = svg;
      this._redraw = false;
      // The node moved with the keys keeps the focus in the new drawing, once
      if (p.focus) draw.querySelector(`.item[data-id="${CSS.escape(p.focus)}"]`)?.focus({ preventScroll: true });
    }
    p.focus = null;
    const text = this._placerInfo(f, items);
    if (info._html !== text) {
      info.innerHTML = text;
      info._html = text;
    }
    const img = root.querySelector(".plan-img");
    if (img.getAttribute("src") !== f.plan.url) img.setAttribute("src", f.plan.url);
    const pct = (v) => `${(100 * v).toFixed(3)}%`;
    Object.assign(img.style, {
      left: pct(frame.x / PW), top: pct(frame.y / frame.vh), width: pct(frame.w / PW), height: pct(frame.h / frame.vh),
    });
  }

  _placerSvg(f, items) {
    const plan = f.plan, selected = this._placing.selected;
    const s = Math.min((PW - 2 * PPAD) / plan.width, (PMAX_H - 2 * PPAD) / plan.height);
    const frame = { x: (PW - plan.width * s) / 2, y: PPAD, w: plan.width * s, h: plan.height * s, s, plan };
    const tray = items.filter((i) => !i.pos);
    frame.vh = Math.round(frame.y + frame.h + PPAD + (tray.length ? 58 : 0));
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
    const svg = `<svg viewBox="0 0 ${PW} ${frame.vh}" role="group" aria-label="The plan of ${esc(floorPhrase(f))}, ${n1(plan.width)} by ${n1(plan.height)} m"><rect class="edge" x="${n1(frame.x)}" y="${n1(frame.y)}" width="${n1(frame.w)}" height="${n1(frame.h)}"/>${below}${marks}</svg>`;
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
    return `${sel ? `<div class="sel-line"><p><b>${esc(sel.name)}</b> ${esc(this._where(sel))}</p>${sel.placed ? `<button class="quiet" data-act="unplace"${p.busy ? " disabled" : ""}>Let Wisp place it</button>` : ""}</div>` : ""}
      <p class="say">${esc(hint)}${pending && loose.length ? " Save to see them follow." : ""}</p>
      <p class="unplaced${loose.length ? "" : " done"}">${loose.length ? `<b>Not placed yet:</b> ${esc(loose.join(", "))}.` : "Every node is placed."}</p>
      ${fit}
      ${p.failure ? `<p class="fail" role="alert">${esc(p.failure)}</p>` : ""}
      <div class="ask-line"><span class="ask-buttons">
        <button data-act="placer-cancel"${p.busy ? " disabled" : ""}>Cancel</button>
        <button class="primary" data-act="placer-save"${p.busy || !pending ? " disabled" : ""}>${p.busy ? "Saving" : "Save"}</button></span></div>`;
  }

  _planRow(f) {
    const key = f.floor ?? "";
    const plan = f.plan;
    const form = this._isOpen("plan", key), removing = this._isOpen("remove-plan", key);
    const placed = plan ? f.nodes.filter((mac) => f.positions?.[mac]?.placed).length : 0;
    const meta = plan
      ? `${metres(plan.width)} by ${metres(plan.height)} m, ${placed} of ${plural(f.nodes.length, "node", "nodes")} placed`
      : "none yet: the map shows the hive's own layout";
    const placing = this._placing?.floor === key;
    const acts = plan
      ? `<button data-act="place"${attr("floor", key)}${placing || form || removing ? " disabled" : ""}>Place nodes</button>
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
      <p>An image of ${esc(floorPhrase(f))} seen from above, and its size in metres. Put the image in Home Assistant's www folder and give its address as /local/ and the file name, or give any web address.</p>
      <label class="field"><span>Image address</span><input data-plan="url" type="text" inputmode="url" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="/local/wisp/plan.png"></label>
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
    rows.push(["plan", this._planRow(f)]);
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
  _buttons(act, label, cls, data, { busy: working = cls === "primary" ? "Starting" : "Clearing", duration = cls === "primary" } = {}) {
    const busy = !!this._busy && this._busy === this._open;
    const failure = this._failure && this._failure.open === this._open ? `<p class="fail" role="alert">${esc(this._failure.message)}</p>` : "";
    return `${failure}<div class="ask-line">${duration ? this._durations() : ""}<span class="ask-buttons">
      <button data-act="close"${busy ? " disabled" : ""}>Cancel</button>
      <button class="${cls}" data-act="${act}"${data}${busy ? " disabled" : ""}>${busy ? working : label}</button></span></div>`;
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
