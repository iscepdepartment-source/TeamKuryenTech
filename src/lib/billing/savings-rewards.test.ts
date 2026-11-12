import { describe, expect, it } from "vitest";
import { calculateBillSavings } from "./savings-rewards";

const bill = (id: string, periodStart: string, periodEnd: string, pesos: number) => ({
  id,
  periodStart,
  periodEnd,
  amountCentavos: pesos * 100,
  kwh: 100,
});

describe("bill savings rewards", () => {
  it("awards one point per ₱10 of verified monthly bill reduction", () => {
    const summary = calculateBillSavings([
      bill("new", "2026-02-01", "2026-03-02", 900),
      bill("old", "2026-01-01", "2026-01-30", 1000),
    ]);

    expect(summary.points).toBe(10);
    expect(summary.totalReductionCentavos).toBe(10145);
    expect(summary.latestReductionCentavos).toBe(10145);
  });

  it("normalizes unequal billing periods and does not award points for an increase", () => {
    const reduction = calculateBillSavings([
      bill("old", "2026-01-01", "2026-01-31", 1000),
      bill("new", "2026-02-01", "2026-02-28", 850),
    ]);
    const increase = calculateBillSavings([
      bill("old", "2026-01-01", "2026-01-31", 900),
      bill("new", "2026-02-01", "2026-02-28", 1000),
    ]);

    expect(reduction.totalReductionCentavos).toBeGreaterThan(0);
    expect(reduction.points).toBeGreaterThan(0);
    expect(increase.totalReductionCentavos).toBe(0);
    expect(increase.points).toBe(0);
  });

  it("does not award bill points without at least two recorded bills", () => {
    expect(calculateBillSavings([bill("only", "2026-01-01", "2026-01-31", 1000)]).points).toBe(0);
    expect(calculateBillSavings([]).latestBill).toBeNull();
  });

  it("does not treat a long gap between recorded bills as a monthly reduction", () => {
    const summary = calculateBillSavings([
      bill("old", "2026-01-01", "2026-01-31", 1000),
      bill("new", "2026-04-01", "2026-04-30", 800),
    ]);

    expect(summary.points).toBe(0);
    expect(summary.latestReductionCentavos).toBeNull();
  });
});
