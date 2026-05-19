/*
 * dismiss-action — Netlify serverless function for snoozing Today's Actions rows.
 *
 * Setup (Netlify → Site settings → Environment variables):
 *   GH_CONTENTS_PAT  Fine-grained GitHub PAT with:
 *                      • Repository: stevegerrard100/portfolio-analyst
 *                      • Permission: Contents → Read and write
 *
 * Set NETLIFY_DISMISS_URL in analyse.yml (and any local .env) so the pipeline
 * embeds the correct endpoint URL into the rendered dashboard:
 *   NETLIFY_DISMISS_URL=https://<your-site>.netlify.app/.netlify/functions/dismiss-action
 *
 * Accepts: POST { id: string, days: number, snoozed_price?: number }
 * Returns: 200 { ok: true, id, snoozed_until }
 */

const REPO_API =
  'https://api.github.com/repos/stevegerrard100/portfolio-analyst/contents/cache/dismissed_actions.json';

const CORS = {
  'Access-Control-Allow-Origin':  '*',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

function json(statusCode, body) {
  return { statusCode, headers: { ...CORS, 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

exports.handler = async (event) => {
  if (event.httpMethod === 'OPTIONS') {
    return { statusCode: 204, headers: CORS, body: '' };
  }
  if (event.httpMethod !== 'POST') {
    return json(405, { error: 'Method not allowed' });
  }

  const pat = process.env.GH_CONTENTS_PAT;
  if (!pat) {
    return json(500, { error: 'GH_CONTENTS_PAT not configured' });
  }

  let body;
  try {
    body = JSON.parse(event.body || '{}');
  } catch {
    return json(400, { error: 'Invalid JSON body' });
  }

  const { id, days, snoozed_price } = body;
  if (!id || days === undefined || days === null) {
    return json(400, { error: 'Missing required fields: id, days' });
  }

  const snoozeUntil = days === 0
    ? '9999-12-31'
    : new Date(Date.now() + days * 86400000).toISOString().split('T')[0];
  const snoozedAt = new Date().toISOString().split('T')[0];

  const ghHeaders = {
    Authorization:  `Bearer ${pat}`,
    Accept:         'application/vnd.github+json',
    'Content-Type': 'application/json',
    'User-Agent':   'portfolio-analyst-dismiss',
  };

  // GET current file (404 is fine on first dismiss)
  let dismissed = [], sha = null;
  const getRes = await fetch(REPO_API, { headers: ghHeaders });
  if (getRes.ok) {
    const file = await getRes.json();
    sha = file.sha;
    dismissed = JSON.parse(Buffer.from(file.content.replace(/\s/g, ''), 'base64').toString('utf8'));
  } else if (getRes.status !== 404) {
    return json(502, { error: `GitHub GET failed: ${getRes.status}` });
  }

  // Upsert entry
  dismissed = dismissed.filter(d => d.id !== id);
  const entry = { id, snoozed_until: snoozeUntil, snoozed_at: snoozedAt };
  if (snoozed_price != null) entry.snoozed_price = snoozed_price;
  dismissed.push(entry);

  // PUT updated file
  const putBody = {
    message: `Snooze action ${id} until ${snoozeUntil} [skip ci]`,
    content: Buffer.from(JSON.stringify(dismissed, null, 2)).toString('base64'),
  };
  if (sha) putBody.sha = sha;

  const putRes = await fetch(REPO_API, { method: 'PUT', headers: ghHeaders, body: JSON.stringify(putBody) });
  if (!putRes.ok) {
    return json(502, { error: `GitHub PUT failed: ${putRes.status}` });
  }

  return json(200, { ok: true, id, snoozed_until: snoozeUntil });
};
