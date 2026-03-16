const CODE = window.QUIZ_CODE;
const PAGE = window.QUIZ_PAGE;

let lastPhase = null;
let lastQuestionIndex = null;
let timerInterval = null;
let localAnswered = false;

function $(id) {
  return document.getElementById(id);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function apiGetState() {
  const res = await fetch(`/api/room/${encodeURIComponent(CODE)}/state`, { cache: "no-store" });
  if (!res.ok) throw new Error("state failed");
  return await res.json();
}

async function apiPost(path, body = {}) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data?.error || "Erreur";
    throw new Error(msg);
  }
  return data;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function renderPlayers(players, container, showScore = false) {
  container.innerHTML = "";
  for (const p of players) {
    const el = document.createElement("div");
    el.className = "player";
    el.innerHTML = `
      <div class="left">
        <div class="dot ${p.connected ? "on" : ""}"></div>
        <div class="name">${escapeHtml(p.name)}</div>
      </div>
      <div class="score">${showScore ? `${p.score} pts` : (p.connected ? "en ligne" : "déconnecté")}</div>
    `;
    container.appendChild(el);
  }
}

function renderPodium(players, container) {
  const top = players.slice(0, 3);
  const slots = [
    { idx: 1, cls: "silver", emoji: "🥈" },
    { idx: 0, cls: "gold", emoji: "🥇" },
    { idx: 2, cls: "bronze", emoji: "🥉" },
  ];
  container.innerHTML = "";
  for (const s of slots) {
    const p = top[s.idx];
    const el = document.createElement("div");
    el.className = `pod ${s.cls}`;
    el.innerHTML = p
      ? `
        <div class="pos">${s.emoji} #${s.idx + 1}</div>
        <div class="pname">${escapeHtml(p.name)}</div>
        <div class="pscore">${p.score} pts</div>
      `
      : `
        <div class="pos">${s.emoji} …</div>
        <div class="pname">—</div>
        <div class="pscore">0 pts</div>
      `;
    container.appendChild(el);
  }
}

function renderScoreTable(players, container) {
  container.innerHTML = "";
  players.forEach((p, i) => {
    const el = document.createElement("div");
    el.className = "rowitem";
    el.innerHTML = `
      <div class="left">
        <div class="ranknum">${i + 1}</div>
        <div class="nm">${escapeHtml(p.name)} ${p.connected ? "" : "<span class='mono' style='opacity:.65'>(off)</span>"}</div>
      </div>
      <div class="sc">${p.score} pts</div>
    `;
    container.appendChild(el);
  });
}

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setTimer(secondsLeft) {
  const el = $("timerValue");
  if (!el) return;
  el.textContent = String(Math.max(0, Math.ceil(secondsLeft)));
  const box = el.closest(".timer");
  if (box) {
    if (secondsLeft <= 5) box.classList.add("soon");
    else box.classList.remove("soon");
  }
}

function startLocalTimer(getSecondsLeft) {
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    setTimer(getSecondsLeft());
  }, 200);
}

function stopLocalTimer() {
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = null;
}

function renderQuestion(state) {
  const q = state.question;
  if (!q) return;

  const qText = $("questionText");
  const answers = $("answers");
  const afterArea = $("afterArea");
  const revealGood = $("revealGood");
  const revealAnecdote = $("revealAnecdote");

  $("progressText").textContent = `Question ${q.index + 1}/${q.total}`;
  $("barInner").style.width = `${((q.index + 1) / q.total) * 100}%`;

  // Transition animation by toggling opacity/transform lightly.
  if (lastQuestionIndex !== q.index) {
    localAnswered = false;
    answers.style.opacity = "0.35";
    answers.style.transform = "translateY(4px)";
    setTimeout(() => {
      answers.style.opacity = "1";
      answers.style.transform = "none";
    }, 120);
  }

  qText.textContent = q.question || "Question mystérieuse…";
  answers.innerHTML = "";

  const labels = [
    ["A", "A"],
    ["B", "B"],
    ["C", "C"],
    ["D", "D"],
  ];

  for (const [key, badge] of labels) {
    const btn = document.createElement("button");
    btn.className = "answer";
    btn.type = "button";
    btn.disabled = state.me?.has_answered || localAnswered || state.phase !== "question";
    btn.innerHTML = `
      <div class="badge">${badge}</div>
      <div class="txt">${escapeHtml(q.choices?.[key] || "—")}</div>
    `;
    btn.addEventListener("click", async () => {
      if (btn.disabled) return;
      localAnswered = true;
      for (const b of answers.querySelectorAll("button.answer")) b.disabled = true;
      try {
        const res = await apiPost(`/api/room/${encodeURIComponent(CODE)}/answer`, { choice: key });
        if (res.correct) btn.classList.add("good");
        else btn.classList.add("bad");
      } catch (e) {
        // silently ignore; state polling will correct UI
      }
    });
    answers.appendChild(btn);
  }

  if (state.phase === "reveal") {
    afterArea.style.display = "";
    const correct = state.reveal?.correct || "—";
    revealGood.textContent = `✅ Bonne réponse : ${correct}`;
    revealAnecdote.textContent = state.reveal?.anecdote || "—";

    // color choices
    for (const b of answers.querySelectorAll("button.answer")) {
      const badge = b.querySelector(".badge")?.textContent?.trim();
      if (badge === correct) b.classList.add("good");
      else b.classList.add("bad");
      b.disabled = true;
    }
  } else {
    afterArea.style.display = "none";
  }

  lastQuestionIndex = q.index;
}

function wireLobbyButtons(state) {
  const shareUrl = state.share_url;
  const shareEl = $("shareUrl");
  if (shareEl) shareEl.textContent = shareUrl;

  const copyBtn = $("copyLinkBtn");
  if (copyBtn) {
    copyBtn.onclick = async () => {
      const ok = await copyText(shareUrl);
      copyBtn.textContent = ok ? "✅ Copié !" : "😅 Copie manuelle";
      setTimeout(() => (copyBtn.textContent = "📋 Copier le lien"), 1200);
    };
  }

  const startBtn = $("startBtn");
  if (startBtn) {
    startBtn.style.display = state.can_start ? "" : "none";
    startBtn.disabled = !state.can_start;
    startBtn.onclick = async () => {
      startBtn.disabled = true;
      startBtn.textContent = "⏳ Génération des questions…";
      try {
        await apiPost(`/api/room/${encodeURIComponent(CODE)}/start`, {});
      } catch (e) {
        startBtn.textContent = "🚀 Lancer la partie";
        startBtn.disabled = false;
      }
    };
  }

  const leaveBtn = $("leaveBtn");
  if (leaveBtn) {
    leaveBtn.onclick = async () => {
      try {
        await apiPost(`/api/room/${encodeURIComponent(CODE)}/leave`, {});
      } finally {
        window.location.href = "/";
      }
    };
  }
}

function wireResultsButtons(state) {
  const replayBtn = $("replayBtn");
  if (!replayBtn) return;
  replayBtn.onclick = async () => {
    replayBtn.disabled = true;
    replayBtn.textContent = "⏳ Création…";
    try {
      const res = await apiPost(`/api/room/${encodeURIComponent(CODE)}/replay`, {});
      window.location.href = res.url;
    } catch (e) {
      replayBtn.disabled = false;
      replayBtn.textContent = "🔁 Rejouer avec les mêmes amis";
    }
  };
}

async function loop() {
  while (true) {
    try {
      const state = await apiGetState();

      if (PAGE === "lobby") {
        $("themeVal").textContent = state.theme || "—";
        $("difficultyVal").textContent = state.difficulty || "—";
        $("countVal").textContent = `${state.num_questions || "—"}`;

        renderPlayers(state.players || [], $("playersList"), false);
        $("minPlayersHint").textContent =
          (state.players?.length || 0) >= 2 ? "Prêts ? Ça va buzzer." : "Minimum 2 joueurs pour démarrer.";

        wireLobbyButtons(state);

        if (state.phase !== "lobby") {
          window.location.reload();
          return;
        }
      }

      if (PAGE === "game") {
        // redirect if finished
        if (state.phase === "finished") {
          window.location.reload();
          return;
        }

        // timer is computed from server timestamps to stay synced-ish
        if (state.phase === "question") {
          const start = state.question_started_at || (Date.now() / 1000);
          startLocalTimer(() => QUESTION_SECONDS - ((Date.now() / 1000) - start));
        } else if (state.phase === "reveal") {
          stopLocalTimer();
          setTimer(0);
        }

        renderQuestion(state);
        renderPodium(state.players || [], $("podium"));
        renderScoreTable(state.players || [], $("scoreTable"));
      }

      if (PAGE === "results") {
        if (state.phase !== "finished") {
          window.location.reload();
          return;
        }
        const title = state.final?.title || "Légende en devenir";
        $("finalTitle").textContent = `Ton titre: ${title}`;
        renderPodium(state.players || [], $("podium"));
        renderScoreTable(state.players || [], $("scoreTable"));
        wireResultsButtons(state);
      }

      // phase change triggers fast refresh
      if (lastPhase && lastPhase !== state.phase && PAGE !== "results") {
        await sleep(350);
      }
      lastPhase = state.phase;
    } catch {
      // ignore transient errors
    }
    await sleep(2000);
  }
}

// Constants available in template
const QUESTION_SECONDS = 20;

loop();

