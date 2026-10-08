import * as npmSemver from "semver";

export function gt(a: string, b: string): boolean {
  if (typeof Bun !== "undefined") {
    return Bun.semver.order(a, b) === 1;
  }

  return npmSemver.gt(a, b, { loose: true });
}

export function gte(a: string, b: string): boolean {
  if (typeof Bun !== "undefined") {
    return Bun.semver.order(a, b) >= 0;
  }

  return npmSemver.gte(a, b, { loose: true });
}

export function lt(a: string, b: string): boolean {
  if (typeof Bun !== "undefined") {
    return Bun.semver.order(a, b) === -1;
  }

  return npmSemver.lt(a, b, { loose: true });
}

export function lte(a: string, b: string): boolean {
  if (typeof Bun !== "undefined") {
    return Bun.semver.order(a, b) <= 0;
  }

  return npmSemver.lte(a, b, { loose: true });
}

export function satisfies(version: string, range: string): boolean {
  if (typeof Bun !== "undefined") {
    return Bun.semver.satisfies(version, range);
  }

  return npmSemver.satisfies(version, range, { loose: true });
}

export function order(a: string, b: string): -1 | 0 | 1 {
  if (typeof Bun !== "undefined") {
    return Bun.semver.order(a, b);
  }

  return npmSemver.compare(a, b, { loose: true }) as -1 | 0 | 1;
}
