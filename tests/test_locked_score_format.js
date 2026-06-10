#!/usr/bin/env node
/**
 * Regression test: locked_score rendering must NEVER produce "[object Object]".
 *
 * The simulator writes locked_score as { home_score, away_score } (see
 * scripts/03_simulate.py:598). Legacy mock fixtures sometimes pass a string.
 * Both must format cleanly; unknown shapes must render nothing.
 *
 * Wired into 09_validate.py as a Node-based check.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '..', 'dashboard', 'app.js');
const src = fs.readFileSync(APP_JS, 'utf8');

// Extract the formatLockedScore definition so we don't pull in DOM globals.
const match = src.match(
  /const formatLockedScore = ls => \{[\s\S]*?return ''; \/\/ unknown shape: render nothing, don't leak \[object Object\]\s*\};/
);
if (!match) {
  console.error('FAIL: formatLockedScore not found in dashboard/app.js — did the helper get removed?');
  process.exit(2);
}

const body = match[0]
  .replace(/^const formatLockedScore = /, '')
  .replace(/;\s*$/, '');
const formatLockedScore = new Function('return (' + body + ')')();

const cases = [
  // [input, expected, label]
  [{ home_score: 2, away_score: 1 }, '2–1', 'dict shape (simulator real)'],
  [{ home_score: 0, away_score: 0 }, '0–0', 'dict 0-0 goalless'],
  [{ home_score: 3, away_score: 2 }, '3–2', 'dict 3-2'],
  ['2-1', '2–1', 'legacy string hyphen → en-dash'],
  ['0–0', '0–0', 'en-dash string passthrough'],
  [null, '', 'null → empty'],
  [undefined, '', 'undefined → empty'],
  ['', '', 'empty string → empty'],
  [{ home_score: 1 }, '', 'incomplete dict (no away_score) → empty (no leak)'],
  [{}, '', 'empty dict → empty'],
  [{ junk: 'x' }, '', 'wrong-shape dict → empty (no leak)'],
];

let pass = 0;
let fail = 0;
for (const [input, expected, label] of cases) {
  const got = formatLockedScore(input);
  // Critical: must NEVER emit "[object Object]"
  if (typeof got === 'string' && got.includes('[object Object]')) {
    console.error('  ✗ CRITICAL', label, '→ leaked [object Object]');
    fail++;
    continue;
  }
  if (got === expected) {
    console.log('  ✓', label, '→', JSON.stringify(got));
    pass++;
  } else {
    console.error('  ✗', label, 'expected', JSON.stringify(expected), 'got', JSON.stringify(got));
    fail++;
  }
}

console.log('---');
console.log(`formatLockedScore: ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
