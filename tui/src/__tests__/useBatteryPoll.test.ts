import { describe, expect, it } from "vitest";

import { sameBatteryInfo, toBatteryInfo } from "../app/useBatteryPoll.js";

describe("sameBatteryInfo", () => {
  const reading = {
    available: true,
    category: "warn" as const,
    percent: 44,
    plugged: false,
  };

  it("matches a fresh object with the same fields, so an unchanged poll is not a new state", () => {
    expect(sameBatteryInfo(reading, { ...reading })).toBe(true);
    expect(sameBatteryInfo(null, null)).toBe(true);
  });

  it("sees a change in any field, and null against a reading", () => {
    expect(sameBatteryInfo(reading, { ...reading, percent: 43 })).toBe(false);
    expect(sameBatteryInfo(reading, { ...reading, plugged: true })).toBe(false);
    expect(sameBatteryInfo(reading, { ...reading, category: "critical" })).toBe(
      false,
    );
    expect(sameBatteryInfo(reading, null)).toBe(false);
    expect(sameBatteryInfo(null, reading)).toBe(false);
  });
});

describe("toBatteryInfo", () => {
  it("returns null for a null payload", () => {
    expect(toBatteryInfo(null)).toBeNull();
  });

  it("maps a full reading through faithfully", () => {
    expect(
      toBatteryInfo({
        available: true,
        category: "warn",
        percent: 44,
        plugged: false,
      }),
    ).toEqual({
      available: true,
      category: "warn",
      percent: 44,
      plugged: false,
    });
  });

  it("clamps and rounds the percent into 0-100", () => {
    expect(
      toBatteryInfo({
        available: true,
        category: "good",
        percent: 142.7,
        plugged: true,
      })?.percent,
    ).toBe(100);
    expect(
      toBatteryInfo({
        available: true,
        category: "critical",
        percent: -5,
        plugged: false,
      })?.percent,
    ).toBe(0);
    expect(
      toBatteryInfo({
        available: true,
        category: "warn",
        percent: 43.4,
        plugged: false,
      })?.percent,
    ).toBe(43);
  });

  it("coerces a missing/invalid percent to null", () => {
    expect(
      toBatteryInfo({ available: true, category: "dim" })?.percent,
    ).toBeNull();
  });

  it("falls back to the dim category for an unknown value", () => {
    expect(
      toBatteryInfo({
        available: true,
        category: "purple",
        percent: 50,
        plugged: false,
      })?.category,
    ).toBe("dim");
  });

  it("treats a non-boolean plugged as unknown (null)", () => {
    expect(
      toBatteryInfo({ available: false, category: "dim", percent: null })
        ?.plugged,
    ).toBeNull();
  });
});
