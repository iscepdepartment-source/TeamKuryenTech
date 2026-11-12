// Consumption Calendar views shared by the Personal, Rental Owner, and Rental Tenant workspaces.
import { buildTimeline, monthGrid, seasonalAverages, yearsCovered } from "./src/lib/calendar.ts";
import { estimateMonthlyKwh } from "./src/lib/personal/estimates.ts";
import { listAppliances, listBills, listSavingsPlans } from "./src/lib/personal/store.ts";
import { currentAccount } from "./src/lib/account.ts";
import {
  beginGoogleCalendarAuthorization,
  disconnectGoogleCalendar,
  getGoogleCalendarStatus,
  listUtilityNotices,
  syncGoogleCalendar,
} from "./src/lib/utility-notices.ts";
import { getMonthlyEnergyTarget } from "./game.js";
import { fill, formatPeso, kindBadge, notice, slot, table, user } from "./ui-kit.js";

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const views = new Map(); // container id -> { records, year }

const kwhText = (kwh) => (kwh === null || kwh === undefined ? "Not measured" : `${kwh.toLocaleString("en-PH", { maximumFractionDigits: 2 })} kWh`);
const percentText = (value) => (value === null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)}%`);

function gridHtml(id) {
  const { records, year } = views.get(id);
  const years = yearsCovered(records);
  const grid = monthGrid(records, year);
  return `
    <div class="toolbar calendar-years" role="group" aria-label="Year">
      ${years.map((y) => `<button type="button" class="btn btn-small ${y === year ? "btn-primary" : "btn-outline"}" data-act="calendar-year" data-view="${id}" data-year="${y}" aria-pressed="${y === year}">${y}</button>`).join("")}
    </div>
    <div class="calendar-grid">
      ${grid.map((cell) => `<div class="calendar-month${cell.periods.length ? " has-data" : ""}">
        <b>${MONTH_NAMES[cell.month - 1]} ${year}</b>
        ${cell.periods.length
          ? cell.periods.map((p) => `<div class="calendar-period"><span translate="no">${user(p.periodStart)} – ${user(p.periodEnd)}</span><small>${kwhText(p.kwh)}${p.amountCentavos !== null ? ` · ${formatPeso(p.amountCentavos)}` : ""}</small><small class="muted-note">${p.overlapDays} day(s) in this month</small></div>`).join("")
          : `<small class="muted-note">No record</small>`}
      </div>`).join("")}
    </div>
    <p class="muted-note">Billing periods keep their exact dates. A period spanning two months appears in both with its full total; kWh are never split into invented daily or monthly values.</p>`;
}

/** Calendar + timeline HTML for recorded billing periods. */
export function calendarSection(ctx, id, records, { monthlyGoalKwh = null, estimatedMonthlyKwh = null, recordedKind = "official_utility_bill" } = {}) {
  if (!records.length) return `<p class="muted-note">No recorded billing periods yet.</p>`;
  const year = views.get(id)?.year ?? yearsCovered(records)[0];
  views.set(id, { records, year });
  const timeline = buildTimeline(records, { monthlyGoalKwh, estimatedMonthlyKwh }).reverse();
  const seasonal = seasonalAverages(records);
  return `
    <div data-calendar="${id}">${gridHtml(id)}</div>
    <h4>Billing periods ${kindBadge(recordedKind)}</h4>
    ${table(
      ["Period", "Days", "Consumption", "Amount", "Avg. per day", "Change (per day)", ...(monthlyGoalKwh ? ["Vs. goal"] : []), ...(estimatedMonthlyKwh ? ["Appliance estimate"] : [])],
      timeline.map((t) => [
        `<span translate="no">${user(t.periodStart)} – ${user(t.periodEnd)}</span>${t.label ? `<div class="muted-note">${user(t.label)}</div>` : ""}`,
        t.days,
        kwhText(t.kwh),
        t.amountCentavos === null ? "—" : formatPeso(t.amountCentavos),
        t.averageKwhPerDay === null ? "—" : `${t.averageKwhPerDay} kWh <small class="muted-note">(period average)</small>`,
        percentText(t.changeVsPreviousPercent),
        ...(monthlyGoalKwh ? [t.goalDifferenceKwh === null ? "—" : `${t.goalDifferenceKwh > 0 ? "+" : ""}${t.goalDifferenceKwh} kWh vs ${t.goalForPeriodKwh} kWh goal`] : []),
        ...(estimatedMonthlyKwh ? [`${t.estimatedForPeriodKwh} kWh ${kindBadge("estimate")}`] : []),
      ]),
    )}
    ${seasonal.length > 1 ? `<h4>Seasonal pattern</h4>${table(["Period ending in", "Avg. kWh per day", "Periods"], seasonal.map((s) => [MONTH_NAMES[s.month - 1], s.averageKwhPerDay, s.periods]))}` : ""}`;
}

export const calendarActions = {
  "calendar-year"(button, ctx) {
    const view = views.get(button.dataset.view);
    if (!view) return;
    view.year = Number(button.dataset.year);
    const container = document.querySelector(`[data-calendar="${CSS.escape(button.dataset.view)}"]`);
    if (container) {
      container.innerHTML = gridHtml(button.dataset.view);
      ctx.translate(container);
    }
  },
  async "google-calendar-connect"() {
    const { authorizationUrl } = await beginGoogleCalendarAuthorization();
    window.location.assign(authorizationUrl);
  },
  async "google-calendar-sync"(_button, ctx) {
    const result = await syncGoogleCalendar();
    ctx.toast(`Google Calendar sync: ${result.synced} added or updated, ${result.skipped} missing schedule times, ${result.failed} failed.`);
    ctx.render();
  },
  async "google-calendar-disconnect"(_button, ctx) {
    if (!window.confirm("Disconnect Google Calendar? Existing events will remain in Google Calendar.")) return;
    await disconnectGoogleCalendar();
    ctx.toast("Google Calendar disconnected. Existing events were not deleted.");
    ctx.render();
  },
};

// ---------------------------------------------------------- personal page
function calendarPage(ctx) {
  return `
    ${ctx.pageHead("Consumption calendar", "Your recorded bills by billing period, compared with your goal and appliance estimates.")}
    ${notice("info", `${kindBadge("official_utility_bill")} Consumption comes from bills you recorded. ${kindBadge("estimate")} Goal and appliance figures are scaled to each period's length. SmartWatts has no meter connection, so there is no daily data.`)}
    ${ctx.panel("Billing calendar", slot("personal-calendar"))}
    <div style="margin-top:14px">${ctx.panel("Savings plans history", slot("savings-history"))}</div>`;
}

async function loadCalendar(ctx, root) {
  const [bills, appliances, plans] = await Promise.all([listBills(), listAppliances(), listSavingsPlans()]);
  let estimatedMonthlyKwh = 0;
  for (const appliance of appliances) {
    try {
      estimatedMonthlyKwh += estimateMonthlyKwh(appliance);
    } catch {
      // Skip incomplete appliance records in the estimate.
    }
  }
  const records = bills.map((b) => ({ periodStart: b.periodStart, periodEnd: b.periodEnd, kwh: b.kwh, amountCentavos: b.amountCentavos, kind: "recorded" }));
  fill(root, "personal-calendar", records.length
    ? calendarSection(ctx, "personal", records, { monthlyGoalKwh: getMonthlyEnergyTarget(), estimatedMonthlyKwh: estimatedMonthlyKwh || null })
    : `<p class="muted-note">No bills recorded yet. Add bills to build your calendar.</p><button class="btn btn-primary" data-page="bills">Add a bill</button>`);
  fill(root, "savings-history", table(
    ["Plan", "Estimated monthly savings"],
    plans.map((p) => [user(p.title), `${p.monthlyKwhSaved} kWh · ${formatPeso(p.monthlySavingsCentavos)} ${kindBadge("estimate")}`]),
    "No saved savings plans yet.",
  ));
}

function utilityNoticesPage(ctx) {
  return `
    ${ctx.pageHead("Interruption calendar & notices", "View forwarded MERALCO interruption and disconnection emails, plus your notice history.")}
    ${notice("info", "Notices are private to your SmartWatts account. Automatic updates require an inbound-email provider configured to call the SmartWatts webhook. Interruption schedules are added to Google Calendar when the email includes reliable start and end times.")}
    <div data-slot="utility-notices">${slot("notices")}</div>`;
}

function dateTime(value) {
  if (!value) return "Not provided in the email";
  const date = new Date(value);
  return user(Number.isNaN(date.getTime()) ? value : date.toLocaleString("en-PH", { dateStyle: "medium", timeStyle: "short" }));
}

function interruptionCalendarHtml(interruptions) {
  const grouped = new Map();
  for (const item of interruptions) {
    if (!item.startAt) continue;
    const date = new Date(item.startAt);
    if (Number.isNaN(date.getTime())) continue;
    const year = date.getFullYear();
    const month = date.getMonth();
    const key = `${year}-${String(month + 1).padStart(2, "0")}`;
    const entries = grouped.get(key) ?? { year, month, byDay: new Map() };
    const dayEntries = entries.byDay.get(date.getDate()) ?? [];
    dayEntries.push(item);
    entries.byDay.set(date.getDate(), dayEntries);
    grouped.set(key, entries);
  }
  if (!grouped.size) return '<p class="muted-note">No interruption dates are available yet. The forwarding provider must supply start_at and end_at when it can reliably extract them.</p>';

  return [...grouped.values()].sort((a, b) => a.year - b.year || a.month - b.month).map(({ year, month, byDay }) => {
    const firstWeekday = (new Date(year, month, 1).getDay() + 6) % 7;
    const dayCount = new Date(year, month + 1, 0).getDate();
    const cells = Array.from({ length: firstWeekday }, () => '<div class="interruption-day empty" aria-hidden="true"></div>');
    for (let day = 1; day <= dayCount; day += 1) {
      const entries = byDay.get(day) ?? [];
      cells.push(`<div class="interruption-day${entries.length ? " has-interruption" : ""}">
        <b>${day}</b>${entries.map((item) => `<span class="interruption-event">${user(item.subject)}${item.serviceArea ? ` · ${user(item.serviceArea)}` : ""}</span>`).join("")}
      </div>`);
    }
    return `<section class="interruption-month"><h4>${new Date(year, month, 1).toLocaleDateString("en-PH", { month: "long", year: "numeric" })}</h4>
      <div class="interruption-weekdays">${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day) => `<b>${day}</b>`).join("")}</div>
      <div class="interruption-days">${cells.join("")}</div></section>`;
  }).join("");
}

function googleCalendarPanel(ctx, status) {
  if (!status.configured) {
    return ctx.panel("Google Calendar", notice("warning", "Google Calendar auto-sync is unavailable until the server is configured with Google OAuth credentials and a token encryption key."));
  }
  if (!status.connected) {
    return ctx.panel("Google Calendar", `${notice("info", "Connect once to let SmartWatts add and update private outage events in your primary Google Calendar.")}<button class="btn btn-primary" type="button" data-act="google-calendar-connect">Connect Google Calendar</button>`);
  }
  return ctx.panel("Google Calendar", `${notice("info", "Connected. New interruption notices with complete schedule times will be added automatically. Use Sync to add existing schedules.")}<div class="toolbar"><button class="btn btn-primary" type="button" data-act="google-calendar-sync">Sync existing schedules</button><button class="btn btn-outline" type="button" data-act="google-calendar-disconnect">Disconnect</button></div>`);
}

function calendarSyncLabel(status) {
  switch (status) {
    case "synced": return "Added to Google Calendar";
    case "not_scheduled": return "Missing start or end time";
    case "not_connected": return "Connect Google Calendar";
    case "not_configured": return "Calendar sync not configured";
    case "failed": return "Sync failed — retry from Google Calendar";
    default: return "Not synced yet";
  }
}

async function loadUtilityNotices(ctx, root) {
  const account = currentAccount();
  if (!account.permanent || !account.emailVerified) {
    fill(root, "utility-notices", notice("info", 'Sign in with a verified email account to view private MERALCO notices. <button class="btn btn-primary" data-page="account">Sign in</button>'));
    ctx.setUtilityNotices([]);
    return;
  }

  const [noticeResults, googleCalendar] = await Promise.all([
    listUtilityNotices(),
    getGoogleCalendarStatus(),
  ]);
  const noticesList = ctx.setUtilityNotices(noticeResults);
  const interruptions = noticesList.filter((item) => item.kind === "interruption");
  const disconnections = noticesList.filter((item) => item.kind === "disconnection");
  const reports = noticesList.filter((item) => item.kind === "report");
  const interruptionRows = interruptions.map((item) => [
    user(item.subject),
    item.serviceArea ? user(item.serviceArea) : "Not specified",
    dateTime(item.startAt),
    dateTime(item.endAt),
    calendarSyncLabel(item.calendarSync),
    user(item.summary),
  ]);
  const disconnectionRows = disconnections.map((item) => [
    user(item.subject),
    dateTime(item.receivedAt),
    user(item.summary),
  ]);
  const reportRows = reports.map((item) => [
    user(item.subject),
    dateTime(item.receivedAt),
    user(item.sender),
    user(item.summary),
  ]);

  fill(root, "utility-notices", `
    <div class="grid stats">
      ${ctx.stat("zap", "Interruption notices", String(interruptions.length), "", "Forwarded email notices", "")}
      ${ctx.stat("triangle-alert", "Disconnection notices", String(disconnections.length), "", "Private to your account", "")}
      ${ctx.stat("receipt-text", "Email reports", String(reports.length), "", "Received notice history", "")}
      ${ctx.stat("bell", "Unread", String(noticesList.filter((item) => !item.readAt && !item.locallyRead).length), "", "In-system alerts", "")}
    </div>
    ${googleCalendarPanel(ctx, googleCalendar)}
    <div class="utility-notice-sections">
      ${ctx.panel("Interruption calendar", table(
        ["Notice", "Service area", "Interruption starts", "Interruption ends", "Google Calendar", "Details"],
        interruptionRows,
        "No interruption notices have been received.",
      ) + interruptionCalendarHtml(interruptions))}
      ${ctx.panel("Disconnection notices", table(
        ["Notice", "Received", "Details"],
        disconnectionRows,
        "No disconnection notices have been received.",
      ))}
      ${ctx.panel("Reports", table(
        ["Email subject", "Received", "Sender", "Summary"],
        reportRows,
        "No forwarded MERALCO emails have been received.",
      ))}
    </div>`);
}

export const calendarPages = {
  calendar: { title: "Consumption Calendar", subtitle: "Billing-period history and goals.", render: calendarPage, load: loadCalendar },
  "utility-notices": { title: "Interruption Calendar & Notices", subtitle: "Private interruption, disconnection, and email reports.", render: utilityNoticesPage, load: loadUtilityNotices },
};
