import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const appSource = readFileSync(
  new URL("../interactive_pdf_app/static/app.js", import.meta.url),
  "utf8",
);
const lifecycleStart = appSource.indexOf("  function wakeJobPolling()");
const lifecycleEnd = appSource.indexOf("\n  function handleJobPayload", lifecycleStart);

assert.notEqual(lifecycleStart, -1, "polling lifecycle start was not found in app.js");
assert.notEqual(lifecycleEnd, -1, "polling lifecycle end was not found in app.js");

const lifecycleSource = appSource.slice(lifecycleStart, lifecycleEnd);

function createHarness({ hidden = false } = {}) {
  let fetchCount = 0;
  let nextTimerId = 1;
  const timers = new Map();
  const state = {
    jobId: "job-1",
    pollSequence: 1,
    activePollSequence: null,
    pollWakeResolvers: new Set(),
    pollingFinished: false,
    appClosed: false,
    connectionUnavailable: false,
    lastPayload: null,
  };
  const context = vm.createContext({
    POLL_DELAY_MS: 850,
    document: { hidden },
    friendlyError: (error) => error?.message || String(error),
    friendlyHttpError: (status) => `HTTP ${status}`,
    getPayloadMessage: () => "",
    handleJobPayload: () => false,
    isNetworkFailure: (error) => error instanceof TypeError,
    readResponse: async (response) => response.payload,
    showFailure: (message) => {
      throw new Error(`Unexpected polling failure: ${message}`);
    },
    state,
    window: {
      clearTimeout: (timerId) => timers.delete(timerId),
      setTimeout: (callback) => {
        const timerId = nextTimerId++;
        timers.set(timerId, callback);
        return timerId;
      },
    },
    apiFetch: async () => {
      fetchCount += 1;
      return { ok: true, status: 200, payload: { status: "RUNNING" } };
    },
  });

  vm.runInContext(
    `${lifecycleSource}\nthis.polling = { pollJob, resumeActiveJobPolling, wakeJobPolling };`,
    context,
  );

  return {
    context,
    fetchCount: () => fetchCount,
    polling: context.polling,
    state,
    stop() {
      state.pollSequence += 1;
      context.polling.wakeJobPolling();
    },
  };
}

async function settle() {
  for (let index = 0; index < 5; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

test("a hidden job pauses and one loop polls immediately when visible", async () => {
  const harness = createHarness({ hidden: true });
  const originalLoop = harness.polling.pollJob(1);
  await settle();
  assert.equal(harness.fetchCount(), 0);

  harness.context.document.hidden = false;
  harness.polling.resumeActiveJobPolling();
  harness.polling.resumeActiveJobPolling();
  await settle();
  assert.equal(harness.fetchCount(), 1);

  harness.stop();
  await originalLoop;
});

test("hiding during the poll delay stops requests until visibility resumes", async () => {
  const harness = createHarness();
  const loop = harness.polling.pollJob(1);
  await settle();
  assert.equal(harness.fetchCount(), 1);

  harness.context.document.hidden = true;
  harness.polling.wakeJobPolling();
  await settle();
  assert.equal(harness.fetchCount(), 1);

  harness.context.document.hidden = false;
  harness.polling.resumeActiveJobPolling();
  harness.polling.resumeActiveJobPolling();
  await settle();
  assert.equal(harness.fetchCount(), 2);

  harness.stop();
  await loop;
});

test("connection restoration creates exactly one replacement loop", async () => {
  const harness = createHarness();
  const disconnectedLoop = harness.polling.pollJob(1);
  await settle();
  assert.equal(harness.fetchCount(), 1);

  harness.state.connectionUnavailable = true;
  harness.state.pollSequence += 1;
  harness.polling.wakeJobPolling();
  await disconnectedLoop;
  assert.equal(harness.state.activePollSequence, null);

  harness.state.connectionUnavailable = false;
  harness.polling.resumeActiveJobPolling();
  harness.polling.resumeActiveJobPolling();
  await settle();
  assert.equal(harness.fetchCount(), 2);

  harness.stop();
  await settle();
});

test("terminal jobs are not restarted by lifecycle events", async () => {
  const harness = createHarness();
  harness.state.pollingFinished = true;
  harness.polling.resumeActiveJobPolling();
  await settle();
  assert.equal(harness.fetchCount(), 0);
});

test("visibility and pageshow handlers both resume active polling", () => {
  assert.match(
    appSource,
    /document\.addEventListener\("visibilitychange",[\s\S]*?resumeActiveJobPolling\(\)/,
  );
  assert.match(
    appSource,
    /window\.addEventListener\("pageshow",[\s\S]*?resumeActiveJobPolling\(\)/,
  );
});
