// Shared setup for the two Bryntum products Procuro uses (Gantt, Scheduler and Calendar).
// Both use the "thin" packages so they share one copy of Bryntum's core.
//
// Bryntum's stylesheets are served from public/bryntum/ (copied there by
// scripts/copy-bryntum-css.js) rather than bundled, and loaded on first use. Components wait for
// them before rendering, because Bryntum measures its layout when it starts.

import { useEffect, useState } from "react";
import "./bryntum.css";

const SHEETS = [
  "fontawesome/css/fontawesome.css",
  "fontawesome/css/solid.css",
  "core.css",
  "grid.css",
  "scheduler.css",
  "schedulerpro.css",
  "gantt.css",
  "calendar.css",
  "svalbard-light.css", // exactly one theme
];

let loading = null;

function loadStyles() {
  if (loading) return loading;
  const base = `${process.env.PUBLIC_URL || ""}/bryntum/`;
  // Insert before the app's own styles so Procuro's overrides win.
  const anchor = document.head.querySelector("link[rel=stylesheet], style");
  loading = Promise.all(
    SHEETS.map(
      (file) =>
        new Promise((resolve) => {
          const link = document.createElement("link");
          link.rel = "stylesheet";
          link.href = base + file;
          link.onload = resolve;
          link.onerror = resolve; // don't block forever; the component still renders
          document.head.insertBefore(link, anchor);
        })
    )
  );
  return loading;
}

export function useBryntumStyles() {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let alive = true;
    loadStyles().then(() => alive && setReady(true));
    return () => {
      alive = false;
    };
  }, []);
  return ready;
}

export function money(v, currency = "USD") {
  if (v === undefined || v === null || Number.isNaN(Number(v))) return "";
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(Number(v));
}

export function day(d) {
  if (!d) return "";
  return new Date(d).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

export function isoDay(d) {
  const x = new Date(d);
  const pad = (n) => String(n).padStart(2, "0");
  return `${x.getFullYear()}-${pad(x.getMonth() + 1)}-${pad(x.getDate())}`;
}