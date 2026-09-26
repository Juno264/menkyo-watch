// 東京リージョンから予約サイトへ中継するだけの関数（予約サイトのドメイン以外は拒否）
const ALLOWED = /(^|\.)tokyo-madoguchi-yoyaku\.com$/;
const DROP_REQ = new Set(["host", "content-length", "accept-encoding", "connection", "x-proxy-token"]);
const DROP_RES = new Set(["content-encoding", "content-length", "transfer-encoding", "connection", "set-cookie"]);

export default async function handler(req, res) {
  try {
    if ((req.headers["x-proxy-token"] || "") !== process.env.PROXY_TOKEN || !process.env.PROXY_TOKEN) {
      return res.status(401).json({ error: "bad token" });
    }
    if (req.method === "GET") { // 疎通確認用
      const r = await fetch("https://ipinfo.io/json").then(r => r.json()).catch(() => ({}));
      return res.status(200).json({ ok: true, region: process.env.VERCEL_REGION, ip: r.ip, country: r.country });
    }
    const { url, method = "GET", headers = {}, body_b64 = null } = req.body || {};
    const u = new URL(url);
    if (u.protocol !== "https:" || !ALLOWED.test(u.hostname)) return res.status(403).json({ error: "host not allowed" });
    const h = {};
    for (const [k, v] of Object.entries(headers)) if (!DROP_REQ.has(k.toLowerCase())) h[k] = v;
    const r = await fetch(url, {
      method, headers: h, redirect: "manual",
      body: body_b64 && !["GET", "HEAD"].includes(method) ? Buffer.from(body_b64, "base64") : undefined,
    });
    const outHeaders = {};
    r.headers.forEach((v, k) => { if (!DROP_RES.has(k)) outHeaders[k] = v; });
    const setCookie = typeof r.headers.getSetCookie === "function" ? r.headers.getSetCookie() : [];
    const buf = Buffer.from(await r.arrayBuffer());
    res.status(200).json({ status: r.status, headers: outHeaders, set_cookie: setCookie, body_b64: buf.toString("base64") });
  } catch (e) {
    res.status(502).json({ error: String(e).slice(0, 300) });
  }
}
