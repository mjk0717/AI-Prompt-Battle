const app = document.getElementById("manager-app");
const socket = io();

const DEFAULT_ROUND_DURATION = 600;
const DEFAULT_ROUND_DURATION_MINUTES = Math.round(DEFAULT_ROUND_DURATION / 60);
const DEFAULT_ROUND_COUNT = 3;

const state = {
  publicState: null,
  roundDurations: defaultRoundDurations(),
  joinUrl: defaultJoinUrl(),
  toast: "",
  scoreDrafts: {},
  scoreDraftTouched: {},
};

function defaultRoundDurations() {
  return Array.from({ length: DEFAULT_ROUND_COUNT }, () => DEFAULT_ROUND_DURATION_MINUTES);
}

function getRoundCount(publicState = state.publicState) {
  const configured = Number(publicState?.config?.max_rounds);
  if (!Number.isFinite(configured)) return DEFAULT_ROUND_COUNT;
  return Math.max(1, Math.round(configured));
}

function defaultJoinUrl() {
  return window.location.origin ? `${window.location.origin}/` : "";
}

function saveRoundDurations(values) {
  state.roundDurations = values;
}

function saveJoinAccessSettings(url) {
  state.joinUrl = url;
}

function normalizeRoundDuration(value, fallback = DEFAULT_ROUND_DURATION) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(10, Math.min(3600, Math.round(parsed)));
}

function normalizeRoundDurationMinutes(value, fallback = DEFAULT_ROUND_DURATION_MINUTES) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  if (parsed > 60) return Math.max(1, Math.min(60, Math.round(parsed / 60)));
  return Math.max(1, Math.min(60, Math.round(parsed)));
}

function normalizeJoinUrl(value, fallback = "") {
  const trimmed = String(value || "").trim();
  if (!trimmed) return fallback;
  if (!/^https?:\/\//i.test(trimmed)) {
    return fallback;
  }
  return trimmed;
}

function syncRoundDurationsFromInput() {
  const values = Array.from({ length: getRoundCount() }, (_, index) => {
    const input = document.getElementById(`round-duration-${index + 1}`);
    return normalizeRoundDurationMinutes(input?.value, state.roundDurations[index] ?? DEFAULT_ROUND_DURATION_MINUTES);
  });
  saveRoundDurations(values);
  return values;
}

function syncManagerSettingsFromState(publicState) {
  const roundCount = getRoundCount(publicState);
  const fallbackRoundDurations = Array.from({ length: roundCount }, () => DEFAULT_ROUND_DURATION_MINUTES);
  const settings = publicState?.lobby?.manager_settings || {};
  const serverRoundDurations = Array.isArray(settings.round_durations) ? settings.round_durations : fallbackRoundDurations.map((minutes) => minutes * 60);
  saveRoundDurations(
    serverRoundDurations
      .slice(0, roundCount)
      .map((value) => normalizeRoundDurationMinutes(value, DEFAULT_ROUND_DURATION_MINUTES))
      .concat(fallbackRoundDurations)
      .slice(0, roundCount),
  );
  saveJoinAccessSettings(normalizeJoinUrl(settings.join_url || defaultJoinUrl(), defaultJoinUrl()));
}

async function persistManagerSettings(updates) {
  const response = await fetch("/api/manager/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  if (!response.ok) {
    let errorMessage = "설정 저장에 실패했습니다.";
    try {
      const payload = await response.json();
      errorMessage = payload.error || errorMessage;
    } catch (error) {
      // Ignore JSON parse errors for empty or non-JSON error responses.
    }
    showToast(errorMessage);
    return false;
  }
  return true;
}

async function updateRoundDurationInput(index, value) {
  const roundCount = getRoundCount();
  const nextValues = state.roundDurations.slice(0, roundCount);
  while (nextValues.length < roundCount) {
    nextValues.push(DEFAULT_ROUND_DURATION_MINUTES);
  }
  nextValues[index] = normalizeRoundDurationMinutes(value, nextValues[index] ?? DEFAULT_ROUND_DURATION_MINUTES);
  saveRoundDurations(nextValues);
  await persistManagerSettings({ roundDurations: nextValues.map((minutes) => minutes * 60) });
}

function syncJoinAccessFromInput() {
  const urlInput = document.getElementById("join-url-input");
  const url = normalizeJoinUrl(urlInput?.value, state.joinUrl || defaultJoinUrl());
  if (urlInput) urlInput.value = url;
  saveJoinAccessSettings(url);
  return { url };
}

function buildJoinUrl() {
  return normalizeJoinUrl(state.joinUrl, "");
}

function updateJoinQrCode() {
  const container = document.getElementById("join-qr-code");
  const link = document.getElementById("join-url-link");
  const hint = document.getElementById("join-qr-hint");
  if (!container || !link || !hint) return;

  const url = buildJoinUrl();
  link.textContent = url || "IP 주소를 입력하면 접속 주소가 표시됩니다.";
  link.href = url || "#";
  link.target = url ? "_blank" : "";
  link.rel = url ? "noreferrer noopener" : "";
  hint.textContent = url
    ? "휴대폰 카메라로 QR을 스캔하면 플레이어 화면으로 바로 접속합니다."
    : "http:// 또는 https://를 포함한 전체 주소를 입력해 접속용 QR 코드를 생성하세요.";

  container.innerHTML = "";
  container.classList.toggle("is-empty", !url);
  if (!url) {
    container.innerHTML = `<div class="empty">QR 코드가 이곳에 표시됩니다</div>`;
    return;
  }
  if (typeof QRCode !== "function") {
    container.innerHTML = `<div class="empty">QR 코드 라이브러리를 불러오지 못했습니다</div>`;
    return;
  }

  new QRCode(container, {
    text: url,
    width: 208,
    height: 208,
    colorDark: "#693b97",
    colorLight: "#ffffff",
    correctLevel: QRCode.CorrectLevel.M,
  });
  container.insertAdjacentHTML(
    "beforeend",
    `
      <div class="qr-logo-badge" aria-hidden="true">
        <img src="/static/image/bgf.jpg" alt="">
      </div>
    `,
  );
}

async function onJoinAccessInput() {
  syncJoinAccessFromInput();
  updateJoinQrCode();
  await persistManagerSettings({
    joinUrl: state.joinUrl,
  });
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

function getReviewRoundKey() {
  const review = state.publicState?.game?.review;
  return review ? String(review.round_number) : "";
}

function syncScoreDraftsFromReview() {
  const review = state.publicState?.game?.review;
  if (!review) {
    state.scoreDrafts = {};
    return;
  }

  const roundKey = getReviewRoundKey();
  if (state.scoreDrafts.__roundKey !== roundKey) {
    state.scoreDrafts = { __roundKey: roundKey };
    state.scoreDraftTouched = {};
  }

  (review.submitted_images || []).forEach((item) => {
    const teamId = item.team_id;
    if (!(teamId in state.scoreDrafts) || !state.scoreDraftTouched[teamId]) {
      state.scoreDrafts[teamId] = Number(review.manager_scores?.[teamId] ?? 0);
    }
  });
}

function setScoreDraft(teamId, value) {
  const parsed = Number(value);
  state.scoreDrafts[teamId] = Number.isFinite(parsed) ? parsed : 0;
  state.scoreDraftTouched[teamId] = true;
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

async function bootstrap() {
  const response = await fetch("/api/bootstrap?role=manager");
  const payload = await response.json();
  state.publicState = payload.state;
  syncManagerSettingsFromState(payload.state);
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

function activeTeams() {
  return state.publicState?.game?.active_team_ids?.length
    ? state.publicState.game.active_team_ids
    : lobbyTeamIds();
}

function onDragStart(event, nickname) {
  event.dataTransfer.setData("text/plain", nickname);
}

function onDrop(event, teamId) {
  event.preventDefault();
  const nickname = event.dataTransfer.getData("text/plain");
  socket.emit("manager:assign_team", { nickname, teamId });
}

function addTestBot(teamId) {
  socket.emit("manager:add_test_bot", { teamId });
}

function onDragOver(event) {
  event.preventDefault();
  event.currentTarget.classList.add("dragover");
}

function onDragLeave(event) {
  event.currentTarget.classList.remove("dragover");
}

async function startGame() {
  const roundDurations = syncRoundDurationsFromInput().map((minutes) => minutes * 60);

  const response = await fetch("/api/manager/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      referencePrompt: "라운드 기준 그림",
      referenceImageUrl: "",
      roundDurations,
    }),
  });
  const payload = await response.json();
  if (!response.ok) return showToast(payload.error || "게임 시작에 실패했습니다.");
  state.publicState = payload.state;
  render();
}

async function resetGame() {
  if (!window.confirm("진행 중인 게임을 바로 초기화할까요? 현재 상태와 점수는 모두 사라집니다.")) return;
  const response = await fetch("/api/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  const payload = await response.json();
  if (!response.ok) return showToast(payload.error || "리셋에 실패했습니다.");
}

async function advanceRound() {
  const scores = Object.fromEntries(
    activeTeams().map((teamId) => {
      const inputValue = document.getElementById(`score-${teamId}`)?.value;
      const resolvedValue = inputValue ?? state.scoreDrafts[teamId] ?? 0;
      const numericValue = Number(resolvedValue || 0);
      setScoreDraft(teamId, numericValue);
      return [teamId, numericValue];
    }),
  );
  const response = await fetch("/api/manager/advance-round", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scores }),
  });
  const payload = await response.json();
  if (!response.ok) return showToast(payload.error || "다음 라운드 진행에 실패했습니다.");
  state.publicState = payload.state;
  render();
}

async function retryJudgeReview() {
  const review = state.publicState?.game?.review;
  if (!review || review.is_judging) return;

  const response = await fetch("/api/manager/retry-review", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  const payload = await response.json();
  if (!response.ok) return showToast(payload.error || "AI 판정 새로고침에 실패했습니다.");
  state.publicState = payload.state;
  render();
}

async function finishRoundEarly() {
  if (!window.confirm("현재 라운드를 지금 바로 종료하고 AI 평가로 넘어갈까요?")) return;

  const response = await fetch("/api/manager/finish-round", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  const payload = await response.json();
  if (!response.ok) return showToast(payload.error || "라운드 강제 종료에 실패했습니다.");
  state.publicState = payload.state;
  render();
}

function currentRound() {
  const rounds = state.publicState?.game?.rounds || [];
  return rounds.length ? rounds[rounds.length - 1] : null;
}

function countdown() {
  const round = currentRound();
  if (!round?.deadline) return "10:00";
  const remaining = Math.max(0, round.deadline - Math.floor(Date.now() / 1000));
  return `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`;
}

function remainingPercent() {
  const round = currentRound();
  if (!round?.deadline) return 100;
  const total = round?.duration_seconds || DEFAULT_ROUND_DURATION;
  const remaining = Math.max(0, round.deadline - Math.floor(Date.now() / 1000));
  return Math.max(0, Math.min(100, Math.round((remaining / total) * 100)));
}

function renderResetSection() {
  return `
    <section class="panel reset-panel">
      <div class="button-row">
        <button class="danger reset-button" onclick="resetGame()">게임 리셋</button>
      </div>
    </section>`;
}

function renderMemberCard(client) {
  return `
    <div class="person-card" draggable="true" ondragstart="dragStart(event, '${client.nickname}')">
      <div>
        <strong>${escapeHtml(client.name)}${client.is_bot ? " [BOT]" : ""}</strong>
        <div class="${client.connected ? "status-online" : "status-offline"}">${client.connected ? "접속 중" : "오프라인"}</div>
      </div>
    </div>`;
}

function renderTeamGuideSlot() {
  return `
    <section class="team-column panel team-guide-column">
      <header><strong>새 팀</strong><span class="badge">자동 생성</span></header>
      <div class="drop-zone drop-zone-guide" ondrop="dropToTeam(event, '__new__')" ondragover="dragOver(event)" ondragleave="dragLeave(event)">
        <div class="team-guide-plus" aria-hidden="true">+</div>
        <div class="empty">플레이어를 놓으면 다음 팀이 만들어집니다</div>
      </div>
    </section>`;
}

function renderManagerLiveTeamsMarkup(round, activeTeamIds) {
  return activeTeamIds.map((teamId) => {
    const teamState = round?.teams?.[teamId];
    const selected = teamState?.submitted
      ? teamState?.generated_images?.find((image) => image.id === teamState.selected_image_id)
      : null;
    return `
      <div class="panel">
        <h3>${teamId}팀</h3>
        <div class="inline-meta">
          <span class="badge">생성 ${teamState?.generations_used || 0}/3</span>
          <span class="badge">${teamState?.submitted ? "제출 완료" : "제출 전"}</span>
        </div>
        <div class="members" style="margin-top:12px">${teamMembers(teamId).map((client) => `<div class="member-chip">${escapeHtml(client.name)}</div>`).join("") || `<div class="empty">팀원이 없습니다</div>`}</div>
        <div class="gallery" style="margin-top:12px">${selected ? `<div class="image-card selected"><img src="${selected.image_url}" alt="제출 이미지"></div>` : `<div class="empty">${teamState?.submitted ? "선택된 이미지가 없습니다" : "아직 제출하지 않았습니다"}</div>`}</div>
      </div>`;
  }).join("");
}

function renderReviewSubmissionsMarkup(review) {
  const submittedMarkup = review.submitted_images.map((item) => `
    <section class="panel">
      <h3>${item.team_id}팀</h3>
      <div class="gallery">
        <div class="image-card">
          <img src="${item.image_url}" alt="${item.team_id}팀 제출 이미지">
        </div>
      </div>
      <div class="members" style="margin-top:12px">${teamMembers(item.team_id).map((client) => `<div class="member-chip">${escapeHtml(client.name)}</div>`).join("") || `<div class="empty">팀원이 없습니다</div>`}</div>
    </section>
  `).join("");

  const failedMarkup = (review.failed_teams || []).map((item) => `
    <section class="panel submit-failed-panel">
      <h3>${item.team_id}팀</h3>
      <div class="submit-failed-text">이미지 제출 실패</div>
      <div class="members" style="margin-top:12px">${teamMembers(item.team_id).map((client) => `<div class="member-chip">${escapeHtml(client.name)}</div>`).join("") || `<div class="empty">팀원이 없습니다</div>`}</div>
    </section>
  `).join("");

  return `${submittedMarkup}${failedMarkup}`;
}

function renderReviewJudgeMarkup(review) {
  const isJudgeError = review.judge_result?.status === "error";
  const isJudgeSkipped = review.judge_result?.status === "skipped";
  const sortedReasons = [...(review.judge_result?.scores || [])]
    .sort((left, right) => String(left.team_id || "").localeCompare(String(right.team_id || "")));
  const similarityByTeamId = Object.fromEntries(
    (review.judge_result?.similarities || []).map((item) => [String(item.team_id || ""), item]),
  );
  return `
    <h2>AI 평가</h2>
    <div class="prompt-bubble">
      <div class="prompt-text">${escapeHtml(review.judge_result?.assistant_message || "평가 메시지가 없습니다.")}</div>
    </div>
    ${review.judge_result?.status === "streaming" ? `<div class="inline-meta" style="margin-top:12px"><span class="badge">AI 평가 중</span></div>` : ``}
    ${isJudgeError ? `<div class="inline-meta" style="margin-top:12px"><span class="badge">AI 판정 실패</span></div>` : ``}
    ${isJudgeSkipped ? `<div class="inline-meta" style="margin-top:12px"><span class="badge">AI 평가 생략</span></div>` : ``}
    ${sortedReasons.length ? `
      <div class="card-list" style="margin-top:16px">
        ${sortedReasons.map((item) => `
          <div class="prompt-bubble">
            <div class="prompt-author">${item.team_id}팀 판정</div>
            <div class="prompt-text">${escapeHtml(item.reason || "판정 사유가 없습니다.")}</div>
            ${similarityByTeamId[String(item.team_id || "")] ? `
              <div class="score-card" style="margin-top:12px">
                <span>${item.team_id}팀 유사도</span>
                <strong>${similarityByTeamId[String(item.team_id || "")].similarity}%</strong>
              </div>
            ` : ``}
          </div>
        `).join("")}
      </div>
    ` : ``}
  `;
}

function renderReviewScoreInputsMarkup(review, activeTeamIds) {
  const isJudging = !!review.is_judging || review.judge_result?.status === "streaming";
  const isJudgeSkipped = review.judge_result?.status === "skipped";
  return `
    <h2>매니저 점수 입력</h2>
    <div class="team-grid">
      ${activeTeamIds.map((teamId) => `
        <div class="card-list">
          <label for="score-${teamId}">${teamId}팀 점수</label>
          <input
            id="score-${teamId}"
            type="number"
            min="0"
            step="1"
            value="${state.scoreDrafts[teamId] ?? review.manager_scores?.[teamId] ?? 0}"
            oninput="updateScoreDraft('${teamId}', this.value)"
          >
        </div>
      `).join("")}
    </div>
    <div class="button-row" style="margin-top:16px">
      ${isJudgeSkipped ? `` : `<button class="secondary" ${isJudging ? "disabled" : ""} onclick="retryJudgeReview()">${isJudging ? "AI 판정 진행 중" : "AI 판정 새로고침"}</button>`}
      <button class="accent" onclick="advanceRound()">${review.round_number >= getRoundCount() ? "최종 결과 보기" : "다음 라운드 진행"}</button>
    </div>
  `;
}

function updateManagerDynamicSections() {
  const status = state.publicState?.game?.status;
  if (status === "running") {
    const round = currentRound();
    const activeTeamIds = round?.active_team_ids || activeTeams();
    const liveTeams = document.getElementById("manager-live-teams");
    if (liveTeams) liveTeams.innerHTML = renderManagerLiveTeamsMarkup(round, activeTeamIds);
    return;
  }

  if (status === "review") {
    const review = state.publicState?.game?.review;
    if (!review) return;
    syncScoreDraftsFromReview();
    const activeTeamIds = review.submitted_images.map((item) => item.team_id);
    const reviewTeams = document.getElementById("manager-review-teams");
    const reviewJudge = document.getElementById("manager-review-judge");
    const reviewScores = document.getElementById("manager-review-scores");
    if (reviewTeams) reviewTeams.innerHTML = renderReviewSubmissionsMarkup(review);
    if (reviewJudge) reviewJudge.innerHTML = renderReviewJudgeMarkup(review);
    if (reviewScores) reviewScores.innerHTML = renderReviewScoreInputsMarkup(review, activeTeamIds);
  }
}

function renderLobby() {
  const teamIds = lobbyTeamIds();
  const roundCount = getRoundCount();
  const durationInputs = Array.from({ length: roundCount }, (_, index) => `
    <input id="round-duration-${index + 1}" type="number" min="1" max="60" step="1" value="${state.roundDurations[index] ?? DEFAULT_ROUND_DURATION_MINUTES}" oninput="updateRoundDurationInput(${index}, this.value)">
  `).join("");
  const roundLabels = Array.from({ length: roundCount }, (_, index) => index + 1).join(", ");
  app.innerHTML = `
    <section class="hero">
      <div class="eyebrow">Manager</div>
      <h1>팀을 구성하고 게임을 시작하세요</h1>
    </section>
    <section class="manager-grid">
      ${teamIds.map((teamId) => `
        <section class="team-column panel">
          <header><strong>${teamId}팀</strong><span class="badge">${teamMembers(teamId).length}명</span></header>
          <div class="drop-zone" ondrop="dropToTeam(event, '${teamId}')" ondragover="dragOver(event)" ondragleave="dragLeave(event)">
            <div class="members">${teamMembers(teamId).map((client) => renderMemberCard(client)).join("") || `<div class="empty">참가자를 이곳으로 드래그하세요</div>`}</div>
          </div>
        </section>`).join("")}
      ${renderTeamGuideSlot()}
    </section>
    <section class="summary-grid">
      <section class="panel">
        <h2>라운드 제한 시간</h2>
        <div class="card-list">
          <label for="round-duration-1">라운드별 제한 시간</label>
          ${durationInputs}
        </div>
        <div class="inline-meta" style="margin-top:12px">
          <span class="badge">${roundLabels}라운드를 각각 다르게 설정할 수 있습니다</span>
          <span class="badge">게임 시작 전까지만 수정할 수 있습니다</span>
          <span class="badge">단위: 분</span>
        </div>
      </section>
      <section class="panel qr-panel">
        <h2>접속용 QR 코드</h2>
        <div class="input-row">
          <div class="card-list">
            <label for="join-url-input">접속 전체 주소</label>
            <input id="join-url-input" type="text" value="${escapeHtml(state.joinUrl)}" placeholder="예: https://event.example.com/ 또는 http://192.168.0.12:24120/" oninput="updateJoinAccess()">
          </div>
        </div>
        <a id="join-url-link" class="qr-link" href="#" target="_blank" rel="noreferrer noopener"></a>
        <div id="join-qr-code" class="qr-code-box"></div>
        <div id="join-qr-hint" class="empty"></div>
      </section>
    </section>
    <section class="panel">
      <div class="button-row">
        <button class="accent" onclick="startGame()">시작</button>
        <button class="secondary" onclick="resetGame()">리셋</button>
      </div>
    </section>`;
}

function renderGame() {
  const round = currentRound();
  const managerReference = round?.reference || state.publicState.game.manager_reference;
  const activeTeamIds = round?.active_team_ids || activeTeams();

  app.innerHTML = `
    <section class="hero" data-screen="manager-game">
      <div class="eyebrow">Live Round ${state.publicState.game.current_round}</div>
      <h1>남은 시간</h1>
      <div class="timer-hero">
        <div class="timer-value" id="countdown-text">${countdown()}</div>
        <div class="inline-meta">
          <span class="badge">제한 시간 ${Math.max(1, Math.round((round?.duration_seconds || DEFAULT_ROUND_DURATION) / 60))}분</span>
        </div>
        <div class="timer-bar" aria-hidden="true">
          <div class="timer-bar-fill" id="countdown-bar" style="width: ${remainingPercent()}%"></div>
        </div>
      </div>
    </section>
    <section class="panel reference">
      <h2>${state.publicState.game.current_round}라운드 기준 그림</h2>
      <img src="${managerReference?.image_url || ""}" alt="기준 그림">
    </section>
    <section class="panel">
      <div class="button-row">
        <button class="warn" onclick="finishRoundEarly()">라운드 강제 종료 후 AI 평가</button>
      </div>
    </section>
    <section class="team-grid" id="manager-live-teams">${renderManagerLiveTeamsMarkup(round, activeTeamIds)}</section>
    ${renderResetSection()}`;
}

function renderReview() {
  const review = state.publicState?.game?.review;
  if (!review) return renderGame();
  syncScoreDraftsFromReview();
  const activeTeamIds = review.submitted_images.map((item) => item.team_id);

  app.innerHTML = `
    <section class="hero" data-screen="manager-review">
      <div class="eyebrow">Review Round ${review.round_number}</div>
      <h1>평가 라운드</h1>
      <div class="inline-meta">
        <span class="badge">제출 이미지를 비교하는 단계</span>
        <span class="badge">점수 입력 후 다음 라운드 진행</span>
      </div>
    </section>
    <section class="panel reference">
      <h2>${review.round_number}라운드 기준 그림</h2>
      <img src="${review.reference.image_url}" alt="기준 그림">
    </section>
    <section class="team-grid" id="manager-review-teams">${renderReviewSubmissionsMarkup(review)}</section>
    <section class="panel" id="manager-review-judge">${renderReviewJudgeMarkup(review)}</section>
    <section class="panel" id="manager-review-scores">${renderReviewScoreInputsMarkup(review, activeTeamIds)}</section>
    ${renderResetSection()}`;
}

function renderResults() {
  const finalRanking = state.publicState?.game?.final_ranking || [];
  const winner = finalRanking[0];
  const podium = finalRanking.slice(0, 3);

  app.innerHTML = `
    <section class="hero ceremony-hero">
      <div class="eyebrow">Award Ceremony</div>
      <h1>최종 시상식</h1>
      <div class="inline-meta">
        <span class="badge">${winner ? `${winner.team_id}팀 우승` : "결과 집계 완료"}</span>
        <span class="badge">새 게임을 바로 준비할 수 있습니다</span>
      </div>
      <div class="button-row" style="margin-top:12px">
        <button class="accent" onclick="resetGame()">새 게임 시작</button>
      </div>
    </section>
    <section class="panel ceremony-panel">
      <div class="ceremony-podium">
        ${podium[1] ? `<div class="podium-card place-2"><div class="podium-rank">2위</div><div class="podium-team">${podium[1].team_id}팀</div><div class="podium-score">${podium[1].score}점</div><div class="podium-members">${teamMembers(podium[1].team_id).map((client) => `<div class="member-chip">${escapeHtml(client.name)}</div>`).join("") || `<div class="empty">팀원이 없습니다</div>`}</div></div>` : ``}
        ${podium[0] ? `<div class="podium-card place-1"><div class="podium-rank">1위</div><div class="podium-team">${podium[0].team_id}팀</div><div class="podium-score">${podium[0].score}점</div><div class="podium-members">${teamMembers(podium[0].team_id).map((client) => `<div class="member-chip">${escapeHtml(client.name)}</div>`).join("") || `<div class="empty">팀원이 없습니다</div>`}</div></div>` : ``}
        ${podium[2] ? `<div class="podium-card place-3"><div class="podium-rank">3위</div><div class="podium-team">${podium[2].team_id}팀</div><div class="podium-score">${podium[2].score}점</div><div class="podium-members">${teamMembers(podium[2].team_id).map((client) => `<div class="member-chip">${escapeHtml(client.name)}</div>`).join("") || `<div class="empty">팀원이 없습니다</div>`}</div></div>` : ``}
      </div>
    </section>
    <section class="panel">
      <h2>최종 순위</h2>
      <div class="card-list">
        ${finalRanking.map((item, index) => `
          <div class="score-card">
            <span>${index + 1}위 · ${item.team_id}팀</span>
            <strong>${item.score}점</strong>
            <div class="podium-members">${teamMembers(item.team_id).map((client) => `<div class="member-chip">${escapeHtml(client.name)}</div>`).join("") || `<div class="empty">팀원이 없습니다</div>`}</div>
          </div>
        `).join("") || `<div class="empty">최종 점수가 없습니다.</div>`}
      </div>
    </section>`;
}

function render() {
  if (!state.publicState) return;

  if (state.publicState.game.status === "lobby") renderLobby();
  else if (state.publicState.game.status === "finished") renderResults();
  else if (state.publicState.game.status === "review") renderReview();
  else renderGame();

  document.querySelector(".toast")?.remove();
  if (state.toast) {
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = state.toast;
    document.body.appendChild(toast);
  }

  if (state.publicState.game.status === "lobby") updateJoinQrCode();
}

window.dragStart = onDragStart;
window.dragOver = onDragOver;
window.dragLeave = onDragLeave;
window.dropToTeam = (event, teamId) => {
  event.currentTarget.classList.remove("dragover");
  onDrop(event, teamId);
};
window.addTestBot = addTestBot;
window.startGame = startGame;
window.resetGame = resetGame;
window.finishRoundEarly = finishRoundEarly;
window.advanceRound = advanceRound;
window.retryJudgeReview = retryJudgeReview;
window.updateScoreDraft = setScoreDraft;
window.updateJoinAccess = onJoinAccessInput;
window.updateRoundDurationInput = updateRoundDurationInput;

socket.on("connect", () => {
  socket.emit("session:join", { role: "manager", sessionToken: "manager" });
});

socket.on("state:update", (payload) => {
  const previousStatus = state.publicState?.game?.status;
  const previousRound = state.publicState?.game?.current_round;
  state.publicState = payload;
  syncManagerSettingsFromState(payload);
  if (payload?.game?.status !== "review") {
    state.scoreDrafts = {};
    state.scoreDraftTouched = {};
  }
  const nextStatus = payload?.game?.status;
  const nextRound = payload?.game?.current_round;
  const canPatchManagerScreen =
    previousStatus === nextStatus
    && previousRound === nextRound
    && ((nextStatus === "running" && !!document.querySelector('[data-screen="manager-game"]'))
      || (nextStatus === "review" && !!document.querySelector('[data-screen="manager-review"]')));

  if (canPatchManagerScreen) {
    updateManagerDynamicSections();
    return;
  }

  render();
});

socket.on("session:error", (payload) => showToast(payload.message));

bootstrap();
window.setInterval(() => {
  const countdownNode = document.getElementById("countdown-text");
  const barNode = document.getElementById("countdown-bar");
  if (countdownNode && state.publicState?.game?.status === "running") countdownNode.textContent = countdown();
  if (barNode && state.publicState?.game?.status === "running") barNode.style.width = `${remainingPercent()}%`;
}, 1000);
