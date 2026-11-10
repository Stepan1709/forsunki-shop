/** Tailwind собирается standalone CLI (без Node): см. стадию css в Dockerfile. */
module.exports = {
  content: ["./app/templates/**/*.html"],
  darkMode: "media", // тёмная тема по настройке системы пользователя
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#fff6ed", 100: "#ffead4", 200: "#ffd2a8", 300: "#ffb170", 400: "#ff8a3a",
          500: "#f26d14", 600: "#e0762b", 700: "#b8530f", 800: "#933f0f", 900: "#76350f",
        },
      },
      fontFamily: {
        // Системные шрифты: ничего не загружается с внешних сайтов.
        sans: ["system-ui", "-apple-system", "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "sans-serif"],
      },
    },
  },
};
