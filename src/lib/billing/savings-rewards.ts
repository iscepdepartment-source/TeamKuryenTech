import { periodDays } from "../calendar";

const AVERAGE_DAYS_PER_MONTH = 365.25 / 12;
const DAY_MS = 86_400_000;

export interface SavingsBill {
  id: string;
  periodStart: string;
  periodEnd: string;
  amountCentavos: number;
  kwh: number;
}

export interface BillSavingsSummary {
  points: number;
  totalReductionCentavos: number;
  latestBill: SavingsBill | null;
  latestReductionCentavos: number | null;
}

function monthlyAmountCentavos(bill: SavingsBill): number {
  return Math.round((bill.amountCentavos / periodDays(bill.periodStart, bill.periodEnd)) * AVERAGE_DAYS_PER_MONTH);
}

export function calculateBillSavings(bills: readonly SavingsBill[]): BillSavingsSummary {
  const sorted = [...bills].sort((a, b) =>
    a.periodEnd.localeCompare(b.periodEnd) || a.periodStart.localeCompare(b.periodStart),
  );
  let totalReductionCentavos = 0;
  let latestReductionCentavos: number | null = null;

  sorted.forEach((bill, index) => {
    if (index === 0) return;
    const previous = sorted[index - 1];
    const daysBetween = (Date.parse(`${bill.periodEnd}T00:00:00Z`) - Date.parse(`${previous.periodEnd}T00:00:00Z`)) / DAY_MS;
    if (bill.periodStart <= previous.periodEnd || daysBetween < 21 || daysBetween > 45) {
      if (index === sorted.length - 1) latestReductionCentavos = null;
      return;
    }
    const reduction = Math.max(0, monthlyAmountCentavos(previous) - monthlyAmountCentavos(bill));
    totalReductionCentavos += reduction;
    if (index === sorted.length - 1) latestReductionCentavos = reduction;
  });

  return {
    points: Math.floor(totalReductionCentavos / 1000),
    totalReductionCentavos,
    latestBill: sorted.at(-1) ?? null,
    latestReductionCentavos,
  };
}
