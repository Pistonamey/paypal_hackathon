// Copies Bryntum's stylesheets (and the FontAwesome icon font they use) into public/bryntum/,
// so they're served as-is instead of going through Create React App's CSS minifier, which
// can't parse some of Bryntum's modern CSS and fails CI builds. Runs before start and build.

const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const mod = (p) => path.join(root, "node_modules", "@bryntum", p);
const out = path.join(root, "public", "bryntum");

const files = {
  "core.css": mod("core-thin/core.css"),
  "grid.css": mod("grid-thin/grid.css"),
  "scheduler.css": mod("scheduler-thin/scheduler.css"),
  "schedulerpro.css": mod("schedulerpro-thin/schedulerpro.css"),
  "gantt.css": mod("gantt-thin/gantt.css"),
  "calendar.css": mod("calendar-thin/calendar.css"),
  "svalbard-light.css": mod("core-thin/svalbard-light.css"),
  "fontawesome/css/fontawesome.css": mod("core-thin/fontawesome/css/fontawesome.css"),
  "fontawesome/css/solid.css": mod("core-thin/fontawesome/css/solid.css"),
  "fontawesome/webfonts/fa-solid-900.woff2": mod("core-thin/fontawesome/webfonts/fa-solid-900.woff2"),
  "fontawesome/webfonts/fa-solid-900.ttf": mod("core-thin/fontawesome/webfonts/fa-solid-900.ttf"),
};

for (const [name, src] of Object.entries(files)) {
  const dest = path.join(out, name);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.copyFileSync(src, dest);
}
console.log(`Copied ${Object.keys(files).length} Bryntum style files to public/bryntum/`);