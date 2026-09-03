# Cookie Profile（Phase 2：登录类网站）

> 状态：**已实现（2026-09-02）**。仓库：`app/archive/cookie_profile.py`、
> `app/config.py`（`cookie_profiles` / `cookie_profiles_file`）、
> `app/archive/fetcher.py:fetch_article`、`app/archive/runner.py:run_archive`、
> `app/tasks/jobs.py`、`app/database/models.py:Task.cookie_profile`、
> `migrations/versions/5c2e97f0a1b8_add_cookie_profile_to_tasks.py`。
> 测试：`tests/test_cookie_profile.py`（19 用例）。

## 目的与红线

部分平台（微信、小红书、知乎等）对未登录访问限制内容，用户希望归档**自己登录后
可见的内容**。Cookie Profile 允许管理员为这些平台配置**用户自备的 cookie**，任务
指定 profile 后，归档管道在调用 ArchiveBOT 服务前按平台注入这些 cookie。

**规格红线（不违反）**：
- **不绕过付费墙 / 访问控制**。profile 只用于**用户自己登录过的网站**（平台要求登录
  才能访问的公开页/个人可见页），绝不用于付费/订阅墙或任何本不应可访问的资源。
- 系统**只**在任务显式指定 `cookie_profile` 时才注入，**从不**自动为任意 URL 附加 cookie。
- cookie 属于敏感凭据：存于管理员控制的配置（env / 配置文件），不入库明文日志。

## 配置格式

两种来源（均可选，若都提供则**按 profile 名合并且文件优先**）：

1. `COOKIE_PROFILES`（`COOKIE_PROFILES` 环境变量，JSON 字符串）
2. `COOKIE_PROFILES_FILE`（环境变量，指向 JSON 配置文件；文件不存在或非法 JSON → 配置加载失败）

结构（Cookie-Editor 格式，每个平台一个 cookie 列表）：

```json
{
  "<profile 名>": {
    "<平台>": [
      { "name": "...", "value": "...", "domain": ".example.com", "path": "/" }
    ]
  }
}
```

字段说明：
- `profile 名`：任意非空字符串，任务用它引用。一个 profile 可为多个平台各配 cookies。
- `<平台>`：`wechat` / `xhs` / `reddit` / `zhihu`（支持注入的平台，见下）。
- 每条 cookie：`name`、`value` 必填，`domain`、`path` 可选（缺省补默认域名：
  wechat→`.mp.weixin.qq.com`、xhs→`.xiaohongshu.com`、reddit→`.reddit.com`、
  zhihu→`.zhihu.com`；`path` 缺省 `/`）。缺 name/value 的项会被丢弃并记日志。

## 支持的平台与注入策略

ArchiveBOT 各服务消费 cookie 的方式不同，本仓库用「包装 / 猴子补丁」注入（不改
vendor 源码，思路同 `app/archive/ssrf_guard.py`）：

| 平台 | 策略 | ArchiveBOT 消费点 | 是否支持 |
|---|---|---|---|
| `wechat` | 文件型：把 cookie 写入临时文件，调用期间接管 `WechatService._COOKIES_PATH` | `wechat_service.save_article` → `_fetch_page_html` 读 `_COOKIES_PATH` | ✅ |
| `twitter` | 方法型特殊（`auth_token`/`ct0`）：提取 profile 中这两条，调用期间挂到 `TwitterService._twitter_auth_pair`，fetcher 据此传给构造器 `xreach_auth_token/ct0`，由 `playwright_scraper` 注入浏览器 `add_cookies` | `twitter_service.get_tweet` → `playwright_scraper.TwitterPlaywrightScraper(context.add_cookies)` | ✅（需 profile 含登录态，否则落 `LOGIN_REQUIRED`） |
| `xhs` | 方法型：接管 `XHSService` `_get_cookies` | `xhs_service` cookie 读取 | ✅ |
| `reddit` | 文件型：接管 `RedditService._COOKIES_PATH` | `reddit_service.load_cookie_file` / `_credential_from_cookie_file` | ✅ |
| `zhihu` | 方法型：调用期间替换 `ZhihuService._get_cookies` | `zhihu_service._fetch_via_api` / `_fetch_content_async` | ✅ |
| `web` | — | `webpage_service.save_page` 无 cookie 读取 | ❌（忽略，记日志） |
| `weibo` | — | `weibo_service` 无 cookie 读取 | ❌（忽略，记日志） |

文件型注入会在服务调用结束后恢复原 `_COOKIES_PATH` 并删除临时 cookie 文件，避免
profile 间污染与凭据残留。

## 任务如何指定 profile

任务表新增 `tasks.cookie_profile`（可空，索引）。创建任务时传入 profile 名即可
（尚无 UI，保留接口与单测）：

```python
create_task(
    db, user_id=..., chat_id=..., url=..., platform="wechat",
    output_types=[...], cookie_profile="login1",
)
```

worker（`jobs._process`）会把 `task.cookie_profile` 原样透传给
`run_archive(cookie_profile=...)` → `fetch_article(cookie_profile=...)` → 注入。

## 行为细节

- 指定了 profile 但该平台在 profile 中没有 cookie：
  - 平台支持注入 → 记 warning，**不带 cookie 继续抓取**；
  - 平台不支持注入 → 记 info，忽略 profile。
- 指定了不存在的 profile → 抓取失败，`ErrorCode.UNKNOWN`（避免静默裸抓）。
- 未指定 profile → 完全不影响既有抓取行为（不注入）。

## 测试

`tests/test_cookie_profile.py` 覆盖：config 加载（env JSON / 文件 / 缺文件 /
结构非法 / 空默认）；`resolve_cookies`（未指定 None、未知 profile 报错、
平台无 cookie None、归一与默认域）；注入策略（wechat 文件型写入并恢复清理、
zhihu 方法型打补丁并恢复、web 不支持 no-op、空 cookies no-op）；平台支持清单；
`create_task` 落库 `cookie_profile`；`process_task` 把 `cookie_profile` 透传
`run_archive`。

## 职责边界与后续

- 本阶段只落地**配置 → 解析 → 注入 → 任务透传**，不做 UI、不校验 cookie 有效性。
- 后续（Phase 2/3）：在设置/管理中为任务选择 profile 的 UI；cookie 导出/导入；
  视频平台（bilibili/youtube/douyin/instagram 等）在对应服务接入抓取后的 cookie 支持。