/* Sets light/dark before the page paints, so there is no flash. Loaded in <head>. */
(function () {
  try {
    var saved = localStorage.getItem("eagle-theme");
    var dark = saved ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.classList.toggle("dark", dark);
  } catch (e) {}
})();
