"use strict";

const booking = document.querySelector("[data-booking-form]");
if (booking) {
  const scheduled = document.getElementById("scheduled-time");
  const input = scheduled.querySelector("input");
  const button = booking.querySelector("button[type=submit]");
  const update = () => {
    const planned = booking.elements.mode.value === "scheduled";
    scheduled.hidden = !planned;
    input.required = planned;
    button.textContent = planned ? "確認預約 →" : "確認借用 →";
  };
  booking.addEventListener("change", update);
  update();
}

document.querySelectorAll("form[data-confirm]").forEach(form => {
  form.addEventListener("submit", event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
document.querySelectorAll("[data-copy]").forEach(button => {
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(document.getElementById(button.dataset.copy).textContent);
      button.textContent = "已複製";
    } catch (_) { button.textContent = "請選取文字複製"; }
  });
});

let clockOffset = 0;
function countdown() {
  document.querySelectorAll("[data-countdown]").forEach(element => {
    const seconds = Math.max(0, Number(element.dataset.countdown) - Math.floor(Date.now() / 1000) - clockOffset);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor(seconds % 3600 / 60);
    const s = seconds % 60;
    element.textContent = [h, m, s].map(n => String(n).padStart(2, "0")).join(":");
  });
}
countdown();
setInterval(countdown, 1000);

async function refreshStatus() {
  if (document.hidden || document.body.dataset.authenticated !== "true") return;
  try {
    const response = await fetch("/api/status", {headers: {Accept: "application/json"}});
    if (response.status === 401) { window.location.assign("/login"); return; }
    if (!response.ok) throw new Error("Status unavailable");
    const data = await response.json();
    clockOffset = data.server_time - Math.floor(Date.now() / 1000);
    const warning = document.getElementById("scheduler-warning");
    warning.textContent = "排程服務暫時離線，借用啟用與回收可能延遲。請聯絡管理員。";
    warning.hidden = data.scheduler_online;
    let changed = false;
    data.rentals.forEach(rental => {
      const element = document.querySelector(`[data-rental-id="${rental.id}"]`);
      if (element && element.dataset.status !== rental.status) changed = true;
    });
    data.devices.forEach(device => {
      const element = document.querySelector(`[data-device-id="${device.id}"]`);
      if (element && element.dataset.state !== device.state) changed = true;
    });
    // Preserve partially entered forms when another user's allocation changes.
    const editing = [...document.querySelectorAll("input,select")].some(el => el === document.activeElement);
    if (changed && !editing) window.location.reload();
  } catch (_) {
    const warning = document.getElementById("scheduler-warning");
    if (warning) {
      warning.hidden = false;
      warning.textContent = "暫時無法更新狀態，請確認網路連線後重新整理。";
    }
  }
}
refreshStatus();
setInterval(refreshStatus, 10000);
document.addEventListener("visibilitychange", refreshStatus);
