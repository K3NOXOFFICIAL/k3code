import { describe, expect, it } from "vitest";

import {
  ADD_PROVIDER_ROW,
  isAddProviderRow,
  pickerNeedsKey,
  providerDisplayNames,
  providerPickerRows,
  providerRowLabel,
} from "../domain/providers.js";

describe("providerDisplayNames", () => {
  it("returns bare names when all are unique", () => {
    expect(
      providerDisplayNames([
        { name: "Anthropic", slug: "anthropic" },
        { name: "OpenAI", slug: "openai" },
      ]),
    ).toEqual(["Anthropic", "OpenAI"]);
  });

  it("appends slug to every collision so the disambiguation is symmetric", () => {
    expect(
      providerDisplayNames([
        { name: "Kimi For Coding", slug: "kimi-coding" },
        { name: "Kimi For Coding", slug: "kimi-coding-cn" },
      ]),
    ).toEqual([
      "Kimi For Coding (kimi-coding)",
      "Kimi For Coding (kimi-coding-cn)",
    ]);
  });

  it("only disambiguates the colliding group", () => {
    expect(
      providerDisplayNames([
        { name: "Anthropic", slug: "anthropic" },
        { name: "Foo", slug: "foo-a" },
        { name: "Foo", slug: "foo-b" },
      ]),
    ).toEqual(["Anthropic", "Foo (foo-a)", "Foo (foo-b)"]);
  });

  it("falls back to plain name if slug is empty", () => {
    expect(
      providerDisplayNames([
        { name: "Foo", slug: "" },
        { name: "Foo", slug: "" },
      ]),
    ).toEqual(["Foo", "Foo"]);
  });

  it("skips disambiguation when slug equals name", () => {
    expect(
      providerDisplayNames([
        { name: "foo", slug: "foo" },
        { name: "foo", slug: "foo" },
      ]),
    ).toEqual(["foo", "foo"]);
  });

  it("handles empty input", () => {
    expect(providerDisplayNames([])).toEqual([]);
  });
});

describe("providerPickerRows key/add rows", () => {
  it("appends the + Add new provider trailing row", () => {
    const rows = providerPickerRows([{ name: "A", slug: "a" }]);
    expect(rows).toHaveLength(2);
    expect(isAddProviderRow(rows[1]!.entry)).toBe(true);
    expect(providerRowLabel(rows[1]!)).toBe(ADD_PROVIDER_ROW.name);
  });

  it("flags an unauthenticated api_key row as needing inline key entry", () => {
    expect(
      pickerNeedsKey({
        authenticated: false,
        auth_type: "api_key",
        key_env: "FOO_KEY",
        name: "Foo",
        slug: "foo",
      }),
    ).toBe(true);
    expect(
      pickerNeedsKey({
        authenticated: true,
        auth_type: "api_key",
        key_env: "FOO_KEY",
        name: "Foo",
        slug: "foo",
      }),
    ).toBe(false);
    expect(
      pickerNeedsKey({
        authenticated: false,
        auth_type: "none",
        name: "Foo",
        slug: "foo",
      }),
    ).toBe(false);
    expect(pickerNeedsKey(undefined)).toBe(false);
  });
});
