(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const messagesEl = $("messages");
  const chipsEl = $("chips");
  const inputEl = $("input");
  const sendBtn = $("send");
  const form = $("composer");

  const WELCOME =
    "Hello! I can answer questions about the clinic or book an appointment for you. How can I help?";
  const QUICK_QUESTIONS = [
    "What are your opening hours?",
    "Where are you located?",
    "How much is a consultation?",
    "I'd like to book an appointment",
  ];

  // Each browser tab keeps its own conversation via a random session id.
  const sessionKey = "clinic-session-id";
  let sessionId = null;
  try { sessionId = localStorage.getItem(sessionKey); } catch (_) { /* storage blocked */ }
  if (!sessionId) {
    sessionId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9_-]/g, "");
    try { localStorage.setItem(sessionKey, sessionId); } catch (_) { /* ignore */ }
  }

  let busy = false;

  /* ---------- tiny, safe markdown: **bold**, - lists, line breaks ---------- */
  const escapeHtml = (s) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function renderText(text) {
    const inline = (s) => escapeHtml(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    const out = [];
    let list = [];
    const flush = () => {
      if (list.length) { out.push("<ul>" + list.map((li) => `<li>${inline(li)}</li>`).join("") + "</ul>"); list = []; }
    };
    for (const line of text.split("\n")) {
      const item = line.match(/^\s*[-*]\s+(.*)$/);
      if (item) { list.push(item[1]); continue; }
      flush();
      if (line.trim()) out.push(`<p>${inline(line)}</p>`);
    }
    flush();
    return out.join("");
  }

  /* ---------- message helpers ---------- */
  function addMessage(role, text, extraClass = "") {
    const el = document.createElement("div");
    el.className = `msg ${role} ${extraClass}`.trim();
    el.innerHTML = renderText(text);
    messagesEl.appendChild(el);
    scrollDown();
    return el;
  }

  function showTyping() {
    const el = document.createElement("div");
    el.className = "msg bot";
    el.innerHTML = '<span class="typing" aria-label="Receptionist is typing"><i></i><i></i><i></i></span>';
    messagesEl.appendChild(el);
    scrollDown();
    return el;
  }

  function scrollDown() { messagesEl.scrollTop = messagesEl.scrollHeight; }

  function setBusy(value) {
    busy = value;
    sendBtn.disabled = value;
    inputEl.disabled = value;
    if (!value) inputEl.focus();
  }

  function renderChips() {
    chipsEl.innerHTML = "";
    for (const q of QUICK_QUESTIONS) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.textContent = q;
      b.addEventListener("click", () => send(q));
      chipsEl.appendChild(b);
    }
  }

  /* ---------- talking to the server ---------- */
  async function send(text) {
    text = text.trim();
    if (!text || busy) return;

    chipsEl.innerHTML = "";
    addMessage("user", text);
    inputEl.value = "";
    autoGrow();
    setBusy(true);
    const typing = showTyping();

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });
      typing.remove();
      if (!res.ok) {
        const detail = (await res.json().catch(() => ({}))).detail;
        const msg = typeof detail === "string" ? detail : "Something went wrong. Please try again.";
        addMessage("bot", msg, "error");
      } else {
        const data = await res.json();
        addMessage("bot", data.reply);
      }
    } catch (_) {
      typing.remove();
      addMessage("bot", "Can't reach the server. Check that it is running, then try again.", "error");
    } finally {
      setBusy(false);
    }
  }

  async function resetChat() {
    if (busy) return;
    try {
      await fetch("/api/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
    } catch (_) { /* the local reset below still works */ }
    messagesEl.innerHTML = "";
    addMessage("bot", WELCOME);
    renderChips();
    inputEl.focus();
  }

  /* ---------- input behaviour ---------- */
  function autoGrow() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + "px";
  }
  inputEl.addEventListener("input", autoGrow);
  inputEl.addEventListener("keydown", (e) => {
    // Enter sends, Shift+Enter makes a new line.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener("submit", (e) => { e.preventDefault(); send(inputEl.value); });
  $("reset").addEventListener("click", resetChat);

  /* ---------- hours board: highlight today ---------- */
  const todayRow = document.querySelector(`#hours li[data-day="${new Date().getDay()}"]`);
  if (todayRow) {
    todayRow.classList.add("today");
    todayRow.firstElementChild.textContent += " (today)";
  }

  /* ---------- server status ---------- */
  fetch("/api/health")
    .then((r) => r.json())
    .then((h) => { if (h.calendar_mode === "demo") $("status-text").textContent = "Online - demo calendar"; })
    .catch(() => {
      $("status-text").textContent = "Offline";
      document.querySelector(".dot").classList.add("offline");
    });

  addMessage("bot", WELCOME);
  renderChips();
  inputEl.focus();
})();
