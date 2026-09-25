/* Shared GitHub API helpers for Where Is My Commit tools */
(function (global) {
  const $ = (id) => document.getElementById(id);

  function bindTokenToggle() {
    const show = $("show-token");
    if (!show) return;
    show.addEventListener("change", (e) => {
      $("token-field").classList.toggle("visible", e.target.checked);
    });
  }

  function parseRepo(raw) {
    if (!raw || !String(raw).trim()) return null;
    const cleaned = String(raw).trim().replace(/\.git$/, "");
    const m = cleaned.match(
      /^(?:https?:\/\/)?(?:www\.)?github\.com\/([^/\s]+)\/([^/\s#?]+)/i
    );
    if (m) return { owner: m[1], repo: m[2] };
    const parts = cleaned.split("/").filter(Boolean);
    if (parts.length === 2) return { owner: parts[0], repo: parts[1] };
    throw new Error("Repo must look like owner/repo or a GitHub URL.");
  }

  function headers() {
    const h = {
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    };
    const tokenEl = $("token");
    const token = tokenEl ? tokenEl.value.trim() : "";
    if (token) h.Authorization = `Bearer ${token}`;
    return h;
  }

  async function gh(path) {
    const res = await fetch(`https://api.github.com${path}`, { headers: headers() });
    const remaining = res.headers.get("X-RateLimit-Remaining");
    if (remaining !== null) gh.lastRemaining = remaining;
    if (res.status === 404) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.message || "Not found (404). Check inputs or token.");
    }
    if (res.status === 403 || res.status === 429) {
      throw new Error("GitHub rate limit hit. Add a personal access token and try again.");
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.message || `GitHub API error (${res.status})`);
    }
    return res;
  }

  function rateNote() {
    return gh.lastRemaining != null
      ? ` · ${gh.lastRemaining} API calls left this hour`
      : "";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatDate(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      });
    } catch {
      return iso;
    }
  }

  function setStatus(msg, isError = false) {
    const el = $("status");
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle("error", isError);
  }

  function showResultShell({ verdictClass, label, main, sub, metaRows }) {
    const result = $("result");
    result.hidden = false;
    result.classList.add("visible");
    const verdict = $("verdict");
    verdict.className = `alert ${verdictClass}`;
    $("verdict-label").textContent = label;
    $("verdict-main").textContent = main;
    $("verdict-sub").innerHTML = sub;
    const meta = $("meta");
    if (meta) {
      meta.innerHTML = (metaRows || [])
        .map(
          ([k, v]) =>
            `<div class="meta-row"><dt>${k}</dt><dd>${v}</dd></div>`
        )
        .join("");
    }
  }

  async function resolveCommit(owner, repo, sha) {
    const res = await gh(`/repos/${owner}/${repo}/commits/${encodeURIComponent(sha)}`);
    return res.json();
  }

  async function fetchPull(owner, repo, number) {
    const res = await gh(`/repos/${owner}/${repo}/pulls/${number}`);
    return res.json();
  }

  async function fetchRepo(owner, repo) {
    const res = await gh(`/repos/${owner}/${repo}`);
    return res.json();
  }

  /** Resolve commit SHA, or PR (#n / URL) to merge commit SHA. */
  async function resolveChange(owner, repo, raw) {
    const text = String(raw).trim();
    const prUrl = text.match(
      /^(?:https?:\/\/)?(?:www\.)?github\.com\/([^/\s]+)\/([^/\s]+)\/pull\/(\d+)/i
    );
    if (prUrl) {
      owner = prUrl[1];
      repo = prUrl[2];
      const pr = await fetchPull(owner, repo, Number(prUrl[3]));
      if (!pr.merged_at || !pr.merge_commit_sha) {
        throw new Error(
          pr.merged_at
            ? `PR #${pr.number} has no merge commit SHA.`
            : `PR #${pr.number} is not merged.`
        );
      }
      return {
        owner,
        repo,
        sha: pr.merge_commit_sha,
        kind: "pr",
        pr,
        label: `PR #${pr.number} merge`,
      };
    }
    const prNum = text.match(/^#?(\d+)$/);
    if (prNum && !/^[0-9a-fA-F]{7,40}$/.test(text)) {
      const pr = await fetchPull(owner, repo, Number(prNum[1]));
      if (!pr.merged_at || !pr.merge_commit_sha) {
        throw new Error(
          pr.merged_at
            ? `PR #${pr.number} has no merge commit SHA.`
            : `PR #${pr.number} is not merged.`
        );
      }
      return {
        owner,
        repo,
        sha: pr.merge_commit_sha,
        kind: "pr",
        pr,
        label: `PR #${pr.number} merge`,
      };
    }
    if (!/^[0-9a-fA-F]{4,40}$/.test(text)) {
      throw new Error("Enter a commit SHA, PR number (#123), or PR URL.");
    }
    const commit = await resolveCommit(owner, repo, text);
    return {
      owner,
      repo,
      sha: commit.sha,
      kind: "commit",
      commit,
      label: commit.sha.slice(0, 7),
    };
  }

  async function tagContains(owner, repo, tag, sha) {
    const res = await gh(
      `/repos/${owner}/${repo}/compare/${encodeURIComponent(sha)}...${encodeURIComponent(tag)}`
    );
    const data = await res.json();
    return {
      contains:
        data.status === "identical" ||
        (data.status === "ahead" && data.behind_by === 0),
      status: data.status,
      ahead_by: data.ahead_by,
      behind_by: data.behind_by,
    };
  }

  async function compare(owner, repo, base, head) {
    const res = await gh(
      `/repos/${owner}/${repo}/compare/${encodeURIComponent(base)}...${encodeURIComponent(head)}`
    );
    return res.json();
  }

  function parseVersion(tag) {
    const cleaned = String(tag).replace(/^v/i, "");
    const m = cleaned.match(/^(\d+(?:\.\d+)*)(?:-rc(\d+))?$/i);
    if (!m) return null;
    return {
      parts: m[1].split(".").map(Number),
      rc: m[2] != null ? Number(m[2]) : null,
      tag,
    };
  }

  function cmpVersion(a, b) {
    const len = Math.max(a.parts.length, b.parts.length);
    for (let i = 0; i < len; i++) {
      const d = (a.parts[i] || 0) - (b.parts[i] || 0);
      if (d) return d;
    }
    if (a.rc == null && b.rc != null) return 1;
    if (a.rc != null && b.rc == null) return -1;
    return (a.rc || 0) - (b.rc || 0);
  }

  async function latestVersionTag(owner, repo) {
    const res = await gh(`/repos/${owner}/${repo}/git/matching-refs/tags`);
    const refs = await res.json();
    const versions = [];
    for (const ref of refs) {
      const tag = ref.ref.replace(/^refs\/tags\//, "");
      const ver = parseVersion(tag);
      if (!ver || ver.rc != null) continue;
      versions.push({ tag, version: ver });
    }
    if (!versions.length) return null;
    versions.sort((a, b) => cmpVersion(a.version, b.version));
    return versions[versions.length - 1].tag;
  }

  function extractPrNumber(message) {
    if (!message) return null;
    const m =
      message.match(/\(#(\d+)\)\s*$/m) ||
      message.match(/Merge pull request #(\d+)/i) ||
      message.match(/\bPR\s*#(\d+)\b/i);
    return m ? Number(m[1]) : null;
  }

  bindTokenToggle();

  global.WIMC = {
    $,
    parseRepo,
    headers,
    gh,
    rateNote,
    escapeHtml,
    formatDate,
    setStatus,
    showResultShell,
    resolveCommit,
    fetchPull,
    fetchRepo,
    resolveChange,
    tagContains,
    compare,
    parseVersion,
    latestVersionTag,
    extractPrNumber,
  };
})(window);
