document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("time[data-local-time]").forEach((node) => {
    const value = node.getAttribute("data-local-time");
    const parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) {
      node.textContent = parsed.toLocaleString(undefined, {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        hour12: false,
      });
      node.title = `${value} (UTC)`;
    }
  });
});
