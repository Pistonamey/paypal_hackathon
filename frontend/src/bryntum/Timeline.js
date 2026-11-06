// The order's whole life on a Bryntum Gantt: the deal, delivery, the PayPal hold and payment,
// and any problem. Data comes from GET /api/orders/{id}/timeline, built from what actually
// happened plus what's planned. Done steps are solid, planned ones are pale, the owner's
// deadline is a red line and today is the current-time line. Baselines show the seller's
// promise next to what really happened.

import { useEffect, useMemo, useRef, useState } from "react";
import { BryntumGantt } from "@bryntum/gantt-react-thin";
import { StringHelper } from "@bryntum/core-thin";
import { Baselines, Dependencies, Indicators, Labels, TaskModel, TaskTooltip } from "@bryntum/gantt-thin";
import { TimeRanges } from "@bryntum/scheduler-thin";

// With thin packages each feature registers itself when imported, so import every one we turn on.
export const TIMELINE_FEATURES = [Baselines, Dependencies, Indicators, Labels, TaskTooltip, TimeRanges];
import { api } from "../api";
import { day, money, useBryntumStyles } from "./base";
import { Bone, GanttSkeleton } from "../ui";

// Extra fields the backend sends with each step. Bryntum declares fields through a static getter.
class MandateTask extends TaskModel {
  static get fields() {
    return [{ name: "status" }, { name: "kind" }, { name: "detail" }, { name: "amount", type: "number" }];
  }
}

const COLORS = {
  deal: "indigo",
  delivery: "teal",
  money: "green",
  returned: "blue",
  problem: "red",
  limit: "gray",
};

function colour(task) {
  if (task.kind === "money" && task.status !== "Done") return "yellow"; // still on hold
  return COLORS[task.kind] || "gray";
}

function prepare(tasks) {
  return tasks.map((g) => ({
    ...g,
    children: g.children.map((t) => ({ ...t, eventColor: colour(t) })),
  }));
}

const STATUS_CLS = { Done: "st-done", Now: "st-now", Planned: "st-planned" };

export default function Timeline({ orderId, version }) {
  const ref = useRef(null);
  const styled = useBryntumStyles();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false); // true once the chart has drawn real data

  useEffect(() => {
    let cancelled = false;
    api
      .timeline(orderId)
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [orderId, version]);

  useEffect(() => {
    const gantt = ref.current?.instance;
    if (!gantt || !data) return;
    gantt.project
      .loadInlineData({
        tasksData: prepare(data.tasks),
        dependenciesData: data.dependencies,
      })
      .then(() => {
        // The owner's deadline line (loadInlineData doesn't load time ranges, so set them directly).
        const ranges = gantt.project.timeRangeStore;
        ranges.removeAll();
        ranges.add(data.timeRanges);
        // Fit the order's whole life, from mandate to final payment, into the visible chart.
        gantt.zoomToSpan({ startDate: new Date(data.startDate), endDate: new Date(data.endDate) });
        requestAnimationFrame(() => setReady(true));
      });
  }, [data, styled]);

  const config = useMemo(
    () => ({
      project: { taskModelClass: MandateTask, autoSetConstraints: false },
      readOnly: true,
      emptyText: "", // the skeleton covers the chart until data arrives, so never flash "No records"
      viewPreset: "weekAndDayLetter",
      rowHeight: 36,
      barMargin: 8,
      columns: [
        {
          type: "name",
          text: "Step",
          width: 236,
          renderer: ({ record, value }) =>
            record.isLeaf
              ? { tag: "span", className: `step step-${record.kind}`, text: value }
              : { tag: "b", text: value },
        },
        {
          text: "Status",
          field: "status",
          width: 90,
          renderer: ({ record, value }) =>
            record.isLeaf && value ? { tag: "span", className: `st ${STATUS_CLS[value] || ""}`, text: value } : "",
        },
      ],
      taskTooltipFeature: {
        template: ({ taskRecord }) =>
          `<div class="tip"><b>${StringHelper.encodeHtml(taskRecord.name)}</b>` +
          `<div>${StringHelper.encodeHtml(day(taskRecord.startDate))}` +
          (taskRecord.isMilestone ? "" : ` – ${StringHelper.encodeHtml(day(taskRecord.endDate))}`) +
          `, ${StringHelper.encodeHtml(taskRecord.status || "")}` +
          (taskRecord.amount ? `, ${StringHelper.encodeHtml(money(taskRecord.amount))}` : "") +
          `</div>` +
          (taskRecord.detail ? `<div class="tip-detail">${StringHelper.encodeHtml(taskRecord.detail)}</div>` : "") +
          `</div>`,
      },
      baselinesFeature: true,
      dependenciesFeature: true,
      timeRangesFeature: { showCurrentTimeLine: { name: "Today" }, showHeaderElements: true },
      indicatorsFeature: {
        items: { deadlineDate: true, earlyDates: false, lateDates: false, constraintDate: false },
      },
      labelsFeature: { right: { field: "name", renderer: ({ taskRecord }) => (taskRecord.isMilestone ? taskRecord.name : "") } },
      percentBarFeature: false,
      projectLinesFeature: false,
      taskMenuFeature: false,
      scheduleMenuFeature: false,
      cellMenuFeature: false,
      taskEditFeature: false,
      cellEditFeature: false,
      taskDragFeature: false,
      taskResizeFeature: false,
      taskDragCreateFeature: false,
      dependencyEditFeature: false,
    }),
    []
  );

  if (error) return <p className="hint warn">Couldn't load the timeline: {error}</p>;

  return (
    <section className="timeline" aria-label="Order timeline">
      <div className="timeline-head">
        <div className="timeline-next">
          {!data && (<><Bone w={260} h={14} /><Bone w={320} h={14} style={{ marginTop: 6 }} /></>)}
          {data && !data.now && !data.next && "Everything's done."}
          {data?.now && (
            <p>
              Now: <b>{data.now.name}</b>
            </p>
          )}
          {data?.next && (
            <p>
              Next: <b>{data.next.name}</b>, {day(data.next.date)}
            </p>
          )}
        </div>
        <ul className="timeline-key">
          <li><i className="k-done" />Done or under way</li>
          <li><i className="k-planned" />Planned</li>
          <li><i className="k-hold" />Money on hold</li>
          <li><i className="k-deadline" />Your deadline</li>
          <li><i className="k-baseline" />What was promised</li>
        </ul>
      </div>
      <div className="timeline-gantt boot" aria-busy={!ready}>
        {styled && <BryntumGantt ref={ref} {...config} />}
        <div className={`boot-cover ${ready ? "is-gone" : ""}`} aria-hidden={ready}>
          <GanttSkeleton bare />
        </div>
      </div>
    </section>
  );
}