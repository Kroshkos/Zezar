/* ============================================================
   Логика сайта Zezar: SPA-навигация, рендеринг документации
   и новостей из content.js
   ============================================================ */

(function () {
  "use strict";

  const pages = document.querySelectorAll(".page");
  const navLinks = document.querySelectorAll(".main-nav .nav-link");
  const burgerBtn = document.getElementById("burgerBtn");
  const mainNav = document.getElementById("mainNav");

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

  window.addEventListener("hashchange", () => showPage(currentPageId()));

  // Кнопки/ссылки с data-nav — плавно переключаем страницы
  document.querySelectorAll("[data-nav]").forEach(el => {
    el.addEventListener("click", () => {
      const id = el.getAttribute("href").replace("#", "");
      // hashchange сработает сам, но если hash тот же — обновим вручную
      setTimeout(() => showPage(id), 0);
    });
  });

  // Бургер-меню (мобильная версия)
  burgerBtn.addEventListener("click", () => mainNav.classList.toggle("open"));

  /* ---------- Документация ---------- */
  const docsToc = document.getElementById("docsToc");
  const docsContent = document.getElementById("docsContent");

  function renderDocs(activeId) {
    docsToc.innerHTML = "";
    docs.forEach(d => {
      const a = document.createElement("a");
      a.href = "#docs/" + d.id;
      a.textContent = d.title;
      a.dataset.docId = d.id;
      if (d.id === activeId) a.classList.add("active");
      docsToc.appendChild(a);
    });
    const doc = docs.find(d => d.id === activeId) || docs[0];
    docsContent.innerHTML = "<h1>" + doc.title + "</h1>" + doc.html;
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

  function renderNews(activeId) {
    const sorted = [...news].sort((a, b) => b.date.localeCompare(a.date));
    newsList.innerHTML = "";
    sorted.forEach(n => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.innerHTML = n.title +
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
      "<h1>" + article.title + "</h1>" +
      '<div class="article-meta">' +
        '<span class="tag">' + article.tag + "</span>" +
        "<span>📅 " + formatDate(article.date) + "</span>" +
      "</div>" +
      article.html;
    newsList.querySelectorAll("button").forEach(b =>
      b.classList.toggle("active", b.dataset.newsId === article.id)
    );
  }

  /* ---------- Превью новостей на главной ---------- */
  const preview = document.getElementById("homeNewsPreview");
  [...news]
    .sort((a, b) => b.date.localeCompare(a.date))
    .slice(0, 3)
    .forEach(n => {
      const li = document.createElement("li");
      li.innerHTML =
        '<a href="#news/' + n.id + '">' + n.title + "</a>" +
        '<span class="preview-date">' + formatDate(n.date) + "</span>";
      preview.appendChild(li);
    });

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

  // Инициализация
  renderDocs(docs[0].id);
  route();
})();
