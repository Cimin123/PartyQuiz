const screen = document.querySelector("#screen");
const params = new URLSearchParams(location.search);
const inviteCode = (params.get("room") || "").toUpperCase();
let activeRoom = "";
let playerId = "";
let hostToken = "";
let role = "";
let pollTimer = null;
let loadingState = false;

const storageKeys = {
  room: "quiznight.room",
  role: "quiznight.role",
  player: "quiznight.player",
  host: "quiznight.host"
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(text, className, onClick) {
  const node = element("button", className, text);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function field(labelText, name, placeholder, type = "text") {
  const label = element("label", "field");
  label.append(element("span", "", labelText));
  const input = document.createElement("input");
  input.name = name;
  input.type = type;
  input.placeholder = placeholder;
  input.required = true;
  label.append(input);
  return label;
}

function showError(message) {
  const existing = screen.querySelector(".error");
  if (existing) existing.remove();
  screen.prepend(element("p", "error", message));
}

async function request(path, data) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Something went wrong.");
  return result;
}

function renderHome() {
  clearInterval(pollTimer);
  screen.replaceChildren();

  const intro = element("section", "intro");
  intro.append(element("p", "eyebrow", "YOUR LIVING ROOM, YOUR GAME SHOW"));
  intro.append(element("h1", "", "Bring the questions.\nBring the noise."));
  intro.append(element("p", "lede", "One host runs the quiz. Everyone joins on their phone. Every screen moves together."));
  screen.append(intro);

  const columns = element("div", "home-grid");
  const hostCard = element("section", "card");
  hostCard.append(element("p", "eyebrow", "FOR THE HOST"));
  hostCard.append(element("h2", "", "Start a new game"));
  hostCard.append(element("p", "muted", "Upload a quiz JSON or CSV file, create a room, and share its code."));
  const hostForm = document.createElement("form");
  hostForm.className = "form";
  const fileLabel = element("label", "field");
  fileLabel.append(element("span", "", "Quiz file (.json or .csv)"));
  const file = document.createElement("input");
  file.name = "quiz";
  file.type = "file";
  file.accept = ".json,.csv,application/json,text/csv";
  file.required = true;
  fileLabel.append(file);
  hostForm.append(fileLabel);
  const modeLabel = element("label", "field");
  modeLabel.append(element("span", "", "Game mode"));
  const modeSelect = document.createElement("select");
  modeSelect.name = "mode";
  modeSelect.append(
    new Option("Automatic — reveal after everyone answers", "auto"),
    new Option("Host controlled — reveal and advance manually", "manual")
  );
  modeLabel.append(modeSelect);
  modeLabel.append(element("span", "field-hint", "Automatic mode shows the answer, then moves on after 3 seconds."));
  hostForm.append(modeLabel);
  const hostSubmit = element("button", "primary", "Create game");
  hostSubmit.type = "submit";
  hostForm.append(hostSubmit);
  hostForm.addEventListener("submit", async event => {
    event.preventDefault();
    hostSubmit.disabled = true;
    try {
      const selectedFile = file.files[0];
      if (!selectedFile) throw new Error("Choose a quiz JSON or CSV file first.");
      let result;
      if (selectedFile.name.toLowerCase().endsWith(".csv")) {
        const title = selectedFile.name.replace(/\.[^.]+$/, "").slice(0, 100);
        result = await request("/api/rooms", {
          quiz_csv: await selectedFile.text(),
          quiz_title: title || "Imported Quiz",
          mode: modeSelect.value
        });
      } else {
        const quiz = JSON.parse(await selectedFile.text());
        result = await request("/api/rooms", { quiz, mode: modeSelect.value });
      }
      activeRoom = result.room_code;
      hostToken = result.host_token;
      role = "host";
      sessionStorage.setItem(storageKeys.room, activeRoom);
      sessionStorage.setItem(storageKeys.role, role);
      sessionStorage.setItem(storageKeys.host, hostToken);
      renderLobby(result.import_warnings || []);
    } catch (error) {
      showError(error.message);
    } finally {
      hostSubmit.disabled = false;
    }
  });
  hostCard.append(hostForm);

  const joinCard = element("section", "card join-card");
  joinCard.append(element("p", "eyebrow", "FOR THE PLAYERS"));
  joinCard.append(element("h2", "", "Join the room"));
  joinCard.append(element("p", "muted", "Enter the code your host is showing."));
  const joinForm = document.createElement("form");
  joinForm.className = "form";
  const codeField = field("Room code", "roomCode", "ABCDE");
  const codeInput = codeField.querySelector("input");
  codeInput.maxLength = 5;
  codeInput.value = inviteCode;
  codeInput.autocomplete = "off";
  codeInput.classList.add("code-input");
  codeInput.addEventListener("input", () => {
    codeInput.value = codeInput.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
  });
  joinForm.append(codeField, field("Player name", "playerName", "What should we call you?"));
  const joinSubmit = element("button", "primary secondary", "Join game");
  joinSubmit.type = "submit";
  joinForm.append(joinSubmit);
  joinForm.addEventListener("submit", async event => {
    event.preventDefault();
    joinSubmit.disabled = true;
    try {
      const result = await request("/api/join", {
        room_code: codeInput.value.trim(),
        name: joinForm.elements.playerName.value.trim()
      });
      activeRoom = result.room_code;
      playerId = result.player_id;
      role = "player";
      sessionStorage.setItem(storageKeys.room, activeRoom);
      sessionStorage.setItem(storageKeys.role, role);
      sessionStorage.setItem(storageKeys.player, playerId);
      history.replaceState(null, "", "/?room=" + encodeURIComponent(activeRoom));
      startPolling();
    } catch (error) {
      showError(error.message);
    } finally {
      joinSubmit.disabled = false;
    }
  });
  joinCard.append(joinForm);
  columns.append(hostCard, joinCard);
  screen.append(columns);
}

function renderLobby(importWarnings = []) {
  screen.replaceChildren();
  const card = element("section", "card lobby-card");
  card.append(element("p", "eyebrow", "YOUR GAME IS READY"));
  card.append(element("h1", "", "Share the room code"));
  const code = element("div", "room-code", activeRoom);
  card.append(code);
  card.append(element("p", "muted", "Players on the same Wi-Fi can open this page and enter the code."));
  card.append(element("p", "invite-link", "Open the LAN address printed in the terminal, then enter this code."));
  if (importWarnings.length) {
    const report = element("details", "import-report");
    const rowLabel = importWarnings.length === 1 ? "row was" : "rows were";
    report.append(element("summary", "", `${importWarnings.length} CSV ${rowLabel} skipped. Expand to see why.`));
    const warningList = element("ul", "");
    importWarnings.forEach(warning => warningList.append(element("li", "", warning)));
    report.append(warningList);
    card.append(report);
  }
  card.append(button("Copy room code", "outline", async () => {
    try {
      await navigator.clipboard.writeText(activeRoom);
      showError("Room code copied.");
      screen.querySelector(".error").classList.add("success");
    } catch (_) {
      showError("Share the room code shown above.");
    }
  }));
  const players = element("div", "waiting-players");
  players.id = "players";
  card.append(players);
  const controls = element("div", "host-controls");
  controls.id = "controls";
  card.append(controls);
  screen.append(card);
  startPolling();
}

function renderGame(state) {
  screen.replaceChildren();
  const header = element("div", "game-header");
  const titleStack = element("div", "");
  titleStack.append(element("p", "eyebrow", role === "host" ? "HOST VIEW" : "PLAYER VIEW"));
  titleStack.append(element("h1", "", state.title));
  header.append(titleStack);
  const roomPill = element("span", "room-pill", "ROOM " + state.room_code);
  header.append(roomPill);
  screen.append(header);

  if (state.phase === "lobby") {
    const section = element("section", "card game-card");
    section.append(element("p", "eyebrow", "GETTING READY"));
    section.append(element("h2", "", "Waiting for the host to start"));
    section.append(element("p", "muted", state.mode === "auto"
      ? "Stay on this screen. Everyone's answer reveals the question, then the next question starts after 3 seconds."
      : "Stay on this screen. The first question will appear for everyone at once."));
    screen.append(section);
  } else if (state.phase === "completed") {
    const section = element("section", "card game-card");
    section.append(element("p", "eyebrow", "THAT'S A WRAP"));
    section.append(element("h2", "", "Final scores"));
    addScoreboard(section, state.players);
    section.append(button("Back to start", "outline", leaveGame));
    screen.append(section);
  } else if (state.question) {
    renderQuestion(state);
  }

  if (state.phase !== "completed") {
    const players = element("section", "card scoreboard");
    players.append(element("h2", "", "Players"));
    addScoreboard(players, state.players);
    screen.append(players);
  }
}

function renderQuestion(state) {
  const question = state.question;
  const section = element("section", "card game-card");
  const meta = element("div", "question-meta");
  meta.append(element("p", "eyebrow", "QUESTION " + question.number + " / " + question.total));
  const answeredCount = state.players.filter(player => player.has_answered).length;
  meta.append(element("span", "answer-count", answeredCount + " / " + state.player_count + " answered"));
  if (question.category) meta.insertBefore(element("span", "category-tag", question.category), meta.lastChild);
  section.append(meta);
  section.append(element("h2", "question-text", question.text));

  const answers = element("div", "answer-grid");
  question.answers.forEach((text, index) => {
    let style = "answer-option";
    if (state.phase === "revealed") {
      if (index === state.correct_answer_index) style += " correct";
      else if (index === state.your_answer) style += " incorrect";
    }
    if (state.phase === "question" && state.your_answer === index) style += " selected";
    const option = button(String.fromCharCode(65 + index) + ".  " + text, style, async () => {
      try {
        await request("/api/answer", {
          room_code: activeRoom,
          player_id: playerId,
          answer_index: index
        });
        await refreshState();
      } catch (error) {
        showError(error.message);
      }
    });
    option.disabled = role === "host" || state.phase !== "question" || state.your_answer !== undefined;
    answers.append(option);
  });
  section.append(answers);

  if (state.phase === "revealed") {
    section.append(element("p", "reveal-note", "Correct answer: " + state.correct_answer));
    if (state.mode === "auto") {
      const message = question.number === question.total
        ? "Final answer revealed. Final scores in 3 seconds…"
        : "Everyone has answered! Next question in 3 seconds…";
      section.append(element("p", "muted center", message));
    }
  } else if (role === "player") {
    section.append(element("p", "muted center", state.your_answer !== undefined ? "Answer locked in. Waiting for the reveal…" : "Choose your answer. You can only answer once."));
  }

  if (role === "host" && state.mode === "manual") {
    const controls = element("div", "host-controls");
    if (state.phase === "question") {
      controls.append(button("Reveal answer", "primary", () => hostAction("reveal")));
    } else {
      const text = question.number === question.total ? "Finish quiz" : "Next question";
      controls.append(button(text, "primary", () => hostAction("next")));
    }
    section.append(controls);
  }
  screen.append(section);
}

function addScoreboard(container, players) {
  if (!players.length) {
    container.append(element("p", "muted", "No players have joined yet."));
    return;
  }
  const list = element("ol", "player-list");
  players.slice().sort((a, b) => b.score - a.score || a.name.localeCompare(b.name)).forEach(player => {
    const row = element("li", "player-row");
    row.append(element("span", "player-name", player.name));
    row.append(element("span", "player-score", player.score + " pts"));
    if (player.has_answered) row.append(element("span", "answered-tag", "ANSWERED"));
    list.append(row);
  });
  container.append(list);
}

async function hostAction(action) {
  try {
    await request("/api/host/action", {
      room_code: activeRoom,
      host_token: hostToken,
      action
    });
    await refreshState();
  } catch (error) {
    showError(error.message);
  }
}

async function refreshState() {
  if (!activeRoom || loadingState) return;
  loadingState = true;
  try {
    const query = new URLSearchParams({ room: activeRoom });
    if (role === "player") query.set("player", playerId);
    const response = await fetch("/api/state?" + query.toString(), { cache: "no-store" });
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || "Could not load the game.");
    if (role === "host" && !hostToken) throw new Error("Host session was lost. Start a new room.");
    if (state.phase === "lobby" && role === "host" && !screen.querySelector(".lobby-card")) {
      renderLobby();
    } else if (state.phase === "lobby" && role === "host") {
      updateLobby(state);
    } else {
      renderGame(state);
    }
  } catch (error) {
    clearInterval(pollTimer);
    showError(error.message);
  } finally {
    loadingState = false;
  }
}

function updateLobby(state) {
  const players = screen.querySelector("#players");
  const controls = screen.querySelector("#controls");
  if (!players || !controls) return;
  players.replaceChildren(element("h2", "", "Players (" + state.player_count + ")"));
  addScoreboard(players, state.players);
  const start = button("Start quiz", "primary", () => hostAction("start"));
  start.disabled = state.mode === "auto" && state.player_count === 0;
  controls.replaceChildren(start);
  if (state.mode === "auto" && state.player_count === 0) {
    controls.append(element("p", "muted", "At least one player must join before starting auto mode."));
  }
}

function startPolling() {
  clearInterval(pollTimer);
  refreshState();
  pollTimer = setInterval(refreshState, 1000);
}

function leaveGame() {
  Object.values(storageKeys).forEach(key => sessionStorage.removeItem(key));
  activeRoom = "";
  playerId = "";
  hostToken = "";
  role = "";
  history.replaceState(null, "", "/");
  renderHome();
}

document.querySelector(".brand").addEventListener("click", event => {
  event.preventDefault();
  leaveGame();
});

function restoreSession() {
  activeRoom = sessionStorage.getItem(storageKeys.room) || "";
  role = sessionStorage.getItem(storageKeys.role) || "";
  playerId = sessionStorage.getItem(storageKeys.player) || "";
  hostToken = sessionStorage.getItem(storageKeys.host) || "";
  if (activeRoom && role && (role === "host" ? hostToken : playerId)) {
    startPolling();
  } else {
    renderHome();
  }
}

restoreSession();
