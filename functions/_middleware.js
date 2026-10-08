// Cloudflare Pages Functions 中间件：给整个站点加访问密码（HTTP Basic 认证）。
// 密码不写在代码里：在 Cloudflare 项目 → 设置 → 变量和机密 里添加机密 SITE_PASSWORD。
// 用户名随便填（可留空），只校验密码。没有设置 SITE_PASSWORD 时一律拒绝访问（安全起见"默认关闭"）。

const enc = new TextEncoder();

async function sha256(s) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}

// 先各自哈希再逐字节比较，避免因长度/内容不同而泄露时间差
async function safeEqual(a, b) {
  const [ha, hb] = await Promise.all([sha256(a), sha256(b)]);
  let diff = 0;
  for (let i = 0; i < ha.length; i++) diff |= ha[i] ^ hb[i];
  return diff === 0;
}

function parseBasic(header) {
  if (!header || !header.startsWith("Basic ")) return null;
  try {
    const raw = atob(header.slice(6).trim());
    const bytes = Uint8Array.from(raw, (c) => c.charCodeAt(0));
    const text = new TextDecoder().decode(bytes);   // 支持中文密码
    const i = text.indexOf(":");
    return i < 0 ? null : { user: text.slice(0, i), pass: text.slice(i + 1) };
  } catch (e) {
    return null;
  }
}

export async function onRequest(context) {
  const { request, env, next } = context;

  if (!env.SITE_PASSWORD) {
    return new Response("站点尚未配置访问密码（缺少 SITE_PASSWORD）。", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
  }

  const cred = parseBasic(request.headers.get("Authorization"));
  if (cred && (await safeEqual(cred.pass, env.SITE_PASSWORD))) {
    const res = await next();
    const headers = new Headers(res.headers);
    headers.set("Cache-Control", "private, no-cache");   // 已验证的内容不让共享缓存保存
    headers.set("X-Robots-Tag", "noindex, nofollow");     // 不让搜索引擎收录
    return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
  }

  return new Response("需要密码才能访问。", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="Protected", charset="UTF-8"',
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}
