"use strict";
// Runs the Ctrl+K command parser (glowstage/ui/commands.js) for tests/test_commands.py:
// reads {ctx, inputs} as JSON on stdin, prints [[input, [{label, action}, ...]], ...] as JSON.
const path = require("path");
const Commands = require(path.join(__dirname, "..", "glowstage", "ui", "commands.js"));
let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", c => { raw += c; });
process.stdin.on("end", () => {
  const { ctx, inputs } = JSON.parse(raw);
  process.stdout.write(JSON.stringify(inputs.map(text => [text, Commands.parse(text, ctx)])));
});
