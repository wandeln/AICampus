/* ──────────────────────────────────────────────────────────────────
   Emoji-Picker + Emojis: geteilt von Forum und DM-Panel.

   window.emojiPicker.open(anchorEl, onPick) → Popover-Grid mit allen Emojis
   window.EMOJIS_QUICK   → die 8 häufigsten (für die Reaktions-Leiste, die
                           bei Message-Hover inline eingeblendet wird)
   window.insertEmojiAtCursor(textarea, emoji) → Emoji an Cursorposition

   Das Popover (position: fixed, z über .dm-panel) schließt bei Klick
   außerhalb, Escape oder Auswahl. Klick-Ziele werden per composedPath
   geprüft — bleibt valide, auch wenn ein Handler dazwischen DOM rendert.
   Kein externes Picker-Plugin (KISS).
   ────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  // Schnell-Reaktionen (Messenger-Konvention: die häufigsten 8)
  const QUICK = ["👍", "❤️", "😂", "😮", "😢", "🙏", "🎉", "🔥"];

  const EMOJIS = [
    // Gesichter
    "😀","😃","😄","😁","😆","😅","🤣","😂","🙂","😉","😊","😇","🥰","😍","🤩","😘","😗","😚","😙","😋","😜","🤪","🤨","🧐","🤓","😎","🥳","😏","😒","😞","😔","😟","😕","🙁","😣","😖","😫","😩","🥺","😢","😭","😤","😠","😡","🤯","😳","🥵","🥶","😱","😨","😰","😥","🤗","🤔","🤭","🤫","🤥","😶","😐","😑","🙄","😯","😦","😮","😲","🥱","😴","🤤","😪","😷","🤒","🤕","🤢","🤮","🤧","😵","🤠","🤡","🥴","😈","👻","💀","👽","🤖",
    // Gesten
    "👍","👎","👌","🤌","🤏","✌️","🤞","🤟","🤘","🤙","👈","👉","👆","👇","☝️","✋","🤚","🖐️","🖖","👋","🫱","🫲","🤝","🙏","💪","✊","👊","🫰",
    // Tiere & Natur
    "🐶","🐱","🐭","🐹","🐰","🦊","🐻","🐼","🐨","🐯","🦁","🐮","🐷","🐸","🐵","🙈","🙉","🙊","🐔","🐧","🐦","🐤","🦆","🦅","🦉","🦇","🐺","🐴","🦄","🐝","🦋","🐌","🐞","🐢","🐍","🦖","🦕","🐙","🦀","🐬","🐳","🐋","🦈","🌸","🌹","🌺","🌷","🌻","🌼","🌿","🍀","🍁","🍂","🍄","🌈","☀️","⛅","☁️","❄️","⛄","☔","🌙","⭐","🌟","✨","⚡","🔥","💧","🌊",
    // Essen & Trinken
    "🍏","🍎","🍊","🍋","🍌","🍉","🍇","🍓","🍒","🍑","🥭","🍍","🥥","🥝","🍅","🥑","🥦","🥕","🌽","🌶️","🥔","🍞","🥐","🥖","🧀","🥚","🍳","🥓","🍗","🍖","🌭","🍔","🍟","🍕","🥪","🌮","🌯","🍜","🍝","🍲","🍣","🍱","🥟","🍤","🍙","🍚","🍧","🍨","🍦","🥧","🧁","🍰","🎂","🍮","🍭","🍬","🍫","🍿","🍩","🍪","🥛","☕","🍵","🥤","🍺","🍻","🥂","🍷","🥃","🍸","🍹",
    // Sport & Hobby
    "⚽","🏀","🏈","⚾","🎾","🏐","🏉","🎱","🏓","🏸","🥊","🥋","🎽","🛹","🛼","⛸️","🎿","⛷️","🏂","🤸","🏌️","🏇","🧘","🏄","🏊","🤽","🚣","🧗","🚵","🚴","🏆","🥇","🥈","🥉","🏅","🎖️","🎫","🎟️","🎪","🤹","🎭","🩰","🎨","🎬","🎤","🎧","🎼","🎹","🥁","🎷","🎺","🎸","🎻","🎲","♟️","🎯","🎳","🎮","🎰","🧩",
    // Dinge
    "⌚","📱","💻","⌨️","🖥️","📷","📸","📹","🎥","📞","📺","📻","⏰","⌛","⏳","📡","🔋","🔌","💡","🔦","🕯️","🧯","💸","💵","💰","💳","💎","⚖️","🧰","🔧","🔨","⚙️","🧲","🪓","🔪","🗡️","⚔️","🛡️","⚗️","🔭","🔬","🧬","🧪","💊","💉","🩹","🚽","🚪","🛏️","🛋️","🪑","🖼️","🧸","🎁","🎈","🎉","🎊","🏮","🎐","🧧","✉️","📩","📨","📧","💌","📦","🏷️","📜","📄","📑","📊","📈","📉","📝","✏️","🖊️","✒️","📌","📍","📎","📏","📚","📖","🔗",
    // Symbole & Farben
    "❤️","🧡","💛","💚","💙","💜","🖤","🤍","🤎","💔","❣️","💕","💞","💓","💗","💖","💘","💝","💟","💌","❤️‍🔥","💯","💢","💥","💫","💦","💨","💬","💭","🗯️","🔊","🔉","🔈","🔇","🔔","🔕","📣","📢","✅","❌","⭕","🛑","⛔","🚫","♻️","©️","®️","™️","➕","➖","➗","✖️","✔️","☑️","🔘","🔵","🔴","🟢","🟡","🟣","🟠","⚪","⚫","🟤","🔶","🔷","🔸","🔹","⬜","⬛",
    // Reisen & Orte
    "🚗","🚕","🚙","🚌","🏎️","🚓","🚑","🚒","🚚","🚲","🛵","🏍️","🚨","✈️","🚀","🛸","🚁","⛵","🚤","🛶","🚢","⚓","🚦","🗺️","🏔️","⛰️","🌋","🗻","🏕️","🏖️","🏝️","🏟️","🎡","🎢","🎠","🏛️","🏗️","🏭","🏢","🏬","🏣","🏥","🏦","🏨","🏪","🏫","💒","🏠","🏡",
    // Flaggen (Europa + übliche)
    "🇩🇪","🇦🇹","🇨🇭","🇫🇷","🇬🇧","🇺🇸","🇪🇸","🇮🇹","🇳🇱","🇵🇹","🇵🇱","🇸🇪","🇳🇴","🇩🇰","🇫🇮","🇮🇪","🇧🇪","🇱🇺","🇨🇿","🇹🇷","🇯🇵","🇰🇷","🇨🇳","🇮🇳","🇧🇷","🇲🇽","🇦🇷","🇬🇷","🇵🇪","🇿🇦",
  ];

  // Breite fix inline (8 × 36 + 7 × 2 + 2 × 8 + 2 Border): Das Projekt nutzt
  // den Tailwind-Play-CDN, der Klassen erst bei Sichtbarkeit im DOM
  // asynchron generiert. Ohne fester Breite wäre die Positions-Messung beim
  // ERSTEN Öffnen falsch (Grid noch ungestyled → Popover so breit wie der
  // Viewport → Auswahl links am Rand statt über dem Anker).
  const POP_WIDTH = 320;

  let pop = null;
  let state = { anchor: null, cb: null };

  function ensurePop() {
    if (pop) return pop;
    pop = document.createElement("div");
    pop.className = "emoji-pop fixed hidden bg-white border border-gray-200 rounded-xl shadow-xl overflow-hidden text-left";
    pop.style.zIndex = "100"; // über .dm-panel (z-60)
    pop.style.width = POP_WIDTH + "px";
    document.body.appendChild(pop);

    pop.addEventListener("click", (e) => {
      const cell = e.target.closest("[data-emoji]");
      if (!cell) return;
      const emoji = cell.dataset.emoji;
      const cb = state.cb;
      close();
      if (cb) cb(emoji);
    });

    // Klick außerhalb / Escape schließt. composedPath = Pfad vom Dispatch-
    // Zeitpunkt: bleibt valide, auch wenn ein Handler dazwischen rendert.
    document.addEventListener("click", (e) => {
      if (!pop || pop.classList.contains("hidden")) return;
      const path = typeof e.composedPath === "function" ? e.composedPath() : [e.target];
      if (path.includes(pop)) return;
      if (state.anchor && path.includes(state.anchor)) return;
      close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") close();
    });
    return pop;
  }

  function renderGrid() {
    // max-height/overflow inline: auch hier nicht auf JIT-Generierung warten
    // (s. POP_WIDTH). max-h-56/overflow-y-auto wirken gleich.
    pop.innerHTML =
      '<div style="max-height:224px;overflow-y:auto" class="grid grid-cols-8 gap-0.5 p-2">' +
      EMOJIS.map((e) =>
        `<button type="button" class="text-lg leading-none w-9 h-9 flex items-center justify-center rounded-lg hover:bg-gray-100 transition" data-emoji="${e}">${e}</button>`
      ).join("") +
      "</div>";
  }

  // Über dem Anker (rechte Kante bündig), in den Viewport geklemmt;
  // nicht genug Platz oben → unter den Anker.
  function position(anchor) {
    const r = anchor.getBoundingClientRect();
    const w = pop.offsetWidth;
    const h = pop.offsetHeight;
    const left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8);
    let top = r.top - h - 6;
    if (top < 8) top = r.bottom + 6;
    top = Math.min(Math.max(8, top), window.innerHeight - h - 8);
    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }

  function open(anchor, onPick) {
    if (!anchor) return;
    const el = ensurePop();
    // Zweiter Klick auf denselben Button = schließen (Toggle).
    if (!el.classList.contains("hidden") && state.anchor === anchor) {
      close();
      return;
    }
    state = { anchor, cb: onPick };
    renderGrid();
    el.classList.remove("hidden");
    position(anchor);
  }

  function close() {
    if (pop) pop.classList.add("hidden");
    state = { anchor: null, cb: null };
  }

  // Popover mit Grid vor-geladen (verborgen): Der Tailwind-Play-CDN
  // generiert die Grid-Klassen erst, wenn sie im DOM sichtbar werden —
  // ohne Warm-Up wäre das ERSTE Öffnen kurz ungestyled (Messung + Look).
  // Bei "interactive" (Script am Body-Ende) direkt, sonst nach DOM-Ready.
  function warmUp() {
    ensurePop();
    renderGrid();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", warmUp);
  } else {
    warmUp();
  }

  window.EMOJIS_QUICK = QUICK;
  window.emojiPicker = { open };

  // Emoji an die Cursorposition des Textareas einfügen (Selection ersetzt).
  window.insertEmojiAtCursor = function insertEmojiAtCursor(textarea, emoji) {
    if (!textarea) return;
    const start = textarea.selectionStart != null ? textarea.selectionStart : textarea.value.length;
    const end = textarea.selectionEnd != null ? textarea.selectionEnd : start;
    textarea.value = textarea.value.slice(0, start) + emoji + textarea.value.slice(end);
    const pos = start + emoji.length;
    textarea.setSelectionRange(pos, pos);
    textarea.focus();
    // "input" → Autosize-Listener der Compose-Felder reagiert.
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  };
})();
