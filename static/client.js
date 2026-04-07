const app = document.getElementById("app");
const socket = io();
const DEFAULT_MAX_SHARED_PROMPT_LENGTH = 50;
const DEFAULT_MAX_GENERATIONS = 3;
const SESSION_NICKNAME_KEY = "prompt-game-nickname";

const state = {
  sessionToken: null,
  nickname: loadStoredNickname(),
  client: null,
  publicState: null,
  selectedImageId: null,
  promptDraft: "",
  showGeneratedImages: false,
  showMissingSharedPromptCard: false,
  isGeneratingImage: false,
  toast: "",
  isPromptInputFocused: false,
  pendingRenderAfterPromptBlur: false,
};

function loadStoredNickname() {
  try {
    return sessionStorage.getItem(SESSION_NICKNAME_KEY) || "";
  } catch (error) {
    return "";
  }
}

function saveStoredNickname(value) {
  try {
    sessionStorage.setItem(SESSION_NICKNAME_KEY, value);
  } catch (error) {
    // Ignore storage errors and keep the in-memory draft only.
  }
}

function getMaxSharedPromptLength() {
  const configured = Number(state.publicState?.config?.max_shared_prompt_length);
  if (!Number.isFinite(configured)) return DEFAULT_MAX_SHARED_PROMPT_LENGTH;
  return Math.max(1, Math.round(configured));
}

function getMaxGenerations() {
  const configured = Number(state.publicState?.config?.max_generations);
  if (!Number.isFinite(configured)) return DEFAULT_MAX_GENERATIONS;
  return Math.max(1, Math.round(configured));
}

function getPromptWarningLength() {
  return Math.max(1, Math.min(getMaxSharedPromptLength(), 40));
}

function showToast(message) {
  state.toast = message;
  render();
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    state.toast = "";
    render();
  }, 2800);
}

function escapeHtml(value = "") {
  return value.replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function formatGenerateButtonLabel() {
  const button = document.querySelector(".generate-button");
  if (!button || button.querySelector(".generate-button-main")) return;

  const text = (button.textContent || "").trim();
  const openParenIndex = text.indexOf("(");
  const closeParenIndex = text.lastIndexOf(")");
  if (openParenIndex < 0 || closeParenIndex <= openParenIndex) return;

  const mainText = text.slice(0, openParenIndex).trim();
  const subText = text.slice(openParenIndex, closeParenIndex + 1).trim();
  if (!mainText || !subText) return;

  button.textContent = "";

  const mainLine = document.createElement("span");
  mainLine.className = "generate-button-main";
  mainLine.textContent = mainText;

  const subLine = document.createElement("span");
  subLine.className = "generate-button-sub";
  subLine.textContent = subText;

  button.append(mainLine, subLine);
}

function placePromptCopyButtonsInline() {
  document.querySelectorAll(".prompt-bubble").forEach((bubble) => {
    const copyButton = bubble.querySelector(".prompt-copy");
    const promptText = bubble.querySelector(".prompt-text");
    if (!copyButton || !promptText || promptText.contains(copyButton)) return;

    copyButton.textContent = "⧉";
    promptText.append(copyButton);
  });
}

async function bootstrap() {
  const response = await fetch("/api/bootstrap");
  const payload = await response.json();
  state.publicState = payload.state;
  render();
}

function syncNickname() {
  const input = document.getElementById("nickname-input");
  if (input) {
    state.nickname = input.value.trim();
    saveStoredNickname(state.nickname);
  }
}

function syncPromptDraft() {
  const input = document.getElementById("prompt-input");
  state.promptDraft = input?.value || "";
  const counter = document.getElementById("prompt-length-counter");
  if (counter) {
    const maxSharedPromptLength = getMaxSharedPromptLength();
    counter.textContent = `${state.promptDraft.length} / ${maxSharedPromptLength}`;
    counter.classList.toggle("is-warning", state.promptDraft.length >= getPromptWarningLength());
  }
}

function setPromptInputFocus(isFocused) {
  state.isPromptInputFocused = !!isFocused;
}

function capturePromptInputForRender() {
  const input = document.getElementById("prompt-input");
  if (!input || document.activeElement !== input) return null;

  return {
    input,
    value: input.value,
    selectionStart: input.selectionStart,
    selectionEnd: input.selectionEnd,
  };
}

function restorePromptInputAfterRender(snapshot) {
  if (!snapshot?.input) return;

  const slot = document.getElementById("prompt-input-slot");
  const nextInput = document.getElementById("prompt-input");
  if (!slot || !nextInput) return;

  snapshot.input.value = snapshot.value;
  snapshot.input.disabled = nextInput.disabled;
  snapshot.input.placeholder = nextInput.placeholder;
  slot.replaceChild(snapshot.input, nextInput);
  snapshot.input.focus({ preventScroll: true });

  if (typeof snapshot.selectionStart === "number" && typeof snapshot.selectionEnd === "number") {
    snapshot.input.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd);
  }
}

async function copySharedPrompt(text) {
  const promptText = String(text || "").trim();
  if (!promptText) return showToast("복사할 프롬프트가 없습니다.");

  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(promptText);
    } else {
      throw new Error("clipboard unavailable");
    }
    showToast("팀 프롬프트를 복사했습니다.");
  } catch (error) {
    showToast("프롬프트 복사에 실패했습니다.");
  }
}

async function submitNickname() {
  syncNickname();
  if (!state.nickname) return showToast("닉네임을 입력해주세요.");

  const response = await fetch("/api/entry-status", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nickname: state.nickname }),
  });
  const payload = await response.json();
  if (!response.ok) return showToast(payload.error || "닉네임 확인에 실패했습니다.");

  if (payload.status === "existing_session") {
    return joinSession(payload.session_token, payload.client);
  }

  return createNewParticipation();
}

async function createNewParticipation() {
  const response = await fetch("/api/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nickname: state.nickname }),
  });
  const payload = await response.json();
  if (!response.ok) return showToast(payload.error || "참여 생성에 실패했습니다.");
  joinSession(payload.sessionToken, payload.client, payload.state);
}

function joinSession(sessionToken, client, nextState = null) {
  state.sessionToken = sessionToken;
  state.client = client;
  state.publicState = nextState || state.publicState;
  saveStoredNickname(state.nickname);
  socket.emit("session:join", { role: "client", sessionToken: state.sessionToken });
  render();
}

function teamMembers(teamId) {
  return (state.publicState?.lobby?.clients || []).filter((client) => client.team_id === teamId);
}

function lobbyTeamIds() {
  const assignments = state.publicState?.lobby?.team_assignments || {};
  const ids = Object.keys(assignments);
  return ids.length ? ids : ["A", "B"];
}

function currentRound() {
  const rounds = state.publicState?.game?.rounds || [];
  return rounds.length ? rounds[rounds.length - 1] : null;
}

function myTeamState() {
  const round = currentRound();
  const teamId = state.client?.team_id;
  return round && teamId ? round.teams[teamId] : null;
}

function buildTeamPrompt(teamState) {
  const draftPrompt = state.promptDraft.trim();
  const sharedPrompts = (teamState?.notes || []).map((note) => note.text.trim()).filter(Boolean);
  const promptParts = [...sharedPrompts];

  if (draftPrompt && !promptParts.includes(draftPrompt)) {
    promptParts.push(draftPrompt);
  }

  return promptParts.join("\n");
}

function countdown() {
  const round = currentRound();
  if (!round?.deadline) return "10:00";
  const remaining = Math.max(0, round.deadline - Math.floor(Date.now() / 1000));
  return `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`;
}

function sharePromptToTeam() {
  const teamState = myTeamState();
  if (state.publicState?.game?.status !== "running") return showToast("진행 중인 라운드에서만 입력할 수 있습니다.");
  if (myTeamState()?.submitted) return showToast("이미 제출한 라운드입니다.");

  if (teamState?.is_generating) return showToast("현재 우리 팀의 이미지 생성이 진행 중입니다.");

  syncPromptDraft();
  const input = document.getElementById("prompt-input");
  const prompt = state.promptDraft.trim();
  if (!prompt) return showToast("공유할 팀 프롬프트를 입력해주세요.");
  const maxSharedPromptLength = getMaxSharedPromptLength();
  if (prompt.length > maxSharedPromptLength) {
    return showToast(`팀 공유 프롬프트는 ${maxSharedPromptLength}자까지 입력할 수 있습니다.`);
  }

  state.showMissingSharedPromptCard = false;
  socket.emit("team:add_note", { sessionToken: state.sessionToken, text: prompt });
  state.promptDraft = "";
  if (input) input.value = "";
}

function generateImage() {
  const teamState = myTeamState();
  if (state.publicState?.game?.status !== "running") return showToast("진행 중인 라운드에서만 생성할 수 있습니다.");
  if (teamState?.submitted) return showToast("이미 제출한 라운드입니다.");

  syncPromptDraft();
  const sharedPrompts = (teamState?.notes || []).map((note) => note.text.trim()).filter(Boolean);
  if (!sharedPrompts.length) {
    state.showMissingSharedPromptCard = true;
    render();
    return;
  }

  const prompt = buildTeamPrompt(teamState);
  if (!prompt) return showToast("먼저 팀 프롬프트를 공유해주세요.");

  const remainingGenerations = Math.max(0, getMaxGenerations() - (teamState?.generations_used || 0));
  const shouldGenerate = window.confirm(`남은 생성 횟수는 ${remainingGenerations}회입니다.\n지금 그림 생성을 시작할까요?`);
  if (!shouldGenerate) return;

  state.showMissingSharedPromptCard = false;
  state.isGeneratingImage = true;
  render();
  socket.emit("team:generate_image", { sessionToken: state.sessionToken, prompt });
}

function submitImage() {
  if (state.publicState?.game?.status !== "running") return showToast("진행 중인 라운드에서만 제출할 수 있습니다.");
  if (myTeamState()?.submitted) return showToast("이미 제출한 라운드입니다.");
  if (!state.selectedImageId) return showToast("제출할 이미지를 선택해주세요.");
  if (!window.confirm("선택한 그림을 제출할까요? 제출 후에는 이 라운드에서 수정할 수 없습니다.")) return;
  socket.emit("team:submit_image", { sessionToken: state.sessionToken, imageId: state.selectedImageId });
}

function deleteSharedPrompt(noteId) {
  if (state.publicState?.game?.status !== "running") return showToast("진행 중인 라운드에서만 삭제할 수 있습니다.");
  if (myTeamState()?.submitted) return showToast("이미 제출한 라운드입니다.");
  if (!window.confirm("이 팀 프롬프트를 삭제할까요?")) return;
  socket.emit("team:delete_note", { sessionToken: state.sessionToken, noteId });
}

function renderEntry() {
  app.innerHTML = `
    <section class="hero">
      <div class="eyebrow">Client Entry</div>
      <h1>닉네임으로 참가할 수 있습니다</h1>
      <p>닉네임을 입력하면 기존 세션으로 복귀하거나 새로운 참가 세션을 만듭니다.</p>
      <div class="panel">
        <h3>닉네임 입력</h3>
        <input id="nickname-input" type="text" maxlength="30" placeholder="예: 노래하는 하루" value="${escapeHtml(state.nickname)}" oninput="syncNickname()">
        <div class="button-row" style="margin-top:12px">
          <button class="accent" onclick="submitNickname()">참가</button>
        </div>
      </div>
    </section>
  `;
}

function renderLobby() {
  const started = state.publicState?.lobby?.started;
  const teamIds = lobbyTeamIds();
  app.innerHTML = `
    <section class="hero">
      <div class="eyebrow">Lobby</div>
      <h1>${escapeHtml(state.client?.name || "")}님이 대기실에 입장했습니다</h1>
      <div class="inline-meta">
        <span class="badge">${state.client?.team_id ? `${state.client.team_id}팀 배정 완료` : "팀 배정 대기 중"}</span>
      </div>
    </section>
    <section class="panel">
      <h2>팀 현황</h2>
      <div class="team-grid">
        ${teamIds.map((teamId) => `
          <div class="team-column">
            <header><strong>${teamId}팀</strong><span class="badge">${teamMembers(teamId).length}명</span></header>
            <div class="members">
              ${teamMembers(teamId).map((member) => `<div class="member-chip">${escapeHtml(member.name)}</div>`).join("") || `<div class="empty">팀원이 없습니다</div>`}
            </div>
          </div>
        `).join("")}
      </div>
      <div style="margin-top:12px">
        ${started ? `<button class="accent" onclick="render()">게임 화면 보기</button>` : `<div class="empty">매니저가 팀 편성을 마치고 게임을 시작하면 자동으로 이동합니다.</div>`}
      </div>
    </section>
  `;
}

function renderSharedPromptsMarkup(teamState) {
  const sharedPrompts = teamState?.notes || [];
  return sharedPrompts.map((note) => `
    <div class="prompt-bubble">
      <button class="prompt-delete" onclick="deleteSharedPrompt('${note.id}')" aria-label="프롬프트 삭제">×</button>
      <div class="prompt-text">${escapeHtml(note.text)}</div>
      <div class="prompt-author">${escapeHtml(note.author)}</div>
    </div>
  `).join("") || `<div class="empty">아직 공유된 팀 프롬프트가 없습니다.</div>`;
}

function renderGenerateSectionMarkup(teamState, submitted) {
  const teamGenerating = !!teamState?.is_generating;
  const maxGenerations = getMaxGenerations();
  const remainingGenerations = Math.max(0, maxGenerations - (teamState?.generations_used || 0));
  const generateButtonLabel = teamGenerating || state.isGeneratingImage
    ? "AI가 그림을 생성하는 중..."
    : `프롬프트로 그림 그리기 (팀 공유 횟수 ${remainingGenerations}회 남음)`;

  return `
    <div class="button-row">
      <button class="generate-button" ${(teamState?.generations_used >= maxGenerations || teamGenerating || state.isGeneratingImage || submitted) ? "disabled" : ""} onclick="generateImage()">${generateButtonLabel}</button>
    </div>
    ${state.showMissingSharedPromptCard ? `
      <div class="prompt-bubble" style="margin-top:16px">
        <div class="prompt-text">공유된 팀 프롬프트가 없습니다.</div>
        <div class="prompt-author">팀 프롬프트를 먼저 공유한 뒤 그림 생성을 진행해주세요.</div>
      </div>
    ` : ``}
  `;
}

function renderGeneratedImagesSectionMarkup(teamState, submitted) {
  const generated = teamState?.generated_images || [];
  const teamGenerating = !!teamState?.is_generating;
  const showGeneratedImages = state.showGeneratedImages || generated.length > 0 || teamGenerating || state.isGeneratingImage;
  if (!showGeneratedImages) return "";

  return `
    <section class="panel">
      <h2>생성된 이미지</h2>
      <div class="gallery">
        ${teamGenerating || state.isGeneratingImage ? `
          <div class="image-card loading-card">
            <div class="loading-preview">
              <div class="loading-ring" aria-hidden="true"></div>
            </div>
          </div>
        ` : ``}
        ${generated.map((image, index) => `
          <div class="image-card ${state.selectedImageId === image.id || teamState?.selected_image_id === image.id ? "selected" : ""} ${index === generated.length - 1 ? "latest" : ""}" onclick="selectImage('${image.id}')">
            <div class="image-order">${index + 1}</div>
            <img src="${image.image_url}" alt="생성 이미지">
          </div>
        `).join("") || `<div class="empty">아직 생성된 이미지가 없습니다.</div>`}
      </div>
      <div class="button-row" style="margin-top:16px">
        <button class="success" ${submitted ? "disabled" : ""} onclick="submitImage()">그림 제출</button>
      </div>
    </section>
  `;
}

function renderSubmissionOverlayMarkup(teamId, teamState, submitted) {
  const generated = teamState?.generated_images || [];
  const submittedImage = generated.find((image) => image.id === teamState?.selected_image_id);
  if (!(submitted && submittedImage)) return "";

  return `
    <section class="submission-overlay panel">
      <div class="submission-card">
        <img src="${submittedImage.image_url}" alt="제출한 그림">
        <div class="submission-team">${teamId}팀</div>
      </div>
    </section>
  `;
}

function updateGameDynamicSections(options = {}) {
  const preserveGeneratingVisuals = !!options.preserveGeneratingVisuals;
  const teamState = myTeamState();
  const teamId = state.client?.team_id;
  if (!teamState || !teamId) return;

  const submitted = !!teamState?.submitted || state.publicState?.game?.status === "review";
  const playStage = document.getElementById("play-stage");
  const sharedPromptsSection = document.getElementById("shared-prompts-section");
  const generateSection = document.getElementById("generate-section");
  const generatedImagesSection = document.getElementById("generated-images-section");
  const submissionOverlaySlot = document.getElementById("submission-overlay-slot");
  const promptInput = document.getElementById("prompt-input");
  const shareButton = document.getElementById("share-prompt-button");

  if (playStage) playStage.className = `play-stage ${submitted ? "is-submitted" : ""}`;
  if (sharedPromptsSection) sharedPromptsSection.innerHTML = renderSharedPromptsMarkup(teamState);
  if (generateSection && !preserveGeneratingVisuals) {
    generateSection.innerHTML = renderGenerateSectionMarkup(teamState, submitted);
    formatGenerateButtonLabel();
  }
  if (generatedImagesSection && !preserveGeneratingVisuals) {
    generatedImagesSection.innerHTML = renderGeneratedImagesSectionMarkup(teamState, submitted);
  }
  if (submissionOverlaySlot) submissionOverlaySlot.innerHTML = renderSubmissionOverlayMarkup(teamId, teamState, submitted);
  if (promptInput) promptInput.disabled = submitted;
  if (shareButton) shareButton.disabled = submitted;
}

function renderGame() {
  const teamState = myTeamState();
  const teamId = state.client?.team_id;
  if (!teamId || !teamState) return renderLobby();

  const submitted = !!teamState?.submitted || state.publicState?.game?.status === "review";
  const maxSharedPromptLength = getMaxSharedPromptLength();
  const promptWarningLength = getPromptWarningLength();

  app.innerHTML = `
    <section class="hero" data-screen="game">
      <div class="eyebrow">Round ${state.publicState.game.current_round}</div>
      <div class="timer-hero">
        <div class="timer-value">남은 시간 <span id="countdown-text">${countdown()}</span></div>
      </div>
    </section>
    <div class="play-stage ${submitted ? "is-submitted" : ""}" id="play-stage">
      <section class="panel">
        <h2>내 프롬프트 입력</h2>
        <div class="card-list">
          <div id="prompt-input-slot" class="prompt-input-slot">
            <textarea id="prompt-input" rows="5" maxlength="${maxSharedPromptLength}" placeholder="팀에 공유하거나 이미지 생성에 사용할 프롬프트를 입력하세요. (최대 ${maxSharedPromptLength}자)" oninput="syncPromptDraft()" onfocus="setPromptInputFocus(true)" onblur="setPromptInputFocus(false)" ${submitted ? "disabled" : ""}>${escapeHtml(state.promptDraft)}</textarea>
            <div id="prompt-length-counter" class="prompt-length-counter ${state.promptDraft.length >= promptWarningLength ? "is-warning" : ""}">${state.promptDraft.length} / ${maxSharedPromptLength}</div>
          </div>
          <div class="button-row">
            <button class="secondary" id="share-prompt-button" ${submitted ? "disabled" : ""} onclick="sharePromptToTeam()">팀 프롬프트 공유</button>
          </div>
        </div>
      </section>
      <section class="panel">
        <h2>공유된 팀 프롬프트</h2>
        <div class="prompt-bubbles" id="shared-prompts-section">${renderSharedPromptsMarkup(teamState)}</div>
      </section>
      <section class="panel" id="generate-section">${renderGenerateSectionMarkup(teamState, submitted)}</section>
      <div id="generated-images-section">${renderGeneratedImagesSectionMarkup(teamState, submitted)}</div>
    </div>
    <div id="submission-overlay-slot">${renderSubmissionOverlayMarkup(teamId, teamState, submitted)}</div>
  `;
}

function renderFinished() {
  const finalRanking = state.publicState?.game?.final_ranking || [];
  const myTeamId = state.client?.team_id;
  const myTeam = finalRanking.find((item) => item.team_id === myTeamId);
  const podium = finalRanking.slice(0, 3);

  app.innerHTML = `
    <section class="hero ceremony-hero">
      <div class="eyebrow">Award Ceremony</div>
      <h1>최종 시상식</h1>
      <div class="inline-meta">
        <span class="badge">${podium[0] ? `${podium[0].team_id}팀 우승` : "결과 집계 완료"}</span>
        ${myTeam ? `<span class="badge">내 팀 ${myTeam.team_id}팀 · 총점 ${myTeam.score}</span>` : ``}
      </div>
    </section>
    <section class="panel ceremony-panel">
      <div class="ceremony-podium">
        ${podium[1] ? `<div class="podium-card place-2"><div class="podium-rank">2위</div><div class="podium-team">${podium[1].team_id}팀</div><div class="podium-score">${podium[1].score}점</div></div>` : ``}
        ${podium[0] ? `<div class="podium-card place-1"><div class="podium-rank">1위</div><div class="podium-team">${podium[0].team_id}팀</div><div class="podium-score">${podium[0].score}점</div></div>` : ``}
        ${podium[2] ? `<div class="podium-card place-3"><div class="podium-rank">3위</div><div class="podium-team">${podium[2].team_id}팀</div><div class="podium-score">${podium[2].score}점</div></div>` : ``}
      </div>
    </section>
    <section class="panel">
      <h2>최종 순위</h2>
      <div class="card-list">
        ${finalRanking.map((item, index) => `
          <div class="score-card">
            <span>${index + 1}위 · ${item.team_id}팀</span>
            <strong>${item.score}점</strong>
          </div>
        `).join("") || `<div class="empty">최종 점수가 없습니다.</div>`}
      </div>
    </section>
  `;
}

function render() {
  const promptInputSnapshot = capturePromptInputForRender();
  if (!state.client) renderEntry();
  else if (state.publicState?.game?.status === "finished") renderFinished();
  else if (["running", "review"].includes(state.publicState?.game?.status)) renderGame();
  else renderLobby();

  restorePromptInputAfterRender(promptInputSnapshot);
  formatGenerateButtonLabel();
  document.querySelector(".toast")?.remove();
  if (state.toast) {
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = state.toast;
    document.body.appendChild(toast);
  }
}

async function copySharedPrompt(text) {
  const promptText = String(text || "").trim();
  if (!promptText) return showToast("복사할 프롬프트가 없습니다.");

  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(promptText);
      showToast("팀 프롬프트를 복사했습니다.");
      return;
    }
    throw new Error("clipboard unavailable");
  } catch (error) {
    const fallbackInput = document.createElement("textarea");
    fallbackInput.value = promptText;
    fallbackInput.setAttribute("readonly", "");
    fallbackInput.style.position = "fixed";
    fallbackInput.style.top = "-9999px";
    fallbackInput.style.opacity = "0";
    document.body.appendChild(fallbackInput);
    fallbackInput.focus();
    fallbackInput.select();

    try {
      const copied = document.execCommand("copy");
      if (!copied) throw new Error("execCommand copy failed");
      showToast("팀 프롬프트를 복사했습니다.");
    } catch (fallbackError) {
      showToast("프롬프트 복사에 실패했습니다.");
    } finally {
      fallbackInput.remove();
    }
  }
}

window.submitNickname = submitNickname;
window.syncPromptDraft = syncPromptDraft;
window.setPromptInputFocus = setPromptInputFocus;
window.sharePromptToTeam = sharePromptToTeam;
window.generateImage = generateImage;
window.submitImage = submitImage;
window.deleteSharedPrompt = deleteSharedPrompt;
window.selectImage = (imageId) => {
  if (myTeamState()?.submitted) return;
  state.selectedImageId = imageId;
  socket.emit("team:select_image", { sessionToken: state.sessionToken, imageId });
  render();
};

socket.on("state:update", (payload) => {
  const previousStatus = state.publicState?.game?.status;
  const previousRound = state.publicState?.game?.current_round;
  const previousTeamId = state.client?.team_id;
  const previousRoundState = (state.publicState?.game?.rounds || []).slice(-1)[0];
  const previousTeamState = previousRoundState && previousTeamId ? previousRoundState.teams?.[previousTeamId] : null;
  const wasGenerating = !!previousTeamState?.is_generating || !!state.isGeneratingImage;
  state.publicState = payload;
  if (!state.client) return;
  if (state.client?.nickname) {
    const freshClient = (payload.lobby?.clients || []).find((item) => item.nickname === state.client.nickname);
    if (freshClient) state.client = { ...state.client, ...freshClient };
  }
  const teamState = myTeamState();
  if (teamState?.selected_image_id) state.selectedImageId = teamState.selected_image_id;
  if ((teamState?.generated_images || []).length > 0) state.showGeneratedImages = true;
  if ((teamState?.notes || []).length > 0) state.showMissingSharedPromptCard = false;
  state.isGeneratingImage = !!teamState?.is_generating;
  const nextStatus = payload?.game?.status;
  const nextRound = payload?.game?.current_round;
  const gameScreenActive = !!document.querySelector('[data-screen="game"]');
  const canPatchGameScreen = gameScreenActive
    && ["running", "review", "finished"].includes(previousStatus)
    && ["running", "review", "finished"].includes(nextStatus)
    && previousRound === nextRound;
  const stillGenerating = !!teamState?.is_generating || !!state.isGeneratingImage;

  if (canPatchGameScreen) {
    updateGameDynamicSections({ preserveGeneratingVisuals: wasGenerating && stillGenerating });
    return;
  }

  render();
});

socket.on("session:joined", (payload) => {
  if (payload.role === "manager") return;
  state.sessionToken = payload.sessionToken;
  state.client = payload.client;
  state.publicState = payload.state;
  render();
});

socket.on("session:error", (payload) => {
  state.isGeneratingImage = false;
  showToast(payload.message);
});

socket.on("team:image_generated", () => {
  state.showGeneratedImages = true;
  state.showMissingSharedPromptCard = false;
  state.isGeneratingImage = false;
  render();
});

bootstrap();
window.setInterval(() => {
  const countdownNode = document.getElementById("countdown-text");
  if (countdownNode && state.publicState?.game?.status === "running") {
    countdownNode.textContent = countdown();
  }
}, 1000);
