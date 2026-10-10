// ==UserScript==
// @name         GMGN Base钱包榜保存
// @namespace    rt01
// @version      2026-10-01
// @description  打开 GMGN 的 Base 钱包榜时，把页面自己请求到的名单存成 JSON。不另发请求。
// @match        https://gmgn.ai/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function () {
  "use strict";
  const seen = new Set();

  function chainOf(url) {
    const m = String(url).match(/\/rank\/(base|bsc)\/wallets\/([^/?]+)/);
    return m ? { chain: m[1], window: m[2] } : null;
  }

  function save(url, text) {
    const info = chainOf(url);
    if (!info || seen.has(info.chain + info.window)) return;
    let rows = [];
    try {
      rows = JSON.parse(text).data.rank || [];
    } catch (e) {
      return;
    }
    if (!rows.length || !rows[0].address) return;
    seen.add(info.chain + info.window);
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: "application/json" }));
    a.download = `gmgn_${info.chain}_${info.window}_${stamp}.json`;
    document.documentElement.appendChild(a);
    a.click();
    a.remove();
    const note = document.createElement("div");
    note.textContent = `已保存 ${info.chain} ${info.window} 钱包 ${rows.length} 个`;
    note.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:999999;background:#111;color:#fff;padding:10px 14px;border-radius:8px;font:14px sans-serif";
    document.documentElement.appendChild(note);
  }

  const origFetch = window.fetch;
  window.fetch = function () {
    return origFetch.apply(this, arguments).then(function (res) {
      const url = (arguments[0] && arguments[0].url) || String(arguments[0] || "");
      if (chainOf(url)) res.clone().text().then(function (text) { save(url, text); });
      return res;
    });
  };

  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    this._gmgnUrl = url;
    return origOpen.apply(this, arguments);
  };
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener("load", function () {
      if (chainOf(this._gmgnUrl) && this.responseText) save(this._gmgnUrl, this.responseText);
    });
    return origSend.apply(this, arguments);
  };
})();
