import { test } from "node:test";
import assert from "node:assert/strict";
import { deliveredFrom } from "../src/pay.mjs";

test("deliveredFrom needs a 2xx with a non-empty, non-error body", () => {
  assert.equal(deliveredFrom(200, '{"result":"0x1"}'), true);
  assert.equal(deliveredFrom(200, "plain text is fine"), true);
  assert.equal(deliveredFrom(200, ""), false);
  assert.equal(deliveredFrom(200, '{"error":"nope"}'), false);
  assert.equal(deliveredFrom(402, '{"ok":true}'), false);
  assert.equal(deliveredFrom(400, "x"), false);
});
