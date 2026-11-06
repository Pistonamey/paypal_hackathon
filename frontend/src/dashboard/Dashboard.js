// The Spend dashboard: one AG Studio instance over Procuro's live orders, offers, sellers and PayPal
// money events, with Procuro's own theme, two custom widgets and a Claude-powered AI team.

import { useEffect, useMemo, useRef, useState } from "react";
import { AgStudioAiModule } from "ag-studio";
import { AgStudio, AgStudioProvider } from "ag-studio-react";
import { api, BASE } from "../api";
import { buildAi } from "./ai";
import { PAGES, spendReport } from "./report";
import { buildStudioData } from "./studioData";
import { mandateStudioTheme } from "./theme";
import { buildWidgets } from "./widgets";
import { DashboardSkeleton, DashboardTilesSkeleton } from "../ui";

const LICENSE = process.env.REACT_APP_AG_STUDIO_LICENSE || undefined;
// v1 had the sample-data filter; v2 layouts could be saved with an empty "Every order" grid.
const SAVED_KEY = "procuro.spendReport.v3";
const OLD_KEYS = ["mandate.spendReport.v2", "mandate.spendReport.v1"];

// A widget whose data mapping has lost every field draws as an empty box (a grid with no columns,
// a chart with no values). That can happen after an edit, so a saved layout is repaired on load:
// any built-in widget left with no fields gets its original definition back.
function hasNoFields(widget) {
  const mapping = widget?.dataMapping;
  if (!mapping) return false; // custom and filter widgets have no mapping to lose
  return Object.values(mapping).every((v) => !Array.isArray(v) || v.length === 0);
}

export function repairLayout(saved) {
  if (!saved?.pages) return saved;
  let repaired = 0;
  const pages = saved.pages.map((page) => {
    const original = spendReport.pages.find((p) => p.id === page.id);
    if (!original || !page.widgets) return page;
    const widgets = { ...page.widgets };
    Object.entries(widgets).forEach(([id, w]) => {
      if (original.widgets[id] && hasNoFields(w)) {
        widgets[id] = original.widgets[id];
        repaired += 1;
      }
    });
    return { ...page, widgets };
  });
  if (repaired) console.info(`Spend dashboard: restored ${repaired} widget(s) that had lost their fields`);
  return { ...saved, pages };
}

function loadSaved() {
  try {
    OLD_KEYS.forEach((k) => window.localStorage.removeItem(k)); // old layouts are dropped, not migrated
    const raw = window.localStorage.getItem(SAVED_KEY);
    return raw ? repairLayout(JSON.parse(raw)) : null;
  } catch {
    return null;
  }
}

function save(state) {
  try {
    window.localStorage.setItem(SAVED_KEY, JSON.stringify(state));
    return true;
  } catch {
    return false;
  }
}

function forget() {
  try {
    window.localStorage.removeItem(SAVED_KEY);
  } catch {
    /* storage unavailable */
  }
}

export default function Dashboard() {
  const [tables, setTables] = useState(null);
  const [aiOn, setAiOn] = useState(false);
  const [saving, setSaving] = useState(null); // null until known; false when orders aren't saved
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .health()
      .then((h) => {
        setAiOn(!!h.ai);
        setSaving({ ok: h.store === "firestore", reason: h.store_error });
      })
      .catch(() => setAiOn(false));
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .analytics()
      .then((t) => !cancelled && setTables(t))
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <p className="banner">Couldn't load the dashboard: {error}</p>;
  if (!tables) return <DashboardSkeleton />;
  if (tables.liveOrders === 0) return <NoOrders saving={saving} />;

  return <SpendStudio tables={tables} aiOn={aiOn} saving={saving} />;
}

function SavingWarning({ saving }) {
  if (!saving || saving.ok) return null;
  return (
    <p className="hint warn">
      Orders aren't being saved: Firebase isn't connected, so they disappear when the server restarts.
      {saving.reason ? ` ${saving.reason}` : ""}
    </p>
  );
}

function NoOrders({ saving }) {
  return (
    <main className="spend spend-empty">
      <h1>Spend control</h1>
      <p>No orders yet. Once your agent buys something, this page shows what you paid, what negotiation saved,
        and where your money is: on hold in PayPal, paid to the seller, or returned to you.</p>
      <SavingWarning saving={saving} />
      <a className="btn primary" href="/">Place your first order</a>
    </main>
  );
}

function SpendStudio({ tables, aiOn, saving }) {
  const apiRef = useRef(null);
  const [mode, setMode] = useState("view");
  const [pageId, setPageId] = useState(PAGES[0].id);
  const [note, setNote] = useState("");
  const [analyst, setAnalyst] = useState(false);
  const [drawn, setDrawn] = useState(false); // cover the widgets until AG Studio has rendered them once
  useEffect(() => {
    const t = setTimeout(() => setDrawn(true), 10000); // never leave the cover up forever
    return () => clearTimeout(t);
  }, []);
  const [initialState] = useState(() => loadSaved() || spendReport);
  const data = useMemo(() => buildStudioData(tables), [tables]);
  const ai = useMemo(() => (aiOn ? buildAi(`${BASE}/api/studio-ai`) : undefined), [aiOn]);
  const pages = (apiRef.current?.getState()?.pages || initialState.pages).map((p, i) => ({
    id: p.id,
    title: PAGES.find((x) => x.id === p.id)?.title || `Page ${i + 1}`,
  }));

  // Opening the analyst shows just the chat and the dashboard: once Studio is in edit mode
  // (which resets its panels), fold the building panels away.
  useEffect(() => {
    if (!analyst || mode !== "edit") return undefined;
    const t = window.setTimeout(() => {
      const studio = apiRef.current;
      if (!studio) return;
      const s = studio.getState();
      studio.setState({
        ...s,
        panels: { ...s.panels, ai: { collapsed: false }, edit: { collapsed: true }, data: { collapsed: true },
          filters: { collapsed: true } },
      });
      setAnalyst(false);
    }, 50);
    return () => window.clearTimeout(t);
  }, [analyst, mode]);

  const goTo = (id) => {
    const studio = apiRef.current;
    if (studio) studio.setState({ ...studio.getState(), selectedPageId: id });
    setPageId(id);
  };

  const flash = (text) => {
    setNote(text);
    window.setTimeout(() => setNote(""), 2500);
  };

  return (
    <main className="spend">
      <header className="spend-head">
        <div>
          <h1>Spend control</h1>
          <p className="hint">
            {tables.liveOrders} order{tables.liveOrders === 1 ? "" : "s"}, all negotiated by your agent and paid through PayPal.
          </p>
          <SavingWarning saving={saving} />
        </div>
        <div className="spend-tools">
          {mode === "edit" ? (
            <>
              <button className="btn" onClick={() => { forget(); window.location.reload(); }}>Reset layout</button>
              <button
                className="btn primary"
                onClick={() => {
                  const ok = apiRef.current && save(apiRef.current.getState());
                  setMode("view");
                  flash(ok ? "Layout saved in this browser." : "Couldn't save in this browser.");
                }}
              >
                Save layout
              </button>
            </>
          ) : (
            <>
              <button className="btn" onClick={() => setMode("edit")}>Edit dashboard</button>
              {/* Studio shows its AI panel in edit mode, so asking the analyst opens it there. */}
              {aiOn && (
                <button className="btn primary" onClick={() => { setAnalyst(true); setMode("edit"); }}>
                  Ask the analyst
                </button>
              )}
            </>
          )}
        </div>
      </header>

      <nav className="spend-pages" aria-label="Dashboard pages">
        {pages.map((p) => (
          <button key={p.id} className={`tab ${p.id === pageId ? "tab-on" : ""}`} onClick={() => goTo(p.id)}
            aria-current={p.id === pageId ? "page" : undefined}>
            {p.title}
          </button>
        ))}
        {note && <span className="spend-note" role="status">{note}</span>}
        {!aiOn && <span className="spend-note">The AI analyst turns on when the server has a Claude API key.</span>}
      </nav>

      <div className="studio-frame boot" aria-busy={!drawn}>
        <AgStudioProvider licenseKey={LICENSE} modules={ai ? [AgStudioAiModule] : undefined}>
          <AgStudio
            style={{ height: "100%", width: "100%" }}
            theme={mandateStudioTheme}
            layout={{ widgetBorderEnabled: true }}
            panels={{ edit: { left: ["ai"], right: ["filters", "edit", "data"] }, view: { right: ["filters"] } }}
            data={data}
            widgets={buildWidgets}
            ai={ai}
            initialState={initialState}
            mode={mode}
            onApiReady={(event) => {
              apiRef.current = event.api;
              if (process.env.NODE_ENV !== "production" || window.location.search.includes("debug")) {
                window.__procuroStudio = event.api; // for debugging in the browser console
              }
            }}
            onRenderStateChanged={(event) => {
              // "rendered" means the layout is up; each widget then still loads its numbers.
              // Lift the cover once no widget shows AG Studio's loading overlay.
              if (event.renderState !== "rendered" || drawn) return;
              let clear = 0;
              const check = setInterval(() => {
                const busy = document.querySelector(".studio-frame .ag-studio-layout-widget-loading-overlay-active");
                clear = busy ? 0 : clear + 1;
                if (clear >= 2) {
                  clearInterval(check);
                  setDrawn(true);
                }
              }, 120);
              setTimeout(() => clearInterval(check), 8000);
            }}
            onStateUpdated={(event) => {
              if (event.state.selectedPageId && event.state.selectedPageId !== pageId) {
                setPageId(event.state.selectedPageId);
              }
            }}
          />
        </AgStudioProvider>
        <div className={`boot-cover studio-cover ${drawn ? "is-gone" : ""}`} aria-hidden={drawn}>
          <DashboardTilesSkeleton />
        </div>
      </div>
    </main>
  );
}