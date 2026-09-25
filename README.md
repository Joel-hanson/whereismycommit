# Where Is My Commit

A small suite of **client-side** GitHub tools for one question: *when did this change ship?*

## Tools

| Page | Tool |
|------|------|
| [`index.html`](index.html) | Landing — pick a tool |
| [`commit.html`](commit.html) | **Where Is My Commit** — SHA → first release/tag |
| [`pr.html`](pr.html) | **Where Is My PR** — PR number/URL → first release/tag |
| [`contains.html`](contains.html) | **Is It In This Version?** — yes/no for a tag |
| [`between.html`](between.html) | **What Changed Between** — commits from tag A → B |
| [`unreleased.html`](unreleased.html) | **What's Unreleased** — default branch since latest tag |

No server. Talks to the GitHub API from your browser.

## Why

- Squash merges rewrite SHAs, so searching for the original commit in tags often fails
- Many projects (e.g. Apache Kafka) use **git tags** but never create GitHub Releases
- Release branches diverge (`3.8.x` vs `3.9.x`), so a simple linear search is wrong

## Quick start (local)

```bash
open index.html
# or
npx serve .
```

## Example

- Repo: `apache/kafka`
- PR: [#15993](https://github.com/apache/kafka/pull/15993)

**Where Is My PR** → first tag **`3.9.0`**.  
**Is It In This Version?** + `3.9.0` → yes; + `3.8.1` → no.

## GitHub Pages

Project site subpath: `https://<user>.github.io/<repo>/`

1. Push to GitHub  
2. **Settings → Pages → Source**: **GitHub Actions**  
3. Deploy workflow: [`.github/workflows/pages.yml`](.github/workflows/pages.yml)

## API usage

Unauthenticated: **60 req/hr**. With a token: **5,000 req/hr**.  
Token stays in the tab and is sent only to `api.github.com`.

## License

Use freely for personal or internal tooling.
