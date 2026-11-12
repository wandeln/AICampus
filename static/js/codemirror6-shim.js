// CodeMirror 6 Shim — stellt window.CodeMirror (die CM5-kompatible Fassade,
// die alle Templates nutzen) sofort bereit.
//
// Die Templates initialisieren Editoren aus klassischen Inline-Skripts per
// CodeMirror.fromTextArea(...). Der CM6-Kern steht erst nach dem Laden des
// ES-Modules (codemirror6-setup.js, deferred) zur Verfügung. Deshalb:
//
//  - Solang das Modul noch nicht geladen ist, erzeugt fromTextArea() einen
//    leichten Platzhalter: Das Original-Textarea wird in ein Wrapper-
//    <div> verschoben und bleibt editierbar — die Seite bleibt voll
//    funktionsfähig, selbst wenn das CDN-Modul nie ankommt (Offline).
//  - Wenn das Modul lädt, werden alle Platzhalter am Ort upgraded: Der
//    echte CM6Editor übernimmt (DOM bleibt an derselben Stelle), die
//    registrierten Handler werden übertragen. Template-Closures halten
//    die Platzhalter-Referenz weiter und funktionieren dadurch unverändert.
//  - Editoren, die NACH dem Modul-Load erzeugt werden (z. B. Skript-
//    Sektionen dynamisch per Klick), erhalten direkt den echten
//    CM6Editor.
(function () {
  "use strict";
  if (window.CodeMirror) return;

  const pending = []; // Platzhalter, die auf das Upgrade warten

  // ── Helfer: CM5-0-basierte {line, ch} ⇄ Zeichenindex ───────────────
  function lineOf(str, idx) {
    let n = 0, i = 0;
    while (i < idx && i < str.length) { if (str.charCodeAt(i) === 10) n++; i++; }
    return n;
  }
  function posIndex(str, line, ch) {
    let idx = 0, n = 0;
    while (n < line && idx < str.length) {
      if (str.charCodeAt(idx) === 10) n++;
      idx++;
    }
    const lineStart = str.lastIndexOf("\n", idx - 1) + 1;
    return Math.min(str.length, lineStart + Math.max(0, ch));
  }

  // ── Platzhalter (Fallback + Vor-Upgrade) ───────────────────────────
  class Placeholder {
    constructor(ta, cfg) {
      this.ta = ta;
      this.cfg = cfg || {};
      this._impl = null;
      this._handlers = { change: [] };
      const self = this;
      this._inputHandler = () => self._fireChange();
      ta.addEventListener("input", this._inputHandler);
      if (this.cfg.readOnly) ta.readOnly = true;
      const wrapper = document.createElement("div");
      wrapper.className = "ta-cm ta-cm-fallback";
      ta.parentNode.insertBefore(wrapper, ta);
      wrapper.appendChild(ta);
      this.wrapper = wrapper;
    }

    _fireChange() {
      for (const fn of this._handlers.change.slice()) {
        try { fn(this); } catch (err) { console.error(err); }
      }
    }

    getValue() { return this._impl ? this._impl.getValue() : this.ta.value; }

    setValue(v) {
      if (this._impl) return this._impl.setValue(v);
      v = String(v == null ? "" : v);
      if (this.ta.value === v) return; // CM5-Verhalten: kein change-Event bei identischem Wert
      this.ta.value = v;
      this._fireChange();
    }

    setInitialContent(v) {
      if (this._impl) return this._impl.setInitialContent(v);
      this.setValue(v);
    }

    getCursor() {
      if (this._impl) return this._impl.getCursor();
      const s = this.ta.selectionStart == null ? 0 : this.ta.selectionStart;
      return { line: lineOf(this.ta.value, s), ch: s - (this.ta.value.lastIndexOf("\n", s - 1) + 1) };
    }

    setCursor(line, ch, opts) {
      if (this._impl) return this._impl.setCursor(line, ch, opts);
      const idx = posIndex(this.ta.value, line, ch);
      try { this.ta.setSelectionRange(idx, idx); } catch (err) { /* ignore */ }
      if (!opts || opts.scroll !== false) this.ta.focus();
    }

    replaceRange(text, from, to) {
      if (this._impl) return this._impl.replaceRange(text, from, to);
      const v = this.ta.value;
      const a = posIndex(v, from.line, from.ch);
      const b = to ? posIndex(v, to.line, to.ch) : a;
      const t = String(text == null ? "" : text);
      this.ta.value = v.slice(0, a) + t + v.slice(b);
      this._fireChange();
    }

    indexFromPos(pos) {
      return this._impl ? this._impl.indexFromPos(pos) : posIndex(this.ta.value, pos.line, pos.ch);
    }

    lastLine() {
      return this._impl ? this._impl.lastLine() : lineOf(this.ta.value, this.ta.value.length);
    }

    on(type, fn) {
      if (this._impl) return this._impl.on(type, fn);
      if (type === "change") this._handlers.change.push(fn);
      return this;
    }

    off(type, fn) {
      if (this._impl) return this._impl.off(type, fn);
      if (type === "change") {
        const i = this._handlers.change.indexOf(fn);
        if (i >= 0) this._handlers.change.splice(i, 1);
      }
      return this;
    }

    focus() { return this._impl ? this._impl.focus() : this.ta.focus(); }
    refresh() { if (this._impl) this._impl.refresh(); } // CM6: no-op (ResizeObserver)

    getOption(name) {
      if (this._impl) return this._impl.getOption(name);
      if (name === "readOnly") return this.ta.readOnly ? "nocursor" : false;
      return this.cfg[name];
    }

    setOption(name, value) {
      if (this._impl) return this._impl.setOption(name, value);
      if (name === "readOnly") this.ta.readOnly = !!value;
    }

    getWrapperElement() { return this._impl ? this._impl.getWrapperElement() : this.wrapper; }
    getScrollerElement() { return this._impl ? this._impl.getScrollerElement() : this.ta; }
    save() { if (this._impl) this._impl.save(); } // Platzhalter: ta ist immer synchron

    toTextArea() {
      if (this._impl) return this._impl.toTextArea();
      this.wrapper.replaceWith(this.ta);
    }

    scrollToLineTop(line) {
      if (this._impl) return this._impl.scrollToLineTop(line);
      const idx = posIndex(this.ta.value, line, 0);
      this.ta.focus();
      try { this.ta.setSelectionRange(idx, idx); } catch (err) { /* ignore */ }
    }

    // Einmaliges Upgrade auf den echten CM6Editor (wird von __connect aufgerufen).
    _upgrade(EditorClass) {
      if (!this.wrapper.isConnected || !this.wrapper.contains(this.ta) || !this.ta.isConnected) return null;
      const impl = new EditorClass(this.ta, this.cfg, this.wrapper);
      impl._handlers = this._handlers; // registrierte Listener übertragen
      // Cursor aus der Fallback-Phase übernehmen: ein Folien-Jump
      // (scrollToLineTop) kann im Platzhalter-Textarea angewendet worden
      // sein, bevor das CM6-Modul geladen war — sonst würde der Upgrade
      // (frischer State, Cursor/Scroll oben) den Jump verwerfen. Auf
      // Mobilnetzen ist das CM6-Modul (CDN) oft langsamer als der lokale
      // Content-Fetch → genau dieses Race.
      const sel = this.ta.selectionStart;
      if (sel != null && sel > 0) {
        try { impl.scrollToLineTop(lineOf(this.ta.value, sel)); } catch (err) { /* ignore */ }
      }
      this.ta.removeEventListener("input", this._inputHandler);
      this.wrapper.classList.remove("ta-cm-fallback");
      this._impl = impl;
      return impl;
    }
  }

  // ── Öffentliche API (CM5-kompatibler Teilmenge) ─────────────────────
  const CodeMirror = {
    __cm6Ready: false,
    __CM6EditorClass: null,
    // Modus-Registrierung — Templates prüfen Verfügbarkeit hier
    // (workspace.js cmModeForPath, markdown-renderer.js, slides_edit.html).
    modes: {
      python: true, javascript: true, htmlmixed: true, xml: true,
      markdown: true, gfm: true, "aicampus-markdown": true,
      clike: true, shell: true, yaml: true,
    },
    mimeModes: {
      "text/css": true, "application/json": true,
      "text/x-csrc": true, "text/x-c++src": true,
      "text/x-nasm": true, "text/x-nginx-conf": true, "text/x-sql": true,
      "text/x-ini": true, "text/x-toml": true, "text/plain": true,
    },

    fromTextArea(ta, cfg) {
      if (CodeMirror.__cm6Ready) return new CodeMirror.__CM6EditorClass(ta, cfg);
      const ph = new Placeholder(ta, cfg);
      pending.push(ph);
      return ph;
    },

    // Wird von codemirror6-setup.js aufgerufen, sobald der CM6-Kern geladen ist.
    __connect(EditorClass) {
      CodeMirror.__CM6EditorClass = EditorClass;
      CodeMirror.__cm6Ready = true;
      for (const ph of pending.slice()) ph._upgrade(EditorClass);
      pending.length = 0;
    },
  };

  window.CodeMirror = CodeMirror;
})();
