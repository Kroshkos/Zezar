/* ============================================================
   Логика сайта Zezar: SPA-навигация, рендеринг документации
   и новостей. Контент подгружается из Markdown-папок репозитория
   (docs/*.md и news/*.md) через эндпоинт /docs/content.json —
   см. app/content_site.py.
   ============================================================ */

(function () {
  "use strict";

  const pages = document.querySelectorAll(".page");
  const navLinks = document.querySelectorAll(".main-nav .nav-link");
  const burgerBtn = document.getElementById("burgerBtn");
  const mainNav = document.getElementById("mainNav");

  let docs = [];
  let news = [];

  /* ---------- Навигация между страницами ---------- */
  function showPage(pageId) {
    pages.forEach(p => p.classList.toggle("active", p.id === pageId));
    navLinks.forEach(a =>
      a.classList.toggle("active", a.getAttribute("href") === "#" + pageId)
    );
    mainNav.classList.remove("open");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function currentPageId() {
    return location.hash.replace("#", "") || "home";
  }

  // Бургер-меню (мобильная версия)
  burgerBtn.addEventListener("click", () => mainNav.classList.toggle("open"));

  /* ---------- Экранирование (fallback для сырого markdown) ---------- */
  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function bodyOf(item) {
    // html приходит с сервера (python-markdown); если его нет — показываем
    // исходный markdown как preformatted-текст, чтобы ничего не терялось.
    if (item.html) return item.html;
    return "<pre>" + esc(item.markdown || "") + "</pre>";
  }

  /* ---------- Документация ---------- */
  const docsToc = document.getElementById("docsToc");
  const docsContent = document.getElementById("docsContent");

  function renderDocs(activeId) {
    docsToc.innerHTML = "";
    if (!docs.length) {
      docsToc.innerHTML = '<div class="empty-hint">Пока нет статей в <code>docs/</code></div>';
      docsContent.innerHTML = "<h1>Документация</h1><p>Раздел заполняется автоматически из папки " +
        "<code>docs/</code> вашего репозитория: добавьте туда файл <code>имя.md</code>, " +
        "и он появится в этом списке после деплоя.</p>";
      return;
    }
    docs.forEach(d => {
      const a = document.createElement("a");
      a.href = "#docs/" + d.id;
      a.textContent = d.title;
      a.dataset.docId = d.id;
      if (d.id === activeId) a.classList.add("active");
      docsToc.appendChild(a);
    });
    const doc = docs.find(d => d.id === activeId) || docs[0];
    docsContent.innerHTML = "<h1>" + esc(doc.title) + "</h1>" + bodyOf(doc);
    docsToc.querySelectorAll("a").forEach(a =>
      a.classList.toggle("active", a.dataset.docId === doc.id)
    );
  }

  docsToc.addEventListener("click", e => {
    const link = e.target.closest("a[data-doc-id]");
    if (!link) return;
    e.preventDefault();
    location.hash = "#docs/" + link.dataset.docId;
  });

  /* ---------- Новости ---------- */
  const newsList = document.getElementById("newsList");
  const newsContent = document.getElementById("newsContent");

  function formatDate(iso) {
    const months = ["января", "февраля", "марта", "апреля", "мая", "июня",
      "июля", "августа", "сентября", "октября", "ноября", "декабря"];
    const d = new Date(iso);
    return d.getDate() + " " + months[d.getMonth()] + " " + d.getFullYear();
  }

  function sortedNews() {
    return [...news].sort((a, b) => b.date.localeCompare(a.date));
  }

  function renderNews(activeId) {
    const sorted = sortedNews();
    newsList.innerHTML = "";
    if (!sorted.length) {
      newsList.innerHTML = '<div class="empty-hint">Пока нет новостей в <code>news/</code></div>';
      newsContent.innerHTML = "<h1>Новости</h1><p>Раздел наполняется автоматически из папки " +
        "<code>news/</code> вашего репозитория: положите туда файл <code>год-месяц-заголовок.md</code>.</p>";
      return;
    }
    sorted.forEach(n => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.innerHTML = esc(n.title) +
        '<span class="item-date">' + formatDate(n.date) + "</span>";
      btn.dataset.newsId = n.id;
      if (n.id === activeId) btn.classList.add("active");
      btn.addEventListener("click", () => {
        location.hash = "#news/" + n.id;
      });
      li.appendChild(btn);
      newsList.appendChild(li);
    });

    const article = sorted.find(n => n.id === activeId) || sorted[0];
    newsContent.innerHTML =
      "<h1>" + esc(article.title) + "</h1>" +
      '<div class="article-meta">' +
        '<span class="tag">' + esc(article.tag) + "</span>" +
        "<span>📅 " + formatDate(article.date) + "</span>" +
      "</div>" +
      bodyOf(article);
    newsList.querySelectorAll("button").forEach(b =>
      b.classList.toggle("active", b.dataset.newsId === article.id)
    );
  }

  /* ---------- Превью новостей на главной ---------- */
  function renderHomePreview() {
    const preview = document.getElementById("homeNewsPreview");
    preview.innerHTML = "";
    sortedNews().slice(0, 3).forEach(n => {
      const li = document.createElement("li");
      li.innerHTML =
        '<a href="#news/' + n.id + '">' + esc(n.title) + "</a>" +
        '<span class="preview-date">' + formatDate(n.date) + "</span>";
      preview.appendChild(li);
    });
  }

  /* ---------- Маршрутизация по hash ----------
     Форматы: #home | #docs | #docs/<id> | #news | #news/<id> */
  function route() {
    const raw = currentPageId();
    const [page, subId] = raw.split("/");

    if (page === "docs") {
      showPage("docs");
      renderDocs(subId);
    } else if (page === "news") {
      showPage("news");
      renderNews(subId);
    } else {
      showPage("home");
    }
  }

  window.addEventListener("hashchange", route);

  /* ---------- Загрузка контента и старт ---------- */
  async function loadContent() {
    try {
      const resp = await fetch("/docs/content.json", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      docs = data.docs || [];
      news = data.news || [];
    } catch (e) {
      docs = [];
      news = [];
      console.error("Не удалось загрузить контент сайта:", e);
    }
    renderHomePreview();
    route();
  }

  loadContent();
})();
