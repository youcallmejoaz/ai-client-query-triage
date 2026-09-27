// Small progressive enhancements; every page works without JavaScript.
document.addEventListener("change", (event) => {
  const el = event.target;
  if (el.matches("[data-autosubmit]")) el.form.requestSubmit();
});
document.addEventListener("submit", (event) => {
  const form = event.target;
  const message = form.dataset.confirm;
  if (message && !window.confirm(message)) {
    event.preventDefault();
    return;
  }
  const button = form.querySelector("button[data-busy]");
  if (button) {
    button.disabled = true;
    button.lastChild.textContent = " " + button.dataset.busy;
  }
});
