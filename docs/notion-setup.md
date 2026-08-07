# What you need before installing

[한국어](notion-setup.ko.md) · [README](../README.md)

## At a glance

| Requirement | Required | Notes |
|---|---|---|
| macOS | ✅ | Uses `launchd` and `caffeinate`. Linux and Windows are not supported |
| Python 3.11+ | ✅ | For `tomllib`. **No third-party packages at all** |
| Claude Code CLI, logged in | ✅ | It writes the report. No extra cost on a subscription plan |
| git | ✅ | Ships with macOS |
| A Notion account and workspace | ✅ | You need to be the workspace **owner** to create a connection |
| A Notion internal connection token | ✅ | Procedure below |
| One parent page | ✅ | Where the report database will live |
| Your git email addresses | ✅ | Without them, no commits are counted at all |
| Codex CLI | — | Collected automatically if present, silently skipped if not |

Check versions:

```bash
sw_vers -productVersion      # macOS
python3 --version            # 3.11 or newer
claude --version             # installed?
claude auth status           # logged in? if this fails, run `claude` and log in
```

---

## Issuing the Notion token

### Why an "internal connection"

Notion has three kinds of token and only one of them suits an unattended job.

| Kind | Expiry | Reach | Verdict |
|---|---|---|---|
| **Internal connection** | **None** | Only pages explicitly shared with it | ✅ **use this** |
| Personal access token (PAT) | 7 days – 1 year | **Everything** its creator can see | ❌ expires, and the job stops silently one day |
| Public connection (OAuth) | Needs refresh | Whatever was authorised | ❌ requires a browser, so it cannot run unattended |

An internal connection acts as a **bot user**, not as you. Access survives its
creator leaving the workspace, and it can see nothing outside the pages shared
with it.

### Steps

**Order matters.** The connection has to exist before a page can be attached to it.

```
①  create the connection      (the token comes from here)
        ↓
②  create the parent page
        ↓
③  attach the connection to that page
```

#### ① Create the connection

1. Log in to Notion **in the workspace where the reports should live**.
2. Go to `app.notion.com/developers/connections`.
3. Left sidebar: **Build → Internal connections**.
4. **Create a new connection** — name it (e.g. `daily-report`) and **pick the
   correct workspace**.
5. On the **Configuration** tab, copy the **Installation access token**. You can
   also get it from the connection list: `•••` → *Retrieve an internal API token*.

> If that menu is not there, the account may not be allowed to create
> connections. The official documentation states this requires a workspace owner.

The token is a long string starting with `ntn_` or `secret_`.

#### ② Create the parent page

Make an empty page in the workspace. Any name will do ("Dev journal", say).

**Why a dedicated page:** access is inherited by child pages. Attaching the
connection to a large existing page exposes everything under it. A fresh page
confines the exposure to what the job itself creates.

#### ③ Attach the connection to the page

1. Open the page.
2. Top right: **`•••`**.
3. At the **bottom** of the popup: **Add connections**.
4. Search for the connection from step ① and select it.
5. Confirm when it warns that child pages are included.

You will know it worked when the connection's name appears in the `•••` menu.

> **This is not the `Share` button.** `Share` invites people; `Add connections`
> attaches integrations. Older versions of Notion put it inside the share
> dialog, but it now lives under `•••`.

If you cannot find it, the developer portal does the same thing: open the
connection → **Content access** tab → **Edit access** → pick the page.

#### ④ Copy the page URL

Copy the page link. It looks like `https://app.notion.com/…`, and
`setup_notion_db.py` extracts the page ID from it.

---

## The values you hand over

Only two:

| Value | Example |
|---|---|
| Token | `ntn_…` (around 50 characters) |
| Parent page URL | `https://app.notion.com/p/…` |

Both go in `.env`. **The database itself is created by the script**, not by
hand — it has eleven properties, and a single character's difference in a
property name makes the date lookup miss, which quietly appends a new row every
day instead of updating one. You typically notice several days later.

> **Never paste the token into a chat, an issue, or a screenshot.** Keep it in
> `.env` with mode 600 and nowhere else.

---

## When it does not work

| Symptom | Cause | Fix |
|---|---|---|
| `404 object_not_found` | The connection is not shared with the parent page | Redo step ③ |
| Reads work, writes return `403` | Missing insert-content capability | Enable it on the Configuration tab |
| `401 unauthorized` | Wrong or expired token | Re-check it; if you are using a PAT, switch to an internal connection |
| No menu for creating connections | Account permissions | Try again as the workspace owner |
| Enterprise plan blocks installation | The owner restricted which connections may be installed | Ask them to allow it |

After installation you can check the state at any time:

```bash
python3 doctor.py --full
```

The checks run in causal order (scheduler → configuration → authentication →
Notion), so **reading from the top, the first failure is usually the cause**.

---

## Other things to decide

Adjust these in `config.toml` after installing. All of them can be changed later.

| Setting | Why it matters |
|---|---|
| `[git] authors` | **Every email you commit under.** Empty means no commits are counted. If your home directory contains forks of other people's repositories, this list is the only thing keeping their work out of your report |
| `[exclude] paths` | Directories never collected at all. Put anything that must not reach Notion — client work, confidential material — here |
| `[report] language` | `ko` or `en` |
| `[day] boundary_hour` | Where the day ends. Default 4, so work at 2 a.m. counts as the previous day |

Finding your own addresses:

```bash
git config user.email
git log --format='%ae' | sort | uniq -c | sort -rn | head
```
