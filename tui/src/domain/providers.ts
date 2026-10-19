export const providerDisplayNames = (
  providers: readonly { name: string; slug: string }[],
): string[] => {
  const counts = new Map<string, number>();

  for (const p of providers) {
    counts.set(p.name, (counts.get(p.name) ?? 0) + 1);
  }

  return providers.map((p) =>
    (counts.get(p.name) ?? 0) > 1 && p.slug && p.slug !== p.name
      ? `${p.name} (${p.slug})`
      : p.name,
  );
};

export interface ProviderPickerEntry {
  authenticated?: boolean | null;
  auth_type?: string | null;
  key_env?: string | null;
  is_current?: boolean | null;
  isCurrent?: boolean | null;
  models?: string[];
  name: string;
  slug: string;
  total_models?: number | null;
  totalModels?: number | null;
  warning?: string | null;
}

export interface ProviderPickerRow {
  displayName: string;
  entry: ProviderPickerEntry;
}

/** Pair each provider entry with its display name (collision-aware). */
export function providerPickerRows(
  providers: readonly ProviderPickerEntry[],
): ProviderPickerRow[] {
  const names = providerDisplayNames(providers);

  return [
    ...providers.map((entry, i) => ({
      displayName: names[i] ?? entry.name ?? entry.slug,
      entry,
    })),
    { displayName: ADD_PROVIDER_ROW.name, entry: ADD_PROVIDER_ROW },
  ];
}

/**
 * Trailing "+ Add new provider" row: picking it opens the popup's add form
 * (providerAddForm.tsx) instead of switching providers. The slug is magic --
 * never a real provider block name.
 */
export const ADD_PROVIDER_SLUG = "__add_provider__";

export const ADD_PROVIDER_ROW: ProviderPickerEntry = {
  name: "+ Add new provider...",
  slug: ADD_PROVIDER_SLUG,
};

export function isAddProviderRow(entry: ProviderPickerEntry): boolean {
  return entry.slug === ADD_PROVIDER_SLUG;
}

/**
 * An unauthenticated `api_key` row whose key can be pasted inline
 * (`model.save_key`) -- mirrors modelPicker's pickerNeedsKey.
 */
export function pickerNeedsKey(entry: ProviderPickerEntry | undefined): boolean {
  return (
    entry?.authenticated === false &&
    entry.auth_type === "api_key" &&
    !!entry.key_env
  );
}

/** Cursor start: the active provider, else the first row. */
export function initialProviderIdx(
  providers: readonly ProviderPickerEntry[],
): number {
  const idx = providers.findIndex((p) => p.is_current ?? p.isCurrent);

  return idx >= 0 ? idx : 0;
}

/** The `/provider` argument the picker emits: name plus optional `--save`. */
export function providerPickerCommand(slug: string, saveDefault: boolean): string {
  return saveDefault ? `${slug} --save` : slug;
}

/** One provider row: `*` current, `○` needs setup, else `●`, plus model count. */
export function providerRowLabel(row: ProviderPickerRow): string {
  if (isAddProviderRow(row.entry)) {
    return row.displayName;
  }

  const p = row.entry;
  const current = p.is_current ?? p.isCurrent;
  const mark = p.authenticated === false ? "○" : current ? "*" : "●";
  const count = p.total_models ?? p.totalModels ?? p.models?.length ?? 0;
  const suffix =
    p.authenticated === false
      ? "(needs setup)"
      : `${count} model${count === 1 ? "" : "s"}`;

  return `${mark} ${row.displayName} · ${suffix}`;
}
