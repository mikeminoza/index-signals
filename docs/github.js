// Lets this site save to your GitHub repository from your own browser.
// The token you paste is kept in this browser's storage only; it is never written to the repository.
window.GH = (function () {
  const KEY = "index-signals-token", WHO = "index-signals-who";
  const T = window.TRACKER || {};
  const repo = (T.system && T.system.repo) || (location.hostname.endsWith("github.io")
    ? location.hostname.split(".")[0] + "/" + location.pathname.split("/").filter(Boolean)[0] : null);

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); return true; } catch { return false; } },
    del(k) { try { localStorage.removeItem(k); } catch { /* storage unavailable */ } },
  };
  const token = () => store.get(KEY);
  const who = () => { try { return JSON.parse(store.get(WHO) || "null"); } catch { return null; } };

  async function api(path, options, useToken) {
    const t = useToken === undefined ? token() : useToken;
    const res = await fetch("https://api.github.com" + path, Object.assign({}, options, {
      headers: Object.assign({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                             t ? {"Authorization": "Bearer " + t} : {}, options && options.body ? {"Content-Type": "application/json"} : {}),
    }));
    if (res.ok) return res.status === 204 ? null : res.json();
    const reasons = {
      401: "GitHub didn't accept the token. It may have expired or been pasted wrongly. Connect again with a new one.",
      403: "The token isn't allowed to do this. It needs Contents: Read and write on this repository.",
      404: "GitHub couldn't find this, or the token has no access to this repository.",
      409: "GitHub was busy with another change. Try again in a moment.",
      422: "GitHub rejected the change. Try again in a moment.",
    };
    throw new Error(reasons[res.status] || "GitHub returned an error (" + res.status + "). Try again.");
  }

  function encode(text) {
    const bytes = new TextEncoder().encode(text);
    let bin = "";
    bytes.forEach(b => { bin += String.fromCharCode(b); });
    return btoa(bin);
  }

  function committer() {
    const w = who();  // GitHub's private no-reply address, so your real email never appears in commits
    return w ? {name: w.login, email: w.id + "+" + w.login + "@users.noreply.github.com"} : undefined;
  }

  return {
    repo,
    connected: () => !!token(),
    who,
    async connect(t) {
      if (!repo) throw new Error("This page doesn't know which repository it belongs to yet. Wait for the bot's next run.");
      const user = await api("/user", null, t);
      await api("/repos/" + repo, null, t);
      if (!store.set(KEY, t)) throw new Error("This browser blocked saving the token. Turn off private browsing and try again.");
      store.set(WHO, JSON.stringify({login: user.login, id: user.id}));
      return user.login;
    },
    disconnect() { store.del(KEY); store.del(WHO); },
    // Adds a new file; never touches an existing one
    create(path, text, message) {
      return api("/repos/" + repo + "/contents/" + path, {method: "PUT",
        body: JSON.stringify({message, content: encode(text), branch: "main", committer: committer()})});
    },
    // Replaces an existing file with new contents
    async replace(path, text, message) {
      const cur = await api("/repos/" + repo + "/contents/" + path + "?ref=main&t=" + Date.now());
      return api("/repos/" + repo + "/contents/" + path, {method: "PUT",
        body: JSON.stringify({message, content: encode(text), sha: cur.sha, branch: "main", committer: committer()})});
    },
    async waiting() {
      try {
        const list = await api("/repos/" + repo + "/contents/state/inbox?ref=main&t=" + Date.now());
        return Array.isArray(list) ? list.filter(f => f.name.endsWith(".json")).length : 0;
      } catch { return 0; }
    },
  };
})();
