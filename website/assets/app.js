// MPXAir documentation — theme, navigation, search, table of contents. No dependencies.
(function () {
  "use strict";
  var root = document.documentElement;

  // ---------------------------------------------------------------- theme
  try {
    var saved = localStorage.getItem("mpxair-theme");
    if (saved) root.setAttribute("data-theme", saved);
  } catch (e) { /* storage may be blocked */ }
  var themeBtn = document.getElementById("theme");
  if (themeBtn) themeBtn.addEventListener("click", function () {
    var dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("mpxair-theme", next); } catch (e) {}
  });

  // ---------------------------------------------------------------- mobile menu
  var side = document.querySelector(".sidebar");
  var menuBtn = document.getElementById("menu");
  if (menuBtn && side) {
    menuBtn.addEventListener("click", function () { side.classList.toggle("open"); });
    document.addEventListener("click", function (e) {
      if (side.classList.contains("open") && !side.contains(e.target) && e.target !== menuBtn && !menuBtn.contains(e.target))
        side.classList.remove("open");
    });
  }
  var active = side && side.querySelector("a.active");
  if (active && active.getBoundingClientRect().bottom > window.innerHeight - 40) active.scrollIntoView({ block: "center" });

  // ---------------------------------------------------------------- headings, anchors, toc
  var article = document.querySelector("article");
  var toc = document.querySelector(".toc nav");
  var heads = article ? article.querySelectorAll("h2[id], h3[id]") : [];
  heads.forEach(function (h) {
    var a = document.createElement("a");
    a.href = "#" + h.id; a.className = "anchor"; a.textContent = "#"; a.setAttribute("aria-label", "Link to this section");
    h.appendChild(a);
    if (toc) {
      var t = document.createElement("a");
      t.href = "#" + h.id; t.textContent = h.firstChild.textContent; t.className = h.tagName.toLowerCase();
      toc.appendChild(t);
    }
  });
  if (toc && !heads.length) toc.parentNode.style.visibility = "hidden";
  if (toc && "IntersectionObserver" in window) {
    var links = toc.querySelectorAll("a");
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) {
          links.forEach(function (l) { l.classList.toggle("active", l.getAttribute("href") === "#" + en.target.id); });
        }
      });
    }, { rootMargin: "-70px 0px -70% 0px" });
    heads.forEach(function (h) { io.observe(h); });
  }

  // ---------------------------------------------------------------- code copy
  document.querySelectorAll("pre").forEach(function (pre) {
    var b = document.createElement("button");
    b.className = "copy"; b.type = "button"; b.textContent = "Copy";
    b.addEventListener("click", function () {
      var code = pre.querySelector("code") || pre;
      navigator.clipboard && navigator.clipboard.writeText(code.innerText).then(function () {
        b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1400);
      });
    });
    pre.appendChild(b);
  });

  // ---------------------------------------------------------------- image zoom
  var lb = document.createElement("div");
  lb.className = "lightbox"; lb.innerHTML = "<img alt=''>";
  document.body.appendChild(lb);
  lb.addEventListener("click", function () { lb.classList.remove("open"); });
  document.querySelectorAll("figure img").forEach(function (img) {
    img.addEventListener("click", function () { lb.querySelector("img").src = img.src; lb.classList.add("open"); });
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") lb.classList.remove("open"); });

  // ---------------------------------------------------------------- search
  var input = document.getElementById("q");
  var box = document.getElementById("results");
  if (!input || !box || !window.MPX_INDEX) return;
  var index = window.MPX_INDEX;
  var sel = -1;

  function esc(s) { return s.replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function snippet(text, words) {
    var low = text.toLowerCase(), at = -1;
    for (var i = 0; i < words.length && at < 0; i++) at = low.indexOf(words[i]);
    if (at < 0) return text.slice(0, 120);
    var s = Math.max(0, at - 50);
    return (s ? "…" : "") + text.slice(s, s + 140) + "…";
  }
  function search(q) {
    var words = q.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) { box.classList.remove("open"); return; }
    var hits = [];
    index.forEach(function (e) {
      var title = e.t.toLowerCase(), body = e.b.toLowerCase(), score = 0, ok = true;
      words.forEach(function (w) {
        if (title.indexOf(w) >= 0) score += 10;
        else if (body.indexOf(w) >= 0) score += 1;
        else ok = false;
      });
      if (ok) hits.push({ e: e, s: score + (e.h ? 0 : 2) });
    });
    hits.sort(function (a, b) { return b.s - a.s; });
    hits = hits.slice(0, 12);
    sel = -1;
    if (!hits.length) { box.innerHTML = "<div class='none'>No results for “" + esc(q) + "”</div>"; }
    else box.innerHTML = hits.map(function (h) {
      return "<a href='" + h.e.u + "'><b>" + esc(h.e.t) + (h.e.p ? " <small style='opacity:.6'>· " + esc(h.e.p) + "</small>" : "") +
        "</b><span>" + esc(snippet(h.e.b, words)) + "</span></a>";
    }).join("");
    box.classList.add("open");
  }
  input.addEventListener("input", function () { search(input.value); });
  input.addEventListener("focus", function () { if (input.value) search(input.value); });
  input.addEventListener("keydown", function (e) {
    var items = box.querySelectorAll("a");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      sel = Math.max(0, Math.min(items.length - 1, sel + (e.key === "ArrowDown" ? 1 : -1)));
      items.forEach(function (a, i) { a.classList.toggle("sel", i === sel); });
    } else if (e.key === "Enter") {
      var go = items[sel >= 0 ? sel : 0];
      if (go) location.href = go.getAttribute("href");
    } else if (e.key === "Escape") { box.classList.remove("open"); input.blur(); }
  });
  document.addEventListener("click", function (e) { if (!box.contains(e.target) && e.target !== input) box.classList.remove("open"); });
  document.addEventListener("keydown", function (e) {
    if ((e.key === "/" || (e.key === "k" && (e.ctrlKey || e.metaKey))) && document.activeElement !== input) {
      e.preventDefault(); input.focus();
    }
  });
})();
