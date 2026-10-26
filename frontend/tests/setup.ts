import "@testing-library/jest-dom/vitest";

// jsdom lacks the native dialog methods; focus trapping is checked in Chromium.
if (!HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
}
