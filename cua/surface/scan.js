// In-frame helper library, injected into every frame we observe or act on.
// It is the web adapter's equivalent of walking a UI Automation tree on
// desktop: compute role / accessible name / neighbouring label for controls
// in pages that have no semantic markup, and resolve label-anchored locators.
//
// Called as:  frame.evaluate(LIB + "; return __cua[op](arg)")  (see web.py)
const __cua = (() => {
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const stripColon = (s) => norm(s).replace(/[:\s]+$/, "").toLowerCase();

  const INTERACTIVE = "a[href],button,input:not([type=hidden]),select,textarea,[onclick],[role=button],[role=link]";
  const TEXTY = "td,th,h1,h2,h3,h4,h5,h6,p,b,font,label,li,span,div.hdr,div";

  function visible(el) {
    if (!el || !el.getClientRects().length) return false;
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none" || st.opacity === "0") return false;
    // Hidden by an ancestor display:none (getClientRects already covers most cases).
    return true;
  }

  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (["submit", "button", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      return "textbox"; // includes password: we treat it as a textbox for targeting
    }
    if (tag === "textarea") return "textbox";
    if (tag === "select") return el.multiple ? "listbox" : "combobox";
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "td") return "cell";
    if (tag === "th") return "columnheader";
    if (el.hasAttribute("onclick")) return "button";
    return "text";
  }

  function ownText(el) {
    let s = "";
    for (const n of el.childNodes) if (n.nodeType === 3) s += n.textContent;
    return norm(s);
  }

  function accessibleName(el) {
    const aria = el.getAttribute("aria-label");
    if (aria) return norm(aria);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean)
        .map((e) => e.innerText).join(" ");
      if (norm(t)) return norm(t);
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return norm(l.innerText);
    }
    const wrap = el.closest("label");
    if (wrap) return norm(wrap.innerText);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "input" && ["submit", "button", "reset"].includes(type)) return norm(el.value);
    if (tag === "button" || tag === "a") return norm(el.innerText);
    if (el.title) return norm(el.title);
    if (el.alt) return norm(el.alt);
    if (el.placeholder) return norm(el.placeholder);
    if (["td", "th", "h1", "h2", "h3", "h4", "h5", "h6"].includes(tag)) return norm(el.innerText).slice(0, 120);
    return "";
  }

  // Visible text that labels a control in a table/legacy layout: the nearest
  // previous cell in the same row with text, else the preceding text.
  function neighborLabel(el) {
    const cell = el.closest("td,th");
    if (cell) {
      let c = cell.previousElementSibling;
      while (c) {
        const t = norm(c.innerText);
        if (t && !c.querySelector(INTERACTIVE)) return t;
        c = c.previousElementSibling;
      }
    }
    let p = el.previousSibling;
    for (let i = 0; p && i < 4; i++, p = p.previousSibling) {
      if (p.nodeType === 1 && !/^(B|FONT|SPAN|LABEL|I|STRONG)$/.test(p.tagName)) break;
      const t = norm(p.textContent);
      if (t) return t.length <= 40 ? t : "";
    }
    return "";
  }

  // For data cells: the row header we can anchor on, and how far right we are.
  function rowAnchor(el) {
    const cell = el.closest("td,th");
    if (!cell) return null;
    let c = cell.previousElementSibling, d = 1;
    while (c) {
      const t = norm(c.innerText);
      // Skip value-looking cells (amounts, ids) — anchors must be labels.
      if (t && !/^[\s$€£\d.,:\-#\/]+$/.test(t) && !/^S\d+$/.test(t)) return { anchor: t, offset: d };
      c = c.previousElementSibling; d++;
    }
    return null;
  }

  function cssPath(el) {
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id) && !/\d{3,}/.test(el.id)) {
      if (document.querySelectorAll("#" + CSS.escape(el.id)).length === 1) return "#" + CSS.escape(el.id);
    }
    const tag = el.tagName.toLowerCase();
    const nm = el.getAttribute("name");
    if (nm && ["input", "select", "textarea"].includes(tag)) {
      const sel = `${tag}[name="${nm}"]`;
      if (document.querySelectorAll(sel).length === 1) return sel;
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.tagName.toLowerCase() !== "body") {
      const t = cur.tagName.toLowerCase();
      let i = 1, s = cur.previousElementSibling;
      while (s) { if (s.tagName === cur.tagName) i++; s = s.previousElementSibling; }
      parts.unshift(`${t}:nth-of-type(${i})`);
      cur = cur.parentElement;
    }
    return "body > " + parts.join(" > ");
  }

  function xpathOf(el) {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1) {
      let i = 1, s = cur.previousElementSibling;
      while (s) { if (s.tagName === cur.tagName) i++; s = s.previousElementSibling; }
      parts.unshift(`${cur.tagName.toLowerCase()}[${i}]`);
      cur = cur.parentElement;
    }
    return "/" + parts.join("/");
  }

  function refOf(el, prefix) {
    if (!el.hasAttribute("data-cua-ref")) {
      const root = document.documentElement;
      const n = Number(root.getAttribute("data-cua-counter") || 0) + 1;
      root.setAttribute("data-cua-counter", String(n));
      el.setAttribute("data-cua-ref", `${prefix}-e${n}`);
    }
    return el.getAttribute("data-cua-ref");
  }

  function info(el) {
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const isPw = tag === "input" && (el.getAttribute("type") || "").toLowerCase() === "password";
    let value = null;
    if (["input", "textarea"].includes(tag) && roleOf(el) === "textbox") value = isPw ? "••••" : el.value;
    if (tag === "select") value = el.options[el.selectedIndex] ? norm(el.options[el.selectedIndex].text) : "";
    return {
      ref: el.getAttribute("data-cua-ref"),
      role: roleOf(el), name: accessibleName(el), tag,
      type: el.getAttribute("type"), text: norm(el.innerText || "").slice(0, 160),
      value, label: neighborLabel(el), password: isPw,
      options: tag === "select" ? Array.from(el.options).map((o) => norm(o.text)) : null,
      enabled: !el.disabled,
      box: { x: r.left, y: r.top, w: r.width, h: r.height },
    };
  }

  function scan(prefix) {
    const out = [];
    const seen = new Set();
    for (const el of document.querySelectorAll(INTERACTIVE)) {
      if (!visible(el)) continue;
      refOf(el, prefix); seen.add(el); out.push(info(el));
    }
    for (const el of document.querySelectorAll(TEXTY)) {
      if (seen.has(el) || !visible(el)) continue;
      const t = el.tagName.toLowerCase() === "td" || el.tagName.toLowerCase() === "th" ? norm(el.innerText) : ownText(el);
      if (!t || el.querySelector(INTERACTIVE) || el.querySelector("table")) continue;
      const cell = el.closest("td,th");
      if (cell && cell !== el && seen.has(cell) && norm(cell.innerText) === t) continue;
      refOf(el, prefix); seen.add(el);
      const i = info(el); i.role = roleOf(el); i.text = t.slice(0, 160); out.push(i);
      if (out.length > 400) break;
    }
    return { title: document.title, text: norm(document.body ? document.body.innerText : "").slice(0, 4000), elements: out };
  }

  function byRef(ref) { return document.querySelector(`[data-cua-ref="${ref}"]`); }

  function describe(ref) {
    const el = byRef(ref);
    if (!el) return null;
    const d = info(el);
    d.css = cssPath(el);
    d.xpath = xpathOf(el);
    d.row = rowAnchor(el);
    return d;
  }

  // Resolve a label-anchored locator. Marks hits with data-cua-hit=<token>.
  function findByLabel({ anchor, relation, role, offset, token }) {
    document.querySelectorAll("[data-cua-hit]").forEach((e) => e.removeAttribute("data-cua-hit"));
    const want = stripColon(anchor);
    const anchors = [];
    for (const el of document.querySelectorAll("td,th,label,span,b,font,p,div")) {
      if (!visible(el)) continue;
      const t = ["td", "th"].includes(el.tagName.toLowerCase()) ? el.innerText : ownText(el);
      if (stripColon(t) === want && !el.querySelector(INTERACTIVE)) anchors.push(el);
    }
    const hits = new Set();
    for (const a of anchors) {
      const cell = a.closest("td,th");
      if (relation === "row_cell") {
        if (!cell) continue;
        let c = cell;
        for (let i = 0; i < (offset || 1) && c; i++) c = c.nextElementSibling;
        if (c) hits.add(c);
      } else {
        let found = null;
        if (cell) {
          for (let c = cell.nextElementSibling; c && !found; c = c.nextElementSibling) {
            for (const ctl of c.querySelectorAll(INTERACTIVE)) {
              if (visible(ctl) && (!role || roleOf(ctl) === role)) { found = ctl; break; }
            }
          }
        } else {
          let n = a.nextElementSibling;
          for (let i = 0; n && i < 5 && !found; i++, n = n.nextElementSibling) {
            const ctl = n.matches(INTERACTIVE) ? n : n.querySelector(INTERACTIVE);
            if (ctl && visible(ctl) && (!role || roleOf(ctl) === role)) found = ctl;
          }
        }
        if (found) hits.add(found);
      }
    }
    for (const h of hits) h.setAttribute("data-cua-hit", token);
    return hits.size;
  }

  // Coordinate → element (screenshot-driven actions are grounded through this).
  function elementAt({ x, y, prefix }) {
    let el = document.elementFromPoint(x, y);
    if (!el) return null;
    const ctl = el.closest(INTERACTIVE);
    if (ctl) el = ctl;
    return refOf(el, prefix);
  }

  function hasRef({ ref }) { return !!byRef(ref); }

  function visibleText() { return document.body ? document.body.innerText : ""; }

  return { scan, describe, findByLabel, elementAt, hasRef, visibleText };
})();
