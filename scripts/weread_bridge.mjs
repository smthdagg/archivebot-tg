#!/usr/bin/env node
/**
 * weread-omni 桥接：Python 侧以子进程调用，stdout 输出 JSON（一行一事件），
 * stderr 只做诊断。协议见 app/archive/weread_client.py。
 *
 * 用法：
 *   node weread_bridge.mjs login [alias]        # 事件流：qr → status → done
 *   node weread_bridge.mjs status               # 校验登录态（拉 1 条订阅验证）
 *   node weread_bridge.mjs search <公众号名>     # 搜索 MP_WXS_<id>
 *   node weread_bridge.mjs subscribe <account_id>
 *   node weread_bridge.mjs unsubscribe <account_id>
 *   node weread_bridge.mjs articles <account_id> [--synckey N] [--count N]
 *
 * 凭据存储由 weread-omni 负责（WEREAD_CONFIG_DIR 重定向），本脚本不碰令牌值。
 * 错误：exit 2，stdout 末行 {"error":{"message","code","status"}}；code 取上游
 * errCode（-2012=登录超时需重扫码，-2041=需人工验证）。
 */

const WEREAD_ALIAS = process.env.WEREAD_ACCOUNT || "default";

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function emitError(err) {
  const code = err?.errCode ?? err?.code;
  emit({
    error: {
      message: String(err?.message ?? err),
      code: typeof code === "number" ? code : undefined,
      status: err?.status,
    },
  });
  process.exit(2);
}

/**
 * 加载 weread-omni。裸包名 import 只沿本脚本目录链解析（覆盖不到 npm -g 全局
 * 目录），因此先试裸名（仓库本地 node_modules），再试显式路径：
 * WEREAD_OMNI_PATH 覆盖 → 常见全局目录。包为纯 ESM（type: module）。
 */
async function loadWereadOmni() {
  try {
    return await import("weread-omni");
  } catch {
    /* 继续尝试显式路径 */
  }
  const candidates = [
    process.env.WEREAD_OMNI_PATH,
    "/usr/local/lib/node_modules/weread-omni", // npm install -g 默认（Docker/常见发行版）
    "/usr/lib/node_modules/weread-omni",       // Debian nodejs 包全局目录
  ].filter(Boolean);
  for (const dir of candidates) {
    for (const entry of ["dist/index.js", "index.js"]) {
      try {
        return await import(`file://${dir}/${entry}`);
      } catch {
        /* 下一个候选 */
      }
    }
  }
  throw new Error(
    "weread-omni not installed (npm install -g weread-omni, requires Node >= 22.13)"
  );
}

async function openCanonical() {
  const { AccountManager } = await loadWereadOmni();
  const manager = new AccountManager({ env: process.env });
  const opened = await manager.open();
  return opened;
}

async function opLogin() {
  const { AccountManager } = await loadWereadOmni();
  const manager = new AccountManager({ env: process.env });
  const deadline = AbortSignal.timeout(5 * 60 * 1000); // 上游扫码总 deadline 5 分钟
  try {
    const result = await manager.login(WEREAD_ALIAS, {
      signal: deadline,
      onQr: (url) => emit({ event: "qr", url }),
      onStatus: (text) => emit({ event: "status", text }),
    });
    emit({
      event: "done",
      account: result.account,
      vid: result.vid ?? null,
    });
  } catch (err) {
    emitError(err);
  }
}

async function opStatus() {
  try {
    const opened = await openCanonical();
    const subs = await opened.canonical.publicAccounts.subscriptions({ count: 1 });
    emit({
      ok: true,
      account: opened.account,
      vid: opened.identity?.vid ?? null,
      subscription_count: subs.totalCount ?? null,
    });
  } catch (err) {
    emitError(err);
  }
}

async function opSearch(name) {
  try {
    const opened = await openCanonical();
    const res = await opened.canonical.search.books(name, { scope: 2, count: 8 });
    const results = (res.books || [])
      .map((b) => b.bookInfo || {})
      .filter((b) => typeof b.bookId === "string" && b.bookId.startsWith("MP_WXS_"))
      .map((b) => ({
        account_id: b.bookId,
        title: b.title ?? "",
        author: b.author ?? "",
        cover: b.cover ?? "",
      }));
    emit({ results });
  } catch (err) {
    emitError(err);
  }
}

async function opArticles(accountId, opts) {
  try {
    const opened = await openCanonical();
    const page = await opened.canonical.publicAccounts.articles(accountId, opts);
    const articles = (page.articles || [])
      .map((a) => {
        const mp = a.mpInfo || {};
        return {
          review_id: a.reviewId ?? null,
          title: a.title ?? mp.title ?? "",
          doc_url: mp.doc_url ?? "",
          article_time: mp.time ?? a.createTime ?? 0,
          mp_name: mp.mp_name ?? "",
          pay_type: mp.payType ?? 0,
          pic_url: mp.pic_url ?? "",
        };
      })
      .filter((a) => !!a.doc_url);
    emit({
      account_id: page.accountId ?? accountId,
      synckey: page.synckey ?? null,
      has_more: page.hasMore ?? 0,
      articles,
    });
  } catch (err) {
    emitError(err);
  }
}

async function opSubscribe(accountId, unsubscribe) {
  try {
    const opened = await openCanonical();
    if (unsubscribe) {
      await opened.canonical.publicAccounts.unsubscribe(accountId);
    } else {
      await opened.canonical.publicAccounts.subscribe(accountId);
    }
    emit({ ok: true });
  } catch (err) {
    emitError(err);
  }
}

async function main() {
  const op = process.argv[2];
  if (op === "login") {
    await opLogin();
    return;
  }
  if (op === "status") {
    await opStatus();
    return;
  }
  const accountId = process.argv[3] || "";
  if (op === "search") {
    const name = accountId.trim();
    if (!name) {
      emitError(new Error("search requires a public account name"));
      return;
    }
    await opSearch(name);
    return;
  }
  if (!accountId) {
    emitError(new Error(`${op} requires account_id`));
    return;
  }
  if (op === "subscribe") {
    await opSubscribe(accountId, false);
    return;
  }
  if (op === "unsubscribe") {
    await opSubscribe(accountId, true);
    return;
  }
  if (op === "articles") {
    const opts = { count: 20 };
    for (let i = 4; i < process.argv.length; i++) {
      const arg = process.argv[i];
      if (arg === "--synckey") opts.synckey = Number(process.argv[++i]);
      else if (arg === "--count") opts.count = Number(process.argv[++i]);
    }
    await opArticles(accountId, opts);
    return;
  }
  emitError(new Error(`unknown op: ${op}`));
}

main().catch(emitError);
