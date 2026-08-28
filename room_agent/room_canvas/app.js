const stageOrder = ["listening", "transcribing", "routing", "executing"];
const stateLabels = {
  STARTING: "Starting",
  READY: "Ready",
  LISTENING: "Listening",
  TRANSCRIBING: "Transcribing",
  ROUTING: "Understanding",
  EXECUTING: "Taking action",
  SUCCESS: "Done",
  ERROR: "Needs attention",
  STOPPED: "Stopped",
};

const $ = (id) => document.getElementById(id);

function duration(value) {
  if (value === null || value === undefined) return "—";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(1)} s`;
}

function resultText(snapshot) {
  const result = snapshot.result || {};
  if (result.message) return result.message;
  if (result.title && result.channel_title) return `${result.title} · ${result.channel_title}`;
  if (result.title) return result.title;
  if (result.query) return `Search ready: ${result.query}`;
  if (result.media_status) return `Playback ${result.media_status.toLowerCase()}`;
  if (snapshot.state === "SUCCESS") return "Command completed successfully.";
  if (snapshot.state === "ERROR") return snapshot.detail || "The command could not be completed.";
  return "Ready for a voice command.";
}

function render(snapshot) {
  document.body.dataset.state = snapshot.state;
  $("state").textContent = stateLabels[snapshot.state] || snapshot.state;
  $("orb-label").textContent = snapshot.state;
  $("detail").textContent = snapshot.detail || "Local and ready";
  $("transcript").textContent = snapshot.transcript || "Say a command to begin.";
  $("route").textContent = snapshot.action
    ? `${snapshot.action} → ${snapshot.target || "local"}`
    : "Waiting for intent";
  $("total-time").textContent = snapshot.total_ms === null
    ? "LOCAL"
    : `TOTAL ${duration(snapshot.total_ms)}`;
  $("result").textContent = resultText(snapshot);

  const activeStage = snapshot.state.toLowerCase();
  stageOrder.forEach((stage) => {
    const item = document.querySelector(`[data-stage="${stage}"]`);
    const elapsed = snapshot.stage_ms ? snapshot.stage_ms[stage] : undefined;
    item.classList.toggle("done", elapsed !== undefined);
    item.classList.toggle("active", activeStage === stage);
    $(`time-${stage}`).textContent = duration(elapsed);
  });
}

async function refresh() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    $("connection").textContent = "Canvas connected";
  } catch (_error) {
    $("connection").textContent = "Reconnecting…";
  }
}

refresh();
setInterval(refresh, 250);
