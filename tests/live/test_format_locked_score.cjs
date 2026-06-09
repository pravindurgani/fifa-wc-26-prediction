// A.6: tests for dashboard/app.js formatLockedScore().
//
// We pull the function out of app.js via regex + eval rather than
// duplicate the source. That way the test stays in sync with the
// implementation automatically — if someone changes the function,
// these tests run against the new version.
//
// Run from repo root:
//   node tests/live/test_format_locked_score.cjs

const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const APP_JS = path.join(REPO_ROOT, 'dashboard', 'app.js');

const src = fs.readFileSync(APP_JS, 'utf-8');
const match = src.match(/function formatLockedScore\([^)]*\)\s*\{[\s\S]*?\n\}/);
if (!match) {
  console.error('FAIL: formatLockedScore not found in dashboard/app.js');
  process.exit(2);
}
// eval the function source so it lands in this CJS file's scope.
eval(match[0]);

const cases = [
  // [input, expected, label]
  [null,           '', 'null → empty string'],
  [undefined,      '', 'undefined → empty string'],
  ['',             '', 'empty string → empty string'],
  ['2-1',          '2-1', 'legacy string passes through unchanged'],
  [{home_score: 2, away_score: 1, status: 'FT'},  '2-1', 'plain FT → "2-1"'],
  [{home_score: 2, away_score: 1, status: 'AET'}, '2-1 AET', 'AET → "2-1 AET"'],
  [{home_score: 0, away_score: 0, status: 'PEN', home_pens: 3, away_pens: 0},
   '0-0 (3-0 pens)', 'PEN scoreless reg → "0-0 (3-0 pens)"'],
  [{home_score: 1, away_score: 1, status: 'PEN', home_pens: 4, away_pens: 5},
   '1-1 (4-5 pens)', 'PEN with regulation goals → "1-1 (4-5 pens)"'],
  [{home_score: 1, away_score: 1, status: 'PEN'},
   '1-1', 'PEN missing sub-scores → falls back to base (no fabrication)'],
  [{home_score: null, away_score: 2},
   '', 'missing home_score → empty (defensive)'],
  [{home_score: 0, away_score: 0},
   '0-0', 'no status defaults to base'],
  [{home_score: 5, away_score: 0, status: 'ft'},
   '5-0', 'lowercase status still works (case-insensitive)'],
];

let pass = 0, fail = 0;
for (const [input, expected, label] of cases) {
  const got = formatLockedScore(input);
  const ok = got === expected;
  if (ok) {
    pass++;
    console.log(`  ✓ ${label}`);
  } else {
    fail++;
    console.log(`  ✗ ${label}`);
    console.log(`      input:    ${JSON.stringify(input)}`);
    console.log(`      expected: ${JSON.stringify(expected)}`);
    console.log(`      got:      ${JSON.stringify(got)}`);
  }
}

console.log(`\n  ${pass}/${pass + fail} passed`);
process.exit(fail ? 1 : 0);
