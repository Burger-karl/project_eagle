/**
 * Tailwind config for the Eagle dashboard.
 *
 * Colours are CSS variables (see src/input.css) so light and dark themes are
 * one swap of the variables, not a `dark:` variant on every element. Each
 * variable holds "R G B" so opacity modifiers such as bg-brand/10 still work.
 */
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;
const tone = (name) => ({
  DEFAULT: token(name),          // text colour     e.g. text-fail
  bg: token(`${name}-bg`),       // soft background e.g. bg-fail-bg
  solid: token(`${name}-solid`), // dots, bars      e.g. bg-fail-solid
});

module.exports = {
  darkMode: "class",
  content: [
    "../templates/**/*.html",
    "../static/dashboard/js/**/*.js",
  ],
  theme: {
    extend: {
      fontFamily: {
        display: ['"Bricolage Grotesque Variable"', '"Segoe UI"', "system-ui", "sans-serif"],
        sans: ['"Public Sans Variable"', '"Segoe UI"', "system-ui", "sans-serif"],
      },
      colors: {
        canvas: token("canvas"),
        panel: token("panel"),
        sunken: token("sunken"),
        ink: token("ink"),
        mute: token("mute"),
        line: token("line"),
        focus: token("focus"),
        brand: { DEFAULT: token("brand"), hover: token("brand-hover"), on: token("brand-on") },
        side: {
          DEFAULT: token("side"),
          ink: token("side-ink"),
          mute: token("side-mute"),
          active: token("side-active"),
        },
        // Invoice status colours
        pend: tone("pend"),   // pending
        sub: tone("sub"),     // submitting
        clr: tone("clr"),     // cleared
        wb: tone("wb"),       // written back to Sage
        fail: tone("fail"),   // failed (will retry)
        rej: tone("rej"),     // rejected by FIRS
        can: tone("can"),     // cancelled
      },
      keyframes: {
        slidein: {
          from: { transform: "translateX(24px)", opacity: "0" },
          to: { transform: "translateX(0)", opacity: "1" },
        },
      },
      animation: { slidein: "slidein .18s ease-out" },
    },
  },
  plugins: [],
};
