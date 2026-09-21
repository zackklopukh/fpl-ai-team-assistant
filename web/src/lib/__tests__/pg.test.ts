import { describe, expect, it } from "vitest";

import { isConfigured, webConnectionString } from "../pg";

const SESSION =
  "postgresql://postgres.abc:p%23ss@aws-0-us-west-2.pooler.supabase.com:5432/postgres?sslmode=require";

describe("webConnectionString", () => {
  it("moves a Supabase pooler address from the session port to the transaction port", () => {
    // Session mode pins a database connection per client for as long as the
    // client stays connected, and allows 15 in total. Serverless instances keep
    // their sockets while frozen, so two warm instances filled it.
    const url = new URL(webConnectionString(SESSION));
    expect(url.hostname).toBe("aws-0-us-west-2.pooler.supabase.com");
    expect(url.port).toBe("6543");
  });

  it("drops sslmode, which node-postgres would otherwise let override its TLS config", () => {
    expect(new URL(webConnectionString(SESSION)).searchParams.has("sslmode")).toBe(false);
  });

  it("keeps the credentials exactly, including an encoded character", () => {
    const url = new URL(webConnectionString(SESSION));
    expect(url.username).toBe("postgres.abc");
    expect(url.password).toBe("p%23ss");
  });

  it("leaves anything that is not the Supabase session pooler alone", () => {
    const local = "postgresql://me:pw@localhost:5432/fpl";
    expect(webConnectionString(local)).toBe(local);
    const already = "postgresql://u:p@aws-0-us-west-2.pooler.supabase.com:6543/postgres";
    expect(new URL(webConnectionString(already)).port).toBe("6543");
  });
});

describe("isConfigured", () => {
  it("treats the .env.example placeholder as unconfigured, not as a bad password", () => {
    expect(isConfigured("postgresql://postgres:REPLACE_WITH_PASSWORD@host/db")).toBe(false);
    expect(isConfigured(undefined)).toBe(false);
    expect(isConfigured(SESSION)).toBe(true);
  });
});
