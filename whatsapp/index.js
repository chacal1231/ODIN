// ODIN's link to one WhatsApp account (whatsapp-web.js): pairing by QR, the account's groups, and sending to the NOC group.
// It only listens inside the Compose network and answers requests carrying a token derived from ODIN's master key;
// ODIN decides what to send and where. The session is kept in /data, so a restart does not ask for the QR again.
// WhatsApp Web automation is not an official API: use a number dedicated to ODIN.
'use strict';
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const { Client, LocalAuth } = require('whatsapp-web.js');

const PORT = 3000;
const LIMIT = 64 * 1024;
const RETRY_MS = 15000;
const key = fs.readFileSync(process.env.ODIN_MASTER_KEY_FILE || '/run/secrets/master.key').toString().trim();
// Same derivation as odin/noc/whatsapp.py: the master key itself never travels.
const token = Buffer.from(crypto.createHmac('sha256', key).update('odin-whatsapp').digest('hex'));

const state = { status: 'starting', qr: null, me: null, error: null, since: Date.now() };
let client = null;

function set(status, extra = {}) {
  Object.assign(state, { status, since: Date.now(), error: null }, extra);
  console.log(JSON.stringify({ status, error: state.error }));
}

function start() {
  // A recreated container has another hostname, so Chromium takes the previous one's profile locks as in use.
  for (const lock of ['SingletonLock', 'SingletonSocket', 'SingletonCookie']) fs.rmSync(`/data/session/${lock}`, { force: true });
  client = new Client({
    authStrategy: new LocalAuth({ dataPath: '/data' }),
    // Its default is ./.wwebjs_cache in /app, which this user cannot write: the failure came after 'authenticated'
    // and before 'ready', silently.
    webVersionCache: { type: 'local', path: '/data/wwebjs_cache' },
    puppeteer: { executablePath: process.env.CHROMIUM || '/usr/bin/chromium', args: ['--no-sandbox', '--disable-dev-shm-usage'] },
  });
  client.on('qr', (qr) => set('qr', { qr }));
  client.on('authenticated', () => set('authenticated', { qr: null }));
  client.on('ready', () => set('ready', { qr: null, me: { number: client.info.wid.user, name: client.info.pushname || '' } }));
  client.on('auth_failure', (message) => set('auth_failure', { error: String(message).slice(0, 200) }));
  client.on('disconnected', (reason) => {
    set('disconnected', { error: String(reason).slice(0, 200), me: null });
    restart();
  });
  client.initialize().catch((error) => {
    set('failed', { error: String(error && error.message).slice(0, 200) });
    restart();
  });
}

function restart() {
  const old = client;
  client = null;
  setTimeout(async () => {
    try { if (old) await old.destroy(); } catch (_) { /* the browser may already be gone */ }
    start();
  }, RETRY_MS);
}

function authorized(req) {
  const given = Buffer.from(String(req.headers.authorization || '').replace(/^Bearer /, ''));
  return given.length === token.length && crypto.timingSafeEqual(given, token);
}

function body(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > LIMIT) { reject(new Error('too_large')); req.destroy(); } else chunks.push(chunk);
    });
    req.on('end', () => { try { resolve(chunks.length ? JSON.parse(Buffer.concat(chunks)) : {}); } catch (e) { reject(e); } });
  });
}

function reply(res, code, data) {
  res.writeHead(code, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(data));
}

function ready() {
  if (state.status !== 'ready' || !client) throw Object.assign(new Error('not_ready'), { code: 409 });
  return client;
}

const routes = {
  'GET /status': async () => state,
  // Read straight from WhatsApp Web's chat collection: getChats() builds a model per chat and, with this WhatsApp Web,
  // fails on the @lid individual chats (an IndexedDB DataError surfacing as "r"), taking the whole list down.
  'GET /groups': async () => (await ready().pupPage.evaluate(() => window.require('WAWebCollections').Chat.getModelsArray()
    .filter((c) => c.id.server === 'g.us')
    .map((c) => ({ id: c.id._serialized, name: c.formattedTitle || c.name || '',
      participants: ((c.groupMetadata && c.groupMetadata.participants) || []).length }))))
    .sort((a, b) => a.name.localeCompare(b.name)),
  'POST /send': async (req) => {
    const { chat_id: chat, text, quoted } = await body(req);
    if (typeof chat !== 'string' || !chat.endsWith('@g.us') || typeof text !== 'string' || !text) {
      throw Object.assign(new Error('invalid'), { code: 422 });
    }
    const options = quoted ? { quotedMessageId: String(quoted) } : {};
    const message = await ready().sendMessage(chat, text.slice(0, 4000), options);
    return { id: message.id._serialized };
  },
  'POST /logout': async () => {
    await ready().logout();
    set('logged_out', { me: null });
    restart();
    return { ok: true };
  },
};

http.createServer(async (req, res) => {
  if (!authorized(req)) return reply(res, 401, { error: 'unauthorized' });
  const route = routes[`${req.method} ${req.url}`];
  if (!route) return reply(res, 404, { error: 'not_found' });
  try {
    reply(res, 200, await route(req));
  } catch (error) {
    reply(res, error.code || 500, { error: String(error.message).slice(0, 200) });
  }
}).listen(PORT, () => start());
