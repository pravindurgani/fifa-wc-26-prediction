// api/dispatch.ts — Vercel Cron handler that dispatches the GitHub Actions
// live-matchday workflow on `main`. Runs every 10 minutes; gates by UTC
// tournament window + hour window so quiet hours are cheap no-ops.
//
// Architecture:
//   Vercel Cron timer (vercel.json) → POST /api/dispatch (this file)
//     → CRON_SECRET auth check
//     → date + hour gate
//     → GitHub workflow_dispatch
//
// Why this exists: GitHub's own cron scheduler throttled the workflow's
// `*/10 * 10-30 6 *` schedule to zero ticks in 6+ hours. External
// dispatch via Vercel Cron is reliable.

export const config = { runtime: 'edge' };

const REPO = 'pravindurgani/fifa-wc-26-prediction';
const WORKFLOW = 'live-matchday.yml';
const REF = 'main';

// Inclusive UTC date windows for the WC 2026 tournament.
const WINDOWS: Array<[string, string]> = [
  ['2026-06-10', '2026-06-30'],
  ['2026-07-01', '2026-07-20'],
];

// Active hour window in UTC: 16:00 → 06:00 next day (covers all kickoffs
// 18:00 BST through final whistles at ~05:00 UTC on PT-zone games, plus
// safety margin for FT + provider lag).
const inHourWindow = (h: number) => h >= 16 || h < 6;

const inDateWindow = (iso: string) =>
  WINDOWS.some(([start, end]) => iso >= start && iso <= end);

export default async function handler(req: Request): Promise<Response> {
  const log = (o: Record<string, unknown>) =>
    console.log(JSON.stringify({ fn: 'dispatch', ...o }));

  // 1. Auth — Vercel Cron sends Authorization: Bearer <CRON_SECRET>.
  // Reject anything else so randos can't trigger workflow runs.
  const auth = req.headers.get('authorization') ?? '';
  const secret = process.env.CRON_SECRET ?? '';
  if (!secret || auth !== `Bearer ${secret}`) {
    log({ event: 'unauthorized' });
    return new Response('unauthorized', { status: 401 });
  }

  // 2. Date + hour gate. Cheap no-op outside the window so we don't
  // waste GitHub Actions minutes on quiet hours.
  const now = new Date();
  const iso = now.toISOString().slice(0, 10);
  const hour = now.getUTCHours();
  if (!inDateWindow(iso)) {
    log({ event: 'skipped', reason: 'outside_tournament', iso, hour });
    return Response.json({ ok: true, skipped: 'outside_tournament', iso });
  }
  if (!inHourWindow(hour)) {
    log({ event: 'skipped', reason: 'outside_match_hours', iso, hour });
    return Response.json({ ok: true, skipped: 'outside_match_hours', hour });
  }

  // 3. Dispatch the workflow_dispatch event.
  const token = process.env.GH_TOKEN;
  if (!token) {
    log({ event: 'misconfigured', reason: 'missing_gh_token' });
    return Response.json(
      { ok: false, error: 'missing_gh_token' },
      { status: 500 }
    );
  }

  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'vercel-cron-wc26-dispatch',
      },
      body: JSON.stringify({ ref: REF }),
    });
  } catch (err) {
    log({ event: 'fetch_error', error: String(err) });
    return Response.json(
      { ok: false, error: 'fetch_error' },
      { status: 502 }
    );
  }

  if (!res.ok) {
    const body = await res.text();
    log({
      event: 'dispatch_failed',
      status: res.status,
      body: body.slice(0, 200),
    });
    return Response.json(
      { ok: false, status: res.status },
      { status: 502 }
    );
  }

  log({ event: 'dispatched', iso, hour });
  return Response.json({ ok: true, dispatched: true, iso, hour });
}
