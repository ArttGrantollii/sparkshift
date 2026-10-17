// Runs before the page paints, so a chosen theme never flashes the other one.
// The choice stays in this browser; without one, the system setting applies.
try {
  const theme = localStorage.getItem("sparkshift-theme");
  if (theme === "light" || theme === "dark") {
    document.documentElement.dataset.theme = theme;
  }
} catch {
  // Storage can be blocked; the system theme still applies.
}
